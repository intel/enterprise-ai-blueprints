# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run 10 diverse queries through main_example.py and collect detailed metrics."""

import asyncio
import json
import os
import re
import subprocess
import sys
import time

QUERIES = [
    # Cat 1: Simple factual (2)
    "What is the capital of France?",
    "What is the speed of light in meters per second?",
    # Cat 2: Quick lookup (3)
    "Who won the 2024 Super Bowl?",
    "What happened in the news today?",
    "Is Python good?",
    # Cat 3: Research (5)
    "Compare PostgreSQL vs MongoDB for a new e-commerce platform",
    "What are the environmental impacts of lithium mining?",
    "How does intermittent fasting affect metabolic health?",
    "What are the health effects of microplastics on humans?",
    "Compare React vs Vue for enterprise applications in 2025",
]

TIMEOUT = 600  # seconds per query


def parse_log(stderr: str) -> dict:
    """Extract metrics from stderr log output."""
    metrics = {
        "category": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "llm_calls": 0,
        "iterations": 0,
        "max_input_tokens_single_call": 0,
        "outcome": "unknown",
    }

    # Extract classification
    m = re.search(r"Query classified as Cat (\d)", stderr)
    if m:
        metrics["category"] = int(m.group(1))

    # Extract total token usage from summary line
    m = re.search(r"TOKEN USAGE:\s*([\d,]+)\s*input\s*\+\s*([\d,]+)\s*output\s*=\s*([\d,]+)\s*total\s*\((\d+)\s*LLM calls\)", stderr)
    if m:
        metrics["input_tokens"] = int(m.group(1).replace(",", ""))
        metrics["output_tokens"] = int(m.group(2).replace(",", ""))
        metrics["total_tokens"] = int(m.group(3).replace(",", ""))
        metrics["llm_calls"] = int(m.group(4))

    # Count agent iterations
    iterations = re.findall(r"Agent iteration (\d+):", stderr)
    if iterations:
        metrics["iterations"] = max(int(i) for i in iterations)

    # Find max input tokens from individual LLM calls
    # Patterns: "Agent LLM usage: input_tokens=X" and "LLM usage (structured): input_tokens=X" and "LLM usage: input_tokens=X"
    input_per_call = re.findall(r"input_tokens=(\d+)", stderr)
    if input_per_call:
        metrics["max_input_tokens_single_call"] = max(int(x) for x in input_per_call)

    # Determine outcome
    if "timed out" in stderr.lower() or "TIMEOUT" in stderr:
        metrics["outcome"] = "TIMEOUT"
    elif "FAILED" in stderr:
        metrics["outcome"] = "FAILED"
    elif "Research complete" in stderr or "COMPLETE" in stderr:
        metrics["outcome"] = "SUCCESS"

    return metrics


def run_query(idx: int, query: str) -> dict:
    """Run a single query and return metrics."""
    print(f"\n[{idx+1}/10] Running: {query[:60]}...", file=sys.stderr)
    start = time.time()

    try:
        result = subprocess.run(
            ["python", "scripts/main_example.py", "--research_query", query],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
        )
        elapsed = time.time() - start
        stderr = result.stderr
        stdout = result.stdout
        timed_out = False
    except subprocess.TimeoutExpired as e:
        elapsed = time.time() - start
        stderr = e.stderr.decode() if e.stderr else ""
        stdout = e.stdout.decode() if e.stdout else ""
        timed_out = True

    metrics = parse_log(stderr)
    if timed_out:
        metrics["outcome"] = "TIMEOUT"

    metrics["query"] = query
    metrics["elapsed_s"] = round(elapsed, 1)
    metrics["report_chars"] = len(stdout.replace("=" * 80, "").replace("RESEARCH REPORT", "").strip())

    # Compute avg input tokens per call
    if metrics["llm_calls"] > 0:
        metrics["avg_input_per_call"] = round(metrics["input_tokens"] / metrics["llm_calls"])
    else:
        metrics["avg_input_per_call"] = 0

    print(f"  -> Cat {metrics['category']} | {metrics['outcome']} | {metrics['elapsed_s']}s | {metrics['total_tokens']} tokens | {metrics['llm_calls']} calls", file=sys.stderr)
    return metrics


def main():
    results = []
    for i, q in enumerate(QUERIES):
        m = run_query(i, q)
        results.append(m)

    # Print table
    print("\n" + "=" * 160)
    print(f"{'#':<3} {'Query':<55} {'Cat':>3} {'Outcome':<8} {'Input':>8} {'Output':>8} {'Calls':>5} {'AvgIn':>7} {'Iters':>5} {'MaxIn':>8} {'Time':>6} {'Report':>6}")
    print("-" * 160)
    for i, m in enumerate(results):
        q_short = m["query"][:53] + (".." if len(m["query"]) > 53 else "")
        print(
            f"{i+1:<3} {q_short:<55} {m['category']:>3} {m['outcome']:<8} "
            f"{m['input_tokens']:>8} {m['output_tokens']:>8} {m['llm_calls']:>5} "
            f"{m['avg_input_per_call']:>7} {m['iterations']:>5} "
            f"{m['max_input_tokens_single_call']:>8} {m['elapsed_s']:>5.0f}s {m['report_chars']:>5}c"
        )
    print("=" * 160)

    # Summary stats
    successes = [m for m in results if m["outcome"] == "SUCCESS"]
    timeouts = [m for m in results if m["outcome"] == "TIMEOUT"]
    total_tokens = sum(m["total_tokens"] for m in results)
    total_calls = sum(m["llm_calls"] for m in results)
    print(f"\nSuccess: {len(successes)}/10 | Timeouts: {len(timeouts)}/10 | Total tokens: {total_tokens:,} | Total LLM calls: {total_calls}")

    # Save raw JSON
    with open("eval_10queries_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Raw results saved to eval_10queries_results.json", file=sys.stderr)


if __name__ == "__main__":
    main()
