# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Parse tool calls from LLM output text."""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ParsedResponse:
    """Result of parsing an LLM response for tool calls."""
    has_tool_call: bool = False
    tool_name: Optional[str] = None
    tool_args: Dict[str, Any] = field(default_factory=dict)
    text_before: str = ""
    text_after: str = ""
    raw_content: str = ""


def parse_tool_call(content: str) -> ParsedResponse:
    """Parse LLM output for tool calls.

    Supports two formats:
    1. XML-style: <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    2. Text-style: TOOL_CALL: name(arg1=value1, arg2=value2)
    """
    result = ParsedResponse(raw_content=content)

    # Format 1: <tool_call> JSON </tool_call>
    match = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            result.has_tool_call = True
            result.tool_name = data.get("name", "")
            result.tool_args = data.get("arguments", {})
            result.text_before = content[:match.start()].strip()
            result.text_after = content[match.end():].strip()
            return result
        except json.JSONDecodeError:
            pass

    # Format 1b: ```json inside tool_call tags
    match = re.search(r'<tool_call>\s*```(?:json)?\s*(.*?)\s*```\s*</tool_call>', content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            result.has_tool_call = True
            result.tool_name = data.get("name", "")
            result.tool_args = data.get("arguments", {})
            result.text_before = content[:match.start()].strip()
            result.text_after = content[match.end():].strip()
            return result
        except json.JSONDecodeError:
            pass

    # Format 2: TOOL_CALL: name(args)
    match = re.search(r'TOOL_CALL:\s*(\w+)\((.*?)\)', content, re.DOTALL)
    if match:
        result.has_tool_call = True
        result.tool_name = match.group(1)
        args_str = match.group(2).strip()
        result.tool_args = _parse_kv_args(args_str)
        result.text_before = content[:match.start()].strip()
        result.text_after = content[match.end():].strip()
        return result

    # Format 3: Bare JSON with name/arguments keys (model sometimes omits tags)
    json_match = re.search(r'\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*"arguments"\s*:\s*(\{[^{}]*\})[^{}]*\}', content, re.DOTALL)
    if json_match:
        try:
            args = json.loads(json_match.group(2))
            result.has_tool_call = True
            result.tool_name = json_match.group(1)
            result.tool_args = args
            result.text_before = content[:json_match.start()].strip()
            result.text_after = content[json_match.end():].strip()
            return result
        except json.JSONDecodeError:
            pass

    # No tool call found — full content is text
    result.text_before = content.strip()
    return result


def parse_all_tool_calls(content: str) -> "list[tuple[str, Dict[str, Any]]]":
    """Extract ALL tool calls from an LLM response, in order.

    Enables the model to request several tools in a single turn so they can be
    executed in parallel. Recognizes the primary `<tool_call>{json}</tool_call>`
    format (optionally fenced with ```json). Returns a list of
    (tool_name, arguments) tuples; empty if none found.
    """
    def _collect(blob: str, out: "list[tuple[str, Dict[str, Any]]]") -> None:
        try:
            data = json.loads(blob.strip())
        except json.JSONDecodeError:
            return
        if isinstance(data, dict) and data.get("name"):
            out.append((data["name"], data.get("arguments", {}) or {}))

    calls: "list[tuple[str, Dict[str, Any]]]" = []

    # Priority 1: explicit <tool_call>...</tool_call> blocks (tolerate ```json inside)
    for m in re.finditer(r'<tool_call>\s*(.*?)\s*</tool_call>', content, re.DOTALL):
        blob = m.group(1).strip()
        fence = re.match(r'```(?:json)?\s*(.*?)\s*```$', blob, re.DOTALL)
        if fence:
            blob = fence.group(1).strip()
        _collect(blob, calls)
    if calls:
        return calls

    # Priority 2: bare ```json ... ``` fenced blocks (model omitted the tags)
    for m in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL):
        _collect(m.group(1), calls)
    if calls:
        return calls

    # Priority 3: bare JSON objects carrying name + arguments keys
    for m in re.finditer(r'\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*"arguments"\s*:\s*(\{[^{}]*\})[^{}]*\}',
                         content, re.DOTALL):
        try:
            args = json.loads(m.group(2))
        except json.JSONDecodeError:
            args = {}
        calls.append((m.group(1), args))
    return calls


def text_before_tool_calls(content: str) -> str:
    """Return any prose that precedes the first tool call (tags, fence, or bare)."""
    positions = []
    for pat in (r'<tool_call>', r'```(?:json)?\s*\{', r'\{[^{}]*"name"\s*:'):
        m = re.search(pat, content)
        if m:
            positions.append(m.start())
    return content[:min(positions)].strip() if positions else content.strip()


def _parse_kv_args(args_str: str) -> Dict[str, Any]:
    """Parse key=value argument pairs."""
    if not args_str:
        return {}
    args = {}
    # Split on commas that aren't inside quotes or brackets
    parts = re.split(r',\s*(?=\w+=)', args_str)
    for part in parts:
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # Try to parse as JSON for lists/dicts/numbers
        try:
            args[key] = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            args[key] = val
    return args


def format_tool_result(tool_name: str, result: Dict[str, Any]) -> str:
    """Format a tool result for injection back into the conversation."""
    return f"<tool_result>\n{json.dumps(result, indent=2, default=str)}\n</tool_result>"


def format_tool_results(pairs: "list[tuple[str, Dict[str, Any]]]") -> str:
    """Format multiple (tool_name, result) pairs for injection in one message."""
    blocks = []
    for name, result in pairs:
        blocks.append(
            f'<tool_result name="{name}">\n'
            f"{json.dumps(result, indent=2, default=str)}\n"
            f"</tool_result>"
        )
    return "\n".join(blocks)
