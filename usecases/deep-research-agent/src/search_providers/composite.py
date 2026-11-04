# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging

from cache import get_search_cache, persist_cache_entry, set_search_cache
from config import settings
from models import SearchResult
from search_providers.base import SearchProvider
from search_providers.tavily import TavilyProvider
from search_providers.serper import SerperProvider

logger = logging.getLogger(__name__)


class CompositeSearchProvider:
    """Fans out to multiple search providers, merges and deduplicates results."""

    def __init__(self, providers: list[SearchProvider]) -> None:
        self._providers = providers

    async def search(
        self,
        query: str,
        *,
        sub_question_id: str = "",
        max_results: int | None = None,
    ) -> list[SearchResult]:
        effective_max = max_results or settings.tavily_max_results

        # Check cache
        cached = get_search_cache(query)
        if cached is not None:
            logger.info("Search cache hit for query: %s (%d results)", query, len(cached))
            results = [SearchResult.model_validate(r) for r in cached]
            for r in results:
                r.sub_question_id = sub_question_id
            return results

        # Fan out to all providers
        provider_names = [type(p).__name__ for p in self._providers]
        logger.info("Searching %d providers (%s) for: %s", len(self._providers), ", ".join(provider_names), query)
        tasks = [
            provider.search(query, max_results=effective_max)
            for provider in self._providers
        ]
        all_results_nested = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge results, handling failures gracefully
        merged: list[SearchResult] = []
        for result in all_results_nested:
            if isinstance(result, Exception):
                logger.warning("Search provider failed: %s", result)
                continue
            merged.extend(result)

        # Deduplicate by URL, keeping highest score
        seen: dict[str, SearchResult] = {}
        for r in merged:
            r.sub_question_id = sub_question_id
            if r.url not in seen or r.score > seen[r.url].score:
                seen[r.url] = r
        deduped = list(seen.values())
        logger.info("Search for '%s': %d raw results, %d after dedup", query, len(merged), len(deduped))

        # Cache results in memory and persist to DB (fire-and-forget)
        set_search_cache(query, [r.model_dump() for r in deduped])
        asyncio.create_task(persist_cache_entry(query, [r.model_dump() for r in deduped]))

        return deduped


def create_search_provider() -> CompositeSearchProvider:
    """Create a composite search provider from configured API keys."""
    providers: list[SearchProvider] = []

    if settings.tavily_api_key:
        providers.append(
            TavilyProvider(
                api_key=settings.tavily_api_key,
                search_depth=settings.tavily_search_depth,
            )
        )

    if settings.serper_api_key:
        providers.append(SerperProvider(api_key=settings.serper_api_key))

    if not providers:
        raise ValueError("No search provider configured. Set TAVILY_API_KEY or SERPER_API_KEY.")

    return CompositeSearchProvider(providers)
