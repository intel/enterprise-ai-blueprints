# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Custom tool executor that merges state updates and emits progress events."""

import json
import logging
import time
from typing import Any, Optional, Sequence

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from models import ProgressEvent, ResearchStatus
from token_tracker import tracker, current_research_id

logger = logging.getLogger(__name__)

# Prerequisite gating: tool -> set of tools that must have completed first
_TOOL_PREREQUISITES: dict[str, set[str]] = {
    "summarize_sources": {"web_search"},
    "evaluate_sources": {"summarize_sources"},  # transitively requires web_search
    "analyze_gaps": {"evaluate_sources"},
    "extract_claims": {"evaluate_sources"},
    "write_report": {"evaluate_sources"},
}

# Max consecutive blocks before auto-recovery kicks in
_AUTO_RECOVER_AFTER = 2


def _resolve_prerequisite_chain(
    tool_name: str, completed: set[str]
) -> list[str]:
    """Walk the prerequisite graph transitively and return missing deps in execution order.

    E.g. for ``write_report`` with nothing completed, returns
    ``["web_search", "evaluate_sources"]``.
    """
    missing_ordered: list[str] = []
    visited: set[str] = set()

    def _walk(name: str) -> None:
        prereqs = _TOOL_PREREQUISITES.get(name, set())
        for dep in sorted(prereqs):  # sorted for deterministic order
            if dep not in completed and dep not in visited:
                visited.add(dep)
                _walk(dep)  # resolve transitive deps first
                missing_ordered.append(dep)

    _walk(tool_name)
    return missing_ordered

# Map tool names to progress status / percentage ranges
_TOOL_PROGRESS: dict[str, tuple[ResearchStatus, float, str]] = {
    "web_search": (ResearchStatus.SEARCHING, 25.0, "Searching the web..."),
    "summarize_sources": (ResearchStatus.EVALUATING, 40.0, "Compressing sources..."),
    "evaluate_sources": (ResearchStatus.EVALUATING, 50.0, "Evaluating sources..."),
    "analyze_gaps": (ResearchStatus.DEEPENING, 62.0, "Analyzing knowledge gaps..."),
    "extract_claims": (ResearchStatus.DEEPENING, 72.0, "Extracting factual claims..."),
    "write_report": (ResearchStatus.SYNTHESIZING, 80.0, "Writing research report..."),
}

# Human-readable component labels for the UI status line
_TOOL_COMPONENT: dict[str, str] = {
    "web_search": "Web search",
    "summarize_sources": "Summarizing sources",
    "evaluate_sources": "Evaluating sources",
    "analyze_gaps": "Gap analysis",
    "extract_claims": "Extracting claims",
    "write_report": "Writing report",
}

# Hardware label for each tool's primary LLM (derived from settings at import time)
# web_search uses no LLM — show secondary hardware (Xeon) since search runs on CPU
def _tool_hardware(tool_name: str) -> str:
    from config import settings
    secondary_tools = {m.strip() for m in settings.secondary_llm_modules.split(",") if m.strip()}
    if tool_name in secondary_tools:
        return settings.secondary_llm_hardware
    if tool_name == "web_search":
        return settings.secondary_llm_hardware  # no LLM, but runs on Xeon
    if tool_name == "summarize_sources" and not settings.use_llm_summarizer:
        return settings.secondary_llm_hardware  # extractive embeddings run on the Xeon CPU
    return settings.llm_hardware


