#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
examples/generate_code.py
Coding Agent (LangGraph) — Python client examples using httpx.

Usage:
    AUTH_TOKEN=<DEVUI_AUTH_TOKEN> python3 generate_code.py [AGENT_URL]
    Default AGENT_URL: http://localhost:8090 (port-forward). Via the AI Solutions
    gateway: https://coding-agent-langgraph.<base_domain_name> (set VERIFY=false
    for the default self-signed CA, or VERIFY=<path to ai-solutions-ca.crt>).

Install dependency:
    pip install httpx
"""

import json
import os
import sys

import httpx

AGENT_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8090"
_verify = os.getenv("VERIFY", "true")
VERIFY = {"true": True, "false": False}.get(_verify.lower(), _verify)
CLIENT = httpx.Client(
    base_url=AGENT_URL,
    timeout=300.0,
    verify=VERIFY,
    headers={"Authorization": f"Bearer {os.getenv('AUTH_TOKEN', '')}"},
)


def health_check() -> dict:
    resp = CLIENT.get("/health")
    resp.raise_for_status()
    return resp.json()


def run_task(task: str, session_id: str = "examples-py") -> list[dict]:
    """Send a task to the agent (Responses API); returns the output items."""
    resp = CLIENT.post("/v1/responses", json={"input": task, "metadata": {"thread_id": session_id}})
    resp.raise_for_status()
    return resp.json()["output"]


def chat(messages: list[dict]) -> str:
    resp = CLIENT.post("/v1/chat/completions", json={"messages": messages})
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def pp(label: str, data) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print('=' * 60)
    print(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else data)


if __name__ == "__main__":
    pp("Health check", health_check())

    # The agent writes the code, runs it in an Agent Sandbox pod and reports back.
    pp("Fibonacci generator", run_task(
        "Write a Python generator that yields Fibonacci numbers, run it and print the first 15."
    ))

    # Same session_id keeps conversation state across calls.
    pp("Session continuity (1/2)", run_task(
        "Write a Python function called celsius_to_fahrenheit and call it with 100.", "demo-session-001"
    ))
    pp("Session continuity (2/2)", run_task(
        "Now call that same function with -40.", "demo-session-001"
    ))

    pp("Chat completions", chat([
        {"role": "user", "content": "Briefly explain the difference between a list and a tuple in Python."},
    ]))
