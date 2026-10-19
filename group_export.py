"""登录账号群组与成员导出为 Excel（groups / members 两个 sheet）。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from openpyxl import Workbook
from telethon import TelegramClient
from telethon.errors import ChatAdminRequiredError, UserNotParticipantError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import (
    Channel,
    ChannelParticipantsAdmins,
    Chat,
    ChatParticipantAdmin,
    ChatParticipantCreator,
    User,
)

logger = logging.getLogger(__name__)

MEMBER_FETCH_DELAY_SECONDS = 0.35
GROUP_FETCH_DELAY_SECONDS = 0.5

GROUP_CSV_HEADERS = [
    "group_id",
    "title",
    "username",
    "type",
    "description",
    "member_count",
    "includes_all_members",
    "online_count",
    "unread_count",
    "is_verified",
    "is_scam",
    "is_fake",
    "avatar_path",
    "exported_at",
]

MEMBER_CSV_HEADERS = [
    "group_id",
    "group_title",
    "user_id",
    "username",
    "first_name",
    "last_name",
    "display_name",
    "is_bot",
    "is_premium",
    "is_admin",
    "is_deleted",
    "phone",
    "avatar_path",
]


@dataclass(frozen=True)
class GroupExportData:
    excel_path: Path
    group_rows: list[dict[str, Any]]
    member_rows: list[dict[str, Any]]
    cancelled: bool = False
    checkpoint_files_sent: bool = False


@dataclass(frozen=True)
class MemberExportFilter:
    exclude_bots: bool = True
    exclude_deleted_accounts: bool = True
    exclude_scam_accounts: bool = True
    exclude_fake_accounts: bool = True


def parse_member_export_filter(config: dict[str, Any]) -> MemberExportFilter:
    return MemberExportFilter(
        exclude_bots=bool(config.get("exclude_bots", True)),
        exclude_deleted_accounts=bool(config.get("exclude_deleted_accounts", True)),
        exclude_scam_accounts=bool(config.get("exclude_scam_accounts", True)),
        exclude_fake_accounts=bool(config.get("exclude_fake_accounts", True)),
    )


def should_include_member(user: User, filters: MemberExportFilter) -> bool:
    if filters.exclude_bots and user.bot:
        return False
    if filters.exclude_deleted_accounts and _is_deleted_account(user):
        return False
    if filters.exclude_scam_accounts and getattr(user, "scam", False):
        return False
    if filters.exclude_fake_accounts and getattr(user, "fake", False):
        return False
    return True


def _is_deleted_account(user: User) -> bool:
    if getattr(user, "deleted", False):
        return True
    first = (user.first_name or "").strip().casefold()
    deleted_names = {
        "deleted account",
        "account deleted",
        "账户已删除",
        "已删除账户",
        "deactivated user",
    }
    return first in deleted_names


def normalize_identifier(value: str | int) -> str:
    return str(value).lstrip("@").lower()


def chat_id_variants(value: str | int) -> set[str]:
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


def build_filter_variant_set(items: list[Any] | None) -> set[str]:
    if not items:
        return set()
    variants: set[str] = set()
    for item in items:
        variants.update(chat_id_variants(item))
    return variants


def entity_identifier_variants(entity: Any) -> set[str]:
    variants: set[str] = set()
    entity_id = getattr(entity, "id", None)
    if entity_id is not None:
        variants.update(chat_id_variants(entity_id))
    username = getattr(entity, "username", None)
    if username:
        variants.add(normalize_identifier(username))
    return variants


def passes_group_filter(
    entity: Any,
    include_variants: set[str],
    exclude_variants: set[str],
) -> bool:
    variants = entity_identifier_variants(entity)
    if exclude_variants and variants & exclude_variants:
        return False
    if include_variants:
        return bool(variants & include_variants)
    return True


def entity_matches_command_filter(
    entity: Any,
    *,
    name_keyword: str | None,
    group_id_variants: frozenset[str] | None,
) -> bool:
    if group_id_variants:
        return bool(entity_identifier_variants(entity) & set(group_id_variants))
    if name_keyword:
        keyword = name_keyword.casefold()
        title = (getattr(entity, "title", None) or "").casefold()
        username = (getattr(entity, "username", None) or "").casefold()
        if keyword in title or keyword in username:
            return True
        return False
    return True


def is_exportable_group(entity: Any) -> bool:
    if isinstance(entity, Chat):
        return True
    if isinstance(entity, Channel):
        return bool(entity.megagroup)
    return False


def group_type_label(entity: Any) -> str:
    if isinstance(entity, Chat):
        return "basic_group"
    if isinstance(entity, Channel) and entity.megagroup:
        return "supergroup"
    if isinstance(entity, Channel):
        return "channel"
    return "unknown"


def user_display_name(user: User) -> str:
    first = (user.first_name or "").strip()
    last = (user.last_name or "").strip()
    name = f"{first} {last}".strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


async def _fetch_group_details(client: TelegramClient, entity: Any) -> dict[str, Any]:
    description = ""
    member_count: int | None = None
    online_count: int | None = None

    try:
        if isinstance(entity, Channel):
            full = await client(GetFullChannelRequest(entity))
            description = (full.full_chat.about or "").strip()
            member_count = getattr(full.full_chat, "participants_count", None)
            online_count = getattr(full.full_chat, "online_count", None)
        elif isinstance(entity, Chat):
            full = await client(GetFullChatRequest(entity.id))
            description = (full.full_chat.about or "").strip()
            member_count = getattr(full.full_chat, "participants_count", None)
    except Exception:
        logger.exception("获取群详情失败: %s", getattr(entity, "title", entity))

    return {
        "description": description,
        "member_count": member_count,
        "online_count": online_count,
    }


async def _download_avatar(
    client: TelegramClient,
    entity: Any,
    dest_dir: Path,
    prefix: str,
) -> str:
    entity_id = getattr(entity, "id", None)
    if entity_id is None:
        return ""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{prefix}_{entity_id}"
    try:
        path = await client.download_profile_photo(entity, file=str(target))
        if path:
            return str(Path(path).resolve())
    except Exception:
        logger.debug("下载头像失败: %s", entity_id, exc_info=True)
    return ""


def _row_values(row: dict[str, Any], headers: list[str]) -> list[Any]:
    return [row.get(key, "") for key in headers]


def _write_export_workbook(
    excel_path: Path,
    group_rows: list[dict[str, Any]],
    member_rows: list[dict[str, Any]],
) -> None:
    workbook = Workbook()
    groups_sheet = workbook.active
    groups_sheet.title = "groups"
    groups_sheet.append(GROUP_CSV_HEADERS)
    for row in group_rows:
        groups_sheet.append(_row_values(row, GROUP_CSV_HEADERS))

    members_sheet = workbook.create_sheet("members")
    members_sheet.append(MEMBER_CSV_HEADERS)
    for row in member_rows:
        members_sheet.append(_row_values(row, MEMBER_CSV_HEADERS))

    excel_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(excel_path)


async def _collect_export_dialogs(
    client: TelegramClient,
    config: dict[str, Any],
    command_filter: Any | None = None,
) -> list[Any]:
    include_variants = build_filter_variant_set(config.get("include_groups"))
    exclude_variants = build_filter_variant_set(config.get("exclude_groups"))
    name_keyword = None
    id_variants: frozenset[str] | None = None
    if command_filter is not None:
        name_keyword = command_filter.name_keyword
        id_variants = command_filter.group_id_variants

    selected = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not is_exportable_group(entity):
            continue
        if not passes_group_filter(entity, include_variants, exclude_variants):
            continue
        if not entity_matches_command_filter(
            entity,
            name_keyword=name_keyword,
            group_id_variants=id_variants,
        ):
            continue
        selected.append(dialog)
    return selected


async def _collect_group_admin_user_ids(
    client: TelegramClient,
    entity: Any,
) -> set[int]:
    admin_ids: set[int] = set()
    try:
        if isinstance(entity, Chat):
            full = await client(GetFullChatRequest(entity.id))
            participants = getattr(full.full_chat.participants, "participants", None) or []
            for participant in participants:
                if isinstance(participant, (ChatParticipantAdmin, ChatParticipantCreator)):
                    admin_ids.add(int(participant.user_id))
            return admin_ids

        async for user in client.iter_participants(
            entity,
            filter=ChannelParticipantsAdmins,
        ):
            if isinstance(user, User):
                admin_ids.add(user.id)
    except (ChatAdminRequiredError, UserNotParticipantError) as exc:
        logger.warning(
            "无法拉取群管理员列表 %s: %s",
            getattr(entity, "title", entity),
            exc,
        )
    except Exception:
        logger.exception(
            "拉取群管理员列表失败: %s",
            getattr(entity, "title", entity),
        )
    return admin_ids


async def _export_single_dialog(
    client: TelegramClient,
    dialog: Any,
    *,
    exported_at: str,
    avatar_dir: Path,
    download_avatars: bool,
    max_members: int,
    member_filter: MemberExportFilter,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entity = dialog.entity
    group_id = entity.id
    title = getattr(entity, "title", "") or ""
    username = getattr(entity, "username", "") or ""
    details = await _fetch_group_details(client, entity)
    group_avatar = ""
    if download_avatars:
        group_avatar = await _download_avatar(client, entity, avatar_dir, "group")

    group_row = {
        "group_id": group_id,
        "title": title,
        "username": username,
        "type": group_type_label(entity),
        "description": details["description"],
        "member_count": details["member_count"],
        "includes_all_members": True,
        "online_count": details["online_count"],
        "unread_count": dialog.unread_count,
        "is_verified": getattr(entity, "verified", False),
        "is_scam": getattr(entity, "scam", False),
        "is_fake": getattr(entity, "fake", False),
        "avatar_path": group_avatar,
        "exported_at": exported_at,
    }

    member_rows: list[dict[str, Any]] = []
    exported_member_count = 0
    hit_member_export_limit = False
    admin_user_ids = await _collect_group_admin_user_ids(client, entity)
    try:
        async for user in client.iter_participants(entity):
            if not isinstance(user, User):
                continue
            if not should_include_member(user, member_filter):
                continue
            avatar_path = ""
            if download_avatars:
                avatar_path = await _download_avatar(client, user, avatar_dir, "user")
            member_rows.append(
                {
                    "group_id": group_id,
                    "group_title": title,
                    "user_id": user.id,
                    "username": user.username or "",
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                    "display_name": user_display_name(user),
                    "is_bot": user.bot,
                    "is_premium": bool(getattr(user, "premium", False)),
                    "is_admin": user.id in admin_user_ids,
                    "is_deleted": bool(getattr(user, "deleted", False)),
                    "phone": user.phone or "",
                    "avatar_path": avatar_path,
                }
            )
            exported_member_count += 1
            if max_members > 0 and exported_member_count >= max_members:
                hit_member_export_limit = True
                logger.info("群 %s 成员已达上限 %d，停止拉取", title, max_members)
                break
            if download_avatars:
                await asyncio.sleep(MEMBER_FETCH_DELAY_SECONDS)
    except (ChatAdminRequiredError, UserNotParticipantError) as exc:
        logger.warning("无法拉取群成员 %s (%s): %s", title, group_id, exc)
    except Exception:
        logger.exception("拉取群成员失败: %s (%s)", title, group_id)

    includes_all_members = max_members <= 0 or not hit_member_export_limit
    group_row["includes_all_members"] = includes_all_members

    await asyncio.sleep(GROUP_FETCH_DELAY_SECONDS)
    return group_row, member_rows


CheckpointCallback = Callable[[int, int, Path], Awaitable[bool]]


async def export_account_groups(
    client: TelegramClient,
    config: dict[str, Any],
    *,
    checkpoint_every: int | None = None,
    on_checkpoint: CheckpointCallback | None = None,
    command_filter: Any | None = None,
) -> GroupExportData:
    """导出 groups_export.xlsx（groups / members 两个 sheet），可按批暂停确认。"""
    output_dir = Path(config.get("output_dir", "exports"))
    download_avatars = bool(config.get("download_avatars", True))
    max_members = int(config.get("max_members_per_group", 0) or 0)
    member_filter = parse_member_export_filter(config)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_root = output_dir / f"groups_{stamp}"
    avatar_dir = export_root / "avatars"
    excel_path = export_root / "groups_export.xlsx"
    exported_at = datetime.now().isoformat(timespec="seconds")

    group_rows: list[dict[str, Any]] = []
    member_rows: list[dict[str, Any]] = []
    checkpoint_files_sent = False
    cancelled = False

    selected = await _collect_export_dialogs(client, config, command_filter)
    total = len(selected)
    logger.info("群导出: 会话中共 %d 个群通过筛选", total)
    if command_filter is not None and total == 0:
        logger.warning("群导出: 命令行筛选未匹配任何群")

    processed = 0
    batch_size = checkpoint_every if checkpoint_every and checkpoint_every > 0 else None

    for dialog in selected:
        group_row, dialog_members = await _export_single_dialog(
            client,
            dialog,
            exported_at=exported_at,
            avatar_dir=avatar_dir,
            download_avatars=download_avatars,
            max_members=max_members,
            member_filter=member_filter,
        )
        group_rows.append(group_row)
        member_rows.extend(dialog_members)
        processed += 1

        need_checkpoint = batch_size and processed % batch_size == 0
        if need_checkpoint and on_checkpoint is not None:
            await asyncio.to_thread(
                _write_export_workbook,
                excel_path,
                group_rows,
                member_rows,
            )
            should_continue = await on_checkpoint(
                processed,
                total,
                excel_path,
            )
            checkpoint_files_sent = True
            if not should_continue:
                cancelled = True
                break

    if not cancelled and (not batch_size or processed % batch_size != 0):
        await asyncio.to_thread(
            _write_export_workbook,
            excel_path,
            group_rows,
            member_rows,
        )
    elif cancelled and not excel_path.is_file():
        await asyncio.to_thread(
            _write_export_workbook,
            excel_path,
            group_rows,
            member_rows,
        )

    return GroupExportData(
        excel_path=excel_path,
        group_rows=group_rows,
        member_rows=member_rows,
        cancelled=cancelled,
        checkpoint_files_sent=checkpoint_files_sent,
    )
