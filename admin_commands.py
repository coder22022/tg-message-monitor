"""管理命令解析与 Bot 菜单定义。"""

from __future__ import annotations

from telegram import BotCommand

ADMIN_COMMAND_ALIASES: dict[str, str] = {
    "/open push": "open_push",
    "/close push": "close_push",
    "/open duty": "open_duty",
    "/close duty": "close_duty",
    "/status": "status",
    "/push_on": "open_push",
    "/push_off": "close_push",
    "/duty_on": "open_duty",
    "/duty_off": "close_duty",
    "/help": "help",
    "/start": "help",
    "/chatid": "chatid",
    "/id": "chatid",
    "/export_groups": "export_groups",
    "/export groups": "export_groups",
}

BOT_MENU_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("status", "查看系统状态"),
    BotCommand("push_on", "开启推送"),
    BotCommand("push_off", "关闭推送"),
    BotCommand("duty_on", "开启值班模式"),
    BotCommand("duty_off", "关闭值班模式"),
    BotCommand("chatid", "获取当前群/会话 ID"),
    BotCommand("export_groups", "导出群与成员 Excel"),
    BotCommand("help", "命令说明"),
)


def normalize_command(text: str) -> str:
    return " ".join(text.strip().lower().split())


def resolve_admin_command(text: str) -> str | None:
    command = normalize_command(text)
    if command in ADMIN_COMMAND_ALIASES:
        return ADMIN_COMMAND_ALIASES[command]
    if not command:
        return None
    first = command.split()[0]
    return ADMIN_COMMAND_ALIASES.get(first) or ADMIN_COMMAND_ALIASES.get(command)
