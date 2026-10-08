#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
examples/banking_client.py
Banking Assistant — natural-language client (Agent Framework DevUI).

DevUI exposes the OpenAI *Responses* API (POST /v1/responses), not
/v1/chat/completions. You address the agent by its entity id (from /v1/entities)
in `metadata.entity_id`, and all /v1/* routes require a bearer token.

Sending natural-language prompts causes the agent's LLM to call the banking MCP
tools (get_balances, get_transactions, get_loans, transfer_funds, ...) and answer
from the returned data.

Usage:
    AGENT_URL=http://localhost:8090 DEVUI_AUTH_TOKEN=<token> python3 banking_client.py

Uses only the Python standard library (no extra dependencies).
"""

import json
import os
import urllib.request

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8090").rstrip("/")
TOKEN = os.environ.get("DEVUI_AUTH_TOKEN", "")
if not TOKEN:
    raise SystemExit("Set DEVUI_AUTH_TOKEN (printed by deploy.sh).")

_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# Natural-language prompts that should trigger MCP tool calls. Ada Lovelace is
# user U1001 with checking ACC-1001-CHK and savings ACC-1001-SAV in the demo data.
PROMPTS = [
    "My user id is U1001. What are the balances across all my accounts?",
    "Show me the 5 most recent transactions on account ACC-1001-CHK.",
    "For user U1001, when is the next mortgage payment due and how much is left on it?",
    "I'm user U1001. Please transfer $200 from ACC-1001-CHK to ACC-1001-SAV. I confirm.",
]


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


def ask(eid: str, prompt: str) -> str:
    resp = _post("/v1/responses", {"input": prompt, "metadata": {"entity_id": eid}, "stream": False})
    if resp.get("status") and resp["status"] != "completed":
        return f"[status:{resp['status']}] {resp.get('error')}"
    out = []
    for it in resp.get("output", []):
        if it.get("type") == "message":
            for c in it.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    out.append(c.get("text", ""))
    return "\n".join(out).strip()


def main() -> None:
    print(f"Target: {AGENT_URL}\n")
    eid = entity_id()
    for i, prompt in enumerate(PROMPTS, 1):
        print(f"=== Query {i} ===")
        print(f"User: {prompt}")
        print(f"Assistant: {ask(eid, prompt)}\n")


if __name__ == "__main__":
    main()
