# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Core agent loop — LLM call → parse → execute tool → repeat."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI

from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, MAX_AGENT_TURNS, MAX_TOKENS_PER_RESPONSE, TEMPERATURE
from agent.prompt_builder import build_system_prompt
from agent.tool_parser import (
    parse_tool_call,
    parse_all_tool_calls,
    text_before_tool_calls,
    format_tool_result,
    format_tool_results,
)
from agent.tool_executor import execute_tools
from tools.registry import execute_tool


@dataclass
class ToolCallRecord:
    name: str
    arguments: Dict[str, Any]
    result: Dict[str, Any]
    duration_ms: float


@dataclass
class AgentResponse:
    content: str
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    turns: int = 0
    total_tokens: int = 0
    total_latency_ms: float = 0
    # Parallel-execution observability
    parallel_batches: int = 0        # turns that ran >1 tool concurrently
    max_batch_size: int = 1          # largest number of tools in one turn
    executor_backend: str = "local"  # "local" or "ray"


_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    return _client


def run_agent(
    query: str,
    customer_id: str = "",
    memory_context: str = "",
    conversation_history: List[Dict[str, str]] = None,
    on_tool_call: Optional[Callable[[str, Dict], None]] = None,
    on_tool_result: Optional[Callable[[str, Dict], None]] = None,
    on_thinking: Optional[Callable[[str], None]] = None,
) -> AgentResponse:
    """Run the agent loop until a final response or max turns reached.

    Args:
        query: The user's message
        customer_id: Authenticated customer ID (if any)
        memory_context: Previous interaction context from memory
        conversation_history: Prior messages in this conversation
        on_tool_call: Callback when a tool is being called
        on_tool_result: Callback when a tool returns
        on_thinking: Callback with intermediate text

    Returns:
        AgentResponse with final text, tool call records, and metadata
    """
    client = _get_client()
    system_prompt = build_system_prompt(customer_id, memory_context)

    messages = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": query})

    tool_calls: List[ToolCallRecord] = []
    total_tokens = 0
    parallel_batches = 0
    max_batch_size = 1
    backend_used = "local"
    start_time = time.time()

    for turn in range(MAX_AGENT_TURNS):
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS_PER_RESPONSE,
            temperature=TEMPERATURE,
            top_p=1.0,
        )

        content = response.choices[0].message.content or ""
        total_tokens += (response.usage.total_tokens if response.usage else 0)

        # Parse ALL tool calls in this turn (may be more than one → run parallel)
        batch = parse_all_tool_calls(content)

        if not batch:
            # Final response
            final_text = text_before_tool_calls(content) or content
            return AgentResponse(
                content=final_text,
                tool_calls=tool_calls,
                turns=turn + 1,
                total_tokens=total_tokens,
                total_latency_ms=(time.time() - start_time) * 1000,
                parallel_batches=parallel_batches,
                max_batch_size=max_batch_size,
                executor_backend=backend_used,
            )

        # Emit intermediate prose and per-call callbacks
        pre_text = text_before_tool_calls(content)
        if on_thinking and pre_text:
            on_thinking(pre_text)
        if on_tool_call:
            for name, args in batch:
                on_tool_call(name, args)

        if len(batch) > 1:
            parallel_batches += 1
        max_batch_size = max(max_batch_size, len(batch))

        # Execute the batch concurrently (local threads or Ray)
        batch_start = time.time()
        results, backend_used = execute_tools(batch)
        batch_duration = (time.time() - batch_start) * 1000
        # Attribute the batch wall-time evenly across calls for the trace
        per_call_ms = batch_duration / len(batch)

        result_pairs = []
        for (name, args), result in zip(batch, results):
            tool_calls.append(ToolCallRecord(
                name=name,
                arguments=args,
                result=result,
                duration_ms=per_call_ms,
            ))
            result_pairs.append((name, result))
            if on_tool_result:
                on_tool_result(name, result)

        # Add assistant turn + all tool results, then continue
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": format_tool_results(result_pairs)})

    # Max turns reached
    return AgentResponse(
        content="I apologize, but I'm having trouble resolving your request. Let me transfer you to a specialist who can help further.",
        tool_calls=tool_calls,
        turns=MAX_AGENT_TURNS,
        total_tokens=total_tokens,
        total_latency_ms=(time.time() - start_time) * 1000,
        parallel_batches=parallel_batches,
        max_batch_size=max_batch_size,
        executor_backend=backend_used,
    )


def run_agent_streaming(
    query: str,
    customer_id: str = "",
    memory_context: str = "",
    conversation_history: List[Dict[str, str]] = None,
):
    """Generator version that yields chunks for streaming UI.

    Yields tuples of (event_type, data):
      ("token", "text chunk")
      ("tool_call", {"name": ..., "arguments": ...})
      ("tool_result", {"name": ..., "result": ...})
      ("done", AgentResponse)
    """
    client = _get_client()
    system_prompt = build_system_prompt(customer_id, memory_context)

    messages = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": query})

    tool_calls: List[ToolCallRecord] = []
    total_tokens = 0
    start_time = time.time()

    for turn in range(MAX_AGENT_TURNS):
        # Stream the response
        stream = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS_PER_RESPONSE,
            temperature=TEMPERATURE,
            top_p=1.0,
            stream=True,
        )

        full_content = ""
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_content += token
                # Don't yield tokens that are part of a tool call tag
                if "<tool_call>" not in full_content:
                    yield ("token", token)
            if chunk.usage:
                total_tokens += chunk.usage.total_tokens

        # Check for tool call(s) in accumulated content
        batch = parse_all_tool_calls(full_content)

        if not batch:
            # Final response
            response = AgentResponse(
                content=text_before_tool_calls(full_content) or full_content,
                tool_calls=tool_calls,
                turns=turn + 1,
                total_tokens=total_tokens,
                total_latency_ms=(time.time() - start_time) * 1000,
            )
            yield ("done", response)
            return

        # Tool call(s) detected — announce all, then execute concurrently
        for name, args in batch:
            yield ("tool_call", {"name": name, "arguments": args})

        batch_start = time.time()
        results, _backend = execute_tools(batch)
        per_call_ms = ((time.time() - batch_start) * 1000) / len(batch)

        result_pairs = []
        for (name, args), result in zip(batch, results):
            tool_calls.append(ToolCallRecord(
                name=name,
                arguments=args,
                result=result,
                duration_ms=per_call_ms,
            ))
            result_pairs.append((name, result))
            yield ("tool_result", {"name": name, "result": result})

        # Continue conversation
        messages.append({"role": "assistant", "content": full_content})
        messages.append({"role": "user", "content": format_tool_results(result_pairs)})

    # Max turns
    response = AgentResponse(
        content="I apologize, but I'm having trouble resolving your request. Let me transfer you to a specialist.",
        tool_calls=tool_calls,
        turns=MAX_AGENT_TURNS,
        total_tokens=total_tokens,
        total_latency_ms=(time.time() - start_time) * 1000,
    )
    yield ("done", response)
