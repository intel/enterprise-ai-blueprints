#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# examples/example_client.sh
# curl-based examples for Unified Decision Gate (the gate, in front of the banking assistant).
# Usage: ./examples/example_client.sh [AGENT_URL]
# Default AGENT_URL: http://localhost:8090
#
# Set DEVUI_AUTH_TOKEN to the token deploy.sh printed (or the one in .env / config/agent-config.env):
# every request needs it as a Bearer token.

set -euo pipefail
AGENT="${1:-http://localhost:8090}"
MODEL="banking-assistant"   # the gate routes by content; the agent picks its entity by name
TOKEN="${DEVUI_AUTH_TOKEN:?set DEVUI_AUTH_TOKEN to the token in .env or config/agent-config.env}"
# One id per customer conversation, so the System-1 gate's decision log can group the requests.
SESSION_ID="${SESSION_ID:-example-$(date +%s)-$$}"

echo "=== Unified Decision Gate examples ==="
echo "Target: $AGENT"
echo ""

# ── Health check ─────────────────────────────────────────────────────────────
echo "--- Health check ---"
curl -s "${AGENT}/health" | python3 -m json.tool || echo "(health endpoint not available)"
echo ""

ask() {
  local prompt="$1"
  curl -s -X POST "${AGENT}/v1/chat/completions" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-Session-Id: ${SESSION_ID}" \
    -H "Content-Type: application/json" \
    -d "{
      \"model\": \"${MODEL}\",
      \"messages\": [
        {\"role\": \"user\", \"content\": \"${prompt}\"}
      ],
      \"max_tokens\": 512
    }" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data['choices'][0]['message']['content'])
"
}

# ── Plain yes/no questions: System-1 answers these itself, no LLM call ───────
echo "--- Yes/no questions (answered by System-1) ---"
ask "Is an overdraft a form of borrowing?"
ask "Is a credit card the same thing as cash?"
echo ""

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
ask "I'm user U1001. Please transfer 200 dollars from ACC-1001-CHK to ACC-1001-SAV."
echo ""
