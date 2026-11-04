# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from langchain_text_splitters import MarkdownTextSplitter

from config import settings
from llm_client import get_llm
from models import (
    ClaimExtractionResponse,
    DecompositionResponse,
    EvaluatedSource,
    EvaluationResponse,
    FactualClaim,
    GapAnalysisResponse,
    MapSummaryResponse,
    ProgressEvent,
    QueryDecomposition,
    ResearchSession,
    ResearchStatus,
    SearchResult,
    SubQuestion,
    Citation,
)
from search_providers import create_search_provider

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]

_splitter = MarkdownTextSplitter(chunk_size=4000, chunk_overlap=200)


class ResearchOrchestrator:
    def __init__(self, session: ResearchSession, on_progress: ProgressCallback) -> None:
        self.session = session
        self.on_progress = on_progress
        self.search = create_search_provider()

    async def _emit(
        self, status: ResearchStatus, message: str, detail: str = "", progress_pct: float = 0.0
    ) -> None:
        self.session.status = status
        event = ProgressEvent(
            research_id=self.session.id,
            status=status,
            message=message,
            detail=detail,
            progress_pct=progress_pct,
        )
        await self.on_progress(event)

    # ── Stage 1: Query Decomposition ──────────────────────────────

    async def decompose_query(self) -> QueryDecomposition:
        await self._emit(ResearchStatus.DECOMPOSING, "Analyzing query and generating sub-questions...", progress_pct=5)

        system_prompt = (
            "You are a research planning assistant. Given a user's research query, "
            "break it down into 3-5 focused sub-questions that together would comprehensively "
            "answer the original query. Each sub-question should explore a different perspective:\n"
            "- factual: basic facts and definitions\n"
            "- causal: causes, mechanisms, reasons\n"
            "- comparative: comparisons, alternatives, trade-offs\n"
            "- current-state: latest developments, current status\n"
            "- implications: consequences, future outlook, impact\n\n"
            "For each sub-question, provide 1-2 concrete web search queries."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Research query: {self.session.query}"},
        ]

        llm = get_llm("decompose_query")
        data = await llm.chat_structured(messages, DecompositionResponse)

        sub_questions = []
        for sq in data.sub_questions[:settings.max_sub_questions]:
            sub_questions.append(
                SubQuestion(
                    question=sq.question,
                    search_queries=sq.search_queries,
                    perspective=sq.perspective,
                )
            )

        decomposition = QueryDecomposition(
            main_intent=data.main_intent,
            sub_questions=sub_questions,
            background_context=data.background_context,
        )
        self.session.decomposition = decomposition

        detail = "\n".join(f"  - [{sq.perspective}] {sq.question}" for sq in sub_questions)
        await self._emit(
            ResearchStatus.DECOMPOSING,
            f"Decomposed into {len(sub_questions)} sub-questions",
            detail=detail,
            progress_pct=15,
        )
        return decomposition

    # ── Stage 2: Parallel Search ──────────────────────────────────

    async def parallel_search(self, decomposition: QueryDecomposition) -> list[SearchResult]:
        await self._emit(ResearchStatus.SEARCHING, "Launching parallel web searches...", progress_pct=20)

        tasks = []
        for sq in decomposition.sub_questions:
            for query in sq.search_queries:
                tasks.append(self.search.search(query, sub_question_id=sq.id))

        all_results_nested = await asyncio.gather(*tasks)
        all_results: list[SearchResult] = []
        for batch in all_results_nested:
            all_results.extend(batch)

        # Deduplicate by URL, keeping highest score
        seen: dict[str, SearchResult] = {}
        for r in all_results:
            if r.url not in seen or r.score > seen[r.url].score:
                seen[r.url] = r
        deduped = list(seen.values())

        self.session.search_results = deduped
        await self._emit(
            ResearchStatus.SEARCHING,
            f"Found {len(deduped)} unique sources from {len(tasks)} searches",
            progress_pct=40,
        )
        return deduped

    # ── Stage 3: Source Evaluation ────────────────────────────────

    async def evaluate_sources(self, results: list[SearchResult]) -> list[EvaluatedSource]:
        await self._emit(ResearchStatus.EVALUATING, "Evaluating source relevance...", progress_pct=45)

        if not results:
            return []

        batch_size = 10
        all_evaluated: list[EvaluatedSource] = []

        for batch_start in range(0, len(results), batch_size):
            batch = results[batch_start : batch_start + batch_size]
            evaluated = await self._evaluate_batch(batch)
            all_evaluated.extend(evaluated)

        kept = [e for e in all_evaluated if e.keep]
        self.session.evaluated_sources = all_evaluated

        await self._emit(
            ResearchStatus.EVALUATING,
            f"Kept {len(kept)} of {len(all_evaluated)} sources (threshold: {settings.relevance_threshold})",
            progress_pct=55,
        )
        return all_evaluated

    async def _evaluate_batch(self, results: list[SearchResult]) -> list[EvaluatedSource]:
        sources_text = ""
        for i, r in enumerate(results):
            # Use first 2-3 chunks instead of truncating to 1500 chars
            full_content = r.raw_content or r.content
            chunks = _splitter.split_text(full_content) if full_content else []
            content = "\n\n".join(chunks[:3]) if chunks else full_content[:3000]
            sources_text += f"\n---\nSOURCE {i}:\nTitle: {r.title}\nURL: {r.url}\nContent: {content}\n"

        system_prompt = (
            "You are a research source evaluator. Given a research query and a batch of sources, "
            "evaluate each source on multiple criteria and extract key findings.\n\n"
            f"Research query: {self.session.query}\n\n"
            "For each source, evaluate:\n"
            "- relevance_score (0.0-1.0): How relevant is this to the research query?\n"
            "- information_density (0.0-1.0): Does it contain unique facts vs generic content?\n"
            "- source_authority (0.0-1.0): Is the domain/publication credible?\n"
            "- key_findings: List of specific factual findings\n"
            f"- keep: true if relevance_score >= {settings.relevance_threshold}\n"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Evaluate these sources:{sources_text}"},
        ]

        llm = get_llm("evaluate_sources")
        data = await llm.chat_structured(messages, EvaluationResponse)

        evaluated: list[EvaluatedSource] = []
        evals_by_idx = {e.source_index: e for e in data.evaluations}

        for r_idx, r in enumerate(results):
            ev = evals_by_idx.get(r_idx)
            if ev:
                evaluated.append(
                    EvaluatedSource(
                        search_result=r,
                        relevance_score=ev.relevance_score,
                        key_findings=ev.key_findings,
                        keep=ev.keep,
                        information_density=ev.information_density,
                        source_authority=ev.source_authority,
                    )
                )
            else:
                evaluated.append(
                    EvaluatedSource(search_result=r, relevance_score=0.0, keep=False)
                )

        return evaluated

    # ── Stage 4: Iterative Deepening ──────────────────────────────

    async def iterative_deepening(self, evaluated: list[EvaluatedSource]) -> list[EvaluatedSource]:
        kept = [e for e in evaluated if e.keep]

        for iteration in range(settings.max_iterations):
            if len(kept) >= settings.min_sources_for_report:
                break

            await self._emit(
                ResearchStatus.DEEPENING,
                f"Iteration {iteration + 1}: only {len(kept)} sources, searching for more...",
                progress_pct=60 + iteration * 5,
            )

            findings_summary = "\n".join(
                f"- {e.search_result.title}: {'; '.join(e.key_findings[:2])}" for e in kept
            )

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a research assistant. Based on the original query and existing findings, "
                        "identify knowledge gaps and generate 2-3 new search queries to fill them."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Original query: {self.session.query}\n\n"
                        f"Existing findings:\n{findings_summary}\n\n"
                        "What gaps remain? Provide follow-up search queries."
                    ),
                },
            ]

            llm = get_llm("analyze_gaps")
            data = await llm.chat_structured(messages, GapAnalysisResponse)
            follow_up_queries = data.follow_up_queries[:3]

            if not follow_up_queries:
                break

            tasks = [self.search.search(q) for q in follow_up_queries]
            new_results_nested = await asyncio.gather(*tasks)
            new_results: list[SearchResult] = []
            for batch in new_results_nested:
                new_results.extend(batch)

            existing_urls = {e.search_result.url for e in evaluated}
            new_results = [r for r in new_results if r.url not in existing_urls]

            if not new_results:
                break

            new_evaluated = await self._evaluate_batch(new_results)
            evaluated.extend(new_evaluated)
            kept = [e for e in evaluated if e.keep]

        self.session.evaluated_sources = evaluated
        await self._emit(
            ResearchStatus.DEEPENING,
            f"Final source count: {len(kept)} kept sources",
            progress_pct=70,
        )
        return evaluated

    # ── Stage 4.5: Cross-source claim extraction ──────────────────

    async def extract_claims(self, evaluated: list[EvaluatedSource]) -> list[FactualClaim]:
        kept = [e for e in evaluated if e.keep]
        if not kept:
            return []

        await self._emit(
            ResearchStatus.DEEPENING,
            "Extracting and cross-referencing claims...",
            progress_pct=72,
        )

        findings_block = ""
        for i, e in enumerate(kept):
            findings_block += (
                f"\n[{i}] {e.search_result.title}\n"
                f"Findings: {'; '.join(e.key_findings)}\n"
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a fact-checking assistant. Given a set of sources with their key findings, "
                    "extract distinct factual claims and note which source indices support each claim. "
                    "Assign a confidence score (0.0-1.0) based on how many sources corroborate the claim."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Research query: {self.session.query}\n\n"
                    f"Sources and findings:{findings_block}\n\n"
                    "Extract the factual claims with source attribution."
                ),
            },
        ]

        llm = get_llm("extract_claims")
        data = await llm.chat_structured(messages, ClaimExtractionResponse)
        self.session.claims = data.claims
        return data.claims

    # ── Stage 5: Map-Reduce Report Synthesis ──────────────────────

    async def synthesize_report(self, evaluated: list[EvaluatedSource]) -> str:
        await self._emit(ResearchStatus.SYNTHESIZING, "Generating research report (map phase)...", progress_pct=75)

        kept = [e for e in evaluated if e.keep]
        kept = sorted(kept, key=lambda e: e.relevance_score, reverse=True)

        # Phase A: Map -- summarize each source individually
        source_summaries = await self._map_sources(kept)

        await self._emit(
            ResearchStatus.SYNTHESIZING,
            f"Synthesizing final report from {len(kept)} source summaries...",
            progress_pct=85,
        )

        # Phase B: Reduce -- synthesize final report from summaries
        report = await self._reduce_report(kept, source_summaries)

        self.session.report_markdown = report

        await self._emit(
            ResearchStatus.SYNTHESIZING,
            "Report generated",
            detail=f"{len(report)} characters, {len(self.session.citations)} citations",
            progress_pct=95,
        )
        return report

    async def _map_sources(self, kept: list[EvaluatedSource]) -> list[str]:
        """Phase A: Generate a structured summary for each kept source."""
        # Process in batches of 5 concurrently to stay within vLLM limits
        batch_size = 5
        all_summaries: list[str] = [""] * len(kept)

        for batch_start in range(0, len(kept), batch_size):
            batch = kept[batch_start : batch_start + batch_size]
            tasks = []
            for e in batch:
                tasks.append(self._summarize_single_source(e))

            results = await asyncio.gather(*tasks)
            for i, summary in enumerate(results):
                all_summaries[batch_start + i] = summary

        return all_summaries

    async def _summarize_single_source(self, source: EvaluatedSource) -> str:
        """Summarize a single source, using chunked content."""
        full_content = source.search_result.raw_content or source.search_result.content
        chunks = _splitter.split_text(full_content) if full_content else []
        # Use up to 5 chunks (about 20K chars) for the summary
        content = "\n\n".join(chunks[:5]) if chunks else full_content[:10000]

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a research analyst. Given a source document and a research query, "
                    "produce a detailed structured summary. Extract:\n"
                    "- Key claims and factual assertions\n"
                    "- Supporting evidence and data points\n"
                    "- How it relates to the research query\n\n"
                    "Be thorough -- capture all relevant information. Write in plain text."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Research query: {self.session.query}\n\n"
                    f"Source: {source.search_result.title}\n"
                    f"URL: {source.search_result.url}\n\n"
                    f"Content:\n{content}"
                ),
            },
        ]

        llm = get_llm("synthesize_report")
        return await llm.chat(messages, max_tokens=2048)

    async def _reduce_report(
        self, kept: list[EvaluatedSource], summaries: list[str]
    ) -> str:
        """Phase B: Synthesize final report from all source summaries."""
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

        self.session.citations = citations

        # Include claims if available for better report structure
        claims_block = ""
        if self.session.claims:
            single_source = [c for c in self.session.claims if len(c.source_indices) == 1]
            multi_source = [c for c in self.session.claims if len(c.source_indices) > 1]
            if multi_source:
                claims_block += "\nWell-corroborated claims:\n"
                for c in multi_source[:10]:
                    claims_block += f"- {c.claim} (sources: {c.source_indices})\n"
            if single_source:
                claims_block += "\nSingle-source claims (less certain):\n"
                for c in single_source[:5]:
                    claims_block += f"- {c.claim} (source: {c.source_indices})\n"

        system_prompt = (
            "You are an expert research report writer. Write a comprehensive, well-structured "
            "markdown report based on the provided source summaries. Include:\n\n"
            "1. A clear title (# heading)\n"
            "2. An executive summary (2-3 sentences)\n"
            "3. Multiple sections with ## headings covering different aspects\n"
            "4. Inline citations using [N] notation (e.g., [1], [2])\n"
            "5. A ## References section at the end listing all cited sources\n\n"
            "Use only information from the provided sources. Be thorough but concise. "
            "When claims are supported by multiple sources, note this. "
            "Flag single-source claims where appropriate."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Research query: {self.session.query}\n\n"
                    f"Sources:\n{sources_block}\n"
                    f"{claims_block}\n"
                    "Write the research report with inline citations."
                ),
            },
        ]

        llm = get_llm("synthesize_report")
        return await llm.chat(messages, max_tokens=settings.llm_max_tokens)

    # ── Main entry point ──────────────────────────────────────────

    async def run(self) -> ResearchSession:
        try:
            decomposition = await self.decompose_query()
            results = await self.parallel_search(decomposition)
            evaluated = await self.evaluate_sources(results)
            evaluated = await self.iterative_deepening(evaluated)
            await self.extract_claims(evaluated)
            await self.synthesize_report(evaluated)

            self.session.status = ResearchStatus.COMPLETE
            await self._emit(ResearchStatus.COMPLETE, "Research complete!", progress_pct=100)

        except Exception as exc:
            logger.exception("Research pipeline failed")
            self.session.status = ResearchStatus.FAILED
            self.session.error = str(exc)
            await self._emit(ResearchStatus.FAILED, f"Research failed: {exc}")

        return self.session
