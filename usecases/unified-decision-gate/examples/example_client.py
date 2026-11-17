#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
examples/example_client.py
Unified Decision Gate — natural-language client using the OpenAI Python SDK.

The front door (the gate on :8090) exposes an OpenAI-compatible /v1/chat/completions
endpoint. Plain yes/no banking questions are answered by System-1 ("Yes." / "No.", no LLM
call); everything else goes to the banking assistant agent, whose LLM calls the banking MCP
tools (get_balances, get_transactions, get_loans, transfer_funds, ...) and answers from the
returned data. Every request needs the DevUI token as its API key.

Usage:
    DEVUI_AUTH_TOKEN=<token from .env or config/agent-config.env> AGENT_URL=http://localhost:8090 python3 examples/example_client.py

Install dependency:
    pip install openai
"""

import os
import time
import uuid

from openai import OpenAI

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8090")
# The OpenAI SDK sends api_key as "Authorization: Bearer <key>", which is exactly what the gate
# checks. It is the DevUI token, not a LiteLLM key.
API_KEY = os.environ.get("DEVUI_AUTH_TOKEN") or os.environ.get("OPENAI_API_KEY", "sk-placeholder")
# Model id registered in LiteLLM (must support tool calling). Match your deploy.
MODEL = "banking-assistant"   # the gate routes by content; the agent picks its entity by name

# X-Session-Id groups this run's requests into one conversation in the gate's decision log.
# The convention for any client (the router included): one id per customer conversation.
SESSION_ID = os.environ.get("SESSION_ID") or f"example-{uuid.uuid4().hex[:12]}"
client = OpenAI(base_url=f"{AGENT_URL}/v1", api_key=API_KEY,
                default_headers={"X-Session-Id": SESSION_ID})

# Plain yes/no banking questions: System-1 answers these itself ("Yes." / "No."), with no LLM
# call.
YES_NO = [
    "Is an overdraft a form of borrowing?",
    "Is a credit card the same thing as cash?",
]

# Natural-language prompts that should trigger MCP tool calls: the gate sends them to the agent. Ada Lovelace is
# user U1001 with checking ACC-1001-CHK and savings ACC-1001-SAV in the demo data.
PROMPTS = [
    "My user id is U1001. What are the balances across all my accounts?",
    "Show me the 5 most recent transactions on account ACC-1001-CHK.",
    "For user U1001, when is the next mortgage payment due and how much is left on it?",
    "I'm user U1001. Please transfer $200 from ACC-1001-CHK to ACC-1001-SAV.",
]


def ask(prompt: str) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
    )
    return resp.choices[0].message.content


def main() -> None:
    print(f"Target: {AGENT_URL}\n")
    for prompt in YES_NO:
        t0 = time.perf_counter()
        answer = ask(prompt)
        print("=== Yes/no question (System-1) ===")
        print(f"User: {prompt}")
        print(f"Assistant: {answer}   ({time.perf_counter() - t0:.2f} s)\n")
    for i, prompt in enumerate(PROMPTS, 1):
        print(f"=== Query {i} ===")
        print(f"User: {prompt}")
        print(f"Assistant: {ask(prompt)}\n")


if __name__ == "__main__":
    main()
