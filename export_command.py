"""群导出命令参数解析。"""

from __future__ import annotations

from dataclasses import dataclass

from admin_commands import normalize_command
from group_export import chat_id_variants

BATCH_SIZE_UPPER_BOUND = 1000


@dataclass(frozen=True)
class ExportCommandFilter:
    """命令行指定的群筛选（名称模糊或群 ID 精确）。"""

    name_keyword: str | None = None
    group_id_variants: frozenset[str] | None = None


@dataclass(frozen=True)
class ExportGroupsCommandArgs:
    batch_size: int | None = None
    command_filter: ExportCommandFilter | None = None


def _export_command_parts(text: str) -> list[str]:
    command = normalize_command(text)
    if command.startswith("/export_groups"):
        parts = command.split()
    elif command.startswith("/export groups"):
        remainder = command[len("/export groups") :].strip()
        parts = ["/export_groups", *remainder.split()] if remainder else ["/export_groups"]
    else:
        return []
    return parts


def _parse_signed_int(value: str) -> int | None:
    text = value.strip()
    if not text.lstrip("-").isdigit():
        return None
    return int(text)


def parse_export_groups_args(text: str | None) -> ExportGroupsCommandArgs:
    if not text:
        return ExportGroupsCommandArgs()

    parts = _export_command_parts(text)
    if len(parts) < 2:
        return ExportGroupsCommandArgs()

    arg = parts[1]
    numeric = _parse_signed_int(arg)
    if numeric is not None:
        if 0 < numeric < BATCH_SIZE_UPPER_BOUND:
            return ExportGroupsCommandArgs(batch_size=numeric)
        if numeric >= BATCH_SIZE_UPPER_BOUND or numeric < 0:
            variants = frozenset(chat_id_variants(numeric))
            return ExportGroupsCommandArgs(
                command_filter=ExportCommandFilter(group_id_variants=variants),
            )
        return ExportGroupsCommandArgs()

    keyword = arg.strip().lstrip("@")
    if not keyword:
        return ExportGroupsCommandArgs()
    return ExportGroupsCommandArgs(
        command_filter=ExportCommandFilter(name_keyword=keyword),
    )


def resolve_export_batch_size(
    command_text: str | None,
    config: dict,
) -> int | None:
    args = parse_export_groups_args(command_text)
    if args.batch_size is not None:
        return args.batch_size

    configured = int(config.get("groups_per_checkpoint") or 0)
    return configured if configured > 0 else None


def export_filter_summary(command_filter: ExportCommandFilter | None) -> str:
    if command_filter is None:
        return ""
    if command_filter.group_id_variants:
        sample = next(iter(command_filter.group_id_variants))
        return f"群 ID 精确匹配: {sample}"
    if command_filter.name_keyword:
        return f"群名称模糊匹配: %{command_filter.name_keyword}%"
    return ""
