# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Sentence-level, query-focused extractive summarization.

Compresses a document by selecting the most query-relevant sentences (via
SentenceTransformer embeddings + a configurable selection method) up to a token
budget, returning them in original document order. Pure/self-contained — no
graph or LLM dependencies. Embeddings run on CPU by default (Intel Xeon); the
``(model, device)`` embedder cache is shared with ``chunk_summarizer``.
"""

from __future__ import annotations

import logging
from threading import Lock
from time import perf_counter

import numpy as np
import nltk
from sentence_transformers import SentenceTransformer

from clean_text import clean_scraped_text

logger = logging.getLogger(__name__)

# The tokenizer is baked into the image (Dockerfile); only download when it is
# missing (local runs). nltk.download writes a lock file next to the data, so
# calling it unconditionally fails on a read-only root filesystem.
try:
    nltk.data.find("tokenizers/punkt_tab")
except LookupError:
    try:
        nltk.download("punkt_tab", quiet=True)
    except Exception as exc:  # offline / read-only — sentence splitting will fail later
        logger.warning("Could not download NLTK punkt_tab tokenizer: %s", exc)

_embedder_cache: dict[tuple[str, str], SentenceTransformer] = {}
_embedder_lock = Lock()


def _get_embedder(model: str, device: str) -> SentenceTransformer:
    """Return a cached SentenceTransformer instance keyed by (model, device)."""
    key = (model, device)
    with _embedder_lock:
        embedder = _embedder_cache.get(key)
        if embedder is None:
            embedder = SentenceTransformer(model, device=device)
            _embedder_cache[key] = embedder
        return embedder


def split_embed_tokenize(
    embedder,
    raw: str,
) -> tuple[np.ndarray, list[str], list[list[str]]]:

    if not raw or not raw.strip():
        return np.empty((0, 0), dtype=np.float32), [], []

    all_sentences = nltk.sent_tokenize(raw)
    if not all_sentences:
        return np.empty((0, 0), dtype=np.float32), [], []

    sentence_embeddings = embedder.encode(all_sentences, convert_to_numpy=True, normalize_embeddings=True)
    tokenized_sentences = [nltk.word_tokenize(s) for s in all_sentences]

    return sentence_embeddings, all_sentences, tokenized_sentences


def _select_mmr(
    embeddings: np.ndarray,  # normalized sentence embeddings
    tokenized_sentences: list[list[str]],
    token_budget: int,
    lambda_param: float = 0.5,
) -> list[int]:

    centroid = embeddings.mean(axis=0)
    centroid_norm = np.linalg.norm(centroid)
    centroid_normed = centroid if centroid_norm == 0 else centroid / centroid_norm

    relevance = embeddings @ centroid_normed
    sim_matrix = embeddings @ embeddings.T

    selected: list[int] = []
    candidates = list(range(len(embeddings)))
    token_count = 0

    while candidates:
        if not selected:
            cand_arr = np.asarray(candidates)
            idx = int(cand_arr[int(np.argmax(relevance[cand_arr]))])
            # Guarantee at least one selection even if it exceeds the budget.
            selected.append(idx)
            candidates.remove(idx)
            token_count += len(tokenized_sentences[idx])
            if token_count >= token_budget:
                break
            continue

        cand_arr = np.asarray(candidates)
        max_sim = sim_matrix[np.ix_(cand_arr, selected)].max(axis=1)
        mmr_scores = lambda_param * relevance[cand_arr] - (1 - lambda_param) * max_sim
        order = np.argsort(mmr_scores)[::-1]

        # Pick the highest-scoring candidate that still fits in the budget.
        picked = None
        for j in order:
            cand_idx = int(cand_arr[int(j)])
            if token_count + len(tokenized_sentences[cand_idx]) <= token_budget:
                picked = cand_idx
                break
        if picked is None:
            break  # No remaining candidate fits.

        selected.append(picked)
        candidates.remove(picked)
        token_count += len(tokenized_sentences[picked])
        if token_count >= token_budget:
            break

    return sorted(selected)


def _top_by_budget(scores: np.ndarray, tokenized_sentences: list[list[str]], token_budget: int) -> list[int]:
    """Select highest-scoring sentence indices whose cumulative token count fits within token_budget.

    The budget is a hard cap: a sentence is skipped if adding it would exceed
    token_budget. At least one sentence is always returned (the top-scoring one)
    when scores are non-empty, even if it alone exceeds the budget.
    """
    if len(scores) == 0:
        return []

    order = np.argsort(scores)[::-1]  # descending
    selected: list[int] = []
    token_count = 0
    for idx in order:
        idx = int(idx)
        sent_tokens = len(tokenized_sentences[idx])
        if not selected:
            # Guarantee at least one sentence.
            selected.append(idx)
            token_count += sent_tokens
            if token_count >= token_budget:
                break
            continue
        if token_count + sent_tokens > token_budget:
            continue
        selected.append(idx)
        token_count += sent_tokens
        if token_count >= token_budget:
            break
    return sorted(selected)


def _select_centroid(embeddings: np.ndarray, tokenized_sentences: list[list[str]], token_budget: int) -> list[int]:
    centroid = embeddings.mean(axis=0)
    norms = np.linalg.norm(embeddings, axis=1)
    centroid_norm = np.linalg.norm(centroid)
    denom = norms * centroid_norm
    denom = np.where(denom == 0, 1.0, denom)
    similarities = (embeddings @ centroid) / denom
    return _top_by_budget(similarities, tokenized_sentences, token_budget)


def _select_lexrank(
    embeddings: np.ndarray,
    tokenized_sentences: list[list[str]],
    token_budget: int,
    max_iter: int = 100,
    tol: float = 1e-8,
) -> list[int]:
    from scipy.special import softmax

    # Build a row-stochastic transition matrix from sentence similarities.
    sim_matrix = embeddings @ embeddings.T
    transition = softmax(sim_matrix, axis=1)

    # Power iteration for the stationary distribution (left eigenvector of
    # `transition` for eigenvalue 1 == right eigenvector of `transition.T`).
    n = transition.shape[0]
    v = np.full(n, 1.0 / n)
    transition_t = transition.T
    for _ in range(max_iter):
        v_new = transition_t @ v
        v_new /= v_new.sum()  # Renormalize to guard against drift.
        if np.linalg.norm(v_new - v, 1) < tol:
            v = v_new
            break
        v = v_new

    return _top_by_budget(v, tokenized_sentences, token_budget)


def _select_cosine_similarity_centrality(
    embeddings: np.ndarray, tokenized_sentences: list[list[str]], token_budget: int
) -> list[int]:
    from sklearn.metrics.pairwise import cosine_similarity

    sim_matrix = cosine_similarity(embeddings)
    centrality = sim_matrix.sum(axis=1)
    return _top_by_budget(centrality, tokenized_sentences, token_budget)


def _select_sentence_centrality(
    embeddings: np.ndarray, tokenized_sentences: list[list[str]], token_budget: int
) -> list[int]:
    """Forward-looking sentence centrality (Gong et al., 2022)."""
    n = len(embeddings)
    if n == 1:
        return [0]

    dot_matrix = embeddings @ embeddings.T
    centrality = np.zeros(n)
    for i in range(n - 1):
        centrality[i] = dot_matrix[i, i + 1:].mean()
    centrality[-1] = centrality[:-1].mean()

    return _top_by_budget(centrality, tokenized_sentences, token_budget)


def extractive_summarize(
    document: str,
    query: str,
    token_budget: int = 4000,
    embedding_model: str = "all-MiniLM-L6-v2",
    embedding_device: str = "cpu",
    selection_method: str = "centroid",
) -> str:
    """Extract the most query-relevant sentences from a document.

    Args:
        document: The full document text to compress.
        query: The research query to score relevance against.
        token_budget: Maximum token budget for the output.

    Returns:
        Compressed text containing the most relevant sentences in
        their original document order, fitting within the token budget.
    """

    # Initialize (cached across calls)
    start_time = perf_counter()
    embedder = _get_embedder(embedding_model, embedding_device)
    logger.debug("Embedder loaded in %.2f ms", (perf_counter() - start_time) * 1000)

    # Clean, split into sentences, and embed
    paragraphs = clean_scraped_text(document)
    cleaned = " ".join(s for sublist in paragraphs for s in sublist)
    embeddings, sentences, tokenized_sentences = split_embed_tokenize(embedder, cleaned)
    total_tokens = sum(len(tokens) for tokens in tokenized_sentences)

    if not sentences:
        return ""

    if token_budget >= total_tokens:
        return " ".join(sentences)

    # Select sentences based on the specified method
    start_time = perf_counter()
    if selection_method == "mmr":
        selected_indices = _select_mmr(embeddings, tokenized_sentences, token_budget, lambda_param=0.5)
    elif selection_method == "centroid":
        selected_indices = _select_centroid(embeddings, tokenized_sentences, token_budget)
    elif selection_method == "lexrank":
        selected_indices = _select_lexrank(embeddings, tokenized_sentences, token_budget)
    elif selection_method == "cosine_similarity_centrality":
        selected_indices = _select_cosine_similarity_centrality(
            embeddings, tokenized_sentences, token_budget
        )
    elif selection_method == "sentence_centrality":
        selected_indices = _select_sentence_centrality(
            embeddings, tokenized_sentences, token_budget
        )
    else:
        raise ValueError(
            f"Unknown selection_method: {selection_method!r}. Use 'centroid', 'mmr', "
            "'lexrank', 'cosine_similarity_centrality', or 'sentence_centrality'."
        )
    logger.debug(
        "Selected %d sentences in %.2f ms", len(selected_indices), (perf_counter() - start_time) * 1000
    )
    # Join selected sentences in their original order
    selected_indices = sorted(selected_indices)
    cleaned = [sentences[i] for i in selected_indices]
    return " ".join(cleaned)
