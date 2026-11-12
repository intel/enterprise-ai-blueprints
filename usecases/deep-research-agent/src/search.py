# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging

from tavily import AsyncTavilyClient
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from cache import get_search_cache, set_search_cache
from config import settings
from models import SearchResult

logger = logging.getLogger(__name__)

_RETRYABLE = (
    ConnectionError,
    TimeoutError,
    OSError,
)


class SearchClient:
    def __init__(self) -> None:
        self._client = AsyncTavilyClient(api_key=settings.tavily_api_key)
        self.search_depth = settings.tavily_search_depth
        self.max_results = settings.tavily_max_results

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def search(
        self,
        query: str,
        *,
        sub_question_id: str = "",
        max_results: int | None = None,
    ) -> list[SearchResult]:
        # Check cache first
        cached = get_search_cache(query)
        if cached is not None:
            results = [SearchResult.model_validate(r) for r in cached]
            # Re-tag with sub_question_id since cache doesn't store it
            for r in results:
                r.sub_question_id = sub_question_id
            return results

        try:
            resp = await self._client.search(
                query=query,
                search_depth=self.search_depth,
                max_results=max_results or self.max_results,
                include_raw_content="markdown",
            )
        except (ConnectionError, TimeoutError, OSError):
            raise  # let tenacity handle these
        except Exception:
            logger.exception("Tavily search failed for query: %s", query)
            return []

        results: list[SearchResult] = []
        for item in resp.get("results", []):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    content=item.get("content", ""),
                    raw_content=item.get("raw_content"),
                    score=item.get("score", 0.0),
                    sub_question_id=sub_question_id,
                )
            )

        # Cache the results (without sub_question_id which is context-specific)
        set_search_cache(query, [r.model_dump() for r in results])

        return results

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def extract(self, urls: list[str]) -> dict[str, str]:
        """Extract full content from specific URLs. Returns {url: content}."""
        if not urls:
            return {}
        try:
            resp = await self._client.extract(urls=urls)
            return {
                item["url"]: item.get("raw_content", "")
                for item in resp.get("results", [])
            }
        except (ConnectionError, TimeoutError, OSError):
            raise
        except Exception:
            logger.exception("Tavily extract failed for urls: %s", urls)
            return {}
