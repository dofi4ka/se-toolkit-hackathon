"""Recipe discovery (ddgs) + parsing (recipe-scrapers), with step-by-step logging."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from recipe_scrapers import scrape_html, scrape_me, scraper_exists_for

logger = logging.getLogger(__name__)


def _title_from_url(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"[-_]+", " ", slug)
    return slug[:80] if slug else url[:60]


def search_recipe_candidates(query: str, max_collect: int = 45) -> list[dict[str, str]]:
    """
    Collect up to max_collect {url, title, source} from DDG (scraper-supported URLs only).
    Call filter_candidates_by_scrape() to keep only URLs that scrape successfully.
    """
    raw_q = (query or "").strip()
    logger.info("search.step1_input: raw_query=%r max_collect=%s", raw_q, max_collect)

    if not raw_q:
        logger.warning("search.step1_input: empty query, abort")
        return []

    # Three DDG passes: recipe-focused, site hint, raw query
    search_queries = [
        f"{raw_q} recipe",
        f"{raw_q} allrecipes",
        raw_q,
    ]

    try:
        from ddgs import DDGS
    except ImportError:
        logger.exception("search.step2_import: ddgs package missing (pip install ddgs)")
        return []

    collected: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for qi, q in enumerate(search_queries):
        if len(collected) >= max_collect:
            logger.info(
                "search.step3_query[%s]: skip (already have %s/%s)",
                qi,
                len(collected),
                max_collect,
            )
            break

        logger.info(
            "search.step3_query[%s]: ddgs.text start q=%r max_hits=%s",
            qi,
            q,
            max_collect * 10,
        )

        try:
            with DDGS() as ddgs:
                # Extra headroom: many hits are dropped (unsupported domains for recipe-scrapers).
                hits = list(ddgs.text(q, max_results=max_collect * 10))
        except Exception as e:
            logger.warning("search.step3_query[%s]: ddgs.text raised: %s", qi, e, exc_info=True)
            continue

        if not hits:
            logger.warning("search.step3_query[%s]: ddgs returned zero hits", qi)
            continue

        logger.info("search.step3_query[%s]: ddgs returned %s raw rows", qi, len(hits))

        for row_i, r in enumerate(hits):
            if len(collected) >= max_collect:
                break
            href = (r.get("href") or r.get("url") or "").strip()
            title = (r.get("title") or "").strip()
            source = (r.get("source") or "").strip()
            logger.info(
                "search.step4_hit[%s.%s]: href=%r title=%r source=%r",
                qi,
                row_i,
                href[:200] if href else href,
                title[:120] if title else title,
                source[:80] if source else source,
            )
            if not href or not href.startswith("http"):
                logger.debug("search.step4_hit[%s.%s]: skip (bad href)", qi, row_i)
                continue
            if href in seen_urls:
                logger.debug("search.step4_hit[%s.%s]: skip (duplicate url)", qi, row_i)
                continue
            if not scraper_exists_for(href):
                logger.info(
                    "search.step4b_skip_unsupported[%s.%s]: url=%r",
                    qi,
                    row_i,
                    href[:200],
                )
                continue
            seen_urls.add(href)
            collected.append(
                {
                    "url": href,
                    "title": title[:120] or _title_from_url(href),
                    "source": source[:40],
                }
            )
            logger.info(
                "search.step5_accept: index=%s total_accepted=%s url=%r",
                len(collected) - 1,
                len(collected),
                href[:120],
            )

        if len(collected) >= max_collect:
            logger.info("search.step5b: collect target reached after query[%s]", qi)
            break

    logger.info(
        "search.step6_done: collected=%s (max_collect %s)",
        len(collected),
        max_collect,
    )
    return collected[:max_collect]


async def filter_candidates_by_scrape(
    candidates: list[dict[str, Any]],
    max_keep: int = 5,
    *,
    on_progress: Callable[[int, int, str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    """
    Keep URLs in order only if scrape_recipe_from_url succeeds.
    Each kept item includes the parsed 'recipe' dict (avoids a second fetch on pick).

    on_progress(attempt, total_candidates, url) is awaited before each scrape (1-based attempt).
    """
    out: list[dict[str, Any]] = []
    total = len(candidates)
    scrape_attempt = 0
    for c in candidates:
        if len(out) >= max_keep:
            break
        url = (c.get("url") or "").strip()
        if not url:
            continue
        scrape_attempt += 1
        if on_progress is not None:
            await on_progress(scrape_attempt, total, url)
        recipe = await asyncio.to_thread(scrape_recipe_from_url, url)
        if recipe is None:
            logger.info("filter_scrape: drop url=%r", url[:200])
            continue
        title = (recipe.get("title") or c.get("title") or "").strip() or _title_from_url(url)
        entry = {
            **c,
            "url": url,
            "title": title[:200],
            "recipe": recipe,
        }
        out.append(entry)
        logger.info("filter_scrape: keep url=%r title=%r", url[:120], title[:80])
    return out


def scrape_recipe_from_url(url: str) -> dict[str, Any] | None:
    """Parse one recipe page. Log each attempt."""
    logger.info("scrape.step1_input: url=%r", url[:300] if url else url)

    try:
        logger.info("scrape.step2_scrape_me: trying scrape_me()")
        s = scrape_me(url)
        title = (s.title() or _title_from_url(url)).strip()
        ingredients = [str(x).strip() for x in (s.ingredients() or []) if str(x).strip()]
        instructions = s.instructions_list() or []
        if not instructions and s.instructions():
            block = str(s.instructions())
            parts = re.split(r"\n+|(?=\d+\.\s)", block)
            instructions = [p.strip() for p in parts if p and p.strip()]
        steps = [str(x).strip() for x in instructions if str(x).strip()]
        if not ingredients and not steps:
            logger.warning(
                "scrape.step2_scrape_me: empty ingredients and steps title=%r",
                title[:80],
            )
            raise ValueError("empty recipe body")
        logger.info(
            "scrape.step2_scrape_me: ok title=%r ingredients=%s steps=%s",
            title[:80],
            len(ingredients),
            len(steps),
        )
        return {
            "url": url,
            "title": title,
            "ingredients": ingredients,
            "steps": steps,
        }
    except Exception as e:
        logger.info("scrape.step2_scrape_me: failed (%s), trying HTTP+scrape_html", e)

    try:
        import httpx

        logger.info("scrape.step3_http: GET %r", url[:200])
        resp = httpx.get(url, follow_redirects=True, timeout=25.0)
        resp.raise_for_status()
        logger.info("scrape.step3_http: status=%s bytes=%s", resp.status_code, len(resp.content))
        s = scrape_html(html=resp.text, org_url=url)
        title = (s.title() or _title_from_url(url)).strip()
        ingredients = [str(x).strip() for x in (s.ingredients() or []) if str(x).strip()]
        instructions = s.instructions_list() or []
        if not instructions and s.instructions():
            block = str(s.instructions())
            parts = re.split(r"\n+|(?=\d+\.\s)", block)
            instructions = [p.strip() for p in parts if p and p.strip()]
        steps = [str(x).strip() for x in instructions if str(x).strip()]
        if not ingredients and not steps:
            logger.warning("scrape.step4_scrape_html: still empty")
            return None
        logger.info(
            "scrape.step4_scrape_html: ok title=%r ingredients=%s steps=%s",
            title[:80],
            len(ingredients),
            len(steps),
        )
        return {
            "url": url,
            "title": title,
            "ingredients": ingredients,
            "steps": steps,
        }
    except Exception as e:
        logger.warning("scrape.step5_fail: url=%r error=%s", url[:200], e, exc_info=True)
        return None