class ResearchToolNode:
    """Executes tool calls from the last AI message.

    Extends LangGraph's standard tool execution with:
    1. **State merging** — parses JSON tool returns, extracts ``state_updates``
    2. **Progress events** — emits ``ProgressEvent`` before each tool
    3. **Error handling** — returns errors as ``ToolMessage`` so the agent
       can recover
    """

    def __init__(self, tools: Sequence[BaseTool]) -> None:
        self._tools_by_name: dict[str, BaseTool] = {t.name: t for t in tools}

    async def __call__(
        self, state: dict[str, Any], config: Optional[RunnableConfig] = None
    ) -> dict[str, Any]:
        """Execute all tool calls in the last AI message."""
        messages: list = state.get("messages", [])
        last_message = messages[-1] if messages else None
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {}

        config = config or {}
        configurable = config.get("configurable", {})
        progress_callback = configurable.get("progress_callback")
        research_id = state.get("research_id", "")

        result_messages: list[ToolMessage] = []
        state_updates: dict[str, Any] = {}
        completed_tools: set[str] = set(state.get("completed_tools", []))
        consecutive_blocked: int = state.get("consecutive_blocked", 0)
        user_query: str = state.get("query", "")

        for tool_call in last_message.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            call_id = tool_call["id"]

            # Prerequisite gating: check that required tools have run
            missing_chain = _resolve_prerequisite_chain(tool_name, completed_tools)
            if missing_chain:
                consecutive_blocked += 1
                next_tool = missing_chain[0]
                pipeline_str = " -> ".join(missing_chain + [tool_name])

                logger.warning(
                    "Tool %s blocked (consecutive=%d): missing chain %s",
                    tool_name, consecutive_blocked, missing_chain,
                )

                # Layer 2: Auto-recovery after repeated blocks
                if (
                    consecutive_blocked >= _AUTO_RECOVER_AFTER
                    and next_tool == "web_search"
                    and "web_search" in self._tools_by_name
                    and user_query
                ):
                    logger.info(
                        "Auto-recovering: executing web_search for query %r",
                        user_query,
                    )
                    try:
                        auto_tool = self._tools_by_name["web_search"]
                        auto_call_input = {
                            "name": "web_search",
                            "args": {
                                "queries": [user_query],
                                "state": state,
                            },
                            "id": call_id,
                            "type": "tool_call",
                        }
                        auto_result = await auto_tool.ainvoke(
                            auto_call_input, config=config,
                        )
                        raw = auto_result.content if isinstance(auto_result, ToolMessage) else auto_result
                        message_text, updates = _parse_tool_result(raw)
                        if updates:
                            state_updates.update(updates)
                        completed_tools.add("web_search")
                        consecutive_blocked = 0
                        result_messages.append(
                            ToolMessage(
                                content=(
                                    f"AUTO-RECOVERY: web_search was executed automatically "
                                    f"because you skipped it.\n\n{message_text}\n\n"
                                    f"NEXT STEP: Call evaluate_sources NOW."
                                ),
                                tool_call_id=call_id,
                                name=tool_name,
                            )
                        )
                        continue
                    except Exception as exc:
                        logger.exception("Auto-recovery web_search failed")
                        # Fall through to the error message path

                result_messages.append(
                    ToolMessage(
                        content=(
                            f"STOP. You cannot call '{tool_name}' yet.\n"
                            f"NEXT STEP: Call {next_tool} NOW"
                            + (f' with queries about: "{user_query}"' if next_tool == "web_search" else "")
                            + f".\nFull pipeline: {pipeline_str}"
                        ),
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )
                continue

            # Tool is allowed — reset consecutive block counter
            consecutive_blocked = 0

            # Emit progress event (tool start)
            _step_info = _TOOL_PROGRESS.get(tool_name)
            if progress_callback and _step_info:
                _s_status, _s_pct, _s_msg = _step_info
                event = ProgressEvent(
                    research_id=research_id,
                    status=_s_status,
                    message=_s_msg,
                    progress_pct=_s_pct,
                    component=_TOOL_COMPONENT.get(tool_name, tool_name),
                    step_start=True,
                    step_hardware=_tool_hardware(tool_name),
                )
                await progress_callback(event)

            logger.info("Executing tool: %s", tool_name)

            tool = self._tools_by_name.get(tool_name)
            if tool is None:
                result_messages.append(
                    ToolMessage(
                        content=f"Error: Unknown tool '{tool_name}'",
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )
                continue

            try:
                # Build a ToolCall dict — required by LangChain when the
                # tool declares InjectedToolCallId / InjectedState params.
                tool_call_input = {
                    "name": tool_name,
                    "args": {**tool_args, "state": state},
                    "id": call_id,
                    "type": "tool_call",
                }

                # Re-assert the research_id ContextVar so llm_client tracking
                # works even if LangGraph executed this node in a new task context.
                current_research_id.set(research_id)
                _before_in  = tracker.get(research_id).input_tokens
                _before_out = tracker.get(research_id).output_tokens
                logger.info("[token-dbg] BEFORE tool=%s before_in=%d before_out=%d rid=%r",
                            tool_name, _before_in, _before_out, research_id[:8])
                _t_tool = time.perf_counter()
                raw_result = await tool.ainvoke(
                    tool_call_input,
                    config=config,
                )
                _tool_elapsed = time.perf_counter() - _t_tool
                logger.info("Tool timing [%s]: e2el=%.3fs", tool_name, _tool_elapsed)
                tracker.track_tool_time(tool_name, _tool_elapsed, hardware=_tool_hardware(tool_name), research_id=research_id)

                # ainvoke with a ToolCall dict returns a ToolMessage;
                # extract the content string before parsing.
                if isinstance(raw_result, ToolMessage):
                    raw_result = raw_result.content

                # Parse JSON result to extract state_updates
                message_text, updates = _parse_tool_result(raw_result)
                if updates:
                    state_updates.update(updates)

                logger.info("Tool %s completed: %s", tool_name, message_text)
                completed_tools.add(tool_name)

                # Track search→evaluate cycles
                if tool_name == "evaluate_sources":
                    cycles = state.get("search_evaluate_cycles", 0) + 1
                    state_updates["search_evaluate_cycles"] = cycles
                    logger.info("Search-evaluate cycle count: %d", cycles)

                result_messages.append(
                    ToolMessage(
                        content=message_text,
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )

                # Emit step_done with per-step token delta
                if progress_callback and _step_info:
                    _usage_now = tracker.get(research_id)
                    _step_in  = _usage_now.input_tokens  - _before_in
                    _step_out = _usage_now.output_tokens - _before_out
                    logger.info("[token-dbg] AFTER  tool=%s now_in=%d now_out=%d delta_in=%d delta_out=%d",
                                tool_name, _usage_now.input_tokens, _usage_now.output_tokens, _step_in, _step_out)
                    _step_hw  = _tool_hardware(tool_name)
                    _step_mdl = ""
                    # Get model name from hw summary — but only for LLM-backed tools.
                    # web_search is API-based; showing a model name there is misleading.
                    _LLM_TOOLS = {"evaluate_sources", "analyze_gaps", "extract_claims", "write_report"}
                    # summarize_sources only uses an LLM in the (non-default) LLM mode;
                    # the extractive path is pure embeddings, so no model name applies.
                    from config import settings as _settings
                    if _settings.use_llm_summarizer:
                        _LLM_TOOLS = _LLM_TOOLS | {"summarize_sources"}
                    if tool_name in _LLM_TOOLS:
                        _hw_snap = tracker.get_hw_summary(research_id)
                        if _step_hw in _hw_snap:
                            _step_mdl = _hw_snap[_step_hw].get("model", "")
                    _s_status, _s_pct, _ = _step_info
                    await progress_callback(ProgressEvent(
                        research_id=research_id,
                        status=_s_status,
                        progress_pct=_s_pct,
                        component=_TOOL_COMPONENT.get(tool_name, tool_name),
                        step_done=True,
                        step_input_tokens=_step_in,
                        step_output_tokens=_step_out,
                        step_hardware=_step_hw,
                        step_model=_step_mdl,
                    ))

            except Exception as exc:
                logger.exception("Tool %s failed", tool_name)
                result_messages.append(
                    ToolMessage(
                        content=f"Error executing {tool_name}: {exc}",
                        tool_call_id=call_id,
                        name=tool_name,
                    )
                )

        output: dict[str, Any] = {"messages": result_messages}
        output.update(state_updates)
        output["completed_tools"] = sorted(completed_tools)
        output["consecutive_blocked"] = consecutive_blocked
        return output


def _parse_tool_result(raw: str) -> tuple[str, dict[str, Any] | None]:
    """Parse a tool's JSON return value into (message, state_updates)."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            message = data.get("message", raw)
            updates = data.get("state_updates")
            return message, updates
    except (json.JSONDecodeError, TypeError):
        pass
    return str(raw), None
