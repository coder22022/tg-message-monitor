"""导出分批确认：继续 / 终止 / 按需下载。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_TIMEOUT_SECONDS = 120


@dataclass
class ExportCommandContext:
    user_id: int
    chat_id: int
    reply_to_message_id: int | None = None


@dataclass
class ExportCheckpointState:
    token: str
    owner_user_id: int
    export_file: Path
    chat_id: int
    reply_to_message_id: int | None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    continue_export: bool = False


class ExportCheckpointCoordinator:
    def __init__(self) -> None:
        self._active: ExportCheckpointState | None = None

    def clear(self) -> None:
        self._active = None

    def get_export_file(self, token: str, user_id: int) -> Path | None:
        state = self._active
        if state is None or state.token != token:
            return None
        if state.owner_user_id != user_id:
            return None
        if not state.export_file.is_file():
            return None
        return state.export_file

    def resolve_callback(self, token: str, user_id: int, continue_export: bool) -> bool:
        state = self._active
        if state is None or state.token != token:
            return False
        if state.owner_user_id != user_id:
            return False
        state.continue_export = continue_export
        state.done.set()
        return True

    async def wait_for_confirmation(
        self,
        bot: Any,
        context: ExportCommandContext,
        *,
        processed: int,
        total: int,
        export_file: Path,
        timeout_seconds: int,
    ) -> bool:
        token = uuid.uuid4().hex[:12]
        state = ExportCheckpointState(
            token=token,
            owner_user_id=context.user_id,
            export_file=export_file,
            chat_id=context.chat_id,
            reply_to_message_id=context.reply_to_message_id,
        )
        self._active = state

        text = (
            f"已导出 {processed}/{total} 个群。\n"
            f"可点击「下载」获取当前汇总 Excel（groups / members 两个 sheet）。\n"
            f"请在 {timeout_seconds} 秒内选择「继续导出」或「终止」。"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "继续导出",
                        callback_data=f"export_cp:go:{token}",
                    ),
                    InlineKeyboardButton(
                        "终止",
                        callback_data=f"export_cp:stop:{token}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "下载",
                        callback_data=f"export_cp:dl:{token}",
                    ),
                ],
            ]
        )

        try:
            await bot.send_message(
                chat_id=context.chat_id,
                text=text,
                reply_markup=keyboard,
                reply_to_message_id=context.reply_to_message_id,
            )

            await asyncio.wait_for(state.done.wait(), timeout=timeout_seconds)
            if not state.continue_export:
                await bot.send_message(
                    chat_id=context.chat_id,
                    text="导出已终止。",
                    reply_to_message_id=context.reply_to_message_id,
                )
                return False

            await bot.send_message(
                chat_id=context.chat_id,
                text="继续导出剩余群组…",
                reply_to_message_id=context.reply_to_message_id,
            )
            return True
        except asyncio.TimeoutError:
            await bot.send_message(
                chat_id=context.chat_id,
                text="确认超时（2 分钟），导出已终止。",
                reply_to_message_id=context.reply_to_message_id,
            )
            return False
        finally:
            self.clear()
