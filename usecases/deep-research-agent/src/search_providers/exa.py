# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import os

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from models import SearchResult

logger = logging.getLogger(__name__)

_RETRYABLE = (ConnectionError, TimeoutError, OSError, httpx.ConnectError, httpx.ReadTimeout)

EXA_API_URL = "https://api.exa.ai/search"
EXA_MAX_TEXT_CHARS = 20000


def _make_exa_http_client() -> httpx.AsyncClient:
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


class ExaProvider:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

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
            async with _make_exa_http_client() as client:
                resp = await client.post(
                    EXA_API_URL,
                    json={
                        "query": query,
                        "type": "auto",
                        "numResults": max_results,
                        "contents": {
                            "text": {"maxCharacters": EXA_MAX_TEXT_CHARS},
                            "highlights": True,
                        },
                    },
                    headers={
                        "x-api-key": self._api_key,
                        "x-exa-integration": "intel-deep-research-agent",
                        "Content-Type": "application/json",
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
        except (ConnectionError, TimeoutError, OSError, httpx.ConnectError, httpx.ReadTimeout):
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503):
                raise ConnectionError(f"Exa HTTP {e.response.status_code}") from e
            logger.exception("Exa search failed for query: %s", query)
            return []
        except Exception:
            logger.exception("Exa search failed for query: %s", query)
            return []

        results: list[SearchResult] = []
        for idx, item in enumerate(data.get("results", [])[:max_results]):
            # Exa returns results ranked but unscored: top result ~0.9, decreasing by 0.1
            score = max(0.3, 0.9 - (idx * 0.1))
            results.append(
                SearchResult(
                    title=item.get("title") or "",
                    url=item.get("url", ""),
                    content=" ".join(item.get("highlights") or []),
                    raw_content=item.get("text"),
                    score=score,
                )
            )
        return results
