"""Telegram handlers."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from contextlib import suppress
from typing import Any, TypeVar
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sous_chef.config import Settings
from sous_chef.services.llm import cap_llm_messages, complete_chat
from sous_chef.services.llm_prompts import system_checklist, system_choosing, system_cooking
from sous_chef.services.rate_limit import allow_llm_request
from sous_chef.services.recipe_agent import fetch_ddg_candidates_via_llm
from sous_chef.services.recipes import (
    filter_candidates_by_scrape,
    scrape_recipe_from_url,
    search_recipe_candidates,
)
from sous_chef.services.telegram_md import send_llm_reply
from sous_chef.state import Mode, SessionState, get_state, redis_key, save_state

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def _await_with_typing_keepalive(
    bot: Any,
    chat_id: int,
    awaitable: Any,
) -> _T:
    """Telegram clears the typing indicator after ~5s; refresh while waiting for slow LLM."""

    async def _pump() -> None:
        try:
            while True:
                await asyncio.sleep(4.0)
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except asyncio.CancelledError:
            return

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    task = asyncio.create_task(_pump())
    try:
        return await awaitable
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _h(s: str) -> str:
    return html.escape(s or "", quote=False)


def reset_to_idle() -> SessionState:
    return SessionState()


def _search_preview_line(index: int, c: dict[str, Any]) -> str:
    """Clickable title; optional LLM description or host label."""
    url = (c.get("url") or "").strip()
    title = (c.get("title") or "").strip() or url
    desc = (c.get("description") or c.get("source") or "").strip()
    safe_href = html.escape(url, quote=True)
    line = f'{index}. <a href="{safe_href}">{_h(title)}</a>'
    if desc:
        line += f"\n   <i>{_h(desc)}</i>"
    else:
        host = urlparse(url).netloc
        if host:
            line += f" — <i>{_h(host)}</i>"
    return line


router = Router(name="bot")

_settings: Settings | None = None
_redis: Any = None


def configure_handlers(settings: Settings, redis_client: Any) -> None:
    global _settings, _redis
    _settings = settings
    _redis = redis_client


def _r():
    assert _redis is not None
    return _redis


def _s():
    assert _settings is not None
    return _settings


async def _load(chat_id: int) -> SessionState:
    return await get_state(_r(), chat_id, _s().state_ttl_seconds)


async def _persist(chat_id: int, state: SessionState) -> None:
    await save_state(_r(), chat_id, state, _s().state_ttl_seconds)


async def _persist_after_interactive_update(chat_id: int, state: SessionState) -> None:
    """Next delete+send only right after an LLM chat turn; reset after any inline UI change."""
    state.user_sent_messages = False
    await _persist(chat_id, state)


async def _delete_callback_message_and_send(
    query: CallbackQuery,
    *,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Remove the message that carried the inline keyboard, send a fresh one."""
    bot = query.bot
    chat_id = query.message.chat.id
    try:
        await query.message.delete()
    except Exception:
        pass
    await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)


