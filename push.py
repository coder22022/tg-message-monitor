from __future__ import annotations

import asyncio
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PUSH_TIMEOUT_SECONDS = 15
DEFAULT_GROUP_BROADCAST_PUSH_LIMIT_PER_MINUTE = 0


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


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def format_curl_command(
    url: str,
    *,
    method: str = "GET",
    timeout: int = DEFAULT_PUSH_TIMEOUT_SECONDS,
) -> str:
    return f"curl -X {method} {_shell_quote(url)} --max-time {timeout} -v -sS"


def format_push_request_log(
    title: str,
    content: str,
    url: str,
    *,
    timeout: int = DEFAULT_PUSH_TIMEOUT_SECONDS,
) -> str:
    content_preview = content if len(content) <= 500 else content[:500] + "..."
    curl = format_curl_command(url, timeout=timeout)
    return (
        f"title: {title}\n"
        f"content: {content_preview}\n"
        f"content_length: {len(content)}\n"
        f"url: {url}\n"
        f"timeout: {timeout}s\n"
        f"curl: {curl}"
    )


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
        self.url = str(config.get("url", "https://api.day.app")).rstrip("/")
        self.device_key = str(
            config.get("device_key") or config.get("push_key", "")
        ).strip()
        self.quiet_periods = parse_quiet_periods(config.get("quiet_hours"))
        self.work_periods = parse_time_periods(config.get("work_hours"), "work_hours")
        self.request_timeout = int(
            config.get("timeout", DEFAULT_PUSH_TIMEOUT_SECONDS)
        )
        self.group_broadcast_push_limit_per_minute = int(
            config.get(
                "group_broadcast_push_limit_per_minute",
                DEFAULT_GROUP_BROADCAST_PUSH_LIMIT_PER_MINUTE,
            )
        )
        self.runtime_enabled = True
        self.duty_mode = False
        self._group_broadcast_push_times: dict[str, deque[datetime]] = defaultdict(deque)

        if self.config_enabled and not self.device_key:
            raise ValueError("push.device_key 未配置")

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

    def can_update_activity(self, moment: datetime | None = None) -> bool:
        return self.is_active() and not self.is_quiet_hours(moment)

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

    def should_notify_group(
        self,
        mentioned: bool,
        replied_to_me: bool,
        in_watch_list: bool = True,
        moment: datetime | None = None,
        skip_self_mention: bool = False,
    ) -> bool:
        # 优先级：推送开关由 send() 控制；值班 > 上班 > 其他
        # skip_self_mention：先丢弃 @当前账号，再走后续规则（转发汇总群去重）
        if skip_self_mention and mentioned:
            return False
        if in_watch_list:
            if mentioned or replied_to_me:
                return True
            if self.duty_mode:
                return True
            return self.is_work_hours(moment)

        if self.duty_mode:
            return False
        if not self.is_work_hours(moment):
            return False
        return mentioned or replied_to_me

    def _group_broadcast_key(self, group_id: str | int) -> str:
        return str(group_id)

    def _prune_group_broadcast_push_times(
        self,
        group_key: str,
        moment: datetime,
    ) -> None:
        times = self._group_broadcast_push_times.get(group_key)
        if not times:
            return
        cutoff = moment.timestamp() - 60
        while times and times[0].timestamp() <= cutoff:
            times.popleft()
        if not times:
            self._group_broadcast_push_times.pop(group_key, None)

    def is_group_broadcast_rate_limited(
        self,
        group_id: str | int,
        moment: datetime | None = None,
    ) -> bool:
        limit = self.group_broadcast_push_limit_per_minute
        if limit <= 0:
            return False
        now = moment or datetime.now()
        group_key = self._group_broadcast_key(group_id)
        self._prune_group_broadcast_push_times(group_key, now)
        times = self._group_broadcast_push_times.get(group_key)
        return bool(times) and len(times) >= limit

    def record_group_broadcast_push(
        self,
        group_id: str | int,
        moment: datetime | None = None,
    ) -> None:
        if self.group_broadcast_push_limit_per_minute <= 0:
            return
        group_key = self._group_broadcast_key(group_id)
        self._group_broadcast_push_times[group_key].append(moment or datetime.now())

    def group_broadcast_rate_limit_text(self) -> str:
        limit = self.group_broadcast_push_limit_per_minute
        if limit <= 0:
            return "未限制"
        return f"每群最多 {limit} 条/分钟"

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
            return "值班模式（配置群全量推送）"
        if self.is_work_hours():
            return "上班时间（配置群全量推送，未配置群 @/回复 推送）"
        return "默认（配置群仅 @/回复 推送）"

    def build_status_lines(self) -> list[str]:
        return [
            f"推送开关: {self.status_text()}",
            f"免打扰: {self.quiet_hours_text()}",
            f"值班模式: {self.duty_mode_text()}",
            f"上班时间: {self.work_hours_text()}",
            f"群组推送策略: {self.group_push_policy_text()}",
            f"群广播限流: {self.group_broadcast_rate_limit_text()}",
        ]

    def _build_bark_url(self, title: str, content: str) -> str:
        encoded_title = urllib.parse.quote(title, safe="")
        encoded_body = urllib.parse.quote(content, safe="")
        return f"{self.url}/{self.device_key}/{encoded_title}/{encoded_body}"

    def _format_request_log(self, title: str, content: str, url: str) -> str:
        return format_push_request_log(
            title,
            content,
            url,
            timeout=self.request_timeout,
        )

    def _send_sync(self, url: str) -> None:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            logger.info("推送成功 [%s]: %s", response.status, body[:200])

    def _log_push_failure(
        self,
        request_log: str,
        exc: BaseException,
    ) -> None:
        if isinstance(exc, urllib.error.HTTPError):
            body = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "推送失败 HTTP %s\n%s\nresponse: %s",
                exc.code,
                request_log,
                body[:500],
            )
            return

        if isinstance(exc, urllib.error.URLError):
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                logger.error(
                    "推送超时 (%ds)\n%s\nerror: %s",
                    self.request_timeout,
                    request_log,
                    reason,
                )
                return
            logger.error(
                "推送网络错误\n%s\nerror: %s",
                request_log,
                reason,
            )
            return

        logger.error("推送失败\n%s\nerror: %s", request_log, exc)

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

        url = self._build_bark_url(title, content)
        request_log = self._format_request_log(title, content, url)
        logger.info("推送请求:\n%s", request_log)
        try:
            await asyncio.to_thread(self._send_sync, url)
        except Exception as exc:
            self._log_push_failure(request_log, exc)
