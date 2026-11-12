# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""LangGraph research agent — graph construction and entry point."""

import ast
import json
import logging
import operator
import re
import time
import uuid
from functools import reduce as _freduce
from typing import Any, Literal, Optional

import openai as _openai

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy

from config import settings
from agent.llm import create_agent_llm, get_agent_hardware, get_agent_model
from agent.prompts import CLASSIFIER_PROMPT, CAT2_ANSWER_PROMPT, RESEARCH_AGENT_SYSTEM_PROMPT
from agent.state import ResearchState
from agent.tools import (
    TOOLS,
    run_evaluate_sources_core,
    run_extract_claims_core,
    run_write_report_core,
)
from agent.tool_node import ResearchToolNode
from llm_client import get_llm
from models import ProgressEvent, QueryClassification, ResearchStatus
from token_tracker import tracker

logger = logging.getLogger(__name__)

MAX_ITERATIONS = settings.agent_max_iterations

# Tool names known to the agent (used by the text-based parser).
_TOOL_NAMES: set[str] = {t.name for t in TOOLS}


# ── Text-based tool-call fallback parser ──────────────────────
#
# vLLM without --enable-auto-tool-choice emits tool calls as plain
# text in AIMessage.content instead of structured tool_calls.  The
# helpers below detect that situation and re-pack the content into
# proper AIMessage.tool_calls so the rest of the graph works.


def _try_parse_json(text: str) -> dict | list | None:
    """Return parsed JSON, or *None* on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _is_tool_call(data: Any, available_tools: set[str]) -> bool:
    """True if *data* looks like ``{"name": "<known tool>", ...}``."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("name"), str)
        and data["name"] in available_tools
    )


def _fix_arg_types(args: dict) -> dict:
    """Convert Python-repr strings to native types.

    Llama models sometimes emit ``"['a', 'b']"`` instead of ``["a", "b"]``.
    We use ``ast.literal_eval`` as a safe parser for these cases.
    """
    fixed: dict = {}
    for key, value in args.items():
        if isinstance(value, str):
            try:
                parsed = ast.literal_eval(value)
                if isinstance(parsed, (list, dict, tuple, set, int, float, bool)):
                    fixed[key] = parsed
                    continue
            except (ValueError, SyntaxError):
                pass
        fixed[key] = value
    return fixed


