# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Run 10 diverse research queries against the deep-research API (port 8081).

Each query is submitted via POST /api/research, then the SSE stream is consumed
to show live progress.  When the run completes the report is printed.  Metrics
are automatically written to results/live/ by the server for the dashboard.

Usage:
    python eval/eval_research_queries.py                   # run all 10
    python eval/eval_research_queries.py --indices 3,5,7   # run subset (1-based)
    python eval/eval_research_queries.py --query "custom"  # single query
    python eval/eval_research_queries.py --api http://host:8081  # custom server
"""

import argparse
import json
import sys
import time
from typing import Generator

import httpx

# ── Canonical 10-query set ────────────────────────────────────────────────────
QUERIES: list[str] = [
    "How do Intel GPUs compare to NVIDIA GPUs for LLM inference in terms of cost,"
    " performance, and ecosystem support?",
    "What does academic research say about how remote work affects productivity in"
    " software engineering teams?",
    "For a typical driver in Europe, is owning an electric car actually cheaper than"
    " a hybrid over a 5-year period?",
    "What techniques are currently used to handle extremely long contexts (100k+"
    " tokens) in LLM systems, and how do they compare?",
    "How competitive are open-source LLMs with proprietary models in real-world"
    " enterprise use cases?",
    "What does sports science research say about the injury rate in CrossFit"
    " compared to other training methods like weightlifting or running?",
    "How is the rise of AI coding assistants affecting software engineering jobs and"
    " productivity?",
    "Why is Python dominant in AI development, and are languages like Rust or Julia"
    " becoming viable alternatives?",
    "How has the use of three-point shooting changed NBA team strategies over the"
    " last 20 years?",
    "Does the type of wood used in an electric guitar significantly affect tone"
    " according to scientific measurements and expert opinions?",
]

# ── ANSI helpers ──────────────────────────────────────────────────────────────
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
_RESET = "\033[0m"

TIMEOUT_S = 600  # max seconds per query


def _sse_events(client: httpx.Client, url: str) -> Generator[dict, None, None]:
    """Yield parsed JSON objects from an SSE stream."""
    with client.stream("GET", url, timeout=TIMEOUT_S) as resp:
        resp.raise_for_status()
        buf = ""
        for chunk in resp.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                data_line = next(
                    (ln[6:] for ln in block.splitlines() if ln.startswith("data:")),
                    None,
                )
                if data_line:
                    try:
                        yield json.loads(data_line)
                    except json.JSONDecodeError:
                        pass


def _run_query(api_base: str, query: str, index: int, total: int) -> dict:
    """Submit one query and stream its progress.  Returns a small result dict."""
    print(f"\n{_BOLD}{'─'*72}{_RESET}")
    print(f"{_BOLD}[{index}/{total}] {query}{_RESET}")
    print(f"{_BOLD}{'─'*72}{_RESET}")

    client = httpx.Client(
        base_url=api_base,
        trust_env=False,   # never route through corporate proxy
        timeout=TIMEOUT_S,
    )

    # ── Submit ────────────────────────────────────────────────────────────────
    resp = client.post("/api/research", json={"query": query})
    resp.raise_for_status()
    research_id = resp.json()["research_id"]
    print(f"{_DIM}research_id: {research_id}{_RESET}")

    # ── Stream ────────────────────────────────────────────────────────────────
    status = "unknown"
    last_component = ""
    thinking_chars = 0
    t0 = time.monotonic()

    try:
        for event in _sse_events(client, f"/api/research/{research_id}/stream"):
            status = event.get("status", status)

            # Skip pure thinking-chunk events — just count chars
            chunk = event.get("thinking_chunk", "")
            if chunk:
                thinking_chars += len(chunk)
                continue

            msg = event.get("message", "").strip()
            component = event.get("component", "").strip()
            tps = event.get("tps", 0.0)
            elapsed = time.monotonic() - t0

            # Print component transitions and meaningful messages
            if component and component != last_component:
                last_component = component
                tps_str = f"  {_DIM}{tps:.1f} tok/s{_RESET}" if tps else ""
                print(f"  {_CYAN}▶ {component}{_RESET}{tps_str}")

            if msg and status not in ("complete", "failed"):
                print(f"    {_DIM}{msg}{_RESET}")

            if status == "complete":
                elapsed_s = time.monotonic() - t0
                hw = event.get("hw_summary", {})
                hw_parts = []
                for hw_name, stats in hw.items():
                    tps_val = stats.get("tps", 0.0)
                    out_tok = stats.get("out_tokens", stats.get("output_tokens", 0))
                    hw_parts.append(f"{hw_name} {out_tok} out-tok @ {tps_val:.1f} tok/s")
                hw_line = "  |  ".join(hw_parts) if hw_parts else ""
                print(
                    f"\n{_GREEN}✓ COMPLETE{_RESET}  {elapsed_s:.1f}s  "
                    f"thinking≈{thinking_chars//3} tok  {hw_line}"
                )
                break

            if status == "failed":
                detail = event.get("detail", event.get("message", ""))
                print(f"\n{_RED}✗ FAILED{_RESET}  {detail}")
                break

    except httpx.ReadTimeout:
        print(f"\n{_RED}✗ TIMEOUT after {TIMEOUT_S}s{_RESET}")
        status = "timeout"
    except KeyboardInterrupt:
        print(f"\n{_YELLOW}⚡ interrupted{_RESET}")
        raise

    # ── Fetch report ──────────────────────────────────────────────────────────
    report_preview = ""
    if status == "complete":
        try:
            r = client.get(f"/api/research/{research_id}")
            if r.status_code == 200:
                session = r.json()
                md = session.get("report_markdown", "")
                if md:
                    lines = md.strip().splitlines()
                    # Print first heading + first 3 content lines as preview
                    preview_lines = [ln for ln in lines if ln.strip()][:5]
                    report_preview = "\n".join(preview_lines)
                    print(f"\n{_DIM}Report preview:{_RESET}")
                    for ln in preview_lines:
                        print(f"  {ln}")
        except Exception as exc:
            print(f"  {_YELLOW}(could not retrieve report: {exc}){_RESET}")

    client.close()
    return {"research_id": research_id, "status": status, "query": query}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run research queries via the API")
    parser.add_argument("--api", default="http://localhost:8081", help="API base URL")
    parser.add_argument(
        "--indices",
        help="Comma-separated 1-based indices to run, e.g. 1,3,5 (default: all)",
    )
    parser.add_argument("--query", help="Run a single ad-hoc query instead of the list")
    args = parser.parse_args()

    if args.query:
        queries = [args.query]
    elif args.indices:
        try:
            idx_list = [int(x.strip()) for x in args.indices.split(",")]
            queries = [QUERIES[i - 1] for i in idx_list]
        except (ValueError, IndexError) as exc:
            print(f"Invalid --indices: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        queries = QUERIES

    total = len(queries)
    print(f"{_BOLD}deep-research eval — {total} quer{'y' if total == 1 else 'ies'}{_RESET}")
    print(f"API: {args.api}  |  Dashboard: http://localhost:8082")

    results = []
    t_start = time.monotonic()

    try:
        for i, q in enumerate(queries, 1):
            result = _run_query(args.api, q, i, total)
            results.append(result)
    except KeyboardInterrupt:
        print(f"\n{_YELLOW}Interrupted — {len(results)}/{total} completed{_RESET}")

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed_total = time.monotonic() - t_start
    n_ok = sum(1 for r in results if r["status"] == "complete")
    n_fail = len(results) - n_ok

    print(f"\n{_BOLD}{'═'*72}{_RESET}")
    print(f"{_BOLD}SUMMARY{_RESET}  {n_ok}/{len(results)} complete  {n_fail} failed  "
          f"total {elapsed_total:.0f}s  avg {elapsed_total/len(results):.0f}s/query" if results
          else f"{_BOLD}SUMMARY{_RESET}  no queries completed")
    print(f"Metrics → http://localhost:8082")
    for r in results:
        icon = _GREEN + "✓" if r["status"] == "complete" else _RED + "✗"
        print(f"  {icon}{_RESET}  {r['query'][:70]}")
    print()


if __name__ == "__main__":
    main()
