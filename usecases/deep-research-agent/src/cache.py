# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import logging
import re

from cachetools import TTLCache

from config import settings

logger = logging.getLogger(__name__)

_TTL_SECONDS = settings.search_cache_ttl_hours * 3600

# Search results: configurable TTL, configurable max entries
_search_cache: TTLCache[str, list] = TTLCache(
    maxsize=settings.search_cache_max_entries,
    ttl=_TTL_SECONDS,
)

# Source content: 7-day TTL, up to 1000 entries
_content_cache: TTLCache[str, str] = TTLCache(maxsize=1000, ttl=604800)

# Hit/miss counters for stats endpoint
_search_hits: int = 0
_search_misses: int = 0


def _normalize_query(query: str) -> str:
    """Lowercase and collapse whitespace so minor phrasing differences share a cache slot."""
    return re.sub(r"\s+", " ", query.strip().lower())


def _hash_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def get_search_cache(query: str) -> list | None:
    global _search_hits, _search_misses
    key = _hash_key(_normalize_query(query))
    result = _search_cache.get(key)
    if result is not None:
        _search_hits += 1
        logger.info("Search cache hit for query: %s", query[:80])
    else:
        _search_misses += 1
    return result


def set_search_cache(query: str, results: list) -> None:
    key = _hash_key(_normalize_query(query))
    _search_cache[key] = results


def get_content_cache(url: str) -> str | None:
    key = _hash_key(url)
    result = _content_cache.get(key)
    if result is not None:
        logger.info("Content cache hit for url: %s", url[:80])
    return result


def set_content_cache(url: str, content: str) -> None:
    key = _hash_key(url)
    _content_cache[key] = content


def clear_search_cache() -> int:
    """Clear the in-memory search cache. Returns the number of entries cleared."""
    count = len(_search_cache)
    _search_cache.clear()
    return count


def get_cache_stats() -> dict:
    """Return runtime stats for the in-memory search cache."""
    total = _search_hits + _search_misses
    hit_rate = round(_search_hits / total, 3) if total > 0 else 0.0
    return {
        "entries": len(_search_cache),
        "max_entries": settings.search_cache_max_entries,
        "ttl_hours": settings.search_cache_ttl_hours,
        "hits": _search_hits,
        "misses": _search_misses,
        "hit_rate": hit_rate,
    }


# ── Async helpers for persistent SQLite-backed cache ─────────────────────────

async def load_cache_from_db() -> int:
    """Warm the in-memory cache from the SQLite persistent store at startup.

    Returns the number of entries loaded.
    """
    from db import SearchCacheRepository, get_session
    try:
        async with get_session() as db:
            repo = SearchCacheRepository(db)
            entries = await repo.load_all_valid()
        for key, results in entries.items():
            _search_cache[key] = results
        logger.info("Loaded %d search cache entries from DB", len(entries))
        return len(entries)
    except Exception:
        logger.exception("Failed to warm search cache from DB")
        return 0


async def persist_cache_entry(query: str, results: list) -> None:
    """Persist a single cache entry to SQLite (fire-and-forget friendly)."""
    from db import SearchCacheRepository, get_session
    key = _hash_key(_normalize_query(query))
    try:
        async with get_session() as db:
            repo = SearchCacheRepository(db)
            await repo.set(key, query, results, _TTL_SECONDS)
    except Exception:
        logger.exception("Failed to persist search cache entry for query: %s", query[:80])
