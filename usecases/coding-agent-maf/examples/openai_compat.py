#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
examples/openai_compat.py
Coding Agent — using the official OpenAI Python SDK against Agent Framework DevUI.

DevUI implements the OpenAI *Responses* API (`client.responses.create`), not
Chat Completions. You address the agent by its entity id (from /v1/entities) via
the `metadata.entity_id` field, and authenticate with the DevUI token as the API
key. The agent generates code and runs it in a Kubernetes sandbox via its tools.

Usage:
    AGENT_URL=http://localhost:8090 DEVUI_AUTH_TOKEN=<token> python3 openai_compat.py

Install dependency:
    pip install "openai>=1.66"   # needs the Responses API (client.responses)
"""

import json
import os
import urllib.request

from openai import OpenAI

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8090").rstrip("/")
TOKEN = os.environ.get("DEVUI_AUTH_TOKEN", "")
if not TOKEN:
    raise SystemExit("Set DEVUI_AUTH_TOKEN (printed by deploy.sh).")

# The DevUI token is presented as the API key (Bearer auth).
client = OpenAI(base_url=f"{AGENT_URL}/v1", api_key=TOKEN)


def entity_id() -> str:
    req = urllib.request.Request(
        f"{AGENT_URL}/v1/entities", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    return json.loads(urllib.request.urlopen(req).read())["entities"][0]["id"]


def ask(eid: str, prompt: str) -> str:
    # `model` is ignored by DevUI (the agent has its own configured model); the
    # agent is selected via metadata.entity_id.
    resp = client.responses.create(model="agent", input=prompt, metadata={"entity_id": eid})
    return resp.output_text


EID = entity_id()

# ── Example 1: simple coding Q&A ─────────────────────────────────────────────
print("=== Example 1: simple Q&A via the OpenAI SDK (Responses API) ===")
print(ask(EID, "Show me a one-liner to flatten a nested list in Python."))
print()

# ── Example 2: generate and execute code in the sandbox ──────────────────────
print("=== Example 2: generate + execute code in the sandbox ===")
print(ask(EID, "Write Python to compute the factorial of 10, run it in the sandbox, "
               "and report the exact result."))
print()

# ── Example 3: code review ────────────────────────────────────────────────────
print("=== Example 3: code review ===")
code_to_review = """
def find_max(lst):
    max_val = lst[0]
    for i in lst:
        if i > max_val:
            max_val = i
    return max_val
"""
print(ask(EID, f"Review this Python code and suggest improvements, concisely:\n{code_to_review}"))
