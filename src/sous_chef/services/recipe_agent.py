"""
Recipe search: LLM turns the user message into up to 3 English DDG queries, then DDG + recipe-scrapers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from recipe_scrapers import scraper_exists_for

from sous_chef.config import Settings

logger = logging.getLogger(__name__)

DDG_HITS_PER_CALL = 22
# Merge more URLs than we show, then keep only scrape-verified links.
MAX_MERGED_URLS = 60

QUERY_REVIEW_SYSTEM = """You expand a user's cooking request into web search queries.

Rules:
- Input may be any language; output search queries must be English only.
- Return exactly one JSON object, no markdown, no prose: {"queries":["..."]}
- Provide 1 to 3 distinct queries. Vary wording (e.g. dish + recipe, regional name, site:allrecipes only if helpful). Keep each short.
- Queries are for DuckDuckGo; avoid redundant near-duplicates.
- Do not include explanations outside the JSON."""


def _parse_queries_json(raw: str) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return []
    if "```" in s:
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.MULTILINE)
        s = re.sub(r"\s*```\s*$", "", s, flags=re.MULTILINE)
        s = s.strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    qs = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(qs, list):
        return []
    out: list[str] = []
    for x in qs[:3]:
        q = str(x).strip()
        if q and q not in out:
            out.append(q)
    return out


def _ddg_search_sync(query: str) -> list[dict[str, str]]:
    """Run DDG text search; keep only http(s) URLs supported by recipe-scrapers."""
    try:
        from ddgs import DDGS
    except ImportError:
        logger.exception("recipe_agent: ddgs missing")
        return []

    q = (query or "").strip()
    if not q:
        return []

    out: list[dict[str, str]] = []
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(q, max_results=DDG_HITS_PER_CALL))
    except Exception as e:
        logger.warning("recipe_agent.search: ddgs failed q=%r err=%s", q, e)
        return []

    for r in hits:
        href = (r.get("href") or r.get("url") or "").strip()
        if not href.startswith("http"):
            continue
        if not scraper_exists_for(href):
            continue
        title = (r.get("title") or "").strip()
        snippet = (r.get("body") or r.get("description") or "")[:300]
        out.append(
            {
                "url": href,
                "title": title[:200],
                "source": snippet[:200],
            }
        )
    logger.info("recipe_agent.search: q=%r hits=%s kept=%s", q, len(hits), len(out))
    return out


def _merge_hits(rounds: list[list[dict[str, str]]], max_n: int) -> list[dict[str, str]]:
    """Dedupe by URL; order = first query's hits first, then next query, etc."""
    seen_url: set[str] = set()
    out: list[dict[str, str]] = []
    for batch in rounds:
        for h in batch:
            u = (h.get("url") or "").strip()
            if not u or u in seen_url:
                continue
            seen_url.add(u)
            out.append(h)
            if len(out) >= max_n:
                return out
    return out


async def fetch_ddg_candidates_via_llm(settings: Settings, user_query: str) -> list[dict[str, str]]:
    """
    LLM expands the request into up to 3 English queries, runs DDG, merges unique URLs
    (scraper-supported only). Does not scrape — caller runs filter_candidates_by_scrape.
    """
    if not settings.llm_api_key or not settings.llm_model:
        return []

    from openai import AsyncOpenAI

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

    models = [m for m in (settings.llm_model, settings.llm_model_fallback) if m]
    if not models:
        return []

    raw_reply: str | None = None
    last_err: Exception | None = None
    for model in models:
        try:
            logger.info("recipe_agent.query_review model=%r", model)
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": QUERY_REVIEW_SYSTEM},
                    {"role": "user", "content": user_query.strip()},
                ],
                max_tokens=400,
                temperature=0.2,
            )
            choice = resp.choices[0] if resp.choices else None
            raw_reply = (choice.message.content or "").strip() if choice else ""
            if raw_reply:
                break
        except Exception as e:
            last_err = e
            logger.warning("recipe_agent query_review model %r failed: %s", model, e)

    if not raw_reply:
        logger.error("recipe_agent: no LLM reply %s", last_err)
        return []

    queries = _parse_queries_json(raw_reply)
    if not queries:
        logger.warning("recipe_agent: could not parse queries from %r", raw_reply[:200])
        return []

    logger.info("recipe_agent: queries=%s", queries)

    async def run_one(q: str) -> list[dict[str, str]]:
        return await asyncio.to_thread(_ddg_search_sync, q)

    rounds = await asyncio.gather(*[run_one(q) for q in queries])
    merged = _merge_hits(list(rounds), MAX_MERGED_URLS)
    logger.info("recipe_agent: merged count=%s (before scrape filter)", len(merged))
    return merged
