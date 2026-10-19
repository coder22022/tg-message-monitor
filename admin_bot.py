"""Telegram 管理机器人：菜单命令下拉，替代手写多词私信命令。"""

from __future__ import annotations

import asyncio
import logging
from datetime import timezone
from typing import TYPE_CHECKING

from telegram import InputFile, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from admin_command_result import AdminCommandResult
from admin_commands import BOT_MENU_COMMANDS
from export_delivery import ExportCommandContext

if TYPE_CHECKING:
    from main import TgMessageMonitor

logger = logging.getLogger(__name__)


class AdminTelegramBot:
    def __init__(self, monitor: TgMessageMonitor, token: str) -> None:
        self._monitor = monitor
        self._token = token
        self._application: Application | None = None

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        return self._monitor.is_command_user(user.id, user.username)

    async def _deny(self, update: Update) -> None:
        if update.message:
            await update.message.reply_text("无权限：仅配置的管理员可使用此机器人")

    async def _run_command(
        self,
        update: Update,
        _context: ContextTypes.DEFAULT_TYPE,
        command_key: str,
    ) -> None:
        if not update.message:
            return
        if not self._authorized(update):
            await self._deny(update)
            return

        user = update.effective_user
        assert user is not None
        sender_name = user.full_name or user.username or str(user.id)
        message_date = update.message.date
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)

        chat_id = update.effective_chat.id if update.effective_chat else None
        message_text = update.message.text or ""
        export_context = None
        if command_key == "export_groups":
            await update.message.reply_text(
                "正在导出群组与成员，请稍候…",
                reply_to_message_id=update.message.message_id,
            )
            export_context = ExportCommandContext(
                user_id=user.id,
                chat_id=chat_id or user.id,
                reply_to_message_id=update.message.message_id,
            )

        result = await self._monitor.run_admin_command(
            command_key,
            sender_name=sender_name,
            message_date=message_date,
            chat_id=chat_id,
            command_text=message_text,
            export_context=export_context,
        )
        await self._reply_result(update, result)

    async def _reply_result(
        self,
        update: Update,
        result: AdminCommandResult,
    ) -> None:
        if not update.message:
            return
        reply_to = update.message.message_id
        attachments = [
            path for path in result.attachment_paths() if path.is_file()
        ]
        if attachments:
            for index, file_path in enumerate(attachments):
                caption = (
                    (result.text if index == 0 else file_path.name)[:1024]
                )
                await update.message.reply_document(
                    document=InputFile(
                        file_path.read_bytes(),
                        filename=file_path.name,
                    ),
                    caption=caption,
                    reply_to_message_id=reply_to,
                )
                await asyncio.sleep(0.25)
            return

        for message in (result.text, *result.reply_messages):
            if not message or not message.strip():
                continue
            await update.message.reply_text(
                message,
                reply_to_message_id=reply_to,
            )

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._run_command(update, context, "status")

    async def _cmd_push_on(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._run_command(update, context, "open_push")

    async def _cmd_push_off(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._run_command(update, context, "close_push")

    async def _cmd_duty_on(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._run_command(update, context, "open_duty")

    async def _cmd_duty_off(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._run_command(update, context, "close_duty")

    async def _cmd_chatid(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._run_command(update, context, "chatid")

    async def _cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._run_command(update, context, "help")

    async def _cmd_export_groups(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._run_command(update, context, "export_groups")

    async def _on_export_checkpoint_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query or not query.data or not query.from_user:
            return

        user = query.from_user
        parts = query.data.split(":")
        if len(parts) != 3 or parts[0] != "export_cp":
            await query.answer()
            return

        if not self._monitor.is_command_user(user.id, user.username):
            await query.answer("无权限", show_alert=True)
            return

        action = parts[1]
        token = parts[2]

        if action == "dl":
            export_file = (
                self._monitor._export_checkpoint_coordinator.get_export_file(
                    token,
                    user.id,
                )
            )
            if export_file is None:
                await query.answer("文件不可用或确认已结束", show_alert=True)
                return
            await query.answer("正在发送文件…")
            reply_to = query.message.message_id if query.message else None
            chat_id = query.message.chat_id if query.message else user.id
            await self.bot.send_document(
                chat_id=chat_id,
                document=InputFile(
                    export_file.read_bytes(),
                    filename=export_file.name,
                ),
                reply_to_message_id=reply_to,
            )
            logger.info("群导出下载: user=%s token=%s file=%s", user.id, token, export_file)
            return

        if action not in ("go", "stop"):
            await query.answer()
            return

        continue_export = action == "go"
        accepted = self._monitor._export_checkpoint_coordinator.resolve_callback(
            token,
            user.id,
            continue_export,
        )
        if not accepted:
            await query.answer("确认已失效或已超时", show_alert=True)
            return

        label = "继续导出" if continue_export else "已终止"
        await query.answer(f"已收到：{label}")
        logger.info(
            "群导出确认: user=%s action=%s token=%s",
            user.id,
            action,
            token,
        )
        if query.message:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                logger.debug("移除确认按钮失败", exc_info=True)

    @property
    def bot(self):
        if self._application is None:
            raise RuntimeError("管理机器人尚未启动")
        return self._application.bot

    def _build_application(self) -> Application:
        app = (
            Application.builder()
            .token(self._token)
            .concurrent_updates(True)
            .post_init(self._register_menu_commands)
            .build()
        )
        app.add_handler(
            CallbackQueryHandler(
                self._on_export_checkpoint_callback,
                pattern=r"^export_cp:",
            )
        )
        for name, handler in (
            ("status", self._cmd_status),
            ("push_on", self._cmd_push_on),
            ("push_off", self._cmd_push_off),
            ("duty_on", self._cmd_duty_on),
            ("duty_off", self._cmd_duty_off),
            ("chatid", self._cmd_chatid),
            ("export_groups", self._cmd_export_groups),
            ("help", self._cmd_help),
            ("start", self._cmd_help),
        ):
            app.add_handler(CommandHandler(name, handler))
        return app

    async def _register_menu_commands(self, app: Application) -> None:
        await app.bot.set_my_commands(list(BOT_MENU_COMMANDS))
        logger.info("管理机器人菜单命令已注册 (%d 项)", len(BOT_MENU_COMMANDS))

    async def run(self) -> None:
        self._application = self._build_application()
        app = self._application
        await app.initialize()
        await app.start()
        logger.info("管理机器人已启动（私信或群内 / 菜单操作）")
        await app.updater.start_polling(drop_pending_updates=True)
        stop = asyncio.Event()
        try:
            await stop.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("管理机器人已停止")
