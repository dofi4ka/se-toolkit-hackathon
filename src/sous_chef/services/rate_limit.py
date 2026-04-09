"""Per-user rate limiting using Redis INCR + TTL."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

RL_KEY = "sous_chef:llm_rl:{user_id}"
WINDOW_SECONDS = 3600


async def allow_llm_request(redis: Redis, user_id: int, max_per_window: int) -> bool:
    """
    Fixed window: at most max_per_window LLM calls per user per hour.
    Returns True if allowed (counter incremented), False if over limit.
    """
    if max_per_window <= 0:
        return False
    key = RL_KEY.format(user_id=user_id)
    n = await redis.incr(key)
    if n == 1:
        await redis.expire(key, WINDOW_SECONDS)
    if n > max_per_window:
        logger.warning("rate_limit: user_id=%s blocked at count=%s max=%s", user_id, n, max_per_window)
        await redis.decr(key)
        return False
    logger.info("rate_limit: user_id=%s count=%s/%s", user_id, n, max_per_window)
    return True
