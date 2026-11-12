# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Research tools that the agent can invoke.

Each tool wraps existing pipeline logic (search providers, LLMClient) and
returns a JSON string with ``{"message": "...", "state_updates": {...}}``.
The custom tool node (``tool_node.py``) parses this to produce both a
``ToolMessage`` for the agent and state field updates for the graph.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Annotated, Union
from urllib.parse import urlparse

from langchain_core.tools import tool, InjectedToolCallId
from langgraph.prebuilt import InjectedState
from pydantic import BeforeValidator

from config import settings
from llm_client import get_llm
from models import (
    Citation,
    ClaimExtractionResponse,
    EvaluatedSource,
    EvaluationResponse,
    FactualClaim,
    GapAnalysisResponse,
    SearchResult,
)
from search_providers import create_search_provider
from extractive_summarizer import extractive_summarize
from llm_summarizer import llm_summarize

from langchain_text_splitters import MarkdownTextSplitter

if TYPE_CHECKING:
    # Annotation-only: the helpers below are typed `llm: LLMClient` but the name
    # was never imported. `from __future__ import annotations` kept that from
    # crashing at import time, so it stayed invisible at runtime while breaking
    # type checkers and typing.get_type_hints().
    from llm_client import LLMClient

logger = logging.getLogger(__name__)

_splitter = MarkdownTextSplitter(chunk_size=4000, chunk_overlap=200)

# Maximum sources to summarize individually in write_report map phase
_MAX_REPORT_SOURCES = 8


def _result(message: str, state_updates: dict | None = None) -> str:
    """Encode a tool return value as JSON for the custom tool node."""
    payload: dict = {"message": message}
    if state_updates:
        payload["state_updates"] = state_updates
    return json.dumps(payload)


# ── Tool 1: web_search ─────────────────────────────────────────


_RECENCY_KEYWORDS = {"today", "latest", "recent", "current", "now", "this week", "this month", "yesterday"}


def _coerce_to_str_list(v: object) -> list[str]:
    """Accept a JSON array, a Python list, or a string repr of a list."""
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        try:
            parsed = ast.literal_eval(v)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            pass  # not a list literal — treat as a single string
        return [v]
    return list(v)  # type: ignore[arg-type]


