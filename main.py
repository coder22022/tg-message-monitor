"""
Telegram 消息监听：私信指定用户、群组 @ 当前用户或引用回复当前用户。

依赖: pip install -r requirements.txt
用法: python main.py -c config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetPeerDialogsRequest

from push import PushClient, display_name, format_message_content
from message_format import format_telegram_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# my.telegram.org 注册失败时可省略配置，使用 Telegram Desktop 公开凭证
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"
HEARTBEAT_CHECK_INTERVAL_SECONDS = 60
DEFAULT_GROUP_BROWSE_INTERVAL_MINUTES = 20
DEFAULT_GROUP_BROWSE_MESSAGE_LIMIT = 5
GROUP_BROWSE_DELAY_SECONDS = 1


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_identifier(value: str | int) -> str:
    return str(value).lstrip("@").lower()


def normalize_command(text: str) -> str:
    return " ".join(text.strip().lower().split())


class TgMessageMonitor:
    def __init__(self, config: dict[str, Any]) -> None:
        tg = config["telegram"]
        self.api_id = int(tg.get("api_id") or DEFAULT_API_ID)
        self.api_hash = str(tg.get("api_hash") or DEFAULT_API_HASH)
        if not tg.get("api_id") or not tg.get("api_hash"):
            logger.warning(
                "未配置 api_id/api_hash，使用内置默认值；"
                "建议在 https://my.telegram.org 申请专属凭证"
            )
        self.phone = str(tg["phone"])
        self.password = str(tg.get("password") or "") or None

        session = tg.get("session", "sessions/tg_monitor")
        session_path = Path(session)
        session_path.parent.mkdir(parents=True, exist_ok=True)

        monitor = config.get("monitor", {})
        self.private_users = {
            normalize_identifier(u) for u in monitor.get("private_users", [])
        }
        self.groups = {normalize_identifier(g) for g in monitor.get("groups", [])}
        self.group_targets = list(monitor.get("groups", []))
        self.group_browse_interval_minutes = int(
            monitor.get(
                "group_browse_interval_minutes",
                DEFAULT_GROUP_BROWSE_INTERVAL_MINUTES,
            )
        )
        self.group_browse_message_limit = int(
            monitor.get(
                "group_browse_message_limit",
                DEFAULT_GROUP_BROWSE_MESSAGE_LIMIT,
            )
        )

        command_cfg = config.get("command", {})
        self.command_users = {
            normalize_identifier(u) for u in command_cfg.get("users", [])
        }

        push_cfg = config.get("push")
        self.push_client = PushClient(push_cfg) if push_cfg else None

        self.client = TelegramClient(str(session_path), self.api_id, self.api_hash)
        self.me = None
        self.start_time: datetime | None = None
        self.message_count = 0
        self._heartbeat_task: asyncio.Task | None = None
        self._group_browse_task: asyncio.Task | None = None

    def _match_identifier(self, entity: Any, identifiers: set[str]) -> bool:
        if not identifiers or entity is None:
            return False

        candidates: set[str] = set()
        entity_id = getattr(entity, "id", None)
        if entity_id is not None:
            candidates.add(str(entity_id))
        username = getattr(entity, "username", None)
        if username:
            candidates.add(normalize_identifier(username))

        return bool(candidates & identifiers)

    def _is_mentioned(self, event: events.NewMessage.Event) -> bool:
        msg = event.message
        if getattr(msg, "mentioned", False):
            return True

        if self.me and self.me.username:
            text = (msg.message or "").lower()
            if f"@{self.me.username.lower()}" in text:
                return True

        return False

    async def _is_reply_to_me(self, event: events.NewMessage.Event) -> bool:
        if not self.me or not event.message.is_reply:
            return False

        reply_msg = await event.get_reply_message()
        if not reply_msg:
            return False

        if reply_msg.sender_id == self.me.id:
            return True
        return bool(reply_msg.out)

    def _resolve_group_ref(self, group_ref: str | int) -> str | int:
        if isinstance(group_ref, int):
            return group_ref
        value = str(group_ref).strip()
        if value.lstrip("-").isdigit():
            return int(value)
        return value.lstrip("@")

    async def _get_read_inbox_max_id(self, entity: Any) -> int:
        input_peer = await self.client.get_input_entity(entity)
        result = await self.client(GetPeerDialogsRequest(peers=[input_peer]))
        if not result.dialogs:
            return 0
        return int(result.dialogs[0].read_inbox_max_id or 0)

    async def _browse_group(self, group_ref: str | int) -> None:
        entity = await self.client.get_entity(self._resolve_group_ref(group_ref))
        group_name = display_name(entity)
        read_inbox_max_id = await self._get_read_inbox_max_id(entity)

        limit = max(1, self.group_browse_message_limit)
        messages = await self.client.get_messages(entity, limit=limit)
        if not messages:
            logger.info("群组浏览: %s (无消息)", group_name)
            return

        max_message_id = max(message.id for message in messages)
        if max_message_id > read_inbox_max_id:
            await self.client.send_read_acknowledge(entity, max_id=max_message_id)
            ack_text = f"已读确认至 {max_message_id}"
        else:
            ack_text = f"已全部已读(≤{read_inbox_max_id})，跳过已读确认"

        logger.info(
            "群组浏览: %s，读取 %d 条，%s",
            group_name,
            len(messages),
            ack_text,
        )

    async def _browse_groups_once(self) -> None:
        if not self.group_targets:
            return

        if not self.push_client:
            logger.info("群组浏览: 未配置推送，跳过活跃更新")
            return

        if not self.push_client.can_update_activity():
            if not self.push_client.is_active():
                logger.info("群组浏览: 推送未开启，跳过活跃更新")
            else:
                logger.info("群组浏览: 免打扰时段，跳过活跃更新")
            return

        if random.random() >= 0.5:
            logger.info("群组浏览: 随机跳过本次活跃更新")
            return

        logger.info("开始浏览群组，共 %d 个", len(self.group_targets))
        for group_ref in self.group_targets:
            try:
                await self._browse_group(group_ref)
            except Exception:
                logger.exception("群组浏览失败: %s", group_ref)
            await asyncio.sleep(GROUP_BROWSE_DELAY_SECONDS)

    async def _group_browse_loop(self) -> None:
        interval_seconds = self.group_browse_interval_minutes * 60
        while True:
            try:
                await self._browse_groups_once()
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("群组浏览任务失败")
                await asyncio.sleep(interval_seconds)

    def _start_group_browse(self) -> None:
        if self.group_browse_interval_minutes <= 0 or not self.group_targets:
            return
        if self._group_browse_task and not self._group_browse_task.done():
            return
        self._group_browse_task = asyncio.create_task(self._group_browse_loop())
        logger.info(
            "群组浏览定时任务已启动，间隔 %d 分钟",
            self.group_browse_interval_minutes,
        )

    def _stop_group_browse(self) -> None:
        if self._group_browse_task and not self._group_browse_task.done():
            self._group_browse_task.cancel()
            logger.info("群组浏览定时任务已停止")
        self._group_browse_task = None

    def _group_browse_status_text(self) -> str:
        if self.group_browse_interval_minutes <= 0:
            return "已关闭"
        if not self.group_targets:
            return "未配置监听群组"

        base = (
            f"每 {self.group_browse_interval_minutes} 分钟浏览 "
            f"{len(self.group_targets)} 个群，每次 {self.group_browse_message_limit} 条"
        )
        if not self.push_client:
            return f"{base}（需配置推送）"

        if not self.push_client.can_update_activity():
            reasons: list[str] = []
            if not self.push_client.is_active():
                reasons.append("推送关闭")
            if self.push_client.is_quiet_hours():
                reasons.append("免打扰中")
            return f"{base}（当前暂停：{'、'.join(reasons)}）"

        return f"{base}（推送开启，50% 概率执行）"

    def _build_status(self) -> str:
        uptime_seconds = 0
        if self.start_time:
            uptime_seconds = int((datetime.now() - self.start_time).total_seconds())

        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        if self.me and self.me.username:
            account = f"@{self.me.username}"
        elif self.me:
            account = str(self.me.id)
        else:
            account = "未知"

        push_status = self.push_client.status_text() if self.push_client else "未配置"
        push_details = (
            self.push_client.build_status_lines() if self.push_client else []
        )
        connection = "已连接" if self.client.is_connected() else "未连接"

        lines = [
            "系统状态",
            f"连接: {connection}",
            f"账号: {account}",
            f"推送: {push_status}",
        ]
        lines.extend(push_details)
        lines.extend(
            [
                f"监听私信用户: {len(self.private_users)}",
                f"监听群组: {len(self.groups)}",
                f"群组浏览: {self._group_browse_status_text()}",
                f"命令用户: {len(self.command_users)}",
                f"运行时长: {hours}小时{minutes}分{seconds}秒",
                f"已处理消息: {self.message_count}",
            ]
        )
        return "\n".join(lines)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL_SECONDS)
                if not self.push_client or not self.push_client.is_active():
                    continue
                if self.push_client.needs_heartbeat():
                    logger.info(
                        "超过%d分钟未推送，发送心跳消息",
                        self.push_client.heartbeat_interval_minutes,
                    )
                    await self.push_client.send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("心跳检查失败")

    def _start_heartbeat(self) -> None:
        if not self.push_client or not self.push_client.is_active():
            return
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        if self.push_client.last_push_time is None:
            self.push_client.mark_push_sent()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("心跳定时任务已启动")

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            logger.info("心跳定时任务已停止")
        self._heartbeat_task = None

    async def _try_handle_command(
        self,
        event: events.NewMessage.Event,
        text: str,
        sender: Any,
    ) -> bool:
        command = normalize_command(text)
        sender_name = display_name(sender)

        if command == "/open push":
            if not self.push_client:
                await event.reply("未配置推送服务")
                return True
            self.push_client.open_push()
            self._start_heartbeat()
            logger.info("命令: 推送已开启")
            await event.reply("推送已开启")
            await self.push_client.send(
                title=sender_name,
                content=f"{sender_name} 执行 /open push，推送已开启",
                message_date=event.message.date,
            )
            return True

        if command == "/close push":
            if not self.push_client:
                await event.reply("未配置推送服务")
                return True
            self.push_client.close_push()
            self._stop_heartbeat()
            logger.info("命令: 推送已关闭")
            await event.reply("推送已关闭，消息仅打印")
            await self.push_client.send(
                title=sender_name,
                content=f"{sender_name} 执行 /close push，推送已关闭",
                message_date=event.message.date,
                force=True,
            )
            return True

        if command == "/open duty":
            if not self.push_client:
                await event.reply("未配置推送服务")
                return True
            self.push_client.open_duty()
            self._start_heartbeat()
            logger.info("命令: 值班模式已开启")
            await event.reply("值班模式已开启（推送已开启，群消息全量推送）")
            await self.push_client.send(
                title=sender_name,
                content=f"{sender_name} 执行 /open duty，值班模式已开启",
                message_date=event.message.date,
            )
            return True

        if command == "/close duty":
            if not self.push_client:
                await event.reply("未配置推送服务")
                return True
            self.push_client.close_duty()
            logger.info("命令: 值班模式已关闭")
            await event.reply("值班模式已关闭（推送保持当前状态）")
            await self.push_client.send(
                title=sender_name,
                content=f"{sender_name} 执行 /close duty，值班模式已关闭",
                message_date=event.message.date,
            )
            return True

        if command == "/status":
            await event.reply(self._build_status())
            return True

        return False

    def _group_id_command(self) -> str | None:
        if not self.me or not self.me.username:
            return None
        return f"/id@{normalize_identifier(self.me.username)}"

    async def _try_handle_group_command(
        self,
        event: events.NewMessage.Event,
        text: str,
        sender: Any,
        chat: Any,
    ) -> bool:
        if not self.command_users or not self._match_identifier(sender, self.command_users):
            return False

        id_command = self._group_id_command()
        if not id_command:
            return False

        if normalize_command(text) != id_command:
            return False

        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            await event.reply("无法获取群 ID")
            return True

        logger.info("命令: %s 返回群 ID %s", id_command, chat_id)
        await event.reply(str(chat_id))
        return True

    async def _handle_private(self, event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        chat = await event.get_chat()
        text = event.message.message or ""

        if self.command_users and self._match_identifier(sender, self.command_users):
            if await self._try_handle_command(event, text, sender):
                return

        if not self.private_users:
            return

        if not self._match_identifier(sender, self.private_users):
            return

        sender_name = display_name(sender)
        display_text = format_telegram_message(event.message, chat)
        content = format_message_content(message=event.message, chat=chat)
        print(f"[私信] 来自 {sender_name}: {display_text}")
        self.message_count += 1
        if self.push_client:
            await self.push_client.send(
                title=sender_name,
                content=content,
                message_date=event.message.date,
            )

    async def _handle_group(self, event: events.NewMessage.Event) -> None:
        chat = await event.get_chat()
        text = event.message.message or ""
        sender = await event.get_sender()

        if self.groups and self._match_identifier(chat, self.groups):
            group_name = display_name(chat)
            sender_name = display_name(sender)
            display_text = format_telegram_message(event.message, chat)
            print(f"[群组] {group_name} | {sender_name}: {display_text}")

        if await self._try_handle_group_command(event, text, sender, chat):
            return

        if not self.groups:
            return

        if not self._match_identifier(chat, self.groups):
            return

        mentioned = self._is_mentioned(event)
        replied_to_me = await self._is_reply_to_me(event)
        should_push = mentioned or replied_to_me
        if self.push_client:
            should_push = self.push_client.should_notify_group(mentioned, replied_to_me)
        if not should_push:
            return

        group_name = display_name(chat)
        sender_name = display_name(sender)
        content = format_message_content(
            message=event.message,
            chat=chat,
            sender_name=sender_name,
        )
        self.message_count += 1
        if self.push_client:
            await self.push_client.send(
                title=group_name,
                content=content,
                message_date=event.message.date,
            )

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        if event.out:
            return

        if event.is_private:
            await self._handle_private(event)
        elif event.is_group:
            await self._handle_group(event)

    async def start(self) -> None:
        await self.client.start(
            phone=self.phone,
            password=self.password,
            code_callback=lambda: input("请输入 Telegram 验证码: ").strip(),
        )

        self.me = await self.client.get_me()
        self.start_time = datetime.now()
        username = f"@{self.me.username}" if self.me.username else str(self.me.id)
        logger.info("已登录: %s (%s)", self.me.first_name, username)
        logger.info(
            "监听配置 - 私信用户: %s, 群组: %s, 命令用户: %s, 群组浏览间隔: %s分钟",
            len(self.private_users),
            len(self.groups),
            len(self.command_users),
            self.group_browse_interval_minutes,
        )

        @self.client.on(events.NewMessage)
        async def on_new_message(event: events.NewMessage.Event) -> None:
            try:
                await self._handle_message(event)
            except Exception:
                logger.exception("处理消息失败")

        logger.info("开始监听，按 Ctrl+C 退出...")
        self._start_heartbeat()
        self._start_group_browse()
        await self.client.run_until_disconnected()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram 消息监听")
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="配置文件路径，默认 config.yaml",
    )
    return parser.parse_args()


async def async_main(config_path: Path) -> None:
    if not config_path.is_file():
        logger.error("配置文件不存在: %s", config_path)
        logger.error("请复制 config.example.yaml 为 config.yaml 并填写信息")
        sys.exit(1)

    config = load_config(config_path)
    monitor = TgMessageMonitor(config)
    await monitor.start()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(async_main(Path(args.config)))
    except KeyboardInterrupt:
        logger.info("已退出")


if __name__ == "__main__":
    main()
