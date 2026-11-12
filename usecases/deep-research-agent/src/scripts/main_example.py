# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""CLI entry point for deep research — run a query from the terminal.

Usage:
    python scripts/main_example.py --research_query what is the weather in London today?
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import uuid

# Allow imports from project root when running from scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import structlog
from langchain_core.messages import AIMessage, HumanMessage

from agent.graph import build_graph
from db import init_db
from models import ProgressEvent, ResearchStatus
from token_tracker import current_research_id, tracker


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


def print_progress(event: ProgressEvent) -> None:
    pct = f"{event.progress_pct:.0f}%" if event.progress_pct else ""
    status = event.status.value.upper()
    parts = [f"[{status}]"]
    if pct:
        parts.append(pct)
    parts.append(event.message)
    if event.detail:
        parts.append(f"— {event.detail}")
    print(" ".join(parts), file=sys.stderr)


async def run(query: str) -> None:
    await init_db()

    research_id = uuid.uuid4().hex
    current_research_id.set(research_id)

    async def progress_callback(event: ProgressEvent) -> None:
        print_progress(event)

    await progress_callback(
        ProgressEvent(
            research_id=research_id,
            status=ResearchStatus.THINKING,
            message="Agent is analyzing the query...",
            progress_pct=5.0,
        )
    )

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
        "search_evaluate_cycles": 0,
        "query_category": 0,
    }

    config = {
        "configurable": {
            "progress_callback": progress_callback,
        },
    }

    timeout_seconds = 1800  # 5-minute timeout for research

    start_time = time.monotonic()

    try:
        final_state = await asyncio.wait_for(
            graph.ainvoke(initial_state, config=config),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        await progress_callback(
            ProgressEvent(
                research_id=research_id,
                status=ResearchStatus.FAILED,
                message=f"Research timed out after {timeout_seconds}s",
                progress_pct=0.0,
            )
        )
        print(
            f"\nResearch timed out after {timeout_seconds}s. "
            "Try a more specific query or increase the timeout.",
            file=sys.stderr,
        )
        final_state = initial_state  # fallback to initial state

    # Extract the report
    report = final_state.get("report_markdown", "")
    if not report:
        messages = final_state.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
                report = msg.content
                break

    print_progress(
        ProgressEvent(
            research_id=research_id,
            status=ResearchStatus.COMPLETE,
            message="Research complete!",
            progress_pct=100.0,
        )
    )

    elapsed = time.monotonic() - start_time
    minutes, seconds = divmod(elapsed, 60)

    # Print token usage summary
    usage = tracker.get(research_id)
    print("\n" + "=" * 80, file=sys.stderr)
    print(
        f"TOKEN USAGE: {usage.input_tokens} input + {usage.output_tokens} output "
        f"= {usage.total_tokens} total ({usage.call_count} LLM calls)",
        file=sys.stderr,
    )
    print(f"TOTAL TIME: {int(minutes)}m {seconds:.1f}s", file=sys.stderr)
    print("=" * 80, file=sys.stderr)

    # Write live metrics for the dashboard
    try:
        tracker.write_metrics(
            research_id,
            query,
            final_state.get("query_category", 0),
            "complete" if report else "failed",
            elapsed,
            iterations=final_state.get("iteration_count", 0),
            source="cli",
            report_markdown=report or "",
        )
    except Exception as _e:
        print(f"[metrics] Failed to write live metrics: {_e}", file=sys.stderr)

    # Print the final report to stdout
    print("\n" + "=" * 80)
    print("RESEARCH REPORT")
    print("=" * 80 + "\n")
    print(report)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a deep research query from the terminal."
    )
    parser.add_argument(
        "--research_query",
        nargs="+",
        required=True,
        help="The research query (multiple words are joined automatically).",
    )
    args = parser.parse_args()
    query = " ".join(args.research_query)

    setup_logging()
    # Emit structured markers for eval dashboard parsing
    from datetime import datetime, timezone as _tz, timedelta as _td
    _il_offset = _td(hours=2)  # IST = UTC+2 (UTC+3 during DST, using UTC+2 as baseline)
    _now_il = (datetime.now(_tz.utc) + _il_offset).strftime("%Y-%m-%d %H:%M")
    print(f"INPUT_SOURCE: cli", file=sys.stderr)
    print(f"QUERY_TIME_IL: {_now_il} IL", file=sys.stderr)
    print(f"Starting research: {query!r}\n", file=sys.stderr)
    asyncio.run(run(query))


if __name__ == "__main__":
    main()
