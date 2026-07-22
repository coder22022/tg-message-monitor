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


def format_message_content(
    text: str | None = None,
    sender_name: str | None = None,
    *,
    message: Any = None,
    chat: Any = None,
) -> str:
    if message is not None:
        from message_format import format_telegram_message

        content = format_telegram_message(message, chat)
    else:
        content = (text or "").strip()
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


DEFAULT_HEARTBEAT_INTERVAL_MINUTES = 30


def _parse_hhmm(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，实际为: {value}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间范围无效: {value}")
    return hour * 60 + minute


class TimePeriod:
    def __init__(self, start: str, end: str) -> None:
        self.start_label = start.strip()
        self.end_label = end.strip()
        self.start_minutes = _parse_hhmm(self.start_label)
        self.end_minutes = _parse_hhmm(self.end_label)

    def contains(self, moment: datetime) -> bool:
        current = moment.hour * 60 + moment.minute
        if self.start_minutes <= self.end_minutes:
            return self.start_minutes <= current < self.end_minutes
        return current >= self.start_minutes or current < self.end_minutes

    def label(self) -> str:
        return f"{self.start_label}-{self.end_label}"


QuietPeriod = TimePeriod


def parse_time_periods(config: list[Any] | None, field_name: str) -> list[TimePeriod]:
    if not config:
        return []

    periods: list[TimePeriod] = []
    for item in config:
        if not isinstance(item, dict):
            raise ValueError(f"{field_name} 每项应为包含 start/end 的对象")
        start = item.get("start")
        end = item.get("end")
        if not start or not end:
            raise ValueError(f"{field_name} 每项需配置 start 和 end")
        periods.append(TimePeriod(str(start), str(end)))
    return periods


def parse_quiet_periods(config: list[Any] | None) -> list[TimePeriod]:
    return parse_time_periods(config, "quiet_hours")


class PushClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config_enabled = bool(config.get("enabled", True))
        self.url = str(config.get("url", "https://push.i-i.me")).rstrip("/")
        self.method = str(config.get("method", "POST")).upper()
        self.push_key = str(config.get("push_key", "")).strip()
        self.msg_type = str(config.get("type", "text"))
        self.heartbeat_interval_minutes = int(
            config.get("heartbeat_interval_minutes", DEFAULT_HEARTBEAT_INTERVAL_MINUTES)
        )
        self.quiet_periods = parse_quiet_periods(config.get("quiet_hours"))
        self.work_periods = parse_time_periods(config.get("work_hours"), "work_hours")
        self.runtime_enabled = True
        self.duty_mode = False
        self.last_push_time: datetime | None = None

        if self.config_enabled and not self.push_key:
            raise ValueError("push.push_key 未配置")

    def open_push(self) -> None:
        self.runtime_enabled = True

    def close_push(self) -> None:
        self.runtime_enabled = False

    def open_duty(self) -> None:
        self.runtime_enabled = True
        self.duty_mode = True

    def close_duty(self) -> None:
        self.duty_mode = False

    def is_active(self) -> bool:
        return self.config_enabled and self.runtime_enabled

    def is_quiet_hours(self, moment: datetime | None = None) -> bool:
        if not self.quiet_periods:
            return False
        now = moment or datetime.now()
        return any(period.contains(now) for period in self.quiet_periods)

    def is_work_hours(self, moment: datetime | None = None) -> bool:
        if not self.work_periods:
            return False
        now = moment or datetime.now()
        return any(period.contains(now) for period in self.work_periods)

    def should_notify_group(self, mentioned: bool, replied_to_me: bool) -> bool:
        if mentioned or replied_to_me:
            return True
        if self.duty_mode:
            return True
        if self.is_work_hours():
            return True
        return False

    def _periods_text(self, periods: list[TimePeriod], active: bool) -> str:
        if not periods:
            return "未配置"
        labels = "、".join(period.label() for period in periods)
        return f"{labels}（{'生效中' if active else '未生效'}）"

    def status_text(self) -> str:
        if not self.config_enabled:
            return "未启用（配置关闭）"
        if not self.runtime_enabled:
            return "已关闭"
        return "已开启"

    def quiet_hours_text(self) -> str:
        return self._periods_text(self.quiet_periods, self.is_quiet_hours())

    def work_hours_text(self) -> str:
        return self._periods_text(self.work_periods, self.is_work_hours())

    def duty_mode_text(self) -> str:
        return "已开启" if self.duty_mode else "已关闭"

    def group_push_policy_text(self) -> str:
        if not self.is_active():
            return "推送已关闭"
        if self.is_quiet_hours():
            return "免打扰中（不推送）"
        if self.duty_mode:
            return "值班模式（群消息全量推送）"
        if self.is_work_hours():
            return "上班时间（群消息全量推送）"
        return "默认（仅 @/回复 推送）"

    def build_status_lines(self) -> list[str]:
        return [
            f"推送开关: {self.status_text()}",
            f"免打扰: {self.quiet_hours_text()}",
            f"值班模式: {self.duty_mode_text()}",
            f"上班时间: {self.work_hours_text()}",
            f"群组推送策略: {self.group_push_policy_text()}",
        ]

    def mark_push_sent(self) -> None:
        self.last_push_time = datetime.now()

    def needs_heartbeat(self) -> bool:
        if not self.is_active() or self.is_quiet_hours():
            return False
        if self.last_push_time is None:
            return True
        interval_seconds = self.heartbeat_interval_minutes * 60
        elapsed = (datetime.now() - self.last_push_time).total_seconds()
        return elapsed >= interval_seconds

    async def send_heartbeat(self) -> None:
        await self.send(
            title="心跳",
            content="监听服务运行正常",
            message_date=datetime.now(),
        )

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
        if not force and self.is_quiet_hours():
            logger.info("免打扰时段，跳过推送: %s", title)
            return

        payload = self._build_payload(title, content, message_date)
        try:
            await asyncio.to_thread(self._send_sync, payload)
            self.mark_push_sent()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("推送失败 HTTP %s: %s", exc.code, body[:200])
        except Exception:
            logger.exception("推送失败")
