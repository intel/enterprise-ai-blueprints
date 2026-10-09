#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
examples/generate_code.py
Coding Agent — Python client (Agent Framework DevUI).

DevUI exposes the OpenAI *Responses* API (POST /v1/responses), not /v1/code or
/v1/chat/completions. You address the agent by its entity id (from /v1/entities)
in `metadata.entity_id`, and all /v1/* routes require a bearer token. The agent
generates code and runs it in a Kubernetes sandbox via its tools.

Usage:
    DEVUI_AUTH_TOKEN=<token> python3 generate_code.py [AGENT_URL]
    Default AGENT_URL: http://localhost:8090

Uses only the Python standard library (no extra dependencies).
"""

import json
import os
import sys
import urllib.request

AGENT_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8090").rstrip("/")
TOKEN = os.environ.get("DEVUI_AUTH_TOKEN", "")
if not TOKEN:
    raise SystemExit("Set DEVUI_AUTH_TOKEN (printed by deploy.sh).")

_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{AGENT_URL}{path}", headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{AGENT_URL}{path}", data=json.dumps(body).encode(), headers=_HEADERS, method="POST"
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def entity_id() -> str:
    return _get("/v1/entities")["entities"][0]["id"]


def run(eid: str, prompt: str) -> None:
    resp = _post("/v1/responses", {"input": prompt, "metadata": {"entity_id": eid}, "stream": False})
    if resp.get("status") and resp["status"] != "completed":
        print(f"[status:{resp['status']}] {resp.get('error')}")
    for it in resp.get("output", []):
        t = it.get("type")
        if t == "function_call":
            print(f"[tool call] {it.get('name')}")
        elif t == "message":
            for c in it.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    print(c.get("text"))


def pp(label: str) -> None:
    print(f"\n{'=' * 60}\n  {label}\n{'=' * 60}")


if __name__ == "__main__":
    pp("Health check")
    print(json.dumps(_get("/health"), indent=2))

    eid = entity_id()
    print(f"\nEntity: {eid}")

    pp("Example 1 — Fibonacci (generate + execute)")
    run(eid, "Write a Python generator that yields Fibonacci numbers, run it in the "
             "sandbox, and print the first 15 Fibonacci numbers.")

    pp("Example 2 — Word frequency (generate + execute)")
    run(eid, "In the sandbox, count and print word frequencies (sorted, descending) for: "
             "'the quick brown fox jumps over the lazy dog the fox'.")

    pp("Example 3 — Install a package and use it")
    run(eid, "Install the 'sympy' package in the sandbox, then compute and print "
             "the factorial of 20 using it.")
