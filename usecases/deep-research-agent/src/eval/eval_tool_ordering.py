# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Evaluation script: run 10 test queries and check tool-ordering behavior.

Measures:
- Whether each query produces a report (or direct answer for simple queries)
- How many prerequisite blocks occurred
- Whether auto-recovery fired
- Whether the circuit breaker was hit
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid

# Allow imports from project root when running from eval/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import structlog
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.graph import build_graph
from db import init_db
from models import ProgressEvent, ResearchStatus
from token_tracker import current_research_id, tracker

# 10 diverse test queries: mix of research (full pipeline) and quick lookups
TEST_QUERIES = [
    # Research queries (should use full pipeline)
    "What are the environmental impacts of lithium mining?",
    "Compare React vs Vue for enterprise applications",
    "What are the latest breakthroughs in quantum computing?",
    "How does intermittent fasting affect metabolic health?",
    "What are the economic effects of remote work on cities?",
    "Explain the current state of nuclear fusion energy research",
    "What are the pros and cons of universal basic income?",
    # Quick lookups (should NOT trigger full pipeline)
    "What is the current population of Tokyo?",
    "Who won the most recent Nobel Prize in Physics?",
    "What programming language is used to build Linux?",
]


def setup_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
        force=True,
    )


async def run_single_query(query: str, query_idx: int) -> dict:
    """Run a single query and return metrics."""
    research_id = uuid.uuid4().hex
    current_research_id.set(research_id)

    graph = build_graph()

    initial_state = {
        "messages": [HumanMessage(content=query)],
        "query": query,
        "research_id": research_id,
        "search_results": [],
        "evaluated_sources": [],
        "claims": [],
        "citations": [],
        "report_markdown": "",
        "search_count": 0,
        "iteration_count": 0,
        "completed_tools": [],
        "search_queries_history": [],
        "consecutive_blocked": 0,
    }

    config = {
        "configurable": {
            "progress_callback": None,
        },
    }

    start = time.time()
    try:
        final_state = await graph.ainvoke(initial_state, config=config)
    except Exception as exc:
        return {
            "query_idx": query_idx,
            "query": query,
            "success": False,
            "error": str(exc),
            "elapsed_s": time.time() - start,
        }
    elapsed = time.time() - start

    # Analyze results
    report = final_state.get("report_markdown", "")
    messages = final_state.get("messages", [])
    iteration_count = final_state.get("iteration_count", 0)
    completed_tools = final_state.get("completed_tools", [])

    # Count blocks and auto-recoveries from message content
    block_count = 0
    auto_recovery_count = 0
    for msg in messages:
        if isinstance(msg, ToolMessage):
            content = msg.content or ""
            if "STOP. You cannot call" in content:
                block_count += 1
            if "AUTO-RECOVERY" in content:
                auto_recovery_count += 1

    # Did it produce a final answer?
    has_report = bool(report and len(report) > 50)
    has_direct_answer = False
    if not has_report:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
                if len(msg.content) > 20:
                    has_direct_answer = True
                    break

    circuit_breaker_hit = iteration_count >= 15  # matches agent_max_iterations

    usage = tracker.get(research_id)

    return {
        "query_idx": query_idx,
        "query": query,
        "success": has_report or has_direct_answer,
        "has_report": has_report,
        "has_direct_answer": has_direct_answer,
        "report_len": len(report),
        "iterations": iteration_count,
        "completed_tools": completed_tools,
        "block_count": block_count,
        "auto_recovery_count": auto_recovery_count,
        "circuit_breaker_hit": circuit_breaker_hit,
        "elapsed_s": round(elapsed, 1),
        "total_tokens": usage.total_tokens,
        "llm_calls": usage.call_count,
    }


async def main() -> None:
    await init_db()

    results = []
    for i, query in enumerate(TEST_QUERIES):
        print(f"\n{'='*80}", file=sys.stderr)
        print(f"[{i+1}/{len(TEST_QUERIES)}] Running: {query}", file=sys.stderr)
        print(f"{'='*80}", file=sys.stderr)

        result = await run_single_query(query, i)
        results.append(result)

        status = "OK" if result["success"] else "FAIL"
        blocks = result.get("block_count", "?")
        recoveries = result.get("auto_recovery_count", "?")
        iters = result.get("iterations", "?")
        cb = " [CIRCUIT BREAKER]" if result.get("circuit_breaker_hit") else ""
        print(
            f"  -> {status} | iters={iters} | blocks={blocks} | "
            f"auto_recoveries={recoveries} | {result.get('elapsed_s', '?')}s{cb}",
            file=sys.stderr,
        )

    # Summary
    print(f"\n\n{'='*80}", file=sys.stderr)
    print("EVALUATION SUMMARY", file=sys.stderr)
    print(f"{'='*80}", file=sys.stderr)

    total = len(results)
    successes = sum(1 for r in results if r["success"])
    failures = sum(1 for r in results if not r["success"])
    cb_hits = sum(1 for r in results if r.get("circuit_breaker_hit"))
    total_blocks = sum(r.get("block_count", 0) for r in results)
    total_recoveries = sum(r.get("auto_recovery_count", 0) for r in results)
    total_tokens = sum(r.get("total_tokens", 0) for r in results)

    print(f"Queries:           {total}", file=sys.stderr)
    print(f"Successes:         {successes}/{total}", file=sys.stderr)
    print(f"Failures:          {failures}/{total}", file=sys.stderr)
    print(f"Circuit breakers:  {cb_hits}/{total}", file=sys.stderr)
    print(f"Total blocks:      {total_blocks}", file=sys.stderr)
    print(f"Auto-recoveries:   {total_recoveries}", file=sys.stderr)
    print(f"Total tokens:      {total_tokens}", file=sys.stderr)
    print(f"{'='*80}\n", file=sys.stderr)

    # Print detailed JSON to stdout
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