async def _sync_interactive_message(
    query: CallbackQuery,
    *,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: InlineKeyboardMarkup | None = None,
    prefer_new_message: bool,
) -> None:
    """
    If user already chatted with the LLM, delete + send so the card stays below the thread.
    Otherwise edit in place (keeps the interactive message at the top when there is no chat).
    """
    if prefer_new_message:
        await _delete_callback_message_and_send(
            query,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    else:
        await query.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def _update_search_status_message(message: Message, status_msg: Message, text: str) -> Message:
    """Edit status text; if edit fails (e.g. message deleted), send a new status message."""
    try:
        await status_msg.edit_text(text)
        return status_msg
    except Exception:
        try:
            await status_msg.delete()
        except Exception:
            pass
        return await message.answer(text)


@router.message(CommandStart())
@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.chat is None:
        return
    chat_id = message.chat.id
    await _persist(chat_id, reset_to_idle())
    await message.answer(
        "<b>Sous-chef</b> — AI-powered recipe search & cooking companion.\n\n"
        "• <b>Smart search</b> — describe a dish in any language; we rewrite queries with AI, "
        "search the web, and only offer links that load as real recipes.\n"
        "• <b>Full flow</b> — pick one → interactive shopping list → guided cooking steps.\n"
        "• <b>Assistant</b> — chat anytime (compare options, substitutions, technique).\n\n"
        "<i>Send a dish name to begin. Use /withdraw to clear the session.</i>",
        parse_mode="HTML",
    )


@router.message(Command("withdraw"))
async def cmd_stop(message: Message) -> None:
    if message.chat is None:
        return
    chat_id = message.chat.id
    await _persist(chat_id, reset_to_idle())
    await message.answer(
        "Session cleared. Send a dish name to search again.",
        parse_mode="HTML",
    )


_DEBUG_PRE_MAX = 3500


@router.message(Command("debug"))
async def cmd_debug(message: Message) -> None:
    """Dump raw session JSON from Redis for this chat (private: chat_id = user)."""
    if message.chat is None:
        return
    chat_id = message.chat.id
    r = _r()
    key = redis_key(chat_id)
    raw = await r.get(key)
    if not raw:
        await message.answer(
            f"No session in Redis.\n\nKey: <code>{_h(key)}</code>",
            parse_mode="HTML",
        )
        return

    s = raw.decode() if isinstance(raw, bytes) else raw
    try:
        data = json.loads(s)
        pretty = json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        pretty = s

    try:
        ttl_sec = await r.ttl(key)
    except Exception:
        ttl_sec = -2
    ttl_line = ""
    if ttl_sec is not None and ttl_sec >= 0:
        ttl_line = f"\nTTL: {ttl_sec}s"

    header = f"<b>Redis key</b> <code>{_h(key)}</code>{ttl_line}\n\n"
    escaped = _h(pretty)
    if len(escaped) <= _DEBUG_PRE_MAX:
        await message.answer(header + f"<pre>{escaped}</pre>", parse_mode="HTML")
        return

    total = (len(escaped) + _DEBUG_PRE_MAX - 1) // _DEBUG_PRE_MAX
    for part_i in range(total):
        chunk = escaped[part_i * _DEBUG_PRE_MAX : (part_i + 1) * _DEBUG_PRE_MAX]
        h = (
            f"<b>Redis key</b> <code>{_h(key)}</code>{ttl_line} "
            f"<i>(part {part_i + 1}/{total})</i>\n\n"
        )
        await message.answer(h + f"<pre>{chunk}</pre>", parse_mode="HTML")


def _recipe_keyboard(candidates: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for i, c in enumerate(candidates[:5]):
        label = (c.get("title") or c.get("url", ""))[:60]
        rows.append([InlineKeyboardButton(text=f"{i + 1}. {label}", callback_data=f"recipe:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _checklist_keyboard(ingredients: list[str], checked: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for i, ing in enumerate(ingredients):
        mark = "✅" if i in checked else "⚫️"
        text = f"{mark} {ing[:45]}"[:64]
        rows.append([InlineKeyboardButton(text=text, callback_data=f"ing:{i}")])
    rows.append([InlineKeyboardButton(text="Start cooking", callback_data="cook:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cooking_keyboard(step_index: int, total: int) -> InlineKeyboardMarkup:
    """Last step: Back + Stop. Earlier steps: Back + Next."""
    is_last = total > 0 and step_index >= total - 1
    if is_last:
        row = [
            InlineKeyboardButton(text="◀ Back", callback_data="cook:back"),
            InlineKeyboardButton(text="Stop cooking", callback_data="cook:stop"),
        ]
    else:
        row = [
            InlineKeyboardButton(text="◀ Back", callback_data="cook:back"),
            InlineKeyboardButton(text="Next ▶", callback_data="cook:next"),
        ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def _handle_llm_turn(
    message: Message,
    *,
    chat_id: int,
    user_id: int,
    state: SessionState,
    system: str,
) -> None:
    text = (message.text or "").strip()
    if not text:
        return

    settings = _s()
    if not settings.llm_api_key or not settings.llm_model:
        await message.answer(
            "Assistant is not configured. Set LLM_API_KEY and LLM_MODEL (or OPENROUTER_*) in .env.",
        )
        return

    if not await allow_llm_request(
        _r(),
        user_id,
        settings.llm_max_requests_per_user_per_hour,
    ):
        await message.answer(
            "Too many assistant requests this hour. Try again later or use /stop and continue later.",
        )
        return

    reply = await _await_with_typing_keepalive(
        message.bot,
        message.chat.id,
        complete_chat(
            settings,
            system=system,
            history=state.llm_messages,
            user_message=text,
        ),
    )
    state.user_sent_messages = True
    state.llm_messages.append({"role": "user", "content": text})
    state.llm_messages.append({"role": "assistant", "content": reply})
    state.llm_messages = cap_llm_messages(state.llm_messages)
    await _persist(chat_id, state)
    await send_llm_reply(message, reply)


@router.message(F.text)
async def on_text(message: Message) -> None:
    if message.chat is None or not message.text or message.text.startswith("/"):
        return
    if message.from_user is None:
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = message.text.strip()
    if not text:
        return

    state = await _load(chat_id)

    if state.mode == Mode.CHOOSING.value and state.candidates:
        logger.info("handler.llm_choosing: chat_id=%s text=%r", chat_id, text)
        sys = system_choosing(query=state.query, candidates=state.candidates)
        await _handle_llm_turn(message, chat_id=chat_id, user_id=user_id, state=state, system=sys)
        return

    if state.mode == Mode.CHECKLIST.value and state.recipe:
        logger.info("handler.llm_checklist: chat_id=%s text=%r", chat_id, text)
        sys = system_checklist(recipe=state.recipe)
        await _handle_llm_turn(message, chat_id=chat_id, user_id=user_id, state=state, system=sys)
        return

    if state.mode == Mode.COOKING.value and state.recipe:
        logger.info("handler.llm_cooking: chat_id=%s text=%r", chat_id, text)
        sys = system_cooking(recipe=state.recipe, step_index=state.step_index)
        await _handle_llm_turn(message, chat_id=chat_id, user_id=user_id, state=state, system=sys)
        return

    # IDLE: LLM expands query to 1–2 English searches, then DDG (+ scraper filter); else DDG-only
    logger.info("handler.search: chat_id=%s text=%r", chat_id, text)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    status_msg = await message.answer("Starting search…")

    settings = _s()
    raw: list[dict[str, Any]] = []
    if settings.llm_api_key and settings.llm_model:
        if await allow_llm_request(
            _r(),
            user_id,
            settings.llm_max_requests_per_user_per_hour,
        ):
            status_msg = await _update_search_status_message(
                message,
                status_msg,
                "Expanding search with AI and querying the web…",
            )
            raw = await fetch_ddg_candidates_via_llm(settings, text)
        else:
            logger.info("handler.search: rate limit hit, falling back to DDG-only")
    if not raw:
        status_msg = await _update_search_status_message(
            message,
            status_msg,
            "Searching the web (no AI query expansion)…",
        )
        raw = search_recipe_candidates(text, max_collect=30)
    if not raw:
        logger.warning("handler.search: no raw URLs for chat_id=%s query=%r", chat_id, text)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer(
            "No recipes found. Try another query or a simpler dish name.",
        )
        return

    status_msg = await _update_search_status_message(
        message,
        status_msg,
        f"Found {len(raw)} links. Opening each page to verify…",
    )

    async def _scrape_progress(attempt: int, total: int, _url: str) -> None:
        nonlocal status_msg
        status_msg = await _update_search_status_message(
            message,
            status_msg,
            f"Verifying recipe pages… ({attempt}/{total})",
        )

    candidates = await filter_candidates_by_scrape(
        raw,
        max_keep=5,
        on_progress=_scrape_progress,
    )
    if not candidates:
        logger.warning("handler.search: no candidates for chat_id=%s query=%r", chat_id, text)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer(
            "No recipes found. Try another query or a simpler dish name.",
        )
        return

    try:
        await status_msg.delete()
    except Exception:
        pass

    state.mode = Mode.CHOOSING.value
    state.query = text
    state.candidates = candidates
    state.recipe = None
    state.checked = []
    state.step_index = 0
    state.user_sent_messages = False
    state.llm_messages = []
    await _persist(chat_id, state)

    lines = [
        f"Results for <b>{_h(text)}</b> — pick one (links open in browser):",
        "",
        "<i>Reply in chat to discuss which option fits you, or tap a number below.</i>",
        "",
    ]
    for i, c in enumerate(candidates, start=1):
        lines.append(_search_preview_line(i, c))
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_recipe_keyboard(candidates),
    )


@router.callback_query(F.data.startswith("recipe:"))
async def cb_recipe(query: CallbackQuery) -> None:
    if query.message is None or query.from_user is None:
        await query.answer()
        return
    chat_id = query.message.chat.id
    try:
        idx = int((query.data or "").split(":")[1])
    except (IndexError, ValueError):
        await query.answer("Invalid")
        return

    state = await _load(chat_id)
    if state.mode != Mode.CHOOSING.value or not state.candidates:
        await query.answer("Session expired — send a search again.", show_alert=True)
        return
    if idx < 0 or idx >= len(state.candidates):
        await query.answer("Invalid")
        return

    cand = state.candidates[idx]
    url = cand["url"]
    logger.info("handler.pick_recipe: chat_id=%s idx=%s url=%r", chat_id, idx, url[:200])

    await query.answer("Loading recipe…")
    await query.message.bot.send_chat_action(query.message.chat.id, ChatAction.TYPING)

    recipe = cand.get("recipe") or scrape_recipe_from_url(url)
    if not recipe:
        await query.message.answer(
            "Could not load this recipe. Pick another option or search again.",
        )
        return

    state.mode = Mode.CHECKLIST.value
    state.recipe = recipe
    state.checked = []
    state.step_index = 0
    state.llm_messages = []
    await _persist(chat_id, state)

    ings = recipe.get("ingredients") or []
    title = recipe.get("title", "Recipe")
    body = (
        f"<b>{_h(title)}</b>\n\nIngredients — tap to toggle.\n\n"
        f"<i>Reply in chat for ingredient tips or substitutions.</i>"
    )
    await _sync_interactive_message(
        query,
        text=body,
        parse_mode="HTML",
        reply_markup=_checklist_keyboard(ings, set()),
        prefer_new_message=state.user_sent_messages,
    )
    await _persist_after_interactive_update(chat_id, state)


@router.callback_query(F.data.startswith("ing:"))
async def cb_ingredient(query: CallbackQuery) -> None:
    if query.message is None or query.from_user is None:
        await query.answer()
        return
    chat_id = query.message.chat.id
    try:
        idx = int((query.data or "").split(":")[1])
    except (IndexError, ValueError):
        await query.answer("Invalid")
        return

    state = await _load(chat_id)
    if state.mode != Mode.CHECKLIST.value or not state.recipe:
        await query.answer("Not in checklist mode.", show_alert=True)
        return
    ings = state.recipe.get("ingredients") or []
    if idx < 0 or idx >= len(ings):
        await query.answer("Invalid")
        return

    checked = set(state.checked)
    if idx in checked:
        checked.discard(idx)
    else:
        checked.add(idx)
    state.checked = sorted(checked)
    await _persist(chat_id, state)

    title = state.recipe.get("title", "Recipe")
    body = (
        f"<b>{_h(title)}</b>\n\nIngredients — tap to toggle.\n\n"
        f"<i>Reply in chat for ingredient tips or substitutions.</i>"
    )
    await _sync_interactive_message(
        query,
        text=body,
        parse_mode="HTML",
        reply_markup=_checklist_keyboard(ings, set(state.checked)),
        prefer_new_message=state.user_sent_messages,
    )
    await _persist_after_interactive_update(chat_id, state)
    await query.answer()


@router.callback_query(F.data == "cook:start")
async def cb_cook_start(query: CallbackQuery) -> None:
    if query.message is None or query.from_user is None:
        await query.answer()
        return
    chat_id = query.message.chat.id
    state = await _load(chat_id)
    if state.mode != Mode.CHECKLIST.value or not state.recipe:
        await query.answer("Start from checklist first.", show_alert=True)
        return
    steps = state.recipe.get("steps") or []
    if not steps:
        await query.answer("No steps in recipe.", show_alert=True)
        return

    state.mode = Mode.COOKING.value
    state.step_index = 0
    state.llm_messages = []
    await _persist(chat_id, state)

    total = len(steps)
    step_text = steps[0]
    title = state.recipe.get("title", "Recipe")
    body = (
        f"<b>{_h(title)}</b> — step 1/{total}\n\n{_h(step_text)}\n\n"
        f"<i>Reply in chat for cooking help.</i>"
    )
    await _sync_interactive_message(
        query,
        text=body,
        parse_mode="HTML",
        reply_markup=_cooking_keyboard(0, total),
        prefer_new_message=state.user_sent_messages,
    )
    await _persist_after_interactive_update(chat_id, state)
    await query.answer()


@router.callback_query(F.data == "cook:stop")
async def cb_cook_stop(query: CallbackQuery) -> None:
    if query.message is None:
        await query.answer()
        return
    chat_id = query.message.chat.id
    state = await _load(chat_id)
    if state.mode != Mode.COOKING.value:
        await query.answer("Not in cooking mode.", show_alert=True)
        return
    prefer_new = state.user_sent_messages
    await _persist(chat_id, reset_to_idle())
    no_keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    await _sync_interactive_message(
        query,
        text="Stopped. Session cleared. Send a dish name to search again.",
        parse_mode="HTML",
        reply_markup=no_keyboard,
        prefer_new_message=prefer_new,
    )
    await query.answer()


@router.callback_query(F.data.in_({"cook:next", "cook:back"}))
async def cb_cook_nav(query: CallbackQuery) -> None:
    if query.message is None or query.from_user is None:
        await query.answer()
        return
    chat_id = query.message.chat.id
    action = (query.data or "").split(":", 1)[1]

    state = await _load(chat_id)
    if state.mode != Mode.COOKING.value or not state.recipe:
        await query.answer("Not in cooking mode.", show_alert=True)
        return
    steps = state.recipe.get("steps") or []
    total = len(steps)
    if total == 0:
        await query.answer("No steps.", show_alert=True)
        return

    i = state.step_index
    if action == "next":
        if i + 1 < total:
            state.step_index = i + 1
        else:
            await query.answer("Last step.")
            return
    elif action == "back":
        if i > 0:
            state.step_index = i - 1
        else:
            await query.answer("First step.")
            return
    else:
        await query.answer()
        return

    await _persist(chat_id, state)
    i = state.step_index
    step_text = steps[i]
    title = state.recipe.get("title", "Recipe")
    body = (
        f"<b>{_h(title)}</b> — step {i + 1}/{total}\n\n{_h(step_text)}\n\n"
        f"<i>Reply in chat for cooking help.</i>"
    )
    await _sync_interactive_message(
        query,
        text=body,
        parse_mode="HTML",
        reply_markup=_cooking_keyboard(i, total),
        prefer_new_message=state.user_sent_messages,
    )
    await _persist_after_interactive_update(chat_id, state)
    await query.answer()
