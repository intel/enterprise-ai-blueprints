# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json

import httpx

from search_providers import exa
from search_providers.exa import ExaProvider


def test_exa_search_maps_results(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "A", "url": "https://a.example", "text": "full a", "highlights": ["hl a"]},
                    {"title": None, "url": "https://b.example", "text": "full b"},
                ]
            },
        )

    monkeypatch.setattr(
        exa,
        "_make_exa_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    results = asyncio.run(ExaProvider(api_key="test-key").search("query", max_results=2))

    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["body"]["query"] == "query"
    assert seen["body"]["numResults"] == 2
    assert [r.url for r in results] == ["https://a.example", "https://b.example"]
    assert results[0].content == "hl a"
    assert results[0].raw_content == "full a"
    assert results[1].title == ""
    assert results[0].score > results[1].score
