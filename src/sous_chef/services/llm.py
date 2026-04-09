"""OpenAI-compatible chat completions (OpenRouter, OpenAI, etc.)."""

from __future__ import annotations

import logging
from typing import Any

from sous_chef.config import Settings

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 3500
# Cap stored + sent history size (pairs of user/assistant).
MAX_HISTORY_MESSAGES = 40


def cap_llm_messages(history: list[dict[str, str]]) -> list[dict[str, str]]:
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    return history[-MAX_HISTORY_MESSAGES:]


async def complete_chat(
    settings: Settings,
    *,
    system: str,
    history: list[dict[str, str]],
    user_message: str,
) -> str:
    if not settings.llm_api_key:
        return "Assistant is not configured (set LLM_API_KEY or OPENROUTER_API_KEY in .env)."

    models = [m for m in (settings.llm_model, settings.llm_model_fallback) if m]
    if not models:
        return "Assistant is not configured (set LLM_MODEL in .env)."

    from openai import AsyncOpenAI

    hist = cap_llm_messages([m for m in history if m.get("role") in ("user", "assistant")])
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(hist)
    messages.append({"role": "user", "content": user_message})

    default_headers: dict[str, str] = {}
    if settings.llm_http_referer:
        default_headers["HTTP-Referer"] = settings.llm_http_referer
    if settings.llm_app_title:
        default_headers["X-Title"] = settings.llm_app_title

    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_api_base_url,
        default_headers=default_headers or None,
    )

    last_err: str | None = None
    for model in models:
        try:
            logger.info("llm.complete_chat model=%r", model)
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=900,
                temperature=0.4,
            )
            choice = resp.choices[0] if resp.choices else None
            text = (choice.message.content or "").strip() if choice else ""
            if not text:
                last_err = "empty response"
                continue
            if len(text) > MAX_REPLY_CHARS:
                text = text[: MAX_REPLY_CHARS - 1] + "…"
            return text
        except Exception as e:
            last_err = str(e)
            logger.warning("llm.complete_chat model %r failed: %s", model, e, exc_info=True)

    return f"Could not get a reply. ({last_err or 'unknown error'})"
