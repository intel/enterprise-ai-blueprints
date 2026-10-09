#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# examples/generate_code.sh
# curl-based examples for the Coding Agent (Agent Framework DevUI).
#
# DevUI exposes the OpenAI *Responses* API (POST /v1/responses), not /v1/code or
# /v1/chat/completions. All /v1/* routes require a bearer token. The agent
# generates code and runs it in a Kubernetes sandbox via its tools.
#
# Usage:
#   DEVUI_AUTH_TOKEN=<token> ./generate_code.sh [AGENT_URL]
# Default AGENT_URL: http://localhost:8090
set -euo pipefail

AGENT="${1:-http://localhost:8090}"
TOKEN="${DEVUI_AUTH_TOKEN:-}"
[[ -n "$TOKEN" ]] || { echo "Set DEVUI_AUTH_TOKEN (printed by deploy.sh)."; exit 1; }
AUTH=(-H "Authorization: Bearer ${TOKEN}")

echo "=== Coding Agent examples ==="
echo "Target: $AGENT"
echo ""

# ── Health check (no auth) ────────────────────────────────────────────────────
echo "--- Health check ---"
curl -s "${AGENT}/health" | python3 -m json.tool
echo ""

# ── Resolve the agent entity id ───────────────────────────────────────────────
EID=$(curl -s "${AGENT}/v1/entities" "${AUTH[@]}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['entities'][0]['id'])")
echo "Entity: $EID"
echo ""

ask() {
  local prompt="$1"
  curl -s -X POST "${AGENT}/v1/responses" "${AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "import json,sys;print(json.dumps({'input':sys.argv[1],'metadata':{'entity_id':sys.argv[2]},'stream':False}))" "$prompt" "$EID")" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
if d.get('status') and d['status'] != 'completed':
    print('[status:%s] %s' % (d['status'], d.get('error')))
for it in d.get('output', []):
    if it.get('type') == 'function_call':
        print('[tool] %s' % it.get('name'))
    elif it.get('type') == 'message':
        for c in it.get('content', []):
            if c.get('type') in ('output_text', 'text'):
                print(c.get('text'))
"
}

# ── Generate and run Python code ─────────────────────────────────────────────
echo "--- Python: Sieve of Eratosthenes (generate + execute) ---"
ask "Write a function that finds all primes up to n with the Sieve of Eratosthenes, run it in the sandbox, and print the primes up to 50."
echo ""

# ── Generate and run a shell command in the sandbox ──────────────────────────
echo "--- Install a package and use it ---"
ask "Install the 'sympy' package in the sandbox, then use it to list the first 10 prime numbers."
echo ""
