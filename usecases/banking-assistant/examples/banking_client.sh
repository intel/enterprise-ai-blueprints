#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# examples/banking_client.sh
# curl-based examples for the Banking Assistant (Agent Framework DevUI).
#
# DevUI exposes the OpenAI *Responses* API (POST /v1/responses), not
# /v1/chat/completions. All /v1/* routes require a bearer token.
#
# Usage:
#   DEVUI_AUTH_TOKEN=<token> ./banking_client.sh [AGENT_URL]
# Default AGENT_URL: http://localhost:8090
set -euo pipefail

AGENT="${1:-http://localhost:8090}"
TOKEN="${DEVUI_AUTH_TOKEN:-}"
[[ -n "$TOKEN" ]] || { echo "Set DEVUI_AUTH_TOKEN (printed by deploy.sh)."; exit 1; }
AUTH=(-H "Authorization: Bearer ${TOKEN}")

echo "=== Banking Assistant examples ==="
echo "Target: $AGENT"
echo ""

# ── Health check (no auth) ────────────────────────────────────────────────────
echo "--- Health check ---"
curl -s "${AGENT}/health" | python3 -m json.tool || echo "(health endpoint not available)"
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
    if it.get('type') == 'message':
        for c in it.get('content', []):
            if c.get('type') in ('output_text', 'text'):
                print(c.get('text'))
"
}

# ── Balances across accounts (triggers get_balances) ─────────────────────────
echo "--- Balances for user U1001 ---"
ask "My user id is U1001. What are the balances across all my accounts?"
echo ""

# ── Recent transactions (triggers get_transactions) ──────────────────────────
echo "--- Recent transactions on ACC-1001-CHK ---"
ask "Show me the 5 most recent transactions on account ACC-1001-CHK."
echo ""

# ── Loan summary (triggers get_loans / get_loan_summary) ─────────────────────
echo "--- Mortgage status for user U1001 ---"
ask "For user U1001, when is the next mortgage payment due and how much is left on it?"
echo ""

# ── Transfer (triggers transfer_funds — agent should confirm first) ──────────
echo "--- Transfer request for user U1001 ---"
ask "I'm user U1001. Please transfer 200 dollars from ACC-1001-CHK to ACC-1001-SAV. I confirm."
echo ""