@tool
async def web_search(
    queries: Annotated[list[str], BeforeValidator(_coerce_to_str_list)],
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Search the web for information. This is always the FIRST tool to call in any research pipeline.

    Args:
        queries: A list of 1-5 search query strings to execute in parallel.
    """
    # Inject date for recency-sensitive queries (Phase 4.1)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    processed_queries: list[str] = []
    for q in queries[:5]:
        if any(kw in q.lower() for kw in _RECENCY_KEYWORDS):
            processed_queries.append(f"{q} {today_str}")
        else:
            processed_queries.append(q)

    logger.info("web_search: executing %d queries: %s", len(processed_queries), processed_queries)
    search_provider = create_search_provider()
    tasks = [search_provider.search(q) for q in processed_queries]
    results_nested = await asyncio.gather(*tasks)

    new_results: list[SearchResult] = []
    for batch in results_nested:
        new_results.extend(batch)

    # Deduplicate against existing results in state
    existing_urls = {r["url"] for r in state.get("search_results", [])}
    seen: dict[str, SearchResult] = {}
    for r in new_results:
        if r.url not in existing_urls and (r.url not in seen or r.score > seen[r.url].score):
            seen[r.url] = r
    deduped = list(seen.values())
    logger.info(
        "web_search: %d raw results, %d after dedup (%d already known)",
        len(new_results), len(deduped), len(existing_urls),
    )

    all_results = state.get("search_results", []) + [r.model_dump() for r in deduped]
    new_count = state.get("search_count", 0) + 1

    # Track search query history for dedup in gap analysis (Phase 1.2)
    history = list(state.get("search_queries_history", []))
    history.extend(queries[:5])

    snippets = []
    for r in deduped[:5]:
        snippet = (r.content or "")[:300]
        if snippet:
            snippets.append(f"- {r.title} ({r.url}): {snippet}")
    snippets_text = "\n".join(snippets) if snippets else ""

    return _result(
        f"Found {len(deduped)} new unique sources from {len(queries)} queries "
        f"(total: {len(all_results)} sources).\n\n"
        f"Top results:\n{snippets_text}",
        state_updates={
            "search_results": all_results,
            "search_count": new_count,
            "search_queries_history": history,
        },
    )


# ── Tool 2: summarize_sources ──────────────────────────────────


@tool
async def summarize_sources(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Compress accumulated search results before evaluation.

    REQUIRES: web_search must be called before this tool.

    Runs summarization over every source that has not yet been compressed,
    storing the result on each source (``compressed_content``) so later steps
    reuse it. The method is LLM-based when ``use_llm_summarizer`` is set
    (overriding the extractive path), otherwise sentence-level extractive when
    ``use_extractive_summarizer`` is set. When both are off, sources pass
    through untouched.
    """
    # Per-request override (split-screen comparison) takes precedence over the
    # global settings so two concurrent runs can differ.
    from token_tracker import current_summarize_override
    override = current_summarize_override.get()

    if override is False:
        return _result("Summarization disabled for this run; sources used as-is.")

    if settings.use_llm_summarizer:
        method = "llm"
    elif settings.use_extractive_summarizer or override is True:
        # override True forces the extractive path on even if settings default off
        method = "extractive"
    else:
        return _result("Summarization is disabled; sources will be used as-is.")

    search_results_dicts = state.get("search_results", [])
    if not search_results_dicts:
        return _result("No search results to compress.")

    results = [SearchResult.model_validate(r) for r in search_results_dicts]
    query = state.get("query", "")

    # Collect the not-yet-compressed sources with usable content.
    pending = [
        r for r in results
        if r.compressed_content is None and (r.raw_content or r.content)
    ]

    if not pending:
        return _result(f"All {len(results)} sources already compressed.")

    if method == "llm":
        # LLM calls are independent — run them concurrently.
        summaries = await asyncio.gather(
            *(
                llm_summarize(
                    r.raw_content or r.content,
                    query,
                    token_budget=settings.extract_budget_map,
                )
                for r in pending
            )
        )
        for r, summary in zip(pending, summaries):
            r.compressed_content = summary or None
    else:
        # Extractive summarization is CPU-bound (embeddings) and synchronous —
        # run it in a worker thread so the event loop isn't blocked.
        def _summarize_all() -> list[str]:
            return [
                extractive_summarize(
                    r.raw_content or r.content,
                    query,
                    token_budget=settings.extract_budget_map,
                    embedding_model=settings.extract_embedding_model,
                    embedding_device=settings.extract_embedding_device,
                    selection_method=settings.extract_selection_method,
                )
                for r in pending
            ]

        summaries = await asyncio.to_thread(_summarize_all)
        for r, summary in zip(pending, summaries):
            r.compressed_content = summary or None

    def _chars(r: SearchResult) -> int:
        return len(r.raw_content or r.content or "")

    orig_chars = sum(_chars(r) for r in pending)
    comp_chars = sum(len(r.compressed_content or "") for r in pending)
    logger.info(
        "summarize_sources: compressed %d of %d sources (method=%s) — %d → %d chars (%.0f%% reduction)",
        len(pending), len(results), method, orig_chars, comp_chars,
        (100.0 * (1 - comp_chars / orig_chars)) if orig_chars else 0.0,
    )

    return _result(
        f"Compressed {len(pending)} of {len(results)} sources "
        f"({orig_chars:,} → {comp_chars:,} chars).",
        state_updates={"search_results": [r.model_dump() for r in results]},
    )


# ── Tool 3: evaluate_sources ───────────────────────────────────


@tool
async def evaluate_sources(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Evaluate and filter accumulated search results for relevance and quality.

    REQUIRES: web_search must be called before this tool.

    Operates on all search results collected so far. Scores each source on
    relevance, information density, and authority. Filters out low-quality
    sources.
    """
    search_results_dicts = state.get("search_results", [])
    if not search_results_dicts:
        return _result("No search results to evaluate.")

    results = [SearchResult.model_validate(r) for r in search_results_dicts]

    # Only evaluate sources not yet evaluated
    already_evaluated_urls = {
        e["search_result"]["url"] for e in state.get("evaluated_sources", [])
    }
    to_evaluate = [r for r in results if r.url not in already_evaluated_urls]

    if not to_evaluate:
        existing = state.get("evaluated_sources", [])
        kept = [e for e in existing if e.get("keep")]
        return _result(
            f"All sources already evaluated. {len(kept)} kept of {len(existing)} total."
        )

    llm = get_llm("evaluate_sources")
    query = state.get("query", "")
    batch_size = 10
    new_evaluated: list[dict] = []

    logger.info("evaluate_sources: %d new sources to evaluate in batches of %d", len(to_evaluate), batch_size)
    for batch_start in range(0, len(to_evaluate), batch_size):
        batch = to_evaluate[batch_start : batch_start + batch_size]
        logger.info("evaluate_sources: batch %d-%d of %d", batch_start + 1, batch_start + len(batch), len(to_evaluate))
        evaluated = await _evaluate_batch(llm, query, batch)
        new_evaluated.extend([e.model_dump() for e in evaluated])

    all_evaluated = state.get("evaluated_sources", []) + new_evaluated

    # Z-score normalization across all batches (Phase 3.1)
    scores = [e.get("relevance_score", 0.0) for e in all_evaluated]
    if len(scores) > 1:
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        std = variance ** 0.5
        if std > 0.05:  # Only normalize if there's meaningful variance
            for e in all_evaluated:
                raw = e.get("relevance_score", 0.0)
                z = (raw - mean) / std
                # Map z-score back to 0-1 range centered around 0.5
                normalized = max(0.0, min(1.0, 0.5 + z * 0.2))
                e["relevance_score"] = normalized
                e["keep"] = normalized >= settings.relevance_threshold
            logger.info(
                "evaluate_sources: z-score normalized %d sources (mean=%.2f, std=%.2f)",
                len(all_evaluated), mean, std,
            )

    kept = [e for e in all_evaluated if e.get("keep")]
    avg_relevance = sum(e.get("relevance_score", 0) for e in all_evaluated) / len(all_evaluated) if all_evaluated else 0
    logger.info("evaluate_sources: kept %d/%d sources, avg relevance=%.2f", len(kept), len(all_evaluated), avg_relevance)

    return _result(
        f"Evaluated {len(to_evaluate)} new sources. "
        f"Kept {len(kept)} of {len(all_evaluated)} total "
        f"(threshold: {settings.relevance_threshold}).",
        state_updates={"evaluated_sources": all_evaluated},
    )


# Domain-based authority heuristics (Phase 2.3)
_AUTHORITY_SCORES: dict[str, float] = {}
_HIGH_AUTHORITY_DOMAINS = {
    "nature.com", "science.org", "sciencedirect.com", "springer.com",
    "wiley.com", "ieee.org", "acm.org", "arxiv.org", "pubmed.ncbi.nlm.nih.gov",
    "scholar.google.com", "jstor.org", "ncbi.nlm.nih.gov",
    "gov", "edu",  # TLD matches
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com",
    "washingtonpost.com", "theguardian.com", "economist.com",
}
_LOW_AUTHORITY_DOMAINS = {
    "reddit.com", "quora.com", "linkedin.com", "facebook.com", "twitter.com",
    "x.com", "tiktok.com", "pinterest.com", "instagram.com",
    "medium.com", "blogspot.com", "wordpress.com",
}


def _domain_authority_score(url: str) -> float:
    """Return a heuristic authority score based on the URL's domain."""
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return 0.5
    hostname = hostname.lower().removeprefix("www.")
    tld = hostname.rsplit(".", 1)[-1] if "." in hostname else ""

    # Check exact domain matches
    for domain in _HIGH_AUTHORITY_DOMAINS:
        if hostname == domain or hostname.endswith(f".{domain}"):
            return 0.9
    if tld in ("gov", "edu"):
        return 0.9

    for domain in _LOW_AUTHORITY_DOMAINS:
        if hostname == domain or hostname.endswith(f".{domain}"):
            return 0.2

    return 0.5


async def _evaluate_batch(
    llm: LLMClient, query: str, results: list[SearchResult]
) -> list[EvaluatedSource]:
    """Evaluate a batch of search results (mirrors researcher._evaluate_batch)."""
    sources_text = ""
    for i, r in enumerate(results):
        # Prefer the pre-computed summary from summarize_sources when present;
        # this is what cuts the tokens (and Bedrock cost) sent to the evaluator.
        source_text = r.compressed_content if r.compressed_content is not None else (r.raw_content or r.content)
        chunks = _splitter.split_text(source_text) if source_text else []
        content = "\n\n".join(chunks[:3]) if chunks else (source_text or "")[:3000]
        sources_text += (
            f"\n---\nSOURCE {i}:\nTitle: {r.title}\nURL: {r.url}\nContent: {content}\n"
        )

    system_prompt = (
        "You are a research source evaluator. Given a research query and a batch of sources, "
        "evaluate each source on multiple criteria and extract key findings.\n\n"
        f"Research query: {query}\n\n"
        "For each source, evaluate:\n"
        "- relevance_score (0.0-1.0): How relevant is this to the research query?\n"
        "- information_density (0.0-1.0): Does it contain unique facts vs generic content?\n"
        "- source_authority (0.0-1.0): Is the domain/publication credible?\n"
        "- key_findings: List of specific factual findings\n"
        f"- keep: true if relevance_score >= {settings.relevance_threshold}\n\n"
        "CALIBRATION: A generic, barely-related source should score relevance ~0.2-0.3. "
        "A moderately relevant source scores ~0.5. A directly relevant, high-quality source "
        "scores ~0.7-0.9. Only score 0.9+ for perfect matches. Apply this consistently."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Evaluate these sources:{sources_text}"},
    ]

    data = await llm.chat_structured(messages, EvaluationResponse, caller="evaluate_sources")

    # ── Debug dump (input + output) ──────────────────────────────
    try:
        import json as _json, pathlib as _pathlib, time as _time
        _debug_dir = _pathlib.Path("results/eval_debug")
        _debug_dir.mkdir(parents=True, exist_ok=True)
        _ts = _time.strftime("%Y%m%d_%H%M%S")
        _slug = query[:40].replace(" ", "_").replace("/", "-")
        _fname = _debug_dir / f"{_ts}_{_slug}.json"
        _fname.write_text(_json.dumps({
            "query": query,
            "input": {
                "sources": [
                    {
                        "index": i,
                        "title": r.title,
                        "url": r.url,
                        "content_chars": len(r.raw_content or r.content or ""),
                        "raw_content": r.raw_content or r.content or "",
                    }
                    for i, r in enumerate(results)
                ],
                "system_prompt": system_prompt,
            },
            "output": {
                "evaluations": [
                    {
                        "source_index": e.source_index,
                        "relevance_score": e.relevance_score,
                        "information_density": e.information_density,
                        "source_authority": e.source_authority,
                        "keep": e.keep,
                        "key_findings": e.key_findings,
                    }
                    for e in (data.evaluations or [])
                ]
            },
        }, indent=2))
        logger.info("evaluate_sources debug dump: %s", _fname)
    except Exception as _exc:
        logger.warning("evaluate_sources debug dump failed: %s", _exc)
    # ── End debug dump ───────────────────────────────────────────

    evaluated: list[EvaluatedSource] = []
    evals_by_idx = {e.source_index: e for e in (data.evaluations or [])}

    for r_idx, r in enumerate(results):
        ev = evals_by_idx.get(r_idx)
        if ev:
            # Blend domain heuristic (60%) with LLM score (40%) for authority
            domain_score = _domain_authority_score(r.url)
            blended_authority = 0.6 * domain_score + 0.4 * ev.source_authority
            evaluated.append(
                EvaluatedSource(
                    search_result=r,
                    relevance_score=ev.relevance_score,
                    key_findings=ev.key_findings,
                    keep=ev.keep,
                    information_density=ev.information_density,
                    source_authority=blended_authority,
                )
            )
        else:
            evaluated.append(
                EvaluatedSource(search_result=r, relevance_score=0.0, keep=False)
            )

    avg_score = sum(e.relevance_score for e in evaluated) / len(evaluated) if evaluated else 0
    kept_count = sum(1 for e in evaluated if e.keep)
    logger.info("_evaluate_batch: %d sources, avg relevance=%.2f, kept=%d", len(evaluated), avg_score, kept_count)

    return evaluated


# ── Tool 3: analyze_gaps ───────────────────────────────────────


@tool
async def analyze_gaps(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Analyze knowledge gaps in current research and suggest follow-up searches.

    REQUIRES: evaluate_sources must be called before this tool.

    Examines the evaluated sources to identify what information is still
    missing. Returns suggested follow-up search queries for the agent to
    use with web_search.
    """
    evaluated_dicts = state.get("evaluated_sources", [])
    kept = [e for e in evaluated_dicts if e.get("keep")]

    if not kept:
        return _result("No kept sources to analyze gaps from. Run evaluate_sources first.")

    # Enforce the configured max search-evaluate-gaps cycles
    cycles = state.get("search_evaluate_cycles", 0)
    if cycles >= settings.max_research_cycles:
        return _result(
            f"Maximum research depth reached ({settings.max_research_cycles} "
            f"cycle{'s' if settings.max_research_cycles != 1 else ''}). Proceed to extract_claims."
        )

    logger.info("analyze_gaps: analyzing %d kept sources for knowledge gaps", len(kept))
    query = state.get("query", "")
    previous_queries = state.get("search_queries_history", [])
    findings_summary = "\n".join(
        f"- {e['search_result']['title']}: "
        f"{'; '.join(e.get('key_findings', [])[:2])}"
        for e in kept
    )

    # Inject previous queries into prompt so LLM avoids them (Phase 1.2)
    previous_queries_text = ""
    if previous_queries:
        previous_queries_text = (
            f"\n\nPrevious search queries already executed (DO NOT repeat these "
            f"or close variations):\n"
            + "\n".join(f"- {q}" for q in previous_queries)
        )

    llm = get_llm("analyze_gaps")
    messages = [
        {
            "role": "system",
            "content": (
                "You identify knowledge gaps and output follow-up search queries. "
                "Respond ONLY with a JSON object of the form "
                '{"follow_up_queries": [...]} containing 2-3 short query strings, '
                "each covering a genuinely different aspect not already searched. "
                "Output only the JSON object — no prose, no explanation."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original query: {query}\n\n"
                f"Existing findings:\n{findings_summary}"
                f"{previous_queries_text}\n\n"
                "Return 2-3 follow-up search queries as JSON now."
            ),
        },
    ]

    data = await llm.chat_structured(messages, GapAnalysisResponse, caller="analyze_gaps")
    follow_ups = (data.follow_up_queries or [])[:3]

    # Programmatic dedup: remove queries too similar to previous ones (Phase 1.2)
    deduped_follow_ups: list[str] = []
    for candidate in follow_ups:
        is_dup = False
        for prev in previous_queries:
            ratio = SequenceMatcher(None, candidate.lower(), prev.lower()).ratio()
            if ratio > 0.7:
                logger.info("analyze_gaps: filtered duplicate query '%s' (similar to '%s', ratio=%.2f)", candidate, prev, ratio)
                is_dup = True
                break
        if not is_dup:
            deduped_follow_ups.append(candidate)

    logger.info("analyze_gaps: generated %d follow-up queries (%d after dedup)", len(follow_ups), len(deduped_follow_ups))

    if not deduped_follow_ups:
        return _result("No significant knowledge gaps identified (all suggested queries already searched).")

    return _result(
        f"Identified {len(deduped_follow_ups)} knowledge gaps. "
        f"Suggested follow-up queries: {deduped_follow_ups}"
    )


# ── Tool 4: extract_claims ────────────────────────────────────


@tool
async def extract_claims(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Extract and cross-reference factual claims from evaluated sources.

    REQUIRES: evaluate_sources must be called before this tool.

    Analyzes kept sources to extract distinct factual claims, notes which
    sources support each claim, and assigns confidence scores based on
    corroboration.
    """
    evaluated_dicts = state.get("evaluated_sources", [])
    kept = [e for e in evaluated_dicts if e.get("keep")]

    if not kept:
        return _result("No kept sources to extract claims from.")

    logger.info("extract_claims: extracting claims from %d kept sources", len(kept))
    query = state.get("query", "")

    # Build overlap hints: detect sources with shared key terms (Phase 3.2)
    source_terms: list[set[str]] = []
    for e in kept:
        terms = set()
        for finding in e.get("key_findings", []):
            terms.update(w.lower() for w in finding.split() if len(w) > 4)
        source_terms.append(terms)

    overlap_hints = ""
    overlap_pairs: list[tuple[int, int]] = []
    for i in range(len(kept)):
        for j in range(i + 1, len(kept)):
            shared = source_terms[i] & source_terms[j]
            if len(shared) >= 3:
                overlap_pairs.append((i, j))
    if overlap_pairs:
        overlap_hints = "\n\nSources that likely discuss overlapping topics:\n"
        for i, j in overlap_pairs[:10]:
            overlap_hints += f"- Source [{i}] and [{j}]\n"

    findings_block = ""
    for i, e in enumerate(kept):
        findings_block += (
            f"\n[{i}] {e['search_result']['title']}\n"
            f"Findings: {'; '.join(e.get('key_findings', []))}\n"
        )

    llm = get_llm("extract_claims")
    messages = [
        {
            "role": "system",
            "content": (
                "You are a fact-checking assistant. Given a set of sources with "
                "their key findings, extract distinct factual claims.\n\n"
                "IMPORTANT - Multi-source attribution:\n"
                "- For EACH claim, check ALL sources to see which ones support it\n"
                "- Many claims will be supported by 2+ sources — list ALL of them in source_indices\n"
                "- Confidence scoring rules:\n"
                "  * 1 source only → confidence 0.3\n"
                "  * 2 sources → confidence 0.6\n"
                "  * 3+ sources → confidence 0.8-1.0\n"
                "- Do NOT create duplicate claims — if two sources say the same thing, "
                "merge them into ONE claim with BOTH source indices"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Research query: {query}\n\n"
                f"Sources and findings:{findings_block}"
                f"{overlap_hints}\n\n"
                "Extract the factual claims. For each claim, list ALL source indices that support it."
            ),
        },
    ]

    data = await llm.chat_structured(messages, ClaimExtractionResponse, caller="extract_claims")

    # Post-process: merge near-duplicate claims (Phase 3.2)
    merged_claims: list[FactualClaim] = []
    used: set[int] = set()
    claims_list = list(data.claims or [])
    for i, claim_a in enumerate(claims_list):
        if i in used:
            continue
        merged_indices = set(claim_a.source_indices)
        best_claim = claim_a.claim
        for j in range(i + 1, len(claims_list)):
            if j in used:
                continue
            ratio = SequenceMatcher(None, claim_a.claim.lower(), claims_list[j].claim.lower()).ratio()
            if ratio > 0.6:
                merged_indices.update(claims_list[j].source_indices)
                used.add(j)
                # Keep the longer claim text
                if len(claims_list[j].claim) > len(best_claim):
                    best_claim = claims_list[j].claim
        used.add(i)
        # Recalculate confidence based on source count
        n_sources = len(merged_indices)
        if n_sources >= 3:
            confidence = min(1.0, 0.8 + (n_sources - 3) * 0.1)
        elif n_sources == 2:
            confidence = 0.6
        else:
            confidence = 0.3
        merged_claims.append(FactualClaim(
            claim=best_claim,
            source_indices=sorted(merged_indices),
            confidence=confidence,
        ))

    claims_dicts = [c.model_dump() for c in merged_claims]

    multi_source = sum(1 for c in merged_claims if len(c.source_indices) > 1)
    single_source = len(merged_claims) - multi_source
    logger.info(
        "extract_claims: %d claims after merge (%d multi-source, %d single-source, %d raw from LLM)",
        len(merged_claims), multi_source, single_source, len(data.claims),
    )

    return _result(
        f"Extracted {len(merged_claims)} factual claims from {len(kept)} sources "
        f"({multi_source} multi-source, {single_source} single-source).",
        state_updates={"claims": claims_dicts},
    )


# ── Tool 5: write_report ──────────────────────────────────────


@tool
async def write_report(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Write the final research report using a map-reduce synthesis strategy.

    REQUIRES: evaluate_sources must be called before this tool.

    Summarizes each kept source individually (map phase), then synthesizes
    all summaries into a cohesive markdown report with inline citations
    (reduce phase).
    """
    evaluated_dicts = state.get("evaluated_sources", [])
    kept_dicts = [e for e in evaluated_dicts if e.get("keep")]

    if not kept_dicts:
        return _result("No kept sources to write a report from.")

    query = state.get("query", "")
    claims_dicts = state.get("claims", [])
    llm = get_llm("write_report")

    # Convert back to Pydantic for convenience
    kept = [EvaluatedSource.model_validate(e) for e in kept_dicts]
    kept = sorted(kept, key=lambda e: e.relevance_score, reverse=True)

    # Cap sources for map-reduce to prevent token explosion (Phase 7)
    if len(kept) > _MAX_REPORT_SOURCES:
        # Score by blended relevance + authority
        kept.sort(key=lambda e: e.relevance_score * 0.6 + e.source_authority * 0.4, reverse=True)
        logger.info(
            "write_report: capping sources from %d to %d (top by relevance*authority)",
            len(kept), _MAX_REPORT_SOURCES,
        )
        kept = kept[:_MAX_REPORT_SOURCES]

    claims = [FactualClaim.model_validate(c) for c in claims_dicts]

    logger.info("write_report: synthesizing report from %d sources and %d claims", len(kept), len(claims))

    # Phase A: Map -- summarize each source individually
    source_summaries = await _map_sources(llm, query, kept)
    logger.info("write_report: map phase complete, %d source summaries generated", len(source_summaries))

    # Phase B: Reduce -- synthesize final report
    report, citations = await _reduce_report(llm, query, kept, source_summaries, claims)
    logger.info("write_report: report generated, %d chars, %d citations", len(report), len(citations))

    citations_dicts = [c.model_dump() for c in citations]

    return _result(
        f"Report written: {len(report)} characters, {len(citations)} citations.",
        state_updates={
            "report_markdown": report,
            "citations": citations_dicts,
        },
    )


async def _map_sources(
    llm: LLMClient, query: str, kept: list[EvaluatedSource]
) -> list[str]:
    """Phase A: Summarize kept sources in batches of 3."""
    batch_size = 3
    all_summaries: list[str] = []

    for batch_start in range(0, len(kept), batch_size):
        batch = kept[batch_start : batch_start + batch_size]
        logger.info(
            "write_report map: summarizing sources %d-%d of %d (batch of %d)",
            batch_start + 1, batch_start + len(batch), len(kept), len(batch),
        )
        batch_summaries = await _summarize_source_batch(llm, query, batch)
        all_summaries.extend(batch_summaries)

    return all_summaries


async def _summarize_source_batch(
    llm: LLMClient, query: str, sources: list[EvaluatedSource]
) -> list[str]:
    """Summarize a batch of 1-3 sources in a single LLM call."""
    sources_block = ""
    for i, source in enumerate(sources):
        # Reuse the summarize_sources compression when present (same summary that
        # evaluate_sources used — computed once, stored on compressed_content).
        # This cuts the tokens (and Bedrock cost) of the report map phase too.
        cached = source.search_result.compressed_content
        if cached is not None:
            content = cached
        else:
            full_content = source.search_result.raw_content or source.search_result.content
            chunks = _splitter.split_text(full_content) if full_content else []
            content = "\n\n".join(chunks[:3]) if chunks else full_content[:2000]
        sources_block += (
            f"\n---\nSOURCE {i+1}: {source.search_result.title}\n"
            f"URL: {source.search_result.url}\n"
            f"Content:\n{content}\n"
        )

    messages = [
        {
            "role": "system",
            "content": (
                "You are a research analyst. Given source documents and a research query, "
                "produce a detailed structured summary for EACH source separately. "
                "For each source, extract:\n"
                "- Key claims and factual assertions\n"
                "- Supporting evidence and data points\n"
                "- How it relates to the research query\n\n"
                "Format: Start each summary with 'SOURCE N:' on its own line, followed by "
                "the summary. Be thorough but concise."
            ),
        },
        {
            "role": "user",
            "content": f"Research query: {query}\n\n{sources_block}",
        },
    ]

    combined = await llm.chat(messages, max_tokens=2048, caller="write_report:map")

    # Split combined response back into individual summaries
    import re as _re
    parts = _re.split(r'SOURCE\s+\d+:', combined)
    parts = [p.strip() for p in parts[1:] if p.strip()]

    # Pad or truncate to match input count
    summaries = []
    for i in range(len(sources)):
        if i < len(parts):
            summaries.append(parts[i])
        else:
            summaries.append(f"Summary unavailable for {sources[i].search_result.title}")

    return summaries


async def _reduce_report(
    llm: LLMClient,
    query: str,
    kept: list[EvaluatedSource],
    summaries: list[str],
    claims: list[FactualClaim],
) -> tuple[str, list[Citation]]:
    """Phase B: Synthesize final report from source summaries."""
    # Deduplicate kept sources by URL (Phase 2.1 — fixes duplicate reference bugs)
    seen_urls: set[str] = set()
    deduped_kept: list[EvaluatedSource] = []
    deduped_summaries: list[str] = []
    for e, summary in zip(kept, summaries):
        if e.search_result.url not in seen_urls:
            seen_urls.add(e.search_result.url)
            deduped_kept.append(e)
            deduped_summaries.append(summary)
    if len(deduped_kept) < len(kept):
        logger.info("_reduce_report: deduped %d -> %d sources by URL", len(kept), len(deduped_kept))
    kept = deduped_kept
    summaries = deduped_summaries

    # Sort by blended authority score (Phase 2.3) — higher-authority sources first
    paired = list(zip(kept, summaries))
    paired.sort(key=lambda x: x[0].source_authority, reverse=True)
    kept = [p[0] for p in paired]
    summaries = [p[1] for p in paired]

    sources_block = ""
    citations: list[Citation] = []
    for i, (e, summary) in enumerate(zip(kept, summaries), 1):
        sources_block += (
            f"\n[{i}] {e.search_result.title}\n"
            f"URL: {e.search_result.url}\n"
            f"Key findings: {'; '.join(e.key_findings)}\n"
            f"Summary: {summary}\n"
        )
        citations.append(
            Citation(number=i, title=e.search_result.title, url=e.search_result.url)
        )

    claims_block = ""
    if claims:
        single_source = [c for c in claims if len(c.source_indices) == 1]
        multi_source = [c for c in claims if len(c.source_indices) > 1]
        if multi_source:
            claims_block += "\nWell-corroborated claims:\n"
            for c in multi_source[:10]:
                claims_block += f"- {c.claim} (sources: {c.source_indices})\n"
        if single_source:
            claims_block += "\nSingle-source claims (less certain):\n"
            for c in single_source[:5]:
                claims_block += f"- {c.claim} (source: {c.source_indices})\n"

    # Phase 2.2: Two-pass outline-first approach
    # Pass 1: Generate section outline
    outline_prompt = (
        "You are a research report planner. Given a research query and source summaries, "
        "produce a section outline for a comprehensive report.\n\n"
        "Requirements:\n"
        "- 4-6 sections with ## headings\n"
        "- Each section must cover a DISTINCT aspect (no overlap)\n"
        "- Include a brief 1-sentence description of what each section covers\n"
        "- Return ONLY the outline, no other text\n\n"
        "Format:\n## Section Title\nBrief description of what this section covers.\n"
    )
    outline_messages = [
        {"role": "system", "content": outline_prompt},
        {"role": "user", "content": f"Research query: {query}\n\nSources:\n{sources_block}"},
    ]
    outline = await llm.chat(outline_messages, max_tokens=1024, caller="write_report:outline")
    logger.info("_reduce_report: generated outline (%d chars)", len(outline))

    # Pass 2: Generate full report following outline
    system_prompt = (
        "You are an expert research report writer. Write a comprehensive, "
        "well-structured markdown report based on the provided source summaries.\n\n"
        "STRUCTURE REQUIREMENTS:\n"
        "1. A clear title (# heading)\n"
        "2. An executive summary (2-3 sentences)\n"
        "3. Follow the section outline provided below EXACTLY\n"
        "4. Inline citations using [N] notation (e.g., [1], [2])\n\n"
        "CRITICAL RULES:\n"
        "- Do NOT write a References section — it will be appended automatically\n"
        "- Each section must cover a DISTINCT aspect — do NOT repeat information across sections\n"
        "- Use only information from the provided sources\n"
        "- When claims are supported by multiple sources, note this\n"
        "- Be thorough — aim for detailed analysis, not just bullet points"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Research query: {query}\n\n"
                f"Section outline to follow:\n{outline}\n\n"
                f"Sources:\n{sources_block}\n"
                f"{claims_block}\n"
                "Write the research report following the outline. Do NOT include a References section."
            ),
        },
    ]

    report = await llm.chat(messages, max_tokens=settings.report_max_tokens, caller="write_report:reduce")

    # Strip any hallucinated References section (Phase 2.1)
    report = re.sub(
        r'\n##\s*References?\s*\n.*',
        '',
        report,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Programmatically append correct references (Phase 2.1)
    references_section = "\n\n## References\n\n"
    for c in citations:
        references_section += f"[{c.number}] {c.title} — {c.url}\n"
    report = report.rstrip() + references_section

    return report, citations


# ── Public helpers for pipeline_recovery_node ─────────────────
# These expose the core logic of each tool without the @tool/InjectedState
# wrapper so that pipeline_recovery_node in graph.py can call them directly.


async def run_evaluate_sources_core(
    query: str,
    search_results_dicts: list[dict],
    already_evaluated_dicts: list[dict],
) -> list[dict]:
    """Core evaluate_sources logic (no @tool wrapper)."""
    results = [SearchResult.model_validate(r) for r in search_results_dicts]
    already_evaluated_urls = {e["search_result"]["url"] for e in already_evaluated_dicts}
    to_evaluate = [r for r in results if r.url not in already_evaluated_urls]

    if not to_evaluate:
        return already_evaluated_dicts

    llm = get_llm("evaluate_sources")
    batch_size = 10
    new_evaluated: list[dict] = []

    for batch_start in range(0, len(to_evaluate), batch_size):
        batch = to_evaluate[batch_start : batch_start + batch_size]
        evaluated = await _evaluate_batch(llm, query, batch)
        new_evaluated.extend([e.model_dump() for e in evaluated])

    all_evaluated = already_evaluated_dicts + new_evaluated

    # Z-score normalization (same logic as evaluate_sources @tool)
    scores = [e.get("relevance_score", 0.0) for e in all_evaluated]
    if len(scores) > 1:
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        std = variance ** 0.5
        if std > 0.05:
            for e in all_evaluated:
                raw = e.get("relevance_score", 0.0)
                z = (raw - mean) / std
                normalized = max(0.0, min(1.0, 0.5 + z * 0.2))
                e["relevance_score"] = normalized
                e["keep"] = normalized >= settings.relevance_threshold

    kept_count = sum(1 for e in all_evaluated if e.get("keep"))
    logger.info(
        "run_evaluate_sources_core: kept %d/%d sources",
        kept_count, len(all_evaluated),
    )
    return all_evaluated


async def run_extract_claims_core(
    query: str,
    evaluated_dicts: list[dict],
) -> list[dict]:
    """Core extract_claims logic (no @tool wrapper)."""
    kept = [e for e in evaluated_dicts if e.get("keep")]
    if not kept:
        return []

    # Build overlap hints
    source_terms: list[set[str]] = []
    for e in kept:
        terms = set()
        for finding in e.get("key_findings", []):
            terms.update(w.lower() for w in finding.split() if len(w) > 4)
        source_terms.append(terms)

    overlap_pairs: list[tuple[int, int]] = []
    for i in range(len(kept)):
        for j in range(i + 1, len(kept)):
            shared = source_terms[i] & source_terms[j]
            if len(shared) >= 3:
                overlap_pairs.append((i, j))

    overlap_hints = ""
    if overlap_pairs:
        overlap_hints = "\n\nSources that likely discuss overlapping topics:\n"
        for i, j in overlap_pairs[:10]:
            overlap_hints += f"- Source [{i}] and [{j}]\n"

    findings_block = ""
    for i, e in enumerate(kept):
        findings_block += (
            f"\n[{i}] {e['search_result']['title']}\n"
            f"Findings: {'; '.join(e.get('key_findings', []))}\n"
        )

    llm = get_llm("extract_claims")
    messages = [
        {
            "role": "system",
            "content": (
                "You are a fact-checking assistant. Given a set of sources with "
                "their key findings, extract distinct factual claims.\n\n"
                "IMPORTANT - Multi-source attribution:\n"
                "- For EACH claim, check ALL sources to see which ones support it\n"
                "- Many claims will be supported by 2+ sources — list ALL of them in source_indices\n"
                "- Confidence scoring rules:\n"
                "  * 1 source only → confidence 0.3\n"
                "  * 2 sources → confidence 0.6\n"
                "  * 3+ sources → confidence 0.8-1.0\n"
                "- Do NOT create duplicate claims — if two sources say the same thing, "
                "merge them into ONE claim with BOTH source indices"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Research query: {query}\n\n"
                f"Sources and findings:{findings_block}"
                f"{overlap_hints}\n\n"
                "Extract the factual claims. For each claim, list ALL source indices that support it."
            ),
        },
    ]

    data = await llm.chat_structured(messages, ClaimExtractionResponse, caller="extract_claims")

    # Merge near-duplicate claims (same logic as extract_claims @tool)
    merged_claims: list[FactualClaim] = []
    used: set[int] = set()
    claims_list = list(data.claims or [])
    for i, claim_a in enumerate(claims_list):
        if i in used:
            continue
        merged_indices = set(claim_a.source_indices)
        best_claim = claim_a.claim
        for j in range(i + 1, len(claims_list)):
            if j in used:
                continue
            ratio = SequenceMatcher(None, claim_a.claim.lower(), claims_list[j].claim.lower()).ratio()
            if ratio > 0.6:
                merged_indices.update(claims_list[j].source_indices)
                used.add(j)
                if len(claims_list[j].claim) > len(best_claim):
                    best_claim = claims_list[j].claim
        used.add(i)
        n_sources = len(merged_indices)
        if n_sources >= 3:
            confidence = min(1.0, 0.8 + (n_sources - 3) * 0.1)
        elif n_sources == 2:
            confidence = 0.6
        else:
            confidence = 0.3
        merged_claims.append(FactualClaim(
            claim=best_claim,
            source_indices=sorted(merged_indices),
            confidence=confidence,
        ))

    logger.info("run_extract_claims_core: %d claims after merge", len(merged_claims))
    return [c.model_dump() for c in merged_claims]


async def run_write_report_core(
    query: str,
    evaluated_dicts: list[dict],
    claims_dicts: list[dict],
) -> tuple[str, list[dict]]:
    """Core write_report logic (no @tool wrapper)."""
    kept_dicts = [e for e in evaluated_dicts if e.get("keep")]
    if not kept_dicts:
        return "", []

    llm = get_llm("write_report")
    kept = [EvaluatedSource.model_validate(e) for e in kept_dicts]
    kept = sorted(kept, key=lambda e: e.relevance_score, reverse=True)

    if len(kept) > _MAX_REPORT_SOURCES:
        kept.sort(key=lambda e: e.relevance_score * 0.6 + e.source_authority * 0.4, reverse=True)
        logger.info(
            "run_write_report_core: capping sources from %d to %d",
            len(kept_dicts), _MAX_REPORT_SOURCES,
        )
        kept = kept[:_MAX_REPORT_SOURCES]

    claims = [FactualClaim.model_validate(c) for c in claims_dicts]

    source_summaries = await _map_sources(llm, query, kept)
    report, citations = await _reduce_report(llm, query, kept, source_summaries, claims)

    logger.info(
        "run_write_report_core: report %d chars, %d citations",
        len(report), len(citations),
    )
    return report, [c.model_dump() for c in citations]


# ── Exported tool list ─────────────────────────────────────────

TOOLS = [web_search, summarize_sources, evaluate_sources, analyze_gaps, extract_claims, write_report]
