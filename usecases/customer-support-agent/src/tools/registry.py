# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tool registry — register, describe, and dispatch tool calls."""
from __future__ import annotations
import inspect
import json
from typing import Any, Callable, Dict, List, Optional


_tools: Dict[str, Dict[str, Any]] = {}


def tool(name: str, description: str, parameters: Dict[str, Any]):
    """Decorator to register a function as a tool."""
    def decorator(fn: Callable):
        _tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "function": fn,
        }
        return fn
    return decorator


def get_tool(name: str) -> Optional[Dict[str, Any]]:
    return _tools.get(name)


def list_tools() -> List[Dict[str, Any]]:
    return [
        {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
        for t in _tools.values()
    ]


def execute_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a registered tool and return its result."""
    entry = _tools.get(name)
    if not entry:
        return {"error": f"Unknown tool: {name}", "available_tools": list(_tools.keys())}
    try:
        fn = entry["function"]
        result = fn(**arguments)
        return {"success": True, "data": result}
    except TypeError as e:
        return {"error": f"Invalid arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"Tool execution failed: {e}"}


def format_tools_for_prompt() -> str:
    """Generate tool descriptions for the system prompt."""
    lines = []
    for t in _tools.values():
        params = t["parameters"].get("properties", {})
        required = t["parameters"].get("required", [])
        param_strs = []
        for pname, pinfo in params.items():
            req_marker = " (required)" if pname in required else " (optional)"
            param_strs.append(f"    - {pname}: {pinfo.get('type', 'string')} — {pinfo.get('description', '')}{req_marker}")
        params_block = "\n".join(param_strs) if param_strs else "    (no parameters)"
        lines.append(f"  {t['name']}: {t['description']}\n  Parameters:\n{params_block}")
    return "\n\n".join(lines)


def import_all_tools():
    """Import all tool modules to trigger registration."""
    from tools import (
        order_tools,
        refund_tools,
        product_tools,
        account_tools,
        escalation_tools,
        knowledge_base_tools,
        inventory_tools,
        shipping_tools,
        promotion_tools,
        warranty_tools,
        communication_tools,
    )
