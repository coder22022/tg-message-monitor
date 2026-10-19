"""
Telegram 消息监听：私信指定用户、配置群组中 @/回复当前用户。
上班时段未配置群中 @/回复当前用户 也会推送（需推送开启）。
skip_mention_groups 中的群会先丢弃 @当前账号 的消息，再走原有推送流程。

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

from admin_command_result import AdminCommandResult
from admin_commands import normalize_command, resolve_admin_command
from export_command import (
    export_filter_summary,
    parse_export_groups_args,
    resolve_export_batch_size,
)
from export_delivery import (
    DEFAULT_CHECKPOINT_TIMEOUT_SECONDS,
    ExportCommandContext,
    ExportCheckpointCoordinator,
)
from group_export import export_account_groups
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
DEFAULT_GROUP_BROWSE_INTERVAL_MINUTES = 20
DEFAULT_GROUP_BROWSE_MESSAGE_LIMIT = 5
GROUP_BROWSE_DELAY_SECONDS = 1
DEFAULT_GROUP_BROWSE_PROBABILITY = {
    "duty": (1 / 19, "1/19"),
    "work": (1 / 3, "1/3"),
    "default": (1 / 29, "1/29"),
}


def parse_probability(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        prob = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            prob = float(numerator.strip()) / float(denominator.strip())
        else:
            prob = float(text)
    else:
        raise ValueError(f"概率配置格式无效: {value!r}")

    if not 0 <= prob <= 1:
        raise ValueError(f"概率需在 0~1 之间: {value!r}")
    return prob


def probability_label(value: Any, prob: float, default_label: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default_label


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_identifier(value: str | int) -> str:
    return str(value).lstrip("@").lower()


def chat_id_variants(value: str | int) -> set[str]:
    """Telegram 超级群组 ID 的多种写法：4215609865 与 -1004215609865。"""
    text = str(value).strip()
    if not text.lstrip("-").isdigit():
        return {normalize_identifier(text)}

    num = int(text)
    variants = {str(num)}
    if num > 0:
        variants.add(f"-100{num}")
    elif text.startswith("-100"):
        variants.add(text[4:])
    return {normalize_identifier(item) for item in variants}


def resolve_group_ref(group_ref: str | int) -> str | int:
    if isinstance(group_ref, int):
        num = group_ref
    else:
        value = str(group_ref).strip()
        if value.lstrip("-").isdigit():
            num = int(value)
        else:
            return value.lstrip("@")

    if num < 0:
        return num
    return int(f"-100{num}")


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
        self.groups = {
            variant
            for group in monitor.get("groups", [])
            for variant in chat_id_variants(group)
        }
        self.skip_mention_group_targets = list(monitor.get("skip_mention_groups") or [])
        self.skip_mention_groups = {
            variant
            for group in self.skip_mention_group_targets
            for variant in chat_id_variants(group)
        }
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
        prob_cfg = monitor.get("group_browse_probability") or {}
        self.group_browse_probabilities: dict[str, tuple[float, str]] = {}
        for key, (default_prob, default_label) in DEFAULT_GROUP_BROWSE_PROBABILITY.items():
            raw = prob_cfg.get(key)
            prob = parse_probability(raw, default_prob)
            label = probability_label(raw, prob, default_label)
            self.group_browse_probabilities[key] = (prob, label)

        command_cfg = config.get("command", {})
        self.command_users = {
            normalize_identifier(u) for u in command_cfg.get("users", [])
        }
        bot_token = command_cfg.get("bot_token")
        self.bot_token = str(bot_token).strip() if bot_token else None
        self.group_export_config = dict(config.get("group_export") or {})

        push_cfg = config.get("push")
        self.push_client = PushClient(push_cfg) if push_cfg else None

        self.client = TelegramClient(str(session_path), self.api_id, self.api_hash)
        self.me = None
        self.start_time: datetime | None = None
        self.message_count = 0
        self._group_browse_task: asyncio.Task | None = None
        self._group_entity_cache: dict[str, Any] = {}
        self._admin_bot_task: asyncio.Task | None = None
        self._admin_bot: Any = None
        self._group_export_lock = asyncio.Lock()
        self._export_checkpoint_coordinator = ExportCheckpointCoordinator()

    def is_command_user(self, user_id: int | None, username: str | None) -> bool:
        if not self.command_users:
            return False
        candidates: set[str] = set()
        if user_id is not None:
            candidates.update(chat_id_variants(user_id))
        if username:
            candidates.add(normalize_identifier(username))
        return bool(candidates & self.command_users)

    def _match_identifier(self, entity: Any, identifiers: set[str]) -> bool:
        if not identifiers or entity is None:
            return False

        candidates: set[str] = set()
        entity_id = getattr(entity, "id", None)
        if entity_id is not None:
            candidates.update(chat_id_variants(entity_id))
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

    async def _get_read_inbox_max_id(self, entity: Any) -> int:
        input_peer = await self.client.get_input_entity(entity)
        result = await self.client(GetPeerDialogsRequest(peers=[input_peer]))
        if not result.dialogs:
            return 0
        return int(result.dialogs[0].read_inbox_max_id or 0)

    def _group_cache_key(self, group_ref: str | int) -> str:
        return normalize_identifier(str(group_ref).strip())

    async def _resolve_group_entity(self, group_ref: str | int) -> Any:
        """解析群组实体；Telethon 需先在会话中见过该群才能仅凭 ID lookup。"""
        cache_key = self._group_cache_key(group_ref)
        cached = self._group_entity_cache.get(cache_key)
        if cached is not None:
            return cached

        target_variants = chat_id_variants(group_ref)
        try:
            entity = await self.client.get_entity(resolve_group_ref(group_ref))
            self._group_entity_cache[cache_key] = entity
            return entity
        except ValueError:
            pass

        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            entity_id = getattr(entity, "id", None)
            if entity_id is not None and chat_id_variants(entity_id) & target_variants:
                self._group_entity_cache[cache_key] = entity
                return entity
            username = getattr(entity, "username", None)
            if username and normalize_identifier(username) in target_variants:
                self._group_entity_cache[cache_key] = entity
                return entity

        raise ValueError(
            f"无法解析群组 {group_ref}，请确认账号已加入该群且曾在客户端中打开过"
        )

    async def _warm_group_entity_cache(self) -> None:
        if not self.group_targets:
            return

        unresolved: list[str] = []
        for group_ref in self.group_targets:
            try:
                entity = await self._resolve_group_entity(group_ref)
                logger.info("群组实体已缓存: %s", display_name(entity))
            except ValueError:
                unresolved.append(str(group_ref))

        if unresolved:
            logger.warning(
                "以下群组无法解析实体，群组浏览将跳过: %s",
                ", ".join(unresolved),
            )

    async def _browse_group(self, group_ref: str | int) -> None:
        entity = await self._resolve_group_entity(group_ref)
        group_name = display_name(entity)
        read_inbox_max_id = await self._get_read_inbox_max_id(entity)

        limit = max(1, self.group_browse_message_limit)
        messages = await self.client.get_messages(entity, limit=limit)
        if not messages:
            if read_inbox_max_id > 0:
                await self.client.send_read_acknowledge(
                    entity, max_id=read_inbox_max_id
                )
                logger.info("群组浏览: %s (无消息)，已更新活跃", group_name)
            else:
                logger.info("群组浏览: %s (无消息)", group_name)
            return

        max_message_id = max(message.id for message in messages)
        ack_max_id = max(max_message_id, read_inbox_max_id)
        await self.client.send_read_acknowledge(entity, max_id=ack_max_id)
        if max_message_id > read_inbox_max_id:
            ack_text = f"已读确认至 {max_message_id}"
        else:
            ack_text = f"活跃更新，已读确认至 {ack_max_id}（无新消息）"

        logger.info(
            "群组浏览: %s，读取 %d 条，%s",
            group_name,
            len(messages),
            ack_text,
        )

    def _current_group_browse_probability(self) -> tuple[float, str]:
        if not self.push_client:
            return 0.0, "未配置"
        if self.push_client.duty_mode:
            return self.group_browse_probabilities["duty"]
        if self.push_client.is_work_hours():
            return self.group_browse_probabilities["work"]
        return self.group_browse_probabilities["default"]

    def _group_browse_probability(self) -> float:
        return self._current_group_browse_probability()[0]

    def _group_browse_probability_text(self) -> str:
        return self._current_group_browse_probability()[1]

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

        probability, prob_text = self._current_group_browse_probability()
        roll = random.random()
        if roll >= probability:
            logger.info(
                "群组浏览: 随机跳过本次活跃更新 (roll=%.3f >= 阈值 %s / %.0f%%)",
                roll,
                prob_text,
                probability * 100,
            )
            return

        logger.info(
            "群组浏览: 随机命中活跃更新 (roll=%.3f < 阈值 %s / %.0f%%)",
            roll,
            prob_text,
            probability * 100,
        )
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
        prob_summary = ", ".join(
            f"{key}={label}({prob:.0%})"
            for key, (prob, label) in self.group_browse_probabilities.items()
        )
        logger.info(
            "群组浏览定时任务已启动，间隔 %d 分钟，概率 %s",
            self.group_browse_interval_minutes,
            prob_summary,
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

        return f"{base}（推送开启，{self._group_browse_probability_text()} 概率执行）"

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
                f"过滤@群组: {len(self.skip_mention_group_targets)}",
                f"群组浏览: {self._group_browse_status_text()}",
                f"命令用户: {len(self.command_users)}",
                f"运行时长: {hours}小时{minutes}分{seconds}秒",
                f"已处理消息: {self.message_count}",
            ]
        )
        return "\n".join(lines)

    def _admin_help_text(self) -> str:
        return (
            "管理命令（可在输入框旁「/」菜单选择）:\n"
            "/status — 系统状态\n"
            "/push_on — 开启推送\n"
            "/push_off — 关闭推送\n"
            "/duty_on — 开启值班模式\n"
            "/duty_off — 关闭值班模式\n"
            "/chatid — 当前群或会话 ID\n"
            "/export_groups [N|keyword|群ID] — 导出 Excel\n"
            "  N<1000: 每批群数；≥1000 或名称: 按 ID/名称筛选群\n\n"
            "监听账号私信仍支持旧写法: /open push、/close push 等"
        )

    async def _run_export_groups(
        self,
        sender_name: str,
        *,
        command_text: str | None = None,
        export_context: ExportCommandContext | None = None,
    ) -> AdminCommandResult:
        if self._group_export_lock.locked():
            return AdminCommandResult("已有群导出任务进行中，请稍后再试")

        export_args = parse_export_groups_args(command_text)
        batch_size = resolve_export_batch_size(
            command_text,
            self.group_export_config,
        )
        filter_note = export_filter_summary(export_args.command_filter)
        if batch_size and (not self.bot_token or not self._admin_bot):
            return AdminCommandResult(
                "分批导出需配置 bot_token 且机器人已启动，请在机器人对话中执行"
            )
        if batch_size and export_context is None:
            return AdminCommandResult("分批导出缺少机器人会话上下文")

        timeout_seconds = int(
            self.group_export_config.get(
                "checkpoint_timeout_seconds",
                DEFAULT_CHECKPOINT_TIMEOUT_SECONDS,
            )
            or DEFAULT_CHECKPOINT_TIMEOUT_SECONDS
        )

        async def on_checkpoint(
            processed: int,
            total: int,
            export_file: Path,
        ) -> bool:
            assert self._admin_bot is not None
            assert export_context is not None
            return await self._export_checkpoint_coordinator.wait_for_confirmation(
                self._admin_bot.bot,
                export_context,
                processed=processed,
                total=total,
                export_file=export_file,
                timeout_seconds=timeout_seconds,
            )

        async with self._group_export_lock:
            logger.info(
                "命令: %s 开始导出群组 (batch=%s, filter=%s)",
                sender_name,
                batch_size or "全部",
                filter_note or "无",
            )
            try:
                export_data = await export_account_groups(
                    self.client,
                    self.group_export_config,
                    checkpoint_every=batch_size,
                    on_checkpoint=on_checkpoint if batch_size else None,
                    command_filter=export_args.command_filter,
                )
            except Exception as exc:
                logger.exception("群导出失败")
                return AdminCommandResult(f"群导出失败: {exc}")

        group_count = len(export_data.group_rows)
        if export_args.command_filter is not None and group_count == 0:
            return AdminCommandResult(
                f"未匹配到任何群（{filter_note or '请检查关键词或群 ID'}）"
            )
        member_count = len(export_data.member_rows)
        excel_path = export_data.excel_path
        logger.info(
            "命令: 群导出结束 %s (%d 群, %d 成员行, cancelled=%s)",
            excel_path,
            group_count,
            member_count,
            export_data.cancelled,
        )

        if export_data.cancelled:
            return AdminCommandResult(
                text=(
                    f"导出已终止：已完成 {group_count} 个群，"
                    f"{member_count} 条成员记录"
                )
            )

        caption = (
            f"导出完成：{group_count} 个群，{member_count} 条成员记录\n"
            f"附件：{excel_path.name}（groups / members 两个 sheet）"
        )
        return AdminCommandResult(
            text=caption,
            documents=(excel_path,),
        )

    async def run_admin_command(
        self,
        command_key: str,
        *,
        sender_name: str,
        message_date: datetime,
        chat_id: int | None = None,
        command_text: str | None = None,
        export_context: ExportCommandContext | None = None,
    ) -> AdminCommandResult:
        if command_key == "help":
            return AdminCommandResult(self._admin_help_text())

        if command_key == "chatid":
            if chat_id is None:
                return AdminCommandResult("无法获取会话 ID")
            logger.info("命令: 机器人返回会话 ID %s", chat_id)
            return AdminCommandResult(str(chat_id))

        if command_key == "status":
            return AdminCommandResult(self._build_status())

        if command_key == "export_groups":
            return await self._run_export_groups(
                sender_name,
                command_text=command_text,
                export_context=export_context,
            )

        if command_key == "open_push":
            if not self.push_client:
                return AdminCommandResult("未配置推送服务")
            self.push_client.open_push()
            logger.info("命令: 推送已开启")
            await self.push_client.send(
                title=sender_name,
                content=f"{sender_name} 执行 push_on，推送已开启",
                message_date=message_date,
            )
            return AdminCommandResult("推送已开启")

        if command_key == "close_push":
            if not self.push_client:
                return AdminCommandResult("未配置推送服务")
            self.push_client.close_push()
            logger.info("命令: 推送已关闭")
            await self.push_client.send(
                title=sender_name,
                content=f"{sender_name} 执行 push_off，推送已关闭",
                message_date=message_date,
                force=True,
            )
            return AdminCommandResult("推送已关闭，消息仅打印")

        if command_key == "open_duty":
            if not self.push_client:
                return AdminCommandResult("未配置推送服务")
            self.push_client.open_duty()
            logger.info("命令: 值班模式已开启")
            await self.push_client.send(
                title=sender_name,
                content=f"{sender_name} 执行 duty_on，值班模式已开启",
                message_date=message_date,
            )
            return AdminCommandResult("值班模式已开启（推送已开启，仅配置群全量推送）")

        if command_key == "close_duty":
            if not self.push_client:
                return AdminCommandResult("未配置推送服务")
            self.push_client.close_duty()
            logger.info("命令: 值班模式已关闭")
            await self.push_client.send(
                title=sender_name,
                content=f"{sender_name} 执行 duty_off，值班模式已关闭",
                message_date=message_date,
            )
            return AdminCommandResult("值班模式已关闭（推送保持当前状态）")

        return AdminCommandResult("未知命令")

    async def _try_handle_command(
        self,
        event: events.NewMessage.Event,
        text: str,
        sender: Any,
    ) -> bool:
        command_key = resolve_admin_command(text)
        if not command_key or command_key in ("help", "chatid"):
            return False

        sender_name = display_name(sender)
        sender_id = getattr(sender, "id", None)
        export_context = None
        if command_key == "export_groups" and sender_id is not None:
            export_context = ExportCommandContext(
                user_id=int(sender_id),
                chat_id=int(sender_id),
            )
        result = await self.run_admin_command(
            command_key,
            sender_name=sender_name,
            message_date=event.message.date,
            chat_id=getattr(await event.get_chat(), "id", None),
            command_text=text,
            export_context=export_context,
        )
        if result.text == "未知命令":
            return False
        await self._reply_admin_result(event, result)
        return True

    async def _reply_admin_result(
        self,
        event: events.NewMessage.Event,
        result: AdminCommandResult,
    ) -> None:
        reply_to = event.message.id
        chat = await event.get_chat()
        attachments = [
            path for path in result.attachment_paths() if path.is_file()
        ]
        if attachments:
            for index, file_path in enumerate(attachments):
                caption = result.text if index == 0 else file_path.name
                await self.client.send_message(
                    chat,
                    caption,
                    reply_to=reply_to,
                    file=file_path,
                )
                await asyncio.sleep(0.25)
            return

        for message in (result.text, *result.reply_messages):
            if not message or not message.strip():
                continue
            await self.client.send_message(
                chat,
                message,
                reply_to=reply_to,
            )

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
            cmd_key = resolve_admin_command(text)
            if cmd_key == "help":
                await event.reply(self._admin_help_text())
                return
            if cmd_key == "chatid":
                chat_id = getattr(chat, "id", None)
                if chat_id is None:
                    await event.reply("无法获取会话 ID")
                else:
                    await event.reply(str(chat_id))
                return
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
        in_watch_list = bool(self.groups) and self._match_identifier(chat, self.groups)

        if in_watch_list:
            group_name = display_name(chat)
            sender_name = display_name(sender)
            display_text = format_telegram_message(event.message, chat)
            print(f"[群组] {group_name} | {sender_name}: {display_text}")

        if await self._try_handle_group_command(event, text, sender, chat):
            return

        mentioned = self._is_mentioned(event)
        in_skip_mention_group = bool(self.skip_mention_groups) and self._match_identifier(
            chat, self.skip_mention_groups
        )
        if in_skip_mention_group and mentioned:
            logger.info("过滤汇总群 @当前账号 消息: %s", display_name(chat))
            return

        need_reply_check = in_watch_list or (
            self.push_client is not None
            and not self.push_client.duty_mode
            and self.push_client.is_work_hours()
        )
        replied_to_me = False
        if need_reply_check:
            replied_to_me = await self._is_reply_to_me(event)

        if self.push_client:
            should_push = self.push_client.should_notify_group(
                mentioned,
                replied_to_me,
                in_watch_list,
                skip_self_mention=in_skip_mention_group,
            )
        else:
            should_push = in_watch_list and (mentioned or replied_to_me)

        if not should_push:
            return

        is_priority = mentioned or replied_to_me
        group_id = getattr(chat, "id", None)
        if self.push_client and not is_priority:
            if self.push_client.is_group_broadcast_rate_limited(group_id or display_name(chat)):
                logger.info(
                    "群组 %s 广播推送已达每分钟上限 (%d)，跳过",
                    display_name(chat),
                    self.push_client.group_broadcast_push_limit_per_minute,
                )
                return

        group_name = display_name(chat)
        sender_name = display_name(sender)
        if not in_watch_list:
            display_text = format_telegram_message(event.message, chat)
            print(f"[群组] {group_name} | {sender_name}: {display_text}")

        content = format_message_content(message=event.message, chat=chat)
        self.message_count += 1
        if self.push_client:
            if not is_priority:
                self.push_client.record_group_broadcast_push(group_id or group_name)
            await self.push_client.send(
                title=f"{group_name}+{sender_name}",
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
            "监听配置 - 私信用户: %s, 群组: %s, 过滤@群组: %s, 命令用户: %s, 群组浏览间隔: %s分钟",
            len(self.private_users),
            len(self.groups),
            len(self.skip_mention_group_targets),
            len(self.command_users),
            self.group_browse_interval_minutes,
        )

        @self.client.on(events.NewMessage)
        async def on_new_message(event: events.NewMessage.Event) -> None:
            try:
                await self._handle_message(event)
            except Exception:
                logger.exception("处理消息失败")

        if self.bot_token:
            if not self.command_users:
                logger.warning("已配置 bot_token 但 command.users 为空，机器人将拒绝所有操作")
            from admin_bot import AdminTelegramBot

            bot = AdminTelegramBot(self, self.bot_token)
            self._admin_bot = bot
            self._admin_bot_task = asyncio.create_task(bot.run())
        elif self.command_users:
            logger.info(
                "未配置 command.bot_token；可在 config 中填写以启用带菜单的管理机器人"
            )

        logger.info("开始监听，按 Ctrl+C 退出...")
        await self._warm_group_entity_cache()
        self._start_group_browse()
        try:
            await self.client.run_until_disconnected()
        finally:
            if self._admin_bot_task and not self._admin_bot_task.done():
                self._admin_bot_task.cancel()
                try:
                    await self._admin_bot_task
                except asyncio.CancelledError:
                    pass


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
