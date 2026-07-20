from __future__ import annotations

from typing import Any

from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaContact,
    MessageMediaDice,
    MessageMediaDocument,
    MessageMediaGame,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaVenue,
    MessageMediaWebPage,
)


def _build_message_link(chat: Any, message_id: int) -> str | None:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"

    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return None

    chat_id_str = str(chat_id)
    if chat_id_str.startswith("-100"):
        internal_id = chat_id_str[4:]
    elif chat_id < 0:
        internal_id = str(abs(chat_id))
    else:
        return None

    return f"https://t.me/c/{internal_id}/{message_id}"


def _try_message_link(chat: Any, message: Any) -> str | None:
    message_id = getattr(message, "id", None)
    if chat is None or not message_id:
        return None
    try:
        return _build_message_link(chat, message_id)
    except Exception:
        return None


def _format_with_link(label: str, link: str | None, extra: str | None = None) -> str:
    detail = f"{label} {extra}".strip() if extra else label
    if link:
        return f"{detail} {link}"
    return detail


def _describe_document(document: Any, link: str | None) -> str:
    attrs = list(getattr(document, "attributes", None) or [])
    mime = getattr(document, "mime_type", "") or ""

    for attr in attrs:
        if isinstance(attr, DocumentAttributeSticker):
            alt = (getattr(attr, "alt", "") or "").strip()
            if alt:
                return alt
            return _format_with_link("[表情]", link)

    filename = None
    for attr in attrs:
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
            break

    for attr in attrs:
        if isinstance(attr, DocumentAttributeAnimated):
            return _format_with_link("[GIF]", link, filename)
        if isinstance(attr, DocumentAttributeVideo):
            if getattr(attr, "round_message", False):
                return _format_with_link("[视频消息]", link, filename)
            return _format_with_link("[视频]", link, filename)
        if isinstance(attr, DocumentAttributeAudio):
            if getattr(attr, "voice", False):
                return _format_with_link("[语音]", link, filename)
            return _format_with_link("[音频]", link, filename)

    if mime.startswith("image/"):
        return _format_with_link("[图片]", link, filename)
    if mime.startswith("video/"):
        return _format_with_link("[视频]", link, filename)
    if mime.startswith("audio/"):
        return _format_with_link("[音频]", link, filename)

    return _format_with_link("[文件]", link, filename)


def _describe_media(media: Any, link: str | None) -> str | None:
    if media is None:
        return None

    if isinstance(media, MessageMediaPhoto):
        return _format_with_link("[图片]", link)

    if isinstance(media, MessageMediaDocument):
        document = media.document
        if document:
            return _describe_document(document, link)
        return _format_with_link("[文件]", link)

    if isinstance(media, MessageMediaWebPage):
        webpage = media.webpage
        if webpage is None:
            return "[网页预览]"
        url = getattr(webpage, "url", None)
        title = getattr(webpage, "title", None)
        if url and title:
            return f"[链接] {title} {url}"
        if url:
            return f"[链接] {url}"
        if title:
            return f"[链接] {title}"
        return "[网页预览]"

    if isinstance(media, MessageMediaContact):
        first = getattr(media, "first_name", "") or ""
        last = getattr(media, "last_name", "") or ""
        phone = getattr(media, "phone_number", "") or ""
        name = f"{first} {last}".strip() or "联系人"
        return f"[联系人] {name} {phone}".strip()

    if isinstance(media, MessageMediaPoll):
        poll = media.poll
        question = getattr(poll, "question", None) if poll else None
        if question:
            return f"[投票] {question}"
        return "[投票]"

    if isinstance(media, MessageMediaDice):
        emoji = getattr(media, "emoticon", "") or "🎲"
        value = getattr(media, "value", None)
        if value is not None:
            return f"{emoji}({value})"
        return emoji

    if isinstance(media, MessageMediaGame):
        game = media.game
        title = getattr(game, "title", None) if game else None
        return f"[游戏] {title}" if title else "[游戏]"

    if isinstance(media, MessageMediaGeo):
        return "[位置]"

    if isinstance(media, MessageMediaVenue):
        title = getattr(media, "title", None)
        return f"[地点] {title}" if title else "[地点]"

    type_name = media.__class__.__name__.removeprefix("MessageMedia")
    return f"[{type_name or '媒体'}]"


def format_telegram_message(message: Any, chat: Any = None) -> str:
    text = (getattr(message, "message", None) or getattr(message, "text", None) or "").strip()
    media = getattr(message, "media", None)
    link = _try_message_link(chat, message) if media is not None else None
    media_desc = _describe_media(media, link)

    if text and media_desc and isinstance(media, MessageMediaWebPage):
        webpage = media.webpage
        url = getattr(webpage, "url", None) if webpage else None
        if url and url in text:
            media_desc = None

    parts: list[str] = []
    if text:
        parts.append(text)
    if media_desc:
        parts.append(media_desc)

    if parts:
        return " ".join(parts)
    return "[空消息]"
