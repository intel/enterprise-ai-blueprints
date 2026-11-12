# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tests for graph routing logic."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.graph import classify_router, should_continue


def _make_state(**overrides):
    base = {
        "messages": [HumanMessage(content="test")],
        "query": "test",
        "research_id": "test123",
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
        "search_evaluate_cycles": 0,
        "query_category": 3,
    }
    base.update(overrides)
    return base


def test_classify_router_cat1():
    state = _make_state(query_category=1)
    assert classify_router(state) == "cat1"


def test_classify_router_cat2():
    state = _make_state(query_category=2)
    assert classify_router(state) == "cat2_search"


def test_classify_router_cat3():
    state = _make_state(query_category=3)
    assert classify_router(state) == "agent"


def test_classify_router_default_is_cat3():
    state = _make_state()
    del state["query_category"]
    assert classify_router(state) == "agent"


def test_should_continue_circuit_breaker():
    state = _make_state(iteration_count=15)
    assert should_continue(state) == "fallback"


def test_should_continue_with_tool_calls():
    msg = AIMessage(content="", tool_calls=[{"name": "web_search", "args": {}, "id": "1", "type": "tool_call"}])
    state = _make_state(messages=[HumanMessage(content="test"), msg], iteration_count=1)
    assert should_continue(state) == "tools"


def test_should_continue_no_tool_calls():
    msg = AIMessage(content="Here is the answer")
    state = _make_state(messages=[HumanMessage(content="test"), msg], iteration_count=1)
    assert should_continue(state) == "fallback"
