import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _opt(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _opt_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    redis_url: str
    log_level: str
    state_ttl_seconds: int = 86400 * 2
    # OpenAI-compatible Chat Completions (OpenRouter, OpenAI, etc.)
    llm_api_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_model_fallback: str = ""
    llm_http_referer: str = ""
    llm_app_title: str = "Sous-chef"
    llm_max_requests_per_user_per_hour: int = 20


def load_settings() -> Settings:
    token = _opt("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    # Prefer LLM_*; fall back to OPENROUTER_* for backward compatibility.
    llm_key = _opt("LLM_API_KEY") or _opt("OPENROUTER_API_KEY")
    llm_base = _opt("LLM_API_BASE_URL") or _opt("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    llm_model = _opt("LLM_MODEL") or _opt("OPENROUTER_MODEL")
    llm_fb = _opt("LLM_MODEL_FALLBACK") or _opt("OPENROUTER_MODEL_CHEAP")
    referer = _opt("LLM_HTTP_REFERER") or _opt("OPENROUTER_HTTP_REFERER", "https://github.com/")
    title = _opt("LLM_APP_TITLE") or _opt("OPENROUTER_APP_TITLE", "Sous-chef")

    return Settings(
        telegram_bot_token=token,
        redis_url=_opt("REDIS_URL", "redis://localhost:6379/0"),
        log_level=_opt("LOG_LEVEL", "INFO").upper() or "INFO",
        llm_api_base_url=llm_base,
        llm_api_key=llm_key,
        llm_model=llm_model,
        llm_model_fallback=llm_fb,
        llm_http_referer=referer,
        llm_app_title=title,
        llm_max_requests_per_user_per_hour=_opt_int("LLM_MAX_REQUESTS_PER_USER_PER_HOUR", 20),
    )
