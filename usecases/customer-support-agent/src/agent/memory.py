# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Long-term semantic memory backed by RediSearch + LiteLLM-served embeddings.

It stores short interaction summaries per customer as vectors in the cluster's
redis-stack (RediSearch) and retrieves the most relevant ones to prime the agent's system
prompt. Embeddings are produced by LiteLLM (AI Solutions inference layer), so no
local embedding model is needed.

Design goals:
- **Fail open.** Any Redis/embedding error degrades to "no memory" rather than
  breaking the chat. Memory is an enhancement, not a hard dependency.
- **Per-customer isolation** via a tag field so one customer never recalls
  another's history.
"""
from __future__ import annotations

import struct
import time
import uuid
from typing import List, Optional

from openai import OpenAI

from config import (
    MEMORY_ENABLED,
    REDIS_URL,
    MEMORY_INDEX,
    MEMORY_TOP_K,
    EMBEDDING_MODEL,
    EMBEDDING_DIMS,
    LLM_BASE_URL,
    LLM_API_KEY,
)

_redis = None                # lazy redis.Redis client
_embed_client: Optional[OpenAI] = None
_init_done = False
_available = False


def _log(msg: str) -> None:
    print(f"[memory] {msg}", flush=True)


def _get_embed_client() -> OpenAI:
    global _embed_client
    if _embed_client is None:
        _embed_client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY or "none")
    return _embed_client


def _embed(text: str) -> Optional[List[float]]:
    """Embed text via LiteLLM. Returns None on failure."""
    try:
        resp = _get_embed_client().embeddings.create(model=EMBEDDING_MODEL, input=text[:8000])
        return resp.data[0].embedding
    except Exception as e:  # noqa: BLE001 - fail open
        _log(f"embed failed: {e}")
        return None


def _to_bytes(vec: List[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _ensure_index() -> bool:
    """Create the RediSearch vector index if it doesn't exist. Idempotent."""
    global _redis, _available
    try:
        import redis
        from redis.commands.search.field import TagField, TextField, VectorField, NumericField
        try:
            # redis-py >= 5 uses snake_case module name
            from redis.commands.search.index_definition import IndexDefinition, IndexType
        except ImportError:
            # older redis-py used camelCase
            from redis.commands.search.indexDefinition import IndexDefinition, IndexType
    except Exception as e:  # noqa: BLE001
        _log(f"redis library unavailable: {e}")
        return False

    try:
        if _redis is None:
            _redis = redis.Redis.from_url(REDIS_URL, decode_responses=False, socket_timeout=5)
        _redis.ping()
    except Exception as e:  # noqa: BLE001
        _log(f"redis connection failed ({REDIS_URL}): {e}")
        return False

    try:
        # Already exists?
        try:
            _redis.ft(MEMORY_INDEX).info()
            _available = True
            return True
        except redis.exceptions.ResponseError:
            pass  # "Unknown index name" — create it below

        schema = (
            TagField("customer_id"),
            TextField("text"),
            NumericField("ts"),
            VectorField(
                "embedding",
                "HNSW",
                {"TYPE": "FLOAT32", "DIM": EMBEDDING_DIMS, "DISTANCE_METRIC": "COSINE"},
            ),
        )
        definition = IndexDefinition(prefix=["csa:mem:"], index_type=IndexType.HASH)
        _redis.ft(MEMORY_INDEX).create_index(fields=schema, definition=definition)
        _log(f"created index {MEMORY_INDEX} (dim={EMBEDDING_DIMS})")
        _available = True
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"index setup failed: {e}")
        return False


def init_memory() -> bool:
    """Initialize memory once at startup. Safe to call repeatedly."""
    global _init_done, _available
    if _init_done:
        return _available
    _init_done = True
    if not MEMORY_ENABLED:
        _log("disabled via MEMORY_ENABLED")
        _available = False
        return False
    _available = _ensure_index()
    _log(f"memory available: {_available}")
    return _available


def is_available() -> bool:
    return _available


def add_memory(customer_id: str, text: str) -> None:
    """Store an interaction summary for a customer. Fail-open."""
    if not _available or not text or not text.strip():
        return
    vec = _embed(text)
    if vec is None:
        return
    try:
        key = f"csa:mem:{uuid.uuid4().hex}"
        _redis.hset(
            key,
            mapping={
                "customer_id": customer_id or "anonymous",
                "text": text.strip()[:2000],
                "ts": int(time.time()),
                "embedding": _to_bytes(vec),
            },
        )
    except Exception as e:  # noqa: BLE001
        _log(f"add_memory failed: {e}")


def search_memory(customer_id: str, query: str, top_k: int = MEMORY_TOP_K) -> str:
    """Return a newline-joined block of the most relevant past interactions.

    Empty string if memory is unavailable or nothing relevant is found.
    """
    if not _available or not query or not query.strip():
        return ""
    vec = _embed(query)
    if vec is None:
        return ""
    try:
        from redis.commands.search.query import Query

        cust = (customer_id or "anonymous").replace("-", r"\-")
        base = f"@customer_id:{{{cust}}}"
        q = (
            Query(f"({base})=>[KNN {top_k} @embedding $vec AS score]")
            .sort_by("score")
            .return_fields("text", "score", "ts")
            .dialect(2)
        )
        res = _redis.ft(MEMORY_INDEX).search(q, query_params={"vec": _to_bytes(vec)})
        lines = []
        for doc in res.docs:
            text = doc.text.decode() if isinstance(doc.text, bytes) else doc.text
            lines.append(f"- {text}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        _log(f"search_memory failed: {e}")
        return ""
