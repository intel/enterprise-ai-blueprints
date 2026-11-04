#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# examples/generate_code.sh
# curl-based examples for the Coding Agent (LangGraph) API.
# Usage: AUTH_TOKEN=<DEVUI_AUTH_TOKEN> ./generate_code.sh [AGENT_URL]
# Default AGENT_URL: http://localhost:8090 (port-forward). Via the AI Solutions
# gateway use https://coding-agent-langgraph.<base_domain_name> (self-signed CA:
# CURL_OPTS=-k or --cacert env/<env>/logs/ai-solutions-ca.crt).

set -euo pipefail
AGENT="${1:-http://localhost:8090}"
AUTH=(-H "Authorization: Bearer ${AUTH_TOKEN:-}")
read -ra EXTRA <<<"${CURL_OPTS:-}"

echo "=== Coding Agent (LangGraph) examples ==="
echo "Target: $AGENT"
echo ""

# ── Health check (no auth) ───────────────────────────────────────────────────
echo "--- Health check ---"
curl -s "${EXTRA[@]}" "${AGENT}/health" | python3 -m json.tool
echo ""

# ── Generate and run Python code in the sandbox (Responses API) ──────────────
echo "--- Responses API: Sieve of Eratosthenes ---"
curl -s "${EXTRA[@]}" -X POST "${AGENT}/v1/responses" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{
    "input": "Write a Python function that finds all primes up to n using the Sieve of Eratosthenes, run it, and print the primes up to 50.",
    "metadata": {"thread_id": "examples-sh"}
  }' | python3 -c "
import sys, json
for item in json.load(sys.stdin).get('output', []):
    if item['type'] == 'message':
        print(item['content'][0]['text'])
    elif item['type'] == 'function_call_output':
        print('[tool output]', item['output'][:500])
"
echo ""

# ── OpenAI-compatible chat completions (runs the agent) ──────────────────────
echo "--- Chat completions ---"
curl -s "${EXTRA[@]}" -X POST "${AGENT}/v1/chat/completions" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "In one sentence, what is a Python decorator?"}
    ]
  }' | python3 -c "
import sys, json
print(json.load(sys.stdin)['choices'][0]['message']['content'])
"
echo ""
