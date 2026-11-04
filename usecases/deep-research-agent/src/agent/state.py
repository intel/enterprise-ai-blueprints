# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class ResearchState(TypedDict):
    """Graph state for the research agent.

    State fields store plain dicts (not Pydantic models) for LangGraph
    serialization compatibility.  Converted to/from Pydantic models inside
    tool functions.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    research_id: str

    # Accumulated research artifacts
    search_results: list[dict]       # raw SearchResult dicts
    evaluated_sources: list[dict]    # raw EvaluatedSource dicts
    claims: list[dict]               # raw FactualClaim dicts
    citations: list[dict]            # raw Citation dicts
    report_markdown: str

    # Counters for circuit breaker
    search_count: int
    iteration_count: int

    # Tool ordering guardrails (Phase 1.1)
    completed_tools: list[str]

    # Search dedup history (Phase 1.2)
    search_queries_history: list[str]

    # Auto-recovery: consecutive prerequisite blocks (Phase 4.2)
    consecutive_blocked: int

    # Query classification (Phase 7)
    query_category: int  # 1, 2, or 3

    # Research cycle counter (Phase 7)
    search_evaluate_cycles: int
