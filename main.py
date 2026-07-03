"""
Telegram 消息监听：私信指定用户、群组 @ 当前用户。

依赖: pip install -r requirements.txt
用法: python main.py -c config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from telethon import TelegramClient, events

from push import PushClient, display_name, format_message_content

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# my.telegram.org 注册失败时可省略配置，使用 Telegram Desktop 公开凭证
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_identifier(value: str | int) -> str:
    return str(value).lstrip("@").lower()


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

        push_cfg = config.get("push")
        self.push_client = PushClient(push_cfg) if push_cfg else None

        self.client = TelegramClient(str(session_path), self.api_id, self.api_hash)
        self.me = None

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

    async def _handle_private(self, event: events.NewMessage.Event) -> None:
        if not self.private_users:
            return

        sender = await event.get_sender()
        if not self._match_identifier(sender, self.private_users):
            return

        text = event.message.message or ""
        sender_name = display_name(sender)
        content = format_message_content(text)
        print(f"[私信] 来自 {sender_name}: {text}")
        if self.push_client:
            await self.push_client.send(
                title=sender_name,
                content=content,
                message_date=event.message.date,
            )

    async def _handle_group(self, event: events.NewMessage.Event) -> None:
        if not self.groups:
            return

        chat = await event.get_chat()
        if not self._match_identifier(chat, self.groups):
            return

        if not self._is_mentioned(event):
            return

        text = event.message.message or ""
        group_name = display_name(chat)
        sender = await event.get_sender()
        sender_name = display_name(sender)
        content = format_message_content(text, sender_name=sender_name)
        my_username = f"@{self.me.username}" if self.me and self.me.username else "当前用户"
        print(f"[群组@{my_username}] {group_name} | {sender_name}: {text}")
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
        username = f"@{self.me.username}" if self.me.username else str(self.me.id)
        logger.info("已登录: %s (%s)", self.me.first_name, username)
        logger.info(
            "监听配置 - 私信用户: %s, 群组: %s",
            len(self.private_users),
            len(self.groups),
        )

        @self.client.on(events.NewMessage)
        async def on_new_message(event: events.NewMessage.Event) -> None:
            try:
                await self._handle_message(event)
            except Exception:
                logger.exception("处理消息失败")

        logger.info("开始监听，按 Ctrl+C 退出...")
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
