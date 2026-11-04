#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# examples/research_query.sh
# curl-based examples for the Deep Research Agent API.
#
# Usage: ./research_query.sh [BASE_URL] ["research question"]
#   BASE_URL default: http://localhost:8081
#
# Set API_KEY when the deployment configures API_KEYS (app-level auth).
#   API_KEY=mykey ./research_query.sh http://localhost:8081

set -euo pipefail

AGENT="${1:-http://localhost:8081}"
QUERY="${2:-What are the latest advances in solid-state battery technology?}"
API_KEY="${API_KEY:-}"

# /health is public; /api/* requires the header only when API_KEYS is set.
AUTH=()
if [[ -n "${API_KEY}" ]]; then
    AUTH=(-H "X-API-Key: ${API_KEY}")
fi

echo "=== Deep Research Agent examples ==="
echo "Target: ${AGENT}"
echo ""

# ── Health check ─────────────────────────────────────────────────────────────
# "vllm" = the configured LLM endpoint, "tavily" = the search provider.
# Both must be "ok" for research to succeed.
echo "--- Health check ---"
curl -s "${AGENT}/health" | python3 -m json.tool
echo ""

# ── Model backend status (primary + secondary slots) ─────────────────────────
echo "--- Model backend status ---"
curl -s "${AUTH[@]}" "${AGENT}/api/system/status" | python3 -m json.tool
echo ""

# ── Start a research run ─────────────────────────────────────────────────────
echo "--- Starting research ---"
echo "Query: ${QUERY}"
PAYLOAD=$(QUERY="${QUERY}" python3 -c 'import json, os; print(json.dumps({"query": os.environ["QUERY"]}))')
RESEARCH_ID=$(
  curl -s "${AUTH[@]}" -X POST "${AGENT}/api/research" \
    -H "Content-Type: application/json" \
    -d "${PAYLOAD}" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["research_id"])'
)
echo "research_id: ${RESEARCH_ID}"
echo ""

# ── Stream live progress (SSE) ───────────────────────────────────────────────
# -N disables curl's output buffering so events print as they arrive. The stream
# ends by itself when the run finishes; a run can take several minutes.
echo "--- Progress stream (SSE) ---"
curl -sN "${AUTH[@]}" "${AGENT}/api/research/${RESEARCH_ID}/stream" | python3 -c '
import json, sys

for line in sys.stdin:
    if not line.startswith("data: "):
        continue
    e = json.loads(line[6:])
    if e.get("thinking_chunk"):          # token-level noise — skip
        continue
    status = str(e.get("status", "")).upper()
    pct = e.get("progress_pct") or 0.0
    text = " ".join(p for p in (e.get("message", ""), e.get("detail", "")) if p)
    print("[%12s] %5.1f%% %s" % (status, pct, text), flush=True)
'
echo ""

# ── Fetch the finished report ────────────────────────────────────────────────
echo "--- Final report ---"
curl -s "${AUTH[@]}" "${AGENT}/api/research/${RESEARCH_ID}" | python3 -c '
import json, sys

s = json.load(sys.stdin)
print("status=%s  sources=%d  claims=%d  citations=%d\n" % (
    s.get("status"),
    len(s.get("evaluated_sources", [])),
    len(s.get("claims", [])),
    len(s.get("citations", [])),
))
print(s.get("report_markdown") or "(no report — error: %s)" % s.get("error"))
'
echo ""

# ── Session history ──────────────────────────────────────────────────────────
echo "--- Recent sessions ---"
curl -s "${AUTH[@]}" "${AGENT}/api/sessions?limit=5" | python3 -m json.tool
echo ""

# ── Search cache statistics ──────────────────────────────────────────────────
echo "--- Search cache stats ---"
curl -s "${AUTH[@]}" "${AGENT}/api/system/cache-stats" | python3 -m json.tool
echo ""

echo "=== Done ==="
echo ""
echo "Other endpoints:"
echo "  DELETE ${AGENT}/api/sessions   # clear session history"
echo "  DELETE ${AGENT}/api/cache      # clear the search cache"
