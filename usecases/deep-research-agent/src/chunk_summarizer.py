# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Heading-aware, chunk-level extractive summarization.

A coarser-grained alternative to ``extractive_summarizer.py`` (which works at
the *sentence* level).  Here a document is split into heading-scoped Markdown
chunks, each chunk is embedded with SentenceTransformers, the chunk embeddings
are aggregated into a single *page embedding*, and chunks are selected by a
blend of similarity-to-query and similarity-to-page-centroid.

Pure functions only — no graph/LLM dependencies.  The SentenceTransformer
loader is reused from ``extractive_summarizer`` so the ``(model, device)``
cache is shared.  Embeddings run on CPU by default (Intel Xeon).
"""

from __future__ import annotations

import numpy as np
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from extractive_summarizer import _get_embedder

# Header levels we split on, paired with the metadata key used to carry the
# heading text.  Splitting stops at ### — deeper headings stay inside a chunk.
_HEADERS_TO_SPLIT_ON = [("#", "h1"), ("##", "h2"), ("###", "h3")]
_HEADER_KEYS = ("h1", "h2", "h3")
_HEADER_PREFIXES = {"h1": "#", "h2": "##", "h3": "###"}


def _prepend_headings(content: str, metadata: dict) -> str:
    """Re-attach the heading trail (from splitter metadata) to chunk text.

    ``MarkdownHeaderTextSplitter`` strips headers into ``metadata`` by default;
    we put them back as a Markdown heading block so headings are *preserved* in
    the chunk content (and so they contribute to the chunk embedding).
    """
    heading_lines = [
        f"{_HEADER_PREFIXES[key]} {metadata[key]}"
        for key in _HEADER_KEYS
        if metadata.get(key)
    ]
    if not heading_lines:
        return content
    prefix = "\n".join(heading_lines)
    return f"{prefix}\n\n{content}" if content else prefix


def split_markdown_into_chunks(
    markdown: str,
    max_chunk_chars: int = 2000,
    overlap: int = 200,
) -> list[str]:
    """Split Markdown into heading-scoped chunks, preserving headings.

    Sections longer than ``max_chunk_chars`` are sub-split with a recursive
    character splitter; the heading trail is retained on every sub-chunk.
    """
    if not markdown or not markdown.strip():
        return []

    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_HEADERS_TO_SPLIT_ON,
        strip_headers=True,
    )
    sections = header_splitter.split_text(markdown)

    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chunk_chars,
        chunk_overlap=overlap,
    )

    chunks: list[str] = []
    for section in sections:
        body = section.page_content.strip()
        if not body:
            continue
        if len(body) <= max_chunk_chars:
            pieces = [body]
        else:
            pieces = char_splitter.split_text(body)
        for piece in pieces:
            chunk = _prepend_headings(piece.strip(), section.metadata)
            if chunk.strip():
                chunks.append(chunk)

    return chunks


def embed_chunks(embedder, chunks: list[str]) -> np.ndarray:
    """Embed chunks into an ``(n, dim)`` array of L2-normalized vectors."""
    if not chunks:
        return np.empty((0, 0), dtype=np.float32)
    return embedder.encode(
        chunks, convert_to_numpy=True, normalize_embeddings=True
    )


def aggregate_page_embedding(chunk_embeddings: np.ndarray) -> np.ndarray:
    """Aggregate chunk embeddings into a single normalized page embedding."""
    if chunk_embeddings.size == 0:
        return np.empty((0,), dtype=np.float32)
    centroid = chunk_embeddings.mean(axis=0)
    norm = np.linalg.norm(centroid)
    return centroid if norm == 0 else centroid / norm


def _select_chunks_by_budget(
    chunks: list[str],
    scores: np.ndarray,
    token_budget: int,
) -> list[int]:
    """Greedily keep highest-scoring chunks within ``token_budget``.

    Token count is approximated by whitespace-split word count (avoids a hard
    ``nltk`` dependency).  At least one chunk is always returned.  Mirrors the
    hard-cap semantics of ``extractive_summarizer._top_by_budget``.
    """
    if not chunks:
        return []

    token_counts = [len(c.split()) for c in chunks]
    order = np.argsort(scores)[::-1]  # descending

    selected: list[int] = []
    token_count = 0
    for idx in order:
        idx = int(idx)
        if not selected:
            # Guarantee at least one chunk.
            selected.append(idx)
            token_count += token_counts[idx]
            if token_count >= token_budget:
                break
            continue
        if token_count + token_counts[idx] > token_budget:
            continue
        selected.append(idx)
        token_count += token_counts[idx]
        if token_count >= token_budget:
            break
    return sorted(selected)


def summarize_from_chunks(
    markdown: str,
    query: str,
    token_budget: int = 4000,
    embedding_model: str = "all-MiniLM-L6-v2",
    embedding_device: str = "cpu",
    max_chunk_chars: int = 2000,
    query_weight: float = 0.5,
) -> tuple[str, list[float]]:
    """Extractively summarize a Markdown document at the chunk level.

    Splits into heading-scoped chunks, embeds each chunk, aggregates a single
    page embedding, then scores chunks by a blend of similarity-to-query and
    similarity-to-page-centroid and keeps the top chunks within the token
    budget (in original document order).

    Args:
        markdown: Clean Markdown document (headings preserved).
        query: Research query to score chunk relevance against.
        token_budget: Approximate word budget for the returned summary.
        embedding_model: SentenceTransformer model name.
        embedding_device: Torch device — ``"cpu"`` for Intel Xeon.
        max_chunk_chars: Max characters per chunk before sub-splitting.
        query_weight: Weight on query similarity vs. page-centroid similarity
            (``0..1``); the complement weights page-centrality.

    Returns:
        ``(summary, page_embedding)`` where ``summary`` is the selected chunks
        joined by blank lines and ``page_embedding`` is the aggregated page
        vector as a list of floats.  Returns ``("", [])`` for empty input.
    """
    chunks = split_markdown_into_chunks(markdown, max_chunk_chars=max_chunk_chars)
    if not chunks:
        return "", []

    embedder = _get_embedder(embedding_model, embedding_device)
    chunk_embeddings = embed_chunks(embedder, chunks)
    page_embedding = aggregate_page_embedding(chunk_embeddings)

    # If the whole document already fits the budget, keep everything.
    total_tokens = sum(len(c.split()) for c in chunks)
    if token_budget >= total_tokens:
        return "\n\n".join(chunks), page_embedding.tolist()

    # Similarity to query (embeddings are normalized → dot == cosine).
    query_emb = embedder.encode(
        [query], convert_to_numpy=True, normalize_embeddings=True
    )[0]
    query_sim = chunk_embeddings @ query_emb
    page_sim = chunk_embeddings @ page_embedding

    scores = query_weight * query_sim + (1.0 - query_weight) * page_sim
    selected = _select_chunks_by_budget(chunks, scores, token_budget)

    summary = "\n\n".join(chunks[i] for i in selected)
    return summary, page_embedding.tolist()
