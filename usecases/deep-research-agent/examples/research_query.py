#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
examples/research_query.py
Deep Research Agent — run a research query against a deployed instance.

Flow (this is exactly what the bundled web UI does):
  1. POST /api/research            -> {"research_id": ..., "status": "pending"}
  2. GET  /api/research/{id}/stream -> Server-Sent Events with live progress
  3. GET  /api/research/{id}        -> the finished session incl. the markdown report

Usage:
    python3 research_query.py [BASE_URL] [--query "..."] [--api-key KEY] [--no-summarize]

    # defaults to http://localhost:8081 and a built-in sample query
    python3 research_query.py
    python3 research_query.py http://localhost:8081 \
        --query "What are the latest advances in solid-state batteries?"
    python3 research_query.py https://deep-research-agent.example.com --api-key mykey

Install dependency:
    pip install httpx
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

DEFAULT_BASE_URL = "http://localhost:8081"
DEFAULT_QUERY = "What are the latest advances in solid-state battery technology?"

# A research run does several LLM calls plus web fetches — minutes, not seconds.
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=30.0, pool=10.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a deep research query against a deployed Deep Research Agent.",
    )
    parser.add_argument(
        "base_url",
        nargs="?",
        default=DEFAULT_BASE_URL,
        help=f"Agent base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument("--query", default=DEFAULT_QUERY, help="The research question.")
    parser.add_argument(
        "--api-key",
        default="",
        help="X-API-Key value. Only needed when the deployment sets API_KEYS.",
    )
    parser.add_argument(
        "--no-summarize",
        action="store_true",
        help="Skip webpage summarization (feeds raw page text to the model instead).",
    )
    return parser.parse_args()


def print_event(event: dict) -> None:
    """Render one progress event on stderr so stdout stays report-only."""
    # Token-level "thinking" chunks are noisy; the UI streams them, we skip them.
    if event.get("thinking_chunk"):
        return

    status = str(event.get("status", "")).upper()
    pct = event.get("progress_pct") or 0.0
    parts = [f"[{status:>12}] {pct:5.1f}%"]
    if event.get("component"):
        parts.append(f"({event['component']})")
    if event.get("message"):
        parts.append(event["message"])
    if event.get("detail"):
        parts.append(f"— {event['detail']}")
    print(" ".join(parts), file=sys.stderr, flush=True)

    if event.get("step_done"):
        print(
            f"               tokens: {event.get('step_input_tokens', 0)} in / "
            f"{event.get('step_output_tokens', 0)} out"
            f"  model={event.get('step_model') or '?'}"
            f"  hw={event.get('step_hardware') or '?'}",
            file=sys.stderr,
            flush=True,
        )


def main() -> int:
    args = parse_args()
    base = args.base_url.rstrip("/")
    headers = {"X-API-Key": args.api_key} if args.api_key else {}

    with httpx.Client(base_url=base, headers=headers, timeout=REQUEST_TIMEOUT) as client:
        # --- 0. Health check (public endpoint — never requires an API key) ---
        health = client.get("/health").json()
        print(f"Health: {json.dumps(health)}", file=sys.stderr)
        if health.get("status") != "healthy":
            print(
                "WARNING: agent reports 'degraded'. Check LLM_BASE_URL/LLM_API_KEY and "
                "TAVILY_API_KEY — research will likely fail.",
                file=sys.stderr,
            )

        # --- 1. Start the research run ---
        payload: dict = {"query": args.query}
        if args.no_summarize:
            payload["summarize"] = False

        resp = client.post("/api/research", json=payload)
        resp.raise_for_status()
        research_id = resp.json()["research_id"]
        print(f"\nResearch started: {research_id}\nQuery: {args.query}\n", file=sys.stderr)

        # --- 2. Follow progress over SSE until the stream closes ---
        final_status = ""
        with client.stream("GET", f"/api/research/{research_id}/stream") as stream:
            stream.raise_for_status()
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                print_event(event)
                final_status = str(event.get("status", ""))

        # --- 3. Fetch the completed session (report + citations) ---
        session = client.get(f"/api/research/{research_id}").json()

    if session.get("error"):
        print(f"\nResearch failed: {session['error']}", file=sys.stderr)
        return 1

    report = session.get("report_markdown", "")
    citations = session.get("citations", [])

    print("\n" + "=" * 78, file=sys.stderr)
    print(
        f"status={session.get('status') or final_status}  "
        f"sources={len(session.get('evaluated_sources', []))}  "
        f"claims={len(session.get('claims', []))}  citations={len(citations)}",
        file=sys.stderr,
    )
    print("=" * 78 + "\n", file=sys.stderr)

    print(report or "(no report produced)")
    return 0 if report else 1


if __name__ == "__main__":
    sys.exit(main())
