#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
examples/openai_compat.py
Coding Agent (LangGraph) — using the official OpenAI Python SDK pointed at the agent.

The agent's /v1/chat/completions endpoint runs the LangGraph agent (with its
sandbox tools) and returns an OpenAI-shaped response, so any OpenAI-compatible
client works. The `model` field is informational; the agent uses MODEL_NAME.

Usage:
    AGENT_URL=http://localhost:8090 AUTH_TOKEN=<DEVUI_AUTH_TOKEN> python3 openai_compat.py

Install dependency:
    pip install openai
"""

import os

from openai import OpenAI

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8090")
# The SDK sends this as "Authorization: Bearer ..." — the agent's DEVUI_AUTH_TOKEN.
API_KEY = os.environ.get("AUTH_TOKEN", "sk-placeholder")

client = OpenAI(base_url=f"{AGENT_URL}/v1", api_key=API_KEY)

# ── Example 1: simple coding Q&A ─────────────────────────────────────────────
print("=== Example 1: simple Q&A via OpenAI SDK ===")
response = client.chat.completions.create(
    model="qwen2-5-coder-14b",
    messages=[
        {"role": "system", "content": "You are an expert Python developer."},
        {"role": "user", "content": "Show me a one-liner to flatten a nested list in Python."},
    ],
    max_tokens=256,
)
print(response.choices[0].message.content)
print()

# ── Example 2: multi-turn conversation ────────────────────────────────────────
print("=== Example 2: multi-turn conversation ===")
messages = [
    {"role": "system", "content": "You are a helpful coding assistant."},
    {"role": "user", "content": "What is the time complexity of binary search?"},
]

r1 = client.chat.completions.create(
    model="qwen2-5-coder-14b",
    messages=messages,
    max_tokens=128,
)
reply1 = r1.choices[0].message.content
print(f"Turn 1: {reply1}\n")

messages.append({"role": "assistant", "content": reply1})
messages.append({"role": "user", "content": "Now show me a Python implementation."})

r2 = client.chat.completions.create(
    model="qwen2-5-coder-14b",
    messages=messages,
    max_tokens=512,
)
print(f"Turn 2:\n{r2.choices[0].message.content}\n")

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

review = client.chat.completions.create(
    model="qwen2-5-coder-14b",
    messages=[
        {"role": "system", "content": "You are a Python code reviewer. Be concise."},
        {"role": "user", "content": f"Review this code:\n```python{code_to_review}```"},
    ],
    max_tokens=256,
)
print(review.choices[0].message.content)
