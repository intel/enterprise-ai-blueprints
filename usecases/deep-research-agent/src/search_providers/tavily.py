# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import os

import httpx
from tavily import AsyncTavilyClient
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from models import SearchResult

logger = logging.getLogger(__name__)

_RETRYABLE = (ConnectionError, TimeoutError, OSError)


def _make_tavily_http_client() -> httpx.AsyncClient:
    """Build an httpx client that uses the corporate HTTP proxy but ignores ALL_PROXY.

    The ALL_PROXY env var uses socks:// which httpx rejects ("Unknown scheme").
    We use trust_env=False and explicitly mount only the http/https proxies.
    """
    http_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    https_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    mounts: dict[str, httpx.AsyncHTTPTransport | None] = {}
    if http_proxy:
        mounts["http://"] = httpx.AsyncHTTPTransport(proxy=http_proxy)
    if https_proxy:
        mounts["https://"] = httpx.AsyncHTTPTransport(proxy=https_proxy)
    return httpx.AsyncClient(trust_env=False, mounts=mounts)


class TavilyProvider:
    def __init__(self, api_key: str, search_depth: str = "advanced") -> None:
        self._client = AsyncTavilyClient(api_key=api_key, client=_make_tavily_http_client())
        self.search_depth = search_depth

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
        max_results: int = 5,
    ) -> list[SearchResult]:
        try:
            resp = await self._client.search(
                query=query,
                search_depth=self.search_depth,
                max_results=max_results,
                include_raw_content="markdown",
            )
        except (ConnectionError, TimeoutError, OSError):
            raise
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
                )
            )
        return results