def _try_parse_text_tool_calls(
    response: AIMessage, available_tools: set[str]
) -> AIMessage | None:
    """If *response* contains text-encoded tool calls, return a fixed copy.

    Returns ``None`` when the response already has structured tool_calls or
    when the content does not look like a tool call.
    """
    # Nothing to do if structured tool calls already present.
    if response.tool_calls:
        return None

    content = (response.content or "").strip()
    if not content:
        return None

    # Normalise common wrappers before attempting JSON parse.
    #   Llama-3:     <|python_tag|>{...}
    #   Nemotron:    <TOOLCALL>[{...}]</TOOLCALL>
    #   Qwen:        ```xml\n<function_call>\n{...}\n</function_call>\n```
    #                or ```json\n{...}\n```  or just <function_call>...</function_call>
    cleaned = content
    if cleaned.startswith("<|python_tag|>"):
        cleaned = cleaned[len("<|python_tag|>"):].strip()

    # Strip fenced code blocks (```xml / ```json / ```).
    fence = re.match(r"^```(?:[a-zA-Z]+)?\s*(.*?)\s*```\s*$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    # <TOOLCALL>...</TOOLCALL> wrapper (Nemotron).
    toolcall = re.search(r"<TOOLCALL>\s*(.*?)\s*</TOOLCALL>", cleaned, re.DOTALL)
    if toolcall:
        cleaned = toolcall.group(1).strip()

    # <function_call>...</function_call> wrapper (Qwen).
    fc = re.search(r"<function_call>\s*(.*?)\s*</function_call>", cleaned, re.DOTALL)
    if fc:
        cleaned = fc.group(1).strip()

    parsed = _try_parse_json(cleaned)
    if parsed is None:
        return None

    # Normalise to a list (single call or batch).
    candidates = parsed if isinstance(parsed, list) else [parsed]

    tool_calls: list[dict] = []
    for item in candidates:
        if not _is_tool_call(item, available_tools):
            continue
        # Llama uses "parameters"; OpenAI uses "arguments".
        raw_args = item.get("parameters") or item.get("arguments") or {}
        if isinstance(raw_args, str):
            raw_args = _try_parse_json(raw_args) or {}
        args = _fix_arg_types(raw_args) if isinstance(raw_args, dict) else raw_args
        tool_calls.append(
            {
                "name": item["name"],
                "args": args,
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "tool_call",
            }
        )

    if not tool_calls:
        return None

    logger.info(
        "Parsed %d text-encoded tool call(s) from AIMessage content", len(tool_calls)
    )

    return AIMessage(
        content="",
        tool_calls=tool_calls,
    )


# ── Nodes ──────────────────────────────────────────────────────


async def agent_node(
    state: ResearchState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Call the LLM with tools bound, prepending the system prompt."""
    llm = create_agent_llm()
    llm_with_tools = llm.bind_tools(TOOLS)

    messages = list(state["messages"])

    # Ensure system prompt is first
    if not messages or not isinstance(messages[0], SystemMessage):
        messages.insert(0, SystemMessage(content=RESEARCH_AGENT_SYSTEM_PROMPT))

    # For Cat 3, inject the classifier's pre-generated search queries
    search_queries = state.get("search_queries_history", [])
    if state.get("iteration_count", 0) == 0 and search_queries:
        from langchain_core.messages import HumanMessage as HM
        hint = f"\n\nStart by calling web_search with these queries: {search_queries}"
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HM):
                messages[i] = HM(content=messages[i].content + hint)
                break

    iteration = state.get("iteration_count", 0) + 1
    logger.info("Agent iteration %d: invoking LLM", iteration)

    # Extract progress_callback and metadata from config for live streaming
    configurable = (config or {}).get("configurable", {})
    progress_callback = configurable.get("progress_callback")
    research_id = state.get("research_id", "")
    hw_label = get_agent_hardware()
    model_name = get_agent_model()

    # Light up the persistent supervisor banner while this call runs.
    if progress_callback:
        await progress_callback(ProgressEvent(
            research_id=research_id,
            status=ResearchStatus.THINKING,
            component="Supervisor agent",
            hardware=hw_label,
            model_name=model_name,
            supervisor_call_start=True,
        ))

    _agent_hw_before = tracker.get_hw_summary(research_id)
    t_llm = time.perf_counter()
    ttft_s: float | None = None
    all_chunks: list = []
    total_chars = 0

    # Server-side buffering: accumulate content chunks and flush every
    # FLUSH_INTERVAL seconds instead of one SSE event per token.
    # This keeps the SSE stream lean (~10-15 events per thinking phase
    # rather than one per token), while still feeling live in the UI.
    _FLUSH_INTERVAL = 0.05  # 50 ms
    _FLUSH_MAX_CHARS = 300  # also flush when buffer reaches this size
    buf = ""
    last_flush = t_llm

    async for chunk in llm_with_tools.astream(messages, config=config):
        now = time.perf_counter()
        has_content = bool(getattr(chunk, "content", None))
        has_tool = bool(getattr(chunk, "tool_call_chunks", None))
        if ttft_s is None and (has_content or has_tool):
            ttft_s = now - t_llm
        # Accumulate and buffer content for the UI thinking panel
        if has_content and progress_callback:
            buf += chunk.content
            total_chars += len(chunk.content)
            if (now - last_flush >= _FLUSH_INTERVAL) or (len(buf) >= _FLUSH_MAX_CHARS):
                elapsed = now - t_llm
                # Approximate TPS: chars / 3.5 chars-per-token / elapsed
                live_tps = round(total_chars / 3.5 / elapsed, 1) if elapsed > 0.05 else 0.0
                await progress_callback(ProgressEvent(
                    research_id=research_id,
                    status=ResearchStatus.THINKING,
                    thinking_chunk=buf,
                    hardware=hw_label,
                    model_name=model_name,
                    component="Supervisor agent",
                    tps=live_tps,
                ))
                buf = ""
                last_flush = now
        all_chunks.append(chunk)

    # Flush any remaining buffered text
    if buf and progress_callback:
        elapsed = time.perf_counter() - t_llm
        live_tps = round(total_chars / 3.5 / elapsed, 1) if elapsed > 0.05 else 0.0
        await progress_callback(ProgressEvent(
            research_id=research_id,
            status=ResearchStatus.THINKING,
            thinking_chunk=buf,
            hardware=hw_label,
            model_name=model_name,
            component="Supervisor agent",
            tps=live_tps,
        ))

    llm_latency = time.perf_counter() - t_llm
    response = _freduce(operator.add, all_chunks) if all_chunks else AIMessage(content="")

    ttft_ms = ttft_s * 1000 if ttft_s is not None else None
    ttft_str = f"{ttft_ms:.1f}" if ttft_ms is not None else "n/a"

    if response.usage_metadata:
        in_tok = response.usage_metadata.get("input_tokens", 0)
        out_tok = response.usage_metadata.get("output_tokens", 0)
        logger.info(
            "Agent LLM usage: input_tokens=%d, output_tokens=%d, total_tokens=%d",
            in_tok, out_tok, in_tok + out_tok,
        )
        tracker.track(in_tok, out_tok, research_id=research_id)
        tracker.track_hw(in_tok, out_tok, llm_latency, hw_label, research_id=research_id, model=model_name,
                         ttft_ms=ttft_s * 1000 if ttft_s is not None else None, itl_ms=None)
        tps_out = out_tok / llm_latency if llm_latency > 0 and out_tok else 0
        tracker.track_call(
            f"agent_node[iter={iteration}]",
            llm_latency,
            ttft_s * 1000 if ttft_s is not None else None,
            None,
            in_tok, out_tok, tps_out,
            research_id=research_id,
        )
        logger.info(
            "Agent LLM timing [iter=%d]: e2el=%.3fs ttft=%sms itl=n/ams input=%d output=%d tps_out=%.1f",
            iteration, llm_latency, ttft_str, in_tok, out_tok, tps_out,
        )
        # Emit the refreshed per-hardware summary so the UI cost bar climbs after
        # the supervisor call too — not only after the first LLMClient sub-agent.
        # (The streaming thinking_chunk events above fire before token counts are
        # known, so this is the supervisor's only chance to push hw_summary.)
        if progress_callback:
            await progress_callback(ProgressEvent(
                research_id=research_id,
                status=ResearchStatus.THINKING,
                hw_summary=tracker.get_hw_summary(research_id),
                hardware=hw_label,
                model_name=model_name,
                component="Supervisor agent",
                tps=round(tps_out, 1),
                supervisor_call_done=True,
                step_input_tokens=in_tok,
                step_output_tokens=out_tok,
            ))
    else:
        in_tok = out_tok = 0
        # Even with no usage metadata, settle the banner so it stops pulsing.
        if progress_callback:
            await progress_callback(ProgressEvent(
                research_id=research_id,
                status=ResearchStatus.THINKING,
                hw_summary=tracker.get_hw_summary(research_id),
                hardware=hw_label,
                model_name=model_name,
                component="Supervisor agent",
                supervisor_call_done=True,
            ))

    # Fallback: if vLLM returned tool calls as plain text, parse them.
    parsed = _try_parse_text_tool_calls(response, _TOOL_NAMES)
    if parsed is not None:
        response = parsed

    tool_names = [tc["name"] for tc in response.tool_calls] if response.tool_calls else []
    if tool_names:
        logger.info("Agent iteration %d: LLM requested tools: %s", iteration, tool_names)
    else:
        logger.info("Agent iteration %d: LLM produced final response (no tool calls)", iteration)

    return {
        "messages": [response],
        "iteration_count": iteration,
    }


# ── Classification & category-specific nodes ──────────────────


async def classify_node(
    state: ResearchState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Classify the query into Cat 1, 2, or 3."""
    query = state["query"]
    llm = get_llm("classify")
    research_id = state.get("research_id", "")
    configurable = (config or {}).get("configurable", {})
    progress_callback = configurable.get("progress_callback")

    if progress_callback:
        await progress_callback(ProgressEvent(
            research_id=research_id,
            status=ResearchStatus.THINKING,
            message="Routing query...",
            progress_pct=8.0,
            component="Semantic routing",
            step_start=True,
            step_hardware=llm.hardware,
        ))

    messages = [
        {"role": "system", "content": CLASSIFIER_PROMPT},
        {"role": "user", "content": query},
    ]

    _hw_before = tracker.get_hw_summary(research_id)
    try:
        classification = await llm.chat_structured(messages, QueryClassification, caller="Semantic routing")
        logger.info(
            "Query classified as Cat %d: %s (queries: %s)",
            classification.category, classification.reasoning, classification.search_queries,
        )
    except Exception:
        logger.exception("Classification failed, defaulting to Cat 3")
        classification = None

    if progress_callback:
        _hw_after = tracker.get_hw_summary(research_id)
        _best, _si, _so, _sh, _sm = -1, 0, 0, llm.hardware, llm.model
        for _hwn, _hws in _hw_after.items():
            _prev = _hw_before.get(_hwn, {})
            _d = (_hws["in_tokens"] - _prev.get("in_tokens", 0)) + (_hws["out_tokens"] - _prev.get("out_tokens", 0))
            if _d > _best:
                _best = _d
                _si = _hws["in_tokens"] - _prev.get("in_tokens", 0)
                _so = _hws["out_tokens"] - _prev.get("out_tokens", 0)
                _sh = _hwn
                _sm = _hws.get("model", "")
        await progress_callback(ProgressEvent(
            research_id=research_id,
            status=ResearchStatus.THINKING,
            progress_pct=10.0,
            component="Semantic routing",
            step_done=True,
            step_hardware=_sh,
            step_model=_sm,
            step_input_tokens=_si,
            step_output_tokens=_so,
        ))

    if classification is None:
        return {"query_category": 3, "search_queries_history": [query]}
    return {"query_category": classification.category, "search_queries_history": classification.search_queries}


async def cat1_answer_node(
    state: ResearchState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Answer simple factual questions directly without tools."""
    query = state["query"]
    llm = get_llm("cat1_answer")

    messages = [
        {"role": "system", "content": "Answer this simple factual question directly and concisely."},
        {"role": "user", "content": query},
    ]
    answer = await llm.chat(messages, max_tokens=512, caller="cat1_answer_node")
    logger.info("Cat 1 direct answer: %d chars", len(answer))
    return {"report_markdown": answer}


async def cat2_search_node(
    state: ResearchState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Execute web search for Cat 2 queries using classifier-generated queries."""
    import asyncio as _asyncio

    from models import SearchResult
    from search_providers import create_search_provider

    configurable = (config or {}).get("configurable", {})
    progress_callback = configurable.get("progress_callback")
    research_id = state.get("research_id", "")

    if progress_callback:
        await progress_callback(ProgressEvent(
            research_id=research_id,
            status=ResearchStatus.SEARCHING,
            message="Searching the web...",
            progress_pct=20.0,
            component="Web search",
        ))

    queries = state.get("search_queries_history", [])
    if not queries:
        queries = [state["query"]]

    search_provider = create_search_provider()
    tasks = [search_provider.search(q) for q in queries[:5]]
    results_nested = await _asyncio.gather(*tasks)
    new_results: list[SearchResult] = []
    for batch in results_nested:
        new_results.extend(batch)

    # Dedup by URL
    seen: dict[str, SearchResult] = {}
    for r in new_results:
        if r.url not in seen or r.score > seen[r.url].score:
            seen[r.url] = r
    deduped = list(seen.values())

    logger.info("Cat 2 search: %d results from %d queries", len(deduped), len(queries))
    return {
        "search_results": [r.model_dump() for r in deduped],
        "search_count": 1,
    }


async def cat2_answer_node(
    state: ResearchState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Answer Cat 2 queries directly from search snippets."""
    configurable = (config or {}).get("configurable", {})
    progress_callback = configurable.get("progress_callback")
    research_id = state.get("research_id", "")

    if progress_callback:
        await progress_callback(ProgressEvent(
            research_id=research_id,
            status=ResearchStatus.THINKING,
            message="Synthesizing answer...",
            progress_pct=70.0,
            component="Answering",
        ))

    query = state["query"]
    results = state.get("search_results", [])

    snippets = []
    for r in results[:10]:
        title = r.get("title", "")
        content = (r.get("content") or "")[:500]
        url = r.get("url", "")
        if content:
            snippets.append(f"- {title} ({url}): {content}")
    snippets_text = "\n".join(snippets) if snippets else "No results found."

    llm = get_llm("cat2_answer")
    prompt = CAT2_ANSWER_PROMPT.format(search_snippets=snippets_text)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": query},
    ]
    answer = await llm.chat(messages, max_tokens=1024, caller="cat2_answer_node")
    logger.info("Cat 2 answer: %d chars from %d snippets", len(answer), len(snippets))
    return {"report_markdown": answer}


async def fallback_answer_node(
    state: ResearchState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Produce a best-effort answer when the circuit breaker fires without a report."""
    if state.get("report_markdown"):
        return {}  # Already have a report, nothing to do

    query = state["query"]
    results = state.get("search_results", [])
    evaluated = state.get("evaluated_sources", [])

    # Use evaluated sources if available, else raw search results.
    # If evaluated exists but all sources were rejected (keep=False), fall
    # through to raw results rather than surfacing "no sources found".
    snippets = []
    if evaluated:
        kept = [e for e in evaluated if e.get("keep")]
        for e in kept[:8]:
            sr = e.get("search_result", {})
            findings = "; ".join(e.get("key_findings", [])[:3])
            snippets.append(f"- {sr.get('title', '')} ({sr.get('url', '')}): {findings}")

    if not snippets and results:
        for r in results[:8]:
            content = (r.get("content") or "")[:300]
            snippets.append(f"- {r.get('title', '')} ({r.get('url', '')}): {content}")

    if not snippets:
        return {"report_markdown": "Unable to complete research — no sources were found."}

    snippets_text = "\n".join(snippets)
    llm = get_llm("fallback_answer")
    messages = [
        {
            "role": "system",
            "content": (
                "You are a research assistant. The full research pipeline could not complete in time. "
                "Based on the sources gathered so far, provide a helpful preliminary summary. "
                "Start with 'Note: This is a preliminary summary based on partial research.\\n\\n' "
                "then give the best answer you can from these sources."
            ),
        },
        {"role": "user", "content": f"Query: {query}\n\nSources gathered:\n{snippets_text}"},
    ]
    answer = await llm.chat(messages, max_tokens=2048, caller="fallback_answer_node")
    logger.info("Fallback answer: %d chars from %d sources", len(answer), len(snippets))
    return {"report_markdown": answer}


async def pipeline_recovery_node(
    state: ResearchState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Complete any missing pipeline stages when the agent exits mid-pipeline.

    Fires when the agent produces no tool call but research_results exist and
    report_markdown is still empty.  Deterministically runs whichever of
    evaluate_sources → extract_claims → write_report have not yet been called.
    """
    query = state["query"]
    logger.warning(
        "pipeline_recovery: agent exited mid-pipeline — completing remaining stages"
    )

    if progress_callback := state.get("progress_callback"):
        await progress_callback(
            ProgressEvent(component="Pipeline recovery", hardware=get_agent_hardware())
        )

    evaluated_dicts = state.get("evaluated_sources", [])
    search_results = state.get("search_results", [])

    # Stage 1: evaluate_sources
    if not evaluated_dicts and search_results:
        logger.info(
            "pipeline_recovery: running evaluate_sources on %d results", len(search_results)
        )
        evaluated_dicts = await run_evaluate_sources_core(query, search_results, [])

    # Stage 2: extract_claims
    claims_dicts = state.get("claims", [])
    if not claims_dicts and any(e.get("keep") for e in evaluated_dicts):
        logger.info("pipeline_recovery: running extract_claims")
        claims_dicts = await run_extract_claims_core(query, evaluated_dicts)

    # Stage 3: write_report
    kept = [e for e in evaluated_dicts if e.get("keep")]
    if not kept:
        logger.warning("pipeline_recovery: no kept sources — returning unable-to-complete message")
        return {"report_markdown": "Unable to complete research — no relevant sources were found."}

    logger.info("pipeline_recovery: writing report from %d kept sources", len(kept))
    report, citations_dicts = await run_write_report_core(query, evaluated_dicts, claims_dicts)

    return {
        "evaluated_sources": evaluated_dicts,
        "claims": claims_dicts,
        "report_markdown": report,
        "citations": citations_dicts,
    }


# ── Routing ────────────────────────────────────────────────────


def classify_router(state: ResearchState) -> Literal["cat1", "cat2_search", "agent"]:
    """Route based on query classification."""
    category = state.get("query_category", 3)
    if category == 1:
        return "cat1"
    elif category == 2:
        return "cat2_search"
    return "agent"


def should_continue(state: ResearchState) -> Literal["tools", "pipeline_recovery", "fallback"]:
    """Decide whether to route to tools, pipeline_recovery, or fallback.

    Routes to ``tools`` if the last message has tool_calls.
    Routes to ``pipeline_recovery`` if the agent exited without calling all
    required pipeline tools (search done, no report yet).
    Routes to ``fallback`` otherwise (circuit breaker or truly empty state).
    """
    iteration_count = state.get("iteration_count", 0)
    if iteration_count >= MAX_ITERATIONS:
        logger.warning(
            "Circuit breaker: reached %d iterations, forcing end", iteration_count
        )
        if state.get("search_results") and not state.get("report_markdown"):
            return "pipeline_recovery"
        return "fallback"

    messages = state.get("messages", [])
    last_message = messages[-1] if messages else None

    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        logger.info("Routing: -> tools (iteration %d)", iteration_count)
        return "tools"

    # Agent exited without a tool call — recover if we have search results but no report
    if state.get("search_results") and not state.get("report_markdown"):
        logger.info(
            "Routing: -> pipeline_recovery (agent exited mid-pipeline, iteration %d)",
            iteration_count,
        )
        return "pipeline_recovery"

    logger.info("Routing: -> fallback (iteration %d)", iteration_count)
    return "fallback"


# ── Graph construction ─────────────────────────────────────────


def build_graph() -> StateGraph:
    """Build and compile the research agent graph."""
    graph = StateGraph(ResearchState)

    # Entry: classify the query
    graph.add_node("classify", classify_node)

    # Cat 1: direct answer
    graph.add_node("cat1", cat1_answer_node)

    # Cat 2: search then answer
    graph.add_node("cat2_search", cat2_search_node)
    graph.add_node("cat2_answer", cat2_answer_node)

    # Cat 3: full research agent loop
    def _agent_retry_on(exc: Exception) -> bool:
        """Retry agent_node on transient API errors (vLLM 404 during restart, connection drops)."""
        return isinstance(exc, (
            ConnectionError, TimeoutError, OSError,
            _openai.APIConnectionError, _openai.APITimeoutError, _openai.NotFoundError,
        ))

    graph.add_node(
        "agent",
        agent_node,
        retry=RetryPolicy(max_attempts=3, initial_interval=5.0, backoff_factor=2.0, retry_on=_agent_retry_on),
    )
    graph.add_node("tools", ResearchToolNode(TOOLS))
    graph.add_node("pipeline_recovery", pipeline_recovery_node)
    graph.add_node("fallback", fallback_answer_node)

    # Wiring
    graph.set_entry_point("classify")
    graph.add_conditional_edges(
        "classify",
        classify_router,
        {"cat1": "cat1", "cat2_search": "cat2_search", "agent": "agent"},
    )

    # Cat 1 → end
    graph.add_edge("cat1", END)

    # Cat 2 → search → answer → end
    graph.add_edge("cat2_search", "cat2_answer")
    graph.add_edge("cat2_answer", END)

    # Cat 3 → agent loop with fallback
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "pipeline_recovery": "pipeline_recovery", "fallback": "fallback"},
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("pipeline_recovery", END)
    graph.add_edge("fallback", END)

    return graph.compile()
