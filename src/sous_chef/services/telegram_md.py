"""LLM replies are Markdown; convert for Telegram (entities preferred over parse_mode strings)."""

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram.enums import ParseMode
from aiogram.types import Message, MessageEntity

logger = logging.getLogger(__name__)

_MAX_TG_TEXT = 4090


def _to_aiogram_entities(raw: list[Any]) -> list[MessageEntity]:  # telegramify MessageEntity
    out: list[MessageEntity] = []
    for e in raw:
        out.append(
            MessageEntity(
                type=e.type,
                offset=e.offset,
                length=e.length,
                url=getattr(e, "url", None),
                language=getattr(e, "language", None),
                custom_emoji_id=getattr(e, "custom_emoji_id", None),
            )
        )
    return out


def format_llm_reply_for_telegram(
    reply: str,
) -> tuple[str, str | None, list[MessageEntity] | None]:
    """
    Return (text, parse_mode, entities).

    - If entities is not None: send with ``entities=...`` and ``parse_mode=None`` so the bot's
      default HTML mode does not eat MarkdownV2 / show raw ``**``.
    - If entities is None: use parse_mode (MarkdownV2 or HTML fallback).
    """
    raw = reply or ""
    if not raw.strip():
        return " ", ParseMode.HTML.value, None

    text_in = raw.strip()
    if len(text_in) > _MAX_TG_TEXT:
        text_in = text_in[: _MAX_TG_TEXT - 1] + "…"

    # 1) Native Telegram entities — most reliable with DefaultBotProperties(parse_mode=HTML).
    try:
        from telegramify_markdown import convert

        plain, tf_entities = convert(text_in, latex_escape=True)
        if not plain.strip():
            raise ValueError("empty convert output")
        entities = _to_aiogram_entities(tf_entities)
        return plain, None, entities
    except Exception as e:
        logger.warning("telegram_md.convert failed: %s", e)

    # 2) MarkdownV2 string
    try:
        from telegramify_markdown import markdownify

        out = markdownify(text_in)
        if not out.strip():
            raise ValueError("empty markdownify output")
        if len(out) > _MAX_TG_TEXT:
            out = out[: _MAX_TG_TEXT - 1] + "…"
        return out, ParseMode.MARKDOWN_V2.value, None
    except Exception as e:
        logger.warning("telegram_md.markdownify failed: %s", e)

    safe = html.escape(text_in, quote=False)
    if len(safe) > _MAX_TG_TEXT:
        safe = safe[: _MAX_TG_TEXT - 1] + "…"
    return safe, ParseMode.HTML.value, None


async def send_llm_reply(message: Message, reply_text: str) -> None:
    """Send assistant reply with correct formatting (used by handlers)."""
    text, parse_mode, entities = format_llm_reply_for_telegram(reply_text)
    if entities is not None:
        await message.answer(
            text,
            entities=entities if entities else None,
            parse_mode=None,
        )
    else:
        await message.answer(text, parse_mode=parse_mode)
