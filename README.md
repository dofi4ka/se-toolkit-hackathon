# Sous-chef

**Telegram bot that finds recipes via AI-assisted web search ([ddgs](https://pypi.org/project/ddgs/)), verifies every link by scraping with [recipe-scrapers](https://github.com/hhursev/recipe-scrapers), then guides shopping and step-by-step cooking—with an optional **LLM assistant** in each phase.**

## Product context

- **End users:** home cooks using Telegram while shopping or cooking.
- **Problem:** jumping between browsers and broken recipe links is awkward during cooking.
- **Flow:** send a dish name → status updates while searching → pick a verified recipe → ingredient checklist (tap to check) → step-by-step cooking (`Next` / `Back`; last step has **Stop cooking**).

## Search pipeline

1. **Query expansion (optional):** if `LLM_API_KEY` and `LLM_MODEL` are set (and rate limit allows), the model returns **1–3 English search strings** from the user’s message (any language).
2. **Web search:** DuckDuckGo via `ddgs`; only URLs supported by `recipe-scrapers` (`scraper_exists_for`) are kept.
3. **Verification:** each candidate URL is **fully scraped** before it appears in the list. Failed pages are dropped; successful parses attach a cached **`recipe`** payload so picking a result does not need a second fetch when possible.
4. **Fallback:** if the LLM path yields nothing, the bot uses direct DDG collection, then the same scrape filter.

Users see **status messages** while work is in progress (e.g. searching, then “found N results, verifying…”).

## Assistant (LLM)

Configure **OpenAI-compatible** APIs (e.g. [OpenRouter](https://openrouter.ai/)) via `.env`:

| Variable | Purpose |
|----------|---------|
| `LLM_API_BASE_URL` | API base (default `https://openrouter.ai/api/v1`) |
| `LLM_API_KEY` | API token |
| `LLM_MODEL` | Model id |
| `LLM_MODEL_FALLBACK` | Optional backup model |
| `LLM_HTTP_REFERER` | Optional `HTTP-Referer` header (some providers) |
| `LLM_APP_TITLE` | Optional `X-Title` header |
| `LLM_MAX_REQUESTS_PER_USER_PER_HOUR` | Rate limit (Redis counter per Telegram user) |

Legacy **`OPENROUTER_*`** env names still work if `LLM_*` is unset.

**Modes (reply in chat when in that mode):**

- **Choosing a recipe:** compare options, ask questions; history is stored in Redis. **Tap a numbered button** to load a recipe—assistant history is cleared for the next phase.
- **Checklist:** substitutions, shopping, metric/imperial; full recipe in context. History clears when you tap **Start cooking**.
- **Cooking:** technique, timing, current step; full recipe + current step in context.

**Formatting:** assistant replies are rendered with **rich text** ([telegramify-markdown](https://pypi.org/project/telegramify-markdown/) → Telegram entities / MarkdownV2). While the model is thinking, **typing** is refreshed so the indicator does not disappear during long requests.

**Inline UI behavior:** after you **send a chat message** to the assistant, the **next** inline update (buttons) may **replace** the card with a new message so it stays below the conversation; after that, updates **edit** the same message until you chat again. (Session flag `user_sent_messages` in Redis.)

## Commands

| Command | Action |
|---------|--------|
| `/start` | Reset session and show the welcome text |
| `/withdraw` | Clear session (recipe, search, LLM history, flags) |
| `/debug` | Dump raw session JSON from Redis for this chat (key, TTL, pretty JSON; may split long output) |

## Stack

| Piece | Role |
|-------|------|
| **Telegram** | Client (aiogram 3); default `parse_mode` HTML for bot-built messages |
| **Backend** | One Python process, long-polling |
| **Redis** | Session JSON per chat (mode, query, candidates + scraped recipes, checklist, steps, LLM history, `user_sent_messages`), TTL |
| **openai** SDK | Chat Completions against `LLM_API_BASE_URL` |
| **telegramify-markdown** | LLM Markdown → Telegram text + entities |
| **ddgs** | Web search for recipe URLs |
| **recipe-scrapers** | Structured ingredients + steps; results filtered with `scraper_exists_for(url)` |

Search/scrape logs: **INFO** under `sous_chef.services.recipes`. Set `LOG_LEVEL=DEBUG` for more detail.

## Development (uv)

```bash
cp .env.example .env
# Set TELEGRAM_BOT_TOKEN and LLM_* if you want the assistant. Start Redis, e.g.:
# docker run -d -p 6379:6379 redis:7-alpine

uv sync
uv run python -m sous_chef.main
```

## Docker Compose

```bash
cp .env.example .env   # TELEGRAM_BOT_TOKEN required; add LLM_* for assistant
docker compose up -d --build
```

Compose sets `REDIS_URL` to the bundled `redis` service. The image uses **`uv sync --frozen`** — after changing `pyproject.toml`, run **`uv lock`** and rebuild so new dependencies (e.g. `telegramify-markdown`) are installed.

## Repository layout

- `src/sous_chef/` — application code  
- `pyproject.toml` / `uv.lock` — dependencies  
- `Dockerfile` / `docker-compose.yml`  

## License

MIT — see [LICENSE](LICENSE).
