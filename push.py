from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def display_name(entity: Any) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"

    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    name = f"{first} {last}".strip()
    if name:
        return name

    title = getattr(entity, "title", None)
    if title:
        return str(title)

    entity_id = getattr(entity, "id", None)
    return str(entity_id) if entity_id is not None else "unknown"


def format_message_content(text: str, sender_name: str | None = None) -> str:
    content = text.strip()
    if not content:
        content = "[非文本消息]"

    if sender_name:
        return f"{sender_name}: {content}"
    return content


def format_message_date(message_date: datetime | None) -> str:
    dt = message_date or datetime.now()
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class PushClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config_enabled = bool(config.get("enabled", True))
        self.url = str(config.get("url", "https://push.i-i.me")).rstrip("/")
        self.method = str(config.get("method", "POST")).upper()
        self.push_key = str(config.get("push_key", "")).strip()
        self.msg_type = str(config.get("type", "text"))
        self.runtime_enabled = True

        if self.config_enabled and not self.push_key:
            raise ValueError("push.push_key 未配置")

    def open_push(self) -> None:
        self.runtime_enabled = True

    def close_push(self) -> None:
        self.runtime_enabled = False

    def is_active(self) -> bool:
        return self.config_enabled and self.runtime_enabled

    def status_text(self) -> str:
        if not self.config_enabled:
            return "未启用（配置关闭）"
        return "已开启" if self.runtime_enabled else "已关闭"

    def _build_payload(
        self,
        title: str,
        content: str,
        message_date: datetime | None,
    ) -> dict[str, str]:
        return {
            "push_key": self.push_key,
            "title": title,
            "content": content,
            "date": format_message_date(message_date),
            "type": self.msg_type,
        }

    def _send_sync(self, payload: dict[str, str]) -> None:
        encoded = urllib.parse.urlencode(payload).encode("utf-8")

        if self.method == "GET":
            request = urllib.request.Request(f"{self.url}?{encoded.decode('utf-8')}")
        else:
            request = urllib.request.Request(
                self.url,
                data=encoded,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            logger.info("推送成功 [%s]: %s", response.status, body[:200])

    async def send(
        self,
        title: str,
        content: str,
        message_date: datetime | None = None,
        force: bool = False,
    ) -> None:
        if not self.config_enabled:
            return
        if not force and not self.runtime_enabled:
            return

        payload = self._build_payload(title, content, message_date)
        try:
            await asyncio.to_thread(self._send_sync, payload)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("推送失败 HTTP %s: %s", exc.code, body[:200])
        except Exception:
            logger.exception("推送失败")
