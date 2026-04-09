"""Redis-backed session state."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class Mode(str, Enum):
    IDLE = "IDLE"
    CHOOSING = "CHOOSING"
    CHECKLIST = "CHECKLIST"
    COOKING = "COOKING"


@dataclass
class SessionState:
    mode: str = Mode.IDLE.value
    query: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    recipe: dict[str, Any] | None = None
    checked: list[int] = field(default_factory=list)
    step_index: int = 0
    # Cooking: optional AI-rewritten text per step index (JSON keys are strings).
    step_ai_rewrite: dict[str, str] = field(default_factory=dict)
    # Per step index (string key): when True, that step's card shows AI text if cached.
    cooking_show_ai_step: dict[str, bool] = field(default_factory=dict)
    # True only until the next inline UI update: set after an LLM reply, cleared after any
    # recipe/checklist/cooking button refresh (including ingredient toggles).
    user_sent_messages: bool = False
    # OpenAI-style history (user/assistant only); system is rebuilt each request.
    llm_messages: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> SessionState:
        data = json.loads(raw)
        raw_hist = data.get("llm_messages") or []
        llm_messages: list[dict[str, str]] = []
        for m in raw_hist:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and "content" in m:
                llm_messages.append(
                    {"role": str(m["role"]), "content": str(m["content"])[:12000]}
                )
        raw_ai = data.get("step_ai_rewrite") or {}
        step_ai_rewrite: dict[str, str] = {}
        if isinstance(raw_ai, dict):
            for k, v in raw_ai.items():
                if v is not None:
                    step_ai_rewrite[str(k)] = str(v)[:12000]

        raw_show = data.get("cooking_show_ai_step")
        cooking_show_ai_step: dict[str, bool] = {}
        if isinstance(raw_show, dict):
            for k, v in raw_show.items():
                cooking_show_ai_step[str(k)] = bool(v)
        # Legacy: single global bool (pre per-step); cannot recover per-step prefs.
        elif raw_show is True:
            cooking_show_ai_step = {}

        return cls(
            mode=data.get("mode", Mode.IDLE.value),
            query=data.get("query", ""),
            candidates=data.get("candidates") or [],
            recipe=data.get("recipe"),
            checked=data.get("checked") or [],
            step_index=int(data.get("step_index", 0)),
            step_ai_rewrite=step_ai_rewrite,
            cooking_show_ai_step=cooking_show_ai_step,
            user_sent_messages=bool(data.get("user_sent_messages")),
            llm_messages=llm_messages,
        )


def redis_key(chat_id: int) -> str:
    return f"sous_chef:v1:{chat_id}"


async def get_state(r: Redis, chat_id: int, ttl: int) -> SessionState:
    raw = await r.get(redis_key(chat_id))
    if not raw:
        return SessionState()
    try:
        return SessionState.from_json(raw.decode() if isinstance(raw, bytes) else raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Bad session JSON for chat %s, resetting", chat_id)
        return SessionState()


async def save_state(r: Redis, chat_id: int, state: SessionState, ttl: int) -> None:
    await r.setex(redis_key(chat_id), ttl, state.to_json())
