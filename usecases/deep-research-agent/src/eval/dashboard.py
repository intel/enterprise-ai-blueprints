# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Live query metrics dashboard.

Watches results/live/ for metrics written by any source (UI, CLI, API)
and serves them in a web dashboard on port 8082.

Usage:
  python eval/eval_fresh_10.py [--port 8082] [--clear] [--serve-only]
"""

import argparse
import http.server
import json
import os
import pathlib
import re
import sys
import threading
import time

# Live metrics dir. RESULTS_DIR is set by the container/Helm chart to the
# mounted volume (e.g. /app/data/results/live) — the same value token_tracker
# writes to. Falls back to <app>/results/live for local runs.
LIVE_DIR = pathlib.Path(
    os.getenv("RESULTS_DIR") or pathlib.Path(__file__).parent.parent / "results" / "live"
)

LIVE_DIRS: list[pathlib.Path] = [LIVE_DIR]


# ── Stats helpers ─────────────────────────────────────────────

def _stats(values: list) -> dict:
    """Return dict with avg, med, min, max, p99. All None if empty."""
    if not values:
        return {"avg": None, "med": None, "min": None, "max": None, "p99": None}
    sv = sorted(values)
    n = len(sv)
    avg = sum(sv) / n
    med = sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2
    p99_idx = max(0, min(n - 1, int(n * 0.99)))
    return {
        "avg": round(avg, 3),
        "med": round(med, 3),
        "min": round(sv[0], 3),
        "max": round(sv[-1], 3),
        "p99": round(sv[p99_idx], 3),
    }


# ── Row computation ───────────────────────────────────────────

_COMP_DEFS = [
    ("classify",       re.compile(r"^Semantic routing|^classify_node")),
    ("agent",          re.compile(r"^agent_node")),
    ("eval_src",       re.compile(r"^evaluate_sources")),
    ("analyze_gaps",   re.compile(r"^analyze_gaps")),
    ("extract_claims", re.compile(r"^extract_claims")),
    ("write_report",   re.compile(r"^write_report")),
]


# Maps comp_key -> module name in secondary_llm_modules (None = always XPU)
_SECONDARY_TOOL_FOR_COMP: dict = {
    "classify":      "classify",
    "agent":         None,
    "eval_src":      "evaluate_sources",
    "analyze_gaps":  "analyze_gaps",
    "extract_claims":"extract_claims",
    "write_report":  "write_report",
}

def _call_hw(call: dict, mod_name, sec_set: set) -> str:
    """Return hw for a call_log entry, falling back to secondary_llm_modules config."""
    stored = call.get("hw")
    if stored:
        return stored
    if mod_name is None:
        return "XPU"
    return "Xeon" if mod_name in sec_set else "XPU"


def _compute_row(idx: int, m: dict, workspace: str = "") -> dict:
    """Map a live metrics JSON dict to a dashboard status row."""
    call_log = m.get("call_log", [])
    tool_times = m.get("tool_times", {})
    elapsed_s = m.get("elapsed_s", 0) or 0

    # Per-call stats (E2EL in ms for display)
    all_lat_ms  = [c["latency_s"] * 1000 for c in call_log if c.get("latency_s") is not None]
    all_ttft_ms = [c["ttft_ms"] for c in call_log if c.get("ttft_ms") is not None]
    all_itl_ms  = [c["itl_ms"]  for c in call_log if c.get("itl_ms")  is not None]
    all_tps     = [c["tps_out"] for c in call_log if c.get("tps_out")]

    e2el_st = _stats(all_lat_ms)
    ttft_st = _stats(all_ttft_ms)
    itl_st  = _stats(all_itl_ms)
    tps_st  = _stats(all_tps)

    # Time breakdown (seconds)
    streaming_calls   = [c for c in call_log if c.get("ttft_ms") is not None]
    structured_calls  = [c for c in call_log if c.get("ttft_ms") is None]
    streaming_e2el_s  = sum(c["latency_s"] for c in streaming_calls)
    structured_e2el_s = sum(c["latency_s"] for c in structured_calls)
    ttft_sum_s        = sum(c["ttft_ms"] for c in streaming_calls) / 1000 if streaming_calls else 0.0
    decode_s          = max(0.0, streaming_e2el_s - ttft_sum_s)
    total_tool_s      = sum(
        (e["elapsed_s"] if isinstance(e, dict) else e)
        for v in tool_times.values() for e in v
    )
    total_llm_s       = streaming_e2el_s + structured_e2el_s
    overhead_s        = max(0.0, elapsed_s - total_llm_s)
    has_breakdown     = bool(call_log)

    # Per-component stats
    secondary_modules = m.get("secondary_llm_modules", "")
    _sec_set = {s.strip() for s in secondary_modules.split(",") if s.strip()}
    comp_row: dict = {}
    for comp_key, pattern in _COMP_DEFS:
        matched = [c for c in call_log if pattern.match(c.get("caller", ""))]
        mod_name = _SECONDARY_TOOL_FOR_COMP.get(comp_key)
        if matched:
            lats      = [c["latency_s"] for c in matched]
            ttfts     = [c["ttft_ms"]   for c in matched if c.get("ttft_ms") is not None]
            tps_vals  = [c["tps_out"]   for c in matched if c.get("tps_out")]
            comp_row[f"comp_{comp_key}_e2el_s"]  = round(sum(lats) / len(lats), 2)
            comp_row[f"comp_{comp_key}_tps"]     = round(sum(tps_vals) / len(tps_vals), 1) if tps_vals else None
            comp_row[f"comp_{comp_key}_ttft_ms"] = round(sum(ttfts) / len(ttfts), 1) if ttfts else None
            # HW-split columns
            xpu_c  = [c for c in matched if _call_hw(c, mod_name, _sec_set) == "XPU"]
            xeon_c = [c for c in matched if _call_hw(c, mod_name, _sec_set) == "Xeon"]
            for hw_tag, hw_calls in (("xpu", xpu_c), ("xeon", xeon_c)):
                if hw_calls:
                    hw_lats = [c["latency_s"] for c in hw_calls]
                    hw_ttfts = [c["ttft_ms"] for c in hw_calls if c.get("ttft_ms") is not None]
                    hw_tps   = [c["tps_out"]  for c in hw_calls if c.get("tps_out")]
                    comp_row[f"comp_{comp_key}_{hw_tag}_e2el_s"]  = round(sum(hw_lats), 2)
                    comp_row[f"comp_{comp_key}_{hw_tag}_ttft_ms"] = round(sum(hw_ttfts) / len(hw_ttfts), 1) if hw_ttfts else None
                    comp_row[f"comp_{comp_key}_{hw_tag}_tps"]     = round(sum(hw_tps)   / len(hw_tps),   1) if hw_tps   else None
                else:
                    comp_row[f"comp_{comp_key}_{hw_tag}_e2el_s"]  = None
                    comp_row[f"comp_{comp_key}_{hw_tag}_ttft_ms"] = None
                    comp_row[f"comp_{comp_key}_{hw_tag}_tps"]     = None
        else:
            for field in ("e2el_s", "tps", "ttft_ms",
                          "xpu_e2el_s", "xpu_ttft_ms", "xpu_tps",
                          "xeon_e2el_s", "xeon_ttft_ms", "xeon_tps"):
                comp_row[f"comp_{comp_key}_{field}"] = None
        comp_row[f"comp_{comp_key}_back"] = None  # backend inference not tracked per-component

    # Status / outcome mapping
    raw_status = m.get("status", "unknown")
    status  = {"complete": "success", "failed": "failed"}.get(raw_status, raw_status)
    outcome = {"complete": "SUCCESS", "failed": "FAILED"}.get(raw_status, raw_status.upper())

    # Israel-time from UTC timestamp
    query_time_il = ""
    ts_utc = m.get("timestamp_utc", "")
    if ts_utc:
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.fromisoformat(ts_utc).astimezone(timezone.utc)
            query_time_il = (dt + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M") + " IL"
        except Exception:
            pass

    # HW label — comma-joined hardware keys (e.g. "XPU", "XPU, Xeon")
    hw_summary = m.get("hw_summary", {}) or {}
    hw_label = ", ".join(sorted(hw_summary.keys())) if hw_summary else ""

    return {
        "idx":         idx,
        "session_id":  m.get("session_id", ""),
        "query":       m.get("query", ""),
        "status":      status,
        "outcome":     outcome,
        "category":    m.get("category"),
        "elapsed_s":   elapsed_s,
        "agg_input":   m.get("agg_input"),
        "agg_output":  m.get("agg_output"),
        "agg_calls":   m.get("agg_calls"),
        # Latency stats (ms)
        "avg_e2el_ms": e2el_st["avg"],
        "med_e2el_ms": e2el_st["med"],
        "max_e2el_ms": e2el_st["max"],
        "avg_ttft_ms": ttft_st["avg"],
        "med_ttft_ms": ttft_st["med"],
        "p99_ttft_ms": ttft_st["p99"],
        "avg_itl_ms":  itl_st["avg"],
        "med_itl_ms":  itl_st["med"],
        "p99_itl_ms":  itl_st["p99"],
        "avg_tps_out": tps_st["avg"],
        "med_tps_out": tps_st["med"],
        "min_tps_out": tps_st["min"],
        # Time breakdown (s)
        "total_ttft_s":        round(ttft_sum_s,        3) if has_breakdown else None,
        "total_decode_s":      round(decode_s,          3) if has_breakdown else None,
        "total_structured_s":  round(structured_e2el_s, 3) if has_breakdown else None,
        "total_tool_s":        round(total_tool_s,      3) if has_breakdown else None,
        "tool_overhead_s":     round(overhead_s,        3) if has_breakdown else None,
        "tool_breakdown_s":    {k: round(sum(e["elapsed_s"] if isinstance(e, dict) else e for e in v), 3) for k, v in tool_times.items()},
        "salvaged":            False,
        "has_report":          bool(m.get("report_markdown")),
        "input_source":        m.get("source", ""),
        "workspace":           workspace,
        "query_time_il":       query_time_il,
        "hw_label":            hw_label,
        **comp_row,
    }


def _get_status_json() -> dict:
    """Scan all LIVE_DIRS and return dashboard data sorted ascending by write time."""
    all_files: list[pathlib.Path] = []
    for d in LIVE_DIRS:
        try:
            if d.exists():
                all_files.extend(d.glob("*.json"))
        except PermissionError:
            pass  # skip inaccessible directories (e.g. other users' clones)
    if not all_files:
        return {"meta": {"total": 0, "last_updated": time.strftime("%H:%M:%S")}, "rows": []}
    # Build a mapping from each file path to its workspace label
    _dir_to_workspace: dict[pathlib.Path, str] = {}
    for d in LIVE_DIRS:
        label = d.parent.parent.parent.name  # results/ -> deep-research/ -> <workspace> e.g. "dkorat"
        _dir_to_workspace[d] = label

    files = sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for idx, fp in enumerate(files, start=1):
        try:
            m = json.loads(fp.read_text())
            workspace = _dir_to_workspace.get(fp.parent, "")
            rows.append(_compute_row(idx, m, workspace=workspace))
        except Exception:
            continue
    return {
        "meta": {"total": len(rows), "last_updated": time.strftime("%H:%M:%S")},
        "rows": rows,
    }


# ── Report page ────────────────────────────────────────────────

def _markdown_page(session_id: str, md: str, query: str = "") -> str:
    import html as _html
    escaped = _html.escape(md)
    query_block = ""
    if query:
        q_esc = _html.escape(query)
        query_block = (
            f'<div class="query-box">'
            f'<span class="query-label">Research query</span>'
            f'<p class="query-text">{q_esc}</p></div>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Report</title>
<style>
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;
  max-width:860px;margin:0 auto;padding:32px 24px;line-height:1.6}}
a{{color:#58a6ff}}
h1,h2,h3{{color:#e6edf3;border-bottom:1px solid #21262d;padding-bottom:.3em}}
pre{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px;
  overflow-x:auto;font-size:.85em}}
code{{background:#161b22;padding:.2em .4em;border-radius:3px;font-size:.85em}}
blockquote{{border-left:4px solid #30363d;margin:0;padding-left:1em;color:#8b949e}}
.back{{display:inline-block;margin-bottom:20px;color:#58a6ff;text-decoration:none;
  font-size:.9em;border:1px solid #30363d;padding:4px 12px;border-radius:4px}}
.back:hover{{background:#161b22}}
.query-box{{background:#161b22;border:1px solid #30363d;border-radius:8px;
  padding:16px 20px;margin-bottom:28px}}
.query-label{{font-size:.75em;font-weight:600;text-transform:uppercase;
  letter-spacing:.08em;color:#8b949e}}
.query-text{{margin:.5em 0 0;font-size:1.05em;color:#e6edf3;font-style:italic}}
</style>
</head>
<body>
<a class="back" href="/">&#x2190; Dashboard</a>
{query_block}
<pre style="white-space:pre-wrap;border:none;background:none;font-family:inherit;
  font-size:1em;padding:0">{escaped}</pre>
</body></html>"""


# ── Dashboard HTML ─────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Research Dashboard</title>
<script src="/vendor/chart.js"></script>
<script src="/vendor/mermaid.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,monospace;background:#0d1117;color:#c9d1d9}
header{padding:14px 24px 10px;border-bottom:1px solid #21262d;display:flex;align-items:baseline;gap:16px}
h1{color:#58a6ff;font-size:1.25em;font-weight:600}
.meta{color:#8b949e;font-size:.82em}
.tabs{display:flex;flex-wrap:wrap;padding:6px 24px;border-bottom:1px solid #21262d;gap:4px}
.tab{padding:8px 16px;cursor:pointer;color:#8b949e;border-bottom:2px solid transparent;font-size:.95em;user-select:none;white-space:nowrap}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
.tab:hover:not(.active){color:#c9d1d9}
.panel{display:none;padding:16px 24px}
.panel.active{display:block}
.col-toggles{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;align-items:center}
.col-toggle-lbl{color:#6e7681;font-size:.75em;margin-right:4px}
.ctbtn{padding:3px 10px;border-radius:12px;border:1px solid #30363d;background:#161b22;color:#8b949e;font-size:.75em;cursor:pointer;user-select:none;transition:all .15s}
.ctbtn.on{background:#0d419d;border-color:#58a6ff;color:#c9d1d9}
.ctbtn:hover{border-color:#58a6ff}
.tbl-wrap{overflow-x:auto;width:100%}
table{border-collapse:collapse;font-size:.8em;white-space:nowrap}
th{background:#161b22;color:#58a6ff;padding:5px 9px;text-align:right;border-bottom:2px solid #30363d;font-weight:500;cursor:pointer;user-select:none;position:sticky;top:0;z-index:1}
th.lft{text-align:left}
th:hover:not(.grp){color:#79c0ff}
th.sort-asc::after{content:' \u25b2';font-size:.65em;vertical-align:super}
th.sort-desc::after{content:' \u25bc';font-size:.65em;vertical-align:super}
th.grp{background:#0d1117;color:#484f58;font-size:.72em;padding:2px 9px;border-bottom:1px solid #21262d;cursor:default;font-weight:400}
td{padding:4px 9px;text-align:right;border-bottom:1px solid #21262d}
td.lft{text-align:left;max-width:300px;overflow:hidden;text-overflow:ellipsis}
tr.success td{color:#3fb950}
tr.timeout td,tr.failed td{color:#f85149}
tr.running td{color:#d29922;font-weight:600}
tr.pending td{color:#484f58}
tr.agg-row td{background:#161b22;color:#8b949e;font-style:italic;border-top:2px solid #30363d}
tr.agg-row td.lft{color:#c9d1d9;font-style:normal;font-weight:600}
th.comp-grp-start, td.comp-grp-start{border-left:2px solid #30363d !important}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle;flex-shrink:0}
.ds{background:#3fb950}.dr{background:#d29922;animation:pulse 1s infinite}.dp{background:#484f58}.df{background:#f85149}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}
.totals{margin-top:8px;color:#8b949e;font-size:.8em;display:flex;gap:8px;flex-wrap:wrap}
.totals span{background:#161b22;padding:3px 9px;border-radius:4px;border:1px solid #21262d}
.totals .lbl{color:#6e7681}.totals .val{color:#c9d1d9;font-weight:600}
.metric-legend{margin-top:18px;padding-top:12px;border-top:1px solid #21262d}
.metric-legend-title{color:#6e7681;font-size:.72em;font-weight:600;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px}
.metric-legend-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:5px}
.ml-card{background:#161b22;border:1px solid #21262d;border-radius:4px;padding:7px 10px;font-size:.75em;display:flex;gap:8px;align-items:baseline}
.ml-grp{color:#58a6ff;font-weight:600;white-space:nowrap;flex-shrink:0;min-width:110px}
.ml-def{color:#6e7681;line-height:1.4}
.charts-section{margin-bottom:24px}
.section-title{color:#8b949e;font-size:.8em;font-weight:600;letter-spacing:.06em;text-transform:uppercase;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #21262d}
.charts-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.chart-card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:14px}
.chart-card h3{color:#58a6ff;margin-bottom:10px;font-size:.8em;font-weight:500}
.chart-card.full{grid-column:1/-1}
canvas{max-height:220px}
.no-data{color:#484f58;font-size:.85em;text-align:center;padding:32px;font-style:italic}
@media(max-width:1100px){.charts-grid{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.charts-grid{grid-template-columns:1fr}}
.arch-wrap{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px;margin-bottom:20px;overflow-x:auto;min-height:300px;display:flex;align-items:center;justify-content:center}
.arch-wrap svg{max-width:100%;height:auto}
.legend-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-top:16px}
.legend-card{background:#161b22;border:1px solid #21262d;border-radius:6px;padding:12px 14px}
.legend-card h4{font-size:.8em;font-weight:600;margin-bottom:6px}
.legend-card p{font-size:.75em;color:#8b949e;line-height:1.5}
.lc-llm h4{color:#8957e5}.lc-tool h4{color:#d29922}.lc-in h4{color:#58a6ff}.lc-out h4{color:#3fb950}
.report-item{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:10px;margin-bottom:10px}
</style>
</head>
<body>
<header>
  <h1>&#x1F9E0; Research Dashboard</h1>
  <div class="meta" id="meta">Loading&hellip;</div>
</header>
<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;border-bottom:1px solid #21262d;margin-bottom:0">
<div class="tabs" style="border-bottom:none;margin-bottom:0">
  <div class="tab active" onclick="showTab('table')">Table</div>
  <div class="tab" onclick="showTab('charts')">Charts</div>
  <div class="tab" onclick="showTab('reports')">Reports</div>
  <div class="tab" onclick="showTab('system')">System</div>
</div>
<div style="margin-left:auto;padding-right:12px;display:flex;align-items:center;gap:8px">
  <label style="color:#8b949e;font-size:.8em">Workspace:</label>
  <select id="ws-filter" onchange="applyFilters()" style="background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:3px 8px;font-size:.8em;cursor:pointer">
    <option value="">All</option>
  </select>
</div>
</div>

<!-- TABLE -->
<div id="panel-table" class="panel active">
  <div class="col-toggles" id="col-toggles"></div>
  <div class="tbl-wrap"><table><thead id="thead"></thead><tbody id="tbody"></tbody></table></div>
  <div class="totals" id="totals"></div>
  <div class="metric-legend">
    <div class="metric-legend-title">Metric Definitions</div>
    <div class="metric-legend-grid">
      <div class="ml-card"><span class="ml-grp">In / Out</span><span class="ml-def">Total input / output tokens across all LLM calls for this query</span></div>
      <div class="ml-card"><span class="ml-grp">Calls</span><span class="ml-def">Number of LLM API calls made</span></div>
      <div class="ml-card"><span class="ml-grp">E2EL</span><span class="ml-def">End-to-end latency per call &mdash; time from request sent to last token received (avg/med)</span></div>
      <div class="ml-card"><span class="ml-grp">TTFT</span><span class="ml-def">Time to first token &mdash; latency until the first output token is generated (avg/med/p99)</span></div>
      <div class="ml-card"><span class="ml-grp">ITL</span><span class="ml-def">Inter-token latency &mdash; average time between consecutive output tokens during decode (avg/med/p99)</span></div>
      <div class="ml-card"><span class="ml-grp">TPS</span><span class="ml-def">Output throughput &mdash; generated tokens per second (avg/med)</span></div>
      <div class="ml-card"><span class="ml-grp">Wall</span><span class="ml-def">Total wall-clock time for the query including search, LLM calls, and overhead</span></div>
      <div class="ml-card"><span class="ml-grp">Breakdown: TTFT</span><span class="ml-def">Sum of first-token latencies across all streaming calls &mdash; pure prefill time</span></div>
      <div class="ml-card"><span class="ml-grp">Breakdown: Decode</span><span class="ml-def">Streaming E2EL minus total TTFT &mdash; time the GPU spent generating tokens</span></div>
      <div class="ml-card"><span class="ml-grp">Breakdown: Struct</span><span class="ml-def">Total E2EL of structured JSON-mode calls (evaluate_sources, analyze_gaps, extract_claims)</span></div>
      <div class="ml-card"><span class="ml-grp">Breakdown: Ovhd</span><span class="ml-def">Wall minus all LLM time &mdash; web search latency, result routing, Python overhead</span></div>
    </div>
  </div>
</div>

<!-- CHARTS -->
<div id="panel-charts" class="panel">
  <div class="charts-section">
    <div class="section-title">Time Breakdown per Query</div>
    <div class="charts-grid">
      <div class="chart-card full" id="tb-card">
        <h3>Wall-clock time split: TTFT / Decode / Structured LLM / Overhead</h3>
        <div id="tb-nodata" class="no-data" style="display:none">No timing data yet</div>
        <canvas id="cTimeBreak"></canvas>
      </div>
    </div>
  </div>
  <div class="charts-section">
    <div class="section-title">Performance Metrics</div>
    <div class="charts-grid">
      <div class="chart-card"><h3>Token Usage per Query</h3><canvas id="cTokens"></canvas></div>
      <div class="chart-card"><h3>Wall Time (s)</h3><canvas id="cTime"></canvas></div>
      <div class="chart-card"><h3>E2EL avg &amp; med (ms)</h3><canvas id="cE2el"></canvas></div>
      <div class="chart-card"><h3>TTFT avg &amp; med (ms)</h3><div id="ttft-nodata" class="no-data" style="display:none">No TTFT data yet</div><canvas id="cTtft"></canvas></div>
      <div class="chart-card"><h3>ITL avg &amp; med (ms)</h3><div id="itl-nodata" class="no-data" style="display:none">No ITL data yet</div><canvas id="cItl"></canvas></div>
      <div class="chart-card"><h3>TPS avg &amp; med</h3><canvas id="cTps"></canvas></div>
    </div>
  </div>
</div>

<!-- REPORTS -->
<div id="panel-reports" class="panel">
  <div id="reports-list"></div>
</div>

<!-- SYSTEM -->
<div id="panel-system" class="panel">
  <div class="section-title">Agent Architecture</div>
  <div class="arch-wrap" id="arch-wrap">
    <div class="no-data" id="arch-loading">Rendering diagram&hellip;</div>
  </div>
  <div class="section-title" style="margin-top:20px">Component Legend</div>
  <div class="legend-grid">
    <div class="legend-card lc-in"><h4>&#x1F50D; Classifier Node</h4><p>Categorizes query into Cat 1 (factual), Cat 2 (search+answer), or Cat 3 (full research). Structured LLM call with JSON-mode response model.</p></div>
    <div class="legend-card lc-llm"><h4>&#x1F916; Agent LLM Node</h4><p>Streaming LLM with tool-calling. Decides which research tool to invoke next. ReAct loop, up to 15 iterations with circuit breaker.</p></div>
    <div class="legend-card lc-tool"><h4>&#x1F50E; web_search</h4><p>Parallel Tavily + Serper search. Returns top sources. Prerequisite for all downstream tools.</p></div>
    <div class="legend-card lc-tool"><h4>&#x2696;&#xFE0F; evaluate_sources</h4><p>Scores each source for relevance, density, and authority. Structured LLM call. Required before extract/analyze.</p></div>
    <div class="legend-card lc-tool"><h4>&#x1F9E9; analyze_gaps</h4><p>Identifies missing information and proposes follow-up queries. Structured LLM call.</p></div>
    <div class="legend-card lc-tool"><h4>&#x1F4CB; extract_claims</h4><p>Cross-references findings and assigns confidence scores to each factual claim. Structured LLM call.</p></div>
    <div class="legend-card lc-llm"><h4>&#x1F4DD; write_report (Map&#x2192;Outline&#x2192;Reduce)</h4><p>3-phase synthesis: map (per-source summaries), outline (structure), reduce (final synthesis). All streaming LLM calls.</p></div>
    <div class="legend-card lc-out"><h4>&#x1F4C4; Research Report</h4><p>Cited Markdown document. Click "Open" in the Reports tab to view any completed report.</p></div>
  </div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────
const _ch = {};
const _ss = {col: null, dir: 1};
const _hiddenGrps = new Set(["Tokens","E2EL ms","TTFT ms","ITL ms","TPS","Time s","Breakdown s"]);
let _lastData = null;
let _wsFilter = "";  // selected workspace, "" = all

function _filteredData(d) {
  if (!_wsFilter) return d;
  return Object.assign({}, d, {rows: d.rows.filter(r => r.workspace === _wsFilter)});
}

function applyFilters() {
  _wsFilter = document.getElementById("ws-filter").value;
  if (_lastData) {
    const fd = _filteredData(_lastData);
    renderTable(fd);
    if (document.getElementById("panel-charts").classList.contains("active")) renderCharts(fd);
    if (document.getElementById("panel-reports").classList.contains("active")) renderReports(fd);
  }
}

function _updateWsDropdown(d) {
  const sel = document.getElementById("ws-filter");
  const prev = sel.value;
  const workspaces = [...new Set(d.rows.map(r => r.workspace).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">All</option>' +
    workspaces.map(w => `<option value="${w}">${w}</option>`).join("");
  // restore previous selection if still valid
  if (workspaces.includes(prev)) sel.value = prev;
  else { sel.value = ""; _wsFilter = ""; }
}
let _archDone = false;

// ── Column definitions ────────────────────────────────────────
const COLS = [
  {id:"idx",              label:"#",       grp:"",            align:"l", agg:"",     v:r=>r.idx},
  {id:"query",            label:"Query",   grp:"",            align:"l", agg:"",     v:r=>r.query.length>52?r.query.slice(0,50)+"\u2026":r.query},
  {id:"category",         label:"Cat",     grp:"",            align:"r", agg:"",     v:r=>r.category||""},
  {id:"outcome",          label:"Status",  grp:"",            align:"l", agg:"",     v:r=>r.outcome||""},
  {id:"input_source",     label:"Source",  grp:"",            align:"l", agg:"",     v:r=>{
    const s=r.input_source||"";
    if(s==="ui")  return '<span style="background:#1a3a2a;color:#3fb950;border:1px solid #2d6a3f;padding:1px 7px;border-radius:8px;font-size:.75em">UI</span>';
    if(s==="cli") return '<span style="background:#0d2040;color:#58a6ff;border:1px solid #1d3d6e;padding:1px 7px;border-radius:8px;font-size:.75em">CLI</span>';
    return s?`<span style="background:#21262d;color:#8b949e;padding:1px 7px;border-radius:8px;font-size:.75em">${s}</span>`:"";
  }},
  {id:"workspace",        label:"Dev",     grp:"",            align:"l", agg:"",     v:r=>{
    const w=r.workspace||"";
    return w?`<span style="background:#21262d;color:#c9d1d9;padding:1px 7px;border-radius:8px;font-size:.75em">${w}</span>`:"";
  }},
  {id:"query_time_il",    label:"Time (IL)",grp:"",           align:"l", agg:"",     v:r=>r.query_time_il||""},
  {id:"hw_label",          label:"HW",      grp:"",            align:"l", agg:"",     v:r=>{
    const hw=r.hw_label||"";
    return hw.split(", ").filter(Boolean).map(h=>{
      const bg=h==="XPU"?"#0d2a3a":"#1a1a1a";
      const cl=h==="XPU"?"#58a6ff":"#d29922";
      const br=h==="XPU"?"#1d4a6e":"#4d3a00";
      return `<span style="background:${bg};color:${cl};border:1px solid ${br};padding:1px 6px;border-radius:8px;font-size:.75em">${h}</span>`;
    }).join(" ");
  }},
  {id:"agg_input",        label:"In",      grp:"Tokens",      align:"r", agg:"sum",  v:r=>r.agg_input!=null?r.agg_input.toLocaleString():""},
  {id:"agg_output",       label:"Out",     grp:"Tokens",      align:"r", agg:"sum",  v:r=>r.agg_output!=null?r.agg_output.toLocaleString():""},
  {id:"agg_calls",        label:"Calls",   grp:"Tokens",      align:"r", agg:"sum",  v:r=>r.agg_calls||""},
  {id:"avg_e2el_ms",      label:"avg",     grp:"E2EL ms",     align:"r", agg:"avgnn",v:r=>fms(r.avg_e2el_ms)},
  {id:"med_e2el_ms",      label:"med",     grp:"E2EL ms",     align:"r", agg:"avgnn",v:r=>fms(r.med_e2el_ms)},
  {id:"avg_ttft_ms",      label:"avg",     grp:"TTFT ms",     align:"r", agg:"avgnn",v:r=>fms(r.avg_ttft_ms)},
  {id:"med_ttft_ms",      label:"med",     grp:"TTFT ms",     align:"r", agg:"avgnn",v:r=>fms(r.med_ttft_ms)},
  {id:"p99_ttft_ms",      label:"p99",     grp:"TTFT ms",     align:"r", agg:"avgnn",v:r=>fms(r.p99_ttft_ms)},
  {id:"avg_itl_ms",       label:"avg",     grp:"ITL ms",      align:"r", agg:"avgnn",v:r=>fms(r.avg_itl_ms)},
  {id:"med_itl_ms",       label:"med",     grp:"ITL ms",      align:"r", agg:"avgnn",v:r=>fms(r.med_itl_ms)},
  {id:"p99_itl_ms",       label:"p99",     grp:"ITL ms",      align:"r", agg:"avgnn",v:r=>fms(r.p99_itl_ms)},
  {id:"avg_tps_out",      label:"avg",     grp:"TPS",         align:"r", agg:"avgnn",v:r=>r.avg_tps_out!=null?r.avg_tps_out.toFixed(1):""},
  {id:"med_tps_out",      label:"med",     grp:"TPS",         align:"r", agg:"avgnn",v:r=>r.med_tps_out!=null?r.med_tps_out.toFixed(1):""},
  {id:"elapsed_s",        label:"Wall",    grp:"Time s",      align:"r", agg:"sum",  v:r=>r.elapsed_s?r.elapsed_s.toFixed(0)+"s":""},
  {id:"total_ttft_s",     label:"TTFT",    grp:"Breakdown s", align:"r", agg:"sum",  v:r=>r.total_ttft_s!=null?r.total_ttft_s.toFixed(1):""},
  {id:"total_decode_s",   label:"Decode",  grp:"Breakdown s", align:"r", agg:"sum",  v:r=>r.total_decode_s!=null?r.total_decode_s.toFixed(1):""},
  {id:"total_structured_s",label:"Struct", grp:"Breakdown s", align:"r", agg:"sum",  v:r=>r.total_structured_s!=null?r.total_structured_s.toFixed(1):""},
  {id:"total_tool_s",     label:"Tools",   grp:"Breakdown s", align:"r", agg:"sum",  v:r=>r.total_tool_s!=null?r.total_tool_s.toFixed(1):""},
  {id:"tool_overhead_s",  label:"Ovhd",    grp:"Breakdown s", align:"r", agg:"sum",  v:r=>r.tool_overhead_s!=null?r.tool_overhead_s.toFixed(1):""},
];

const _COMP_META = [
  {key:"classify",       grp:"Classify"},
  {key:"agent",          grp:"Agent"},
  {key:"eval_src",       grp:"Eval Src"},
  {key:"analyze_gaps",   grp:"Analyze Gaps"},
  {key:"extract_claims", grp:"Ext Claims"},
  {key:"write_report",   grp:"Write Report"},
];
_COMP_META.forEach(({key, grp}) => {
  ["xpu","xeon"].forEach(hw => {
    const hwLabel = hw === "xpu" ? "XPU" : "Xeon";
    const grpHw = `${grp} ${hwLabel}`;
    COLS.push(
      {id:`comp_${key}_${hw}_e2el_s`,  label:"E2EL s",  grp:grpHw, align:"r", agg:"avgnn",
        v:r=>r[`comp_${key}_${hw}_e2el_s`]!=null?r[`comp_${key}_${hw}_e2el_s`].toFixed(1):""},
      {id:`comp_${key}_${hw}_ttft_ms`, label:"TTFT ms", grp:grpHw, align:"r", agg:"avgnn",
        v:r=>r[`comp_${key}_${hw}_ttft_ms`]!=null?Math.round(r[`comp_${key}_${hw}_ttft_ms`])+"":""},
      {id:`comp_${key}_${hw}_tps`,     label:"TPS",     grp:grpHw, align:"r", agg:"avgnn",
        v:r=>r[`comp_${key}_${hw}_tps`]!=null?r[`comp_${key}_${hw}_tps`].toFixed(1):""},
    );
  });
});

// ── Helpers ───────────────────────────────────────────────────
const fms = v => v != null ? Math.round(v).toLocaleString() : "";
const visCOLS = () => COLS.filter(c => !c.grp || !_hiddenGrps.has(c.grp));

function showTab(n) {
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.textContent.trim().toLowerCase().includes(n)));
  ["table","charts","reports","system"].forEach(p =>
    document.getElementById("panel-"+p).classList.toggle("active", p===n));
  if (n === "charts" && _lastData) renderCharts(_lastData);
  if (n === "system" && !_archDone) renderArch();
}

function buildToggles() {
  const grps = [...new Set(COLS.map(c => c.grp).filter(Boolean))];
  const wrap = document.getElementById("col-toggles");
  wrap.innerHTML = '<span class="col-toggle-lbl">Columns:</span>' +
    grps.map(g => {
      const on = !_hiddenGrps.has(g);
      return `<button class="ctbtn ${on?'on':''}" id="ctbtn-${g.replace(/ /g,'_')}" onclick="toggleGrp('${g}')">${g}</button>`;
    }).join("");
}
function toggleGrp(g) {
  if (_hiddenGrps.has(g)) _hiddenGrps.delete(g); else _hiddenGrps.add(g);
  document.getElementById("ctbtn-"+g.replace(/ /g,"_"))?.classList.toggle("on", !_hiddenGrps.has(g));
  if (_lastData) renderTable(_lastData);
}

function sortBy(col) {
  if (_ss.col === col) _ss.dir *= -1; else {_ss.col = col; _ss.dir = 1;}
  if (_lastData) renderTable(_lastData);
}
function srtRows(rows) {
  if (!_ss.col) return rows;
  return [...rows].sort((a, b) => {
    const va = a[_ss.col], vb = b[_ss.col];
    if (va == null && vb == null) return 0;
    if (va == null) return _ss.dir; if (vb == null) return -_ss.dir;
    return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * _ss.dir;
  });
}

// ── Table ─────────────────────────────────────────────────────
const TC = {success:"success",running:"running",timeout:"failed",failed:"failed",pending:"pending"};
const DC = {success:"ds",running:"dr",timeout:"df",failed:"df",pending:"dp"};

function buildHdr() {
  const vc = visCOLS();
  const _COMP_GRPS = new Set(_COMP_META.map(c => c.grp));
  const compGrpStartIdx = new Set();
  let prevGrp = null;
  vc.forEach((c, i) => {
    if (_COMP_GRPS.has(c.grp) && c.grp !== prevGrp) compGrpStartIdx.add(i);
    prevGrp = c.grp;
  });
  let grpHtml = "";
  let prev = "__", span = 0, grpI = 0, grpStartI = 0;
  for (let i = 0; i < vc.length; i++) {
    const c = vc[i];
    if (c.grp === prev) { span++; }
    else {
      if (prev !== "__") {
        const border = compGrpStartIdx.has(grpStartI) ? " comp-grp-start" : "";
        grpHtml += `<th class="grp${border}" colspan="${span}">${prev}</th>`;
      }
      prev = c.grp; span = 1; grpStartI = i;
    }
  }
  { const border = compGrpStartIdx.has(grpStartI) ? " comp-grp-start" : "";
    grpHtml += `<th class="grp${border}" colspan="${span}">${prev}</th>`; }
  const colHtml = vc.map((c, i) => {
    const sc = _ss.col === c.id ? (_ss.dir===1?" sort-asc":" sort-desc") : "";
    const al = c.align === "l" ? " lft" : "";
    const sep = compGrpStartIdx.has(i) ? " comp-grp-start" : "";
    return `<th class="${al}${sc}${sep}" onclick="sortBy('${c.id}')">${c.label}</th>`;
  }).join("");
  document.getElementById("thead").innerHTML = `<tr>${grpHtml}</tr><tr>${colHtml}</tr>`;
}

function computeAgg(rows) {
  const done = rows.filter(r => ["success","failed","timeout"].includes(r.status));
  if (!done.length) return null;
  const agg = {
    idx: "\u03A3", query: `All ${done.length} / ${rows.length} queries`,
    status: "agg", outcome: `${done.length} / ${rows.length} done`,
    category: "", salvaged: false,
  };
  for (const c of COLS) {
    if (c.agg === "sum") {
      agg[c.id] = done.reduce((s, r) => s + (r[c.id] || 0), 0);
    } else if (c.agg === "avgnn") {
      const vals = done.map(r => r[c.id]).filter(v => v != null);
      agg[c.id] = vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : null;
    }
  }
  return agg;
}

function renderRow(r, cls) {
  const vc = visCOLS();
  const _COMP_GRPS = new Set(_COMP_META.map(c => c.grp));
  const compGrpStartIdx = new Set();
  let prevGrp = null;
  vc.forEach((c, i) => {
    if (_COMP_GRPS.has(c.grp) && c.grp !== prevGrp) compGrpStartIdx.add(i);
    prevGrp = c.grp;
  });
  return `<tr class="${cls}">${vc.map((c, i) => {
    const parts = [];
    if (c.align === "l") parts.push("lft");
    if (compGrpStartIdx.has(i)) parts.push("comp-grp-start");
    const cls2 = parts.length ? ` class="${parts.join(" ")}"` : "";
    let v = c.v(r);
    if (i === 1 && cls !== "agg-row") {
      v = `<span class="dot ${DC[r.status]||"dp"}"></span>` + v;
    }
    return `<td${cls2}>${v}</td>`;
  }).join("")}</tr>`;
}

function renderTable(d) {
  buildHdr();
  const sorted = srtRows(d.rows);
  document.getElementById("tbody").innerHTML =
    sorted.map(r => renderRow(r, TC[r.status]||"pending")).join("") +
    (computeAgg(d.rows) ? renderRow(computeAgg(d.rows), "agg-row") : "");

  const fin = d.rows.filter(r => ["success","failed","timeout"].includes(r.status));
  const tIn  = fin.reduce((s, r) => s + (r.agg_input  || 0), 0);
  const tOut = fin.reduce((s, r) => s + (r.agg_output || 0), 0);
  const tC   = fin.reduce((s, r) => s + (r.agg_calls  || 0), 0);
  const tT   = fin.reduce((s, r) => s + (r.elapsed_s  || 0), 0);
  const ttfts = fin.filter(r => r.avg_ttft_ms != null);
  const e2els = fin.filter(r => r.avg_e2el_ms != null);
  const avgTtft = ttfts.length ? ttfts.reduce((s,r)=>s+r.avg_ttft_ms,0)/ttfts.length : null;
  const avgE2el = e2els.length ? e2els.reduce((s,r)=>s+r.avg_e2el_ms,0)/e2els.length : null;
  document.getElementById("totals").innerHTML = [
    ["Input", tIn.toLocaleString()],
    ["Output", tOut.toLocaleString()],
    ["Calls", tC],
    ["Wall", tT.toFixed(0)+"s"],
    avgTtft!=null?["AvgTTFT", avgTtft.toFixed(0)+"ms"]:null,
    avgE2el!=null?["AvgE2EL", avgE2el.toFixed(0)+"ms"]:null,
    [`Total`, fin.length],
  ].filter(Boolean).map(([l,v]) =>
    `<span><span class="lbl">${l}&nbsp;</span><span class="val">${v}</span></span>`
  ).join("");
}

// ── Charts ────────────────────────────────────────────────────
const CLR = {
  in:"#58a6ff", out:"#3fb950", time:"#d29922",
  e2el_a:"#bc8cff", e2el_m:"#8957e5",
  ttft_a:"#ff7b72", ttft_m:"#e85249",
  itl_a:"#79c0ff", itl_m:"#388bfd",
  tps_a:"#56d364", tps_m:"#26a641",
  tb_ttft:"#388bfd", tb_decode:"#8957e5", tb_struct:"#6e7681", tb_ovhd:"#30363d",
};

function mkBar(id, labels, datasets) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const opts = {
    responsive:true, maintainAspectRatio:true,
    plugins:{legend:{labels:{color:"#8b949e",font:{size:10}}}},
    scales:{
      x:{ticks:{color:"#6e7681",maxRotation:35,font:{size:9}},grid:{color:"#21262d"}},
      y:{ticks:{color:"#8b949e",font:{size:9}},grid:{color:"#21262d"}},
    }
  };
  if (_ch[id]) {_ch[id].data.labels=labels;_ch[id].data.datasets=datasets;_ch[id].update("none");return;}
  _ch[id] = new Chart(canvas, {type:"bar", data:{labels,datasets}, options:opts});
}

function mkBarStacked(id, labels, datasets, xLabel) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const xl = xLabel || "seconds";
  const opts = {
    indexAxis: 'y', responsive:true, maintainAspectRatio:false,
    plugins:{
      legend:{labels:{color:"#8b949e",font:{size:10}}},
      tooltip:{callbacks:{label: ctx => {
        const total = ctx.chart.data.datasets.reduce((s,ds)=>s+(ds.data[ctx.dataIndex]||0),0);
        const pct = total > 0 ? (ctx.parsed.x / total * 100).toFixed(1) : 0;
        const fmt = xl === "tokens" ? ctx.parsed.x.toLocaleString() : ctx.parsed.x.toFixed(2)+"s";
        return `${ctx.dataset.label}: ${fmt} (${pct}%)`;
      }}},
    },
    scales:{
      x:{stacked:true,ticks:{color:"#8b949e",font:{size:9}},grid:{color:"#21262d"},title:{display:true,text:xl,color:"#6e7681",font:{size:9}}},
      y:{stacked:true,ticks:{color:"#6e7681",font:{size:9}},grid:{color:"#21262d"}},
    }
  };
  if (_ch[id]) {_ch[id].data.labels=labels;_ch[id].data.datasets=datasets;_ch[id].update("none");return;}
  canvas.style.height = "320px";
  _ch[id] = new Chart(canvas, {type:"bar", data:{labels,datasets}, options:opts});
}

const TB_SEGS = [
  {key:"total_ttft_s",       label:"TTFT (prefill)",       color:CLR.tb_ttft},
  {key:"total_decode_s",     label:"Decode (token gen)",   color:CLR.tb_decode},
  {key:"total_structured_s", label:"Structured LLM",       color:CLR.tb_struct},
  {key:"tool_overhead_s",    label:"Non-LLM (search/routing)", color:CLR.tb_ovhd},
];

function renderCharts(d) {
  if (!d) return;
  const fin = d.rows.filter(r => r.agg_input != null);
  const lbl = fin.map(r => `Q${r.idx}`);

  const tbRows = fin.filter(r => r.total_ttft_s != null || r.total_structured_s != null);
  if (tbRows.length) {
    const agg_tb = {};
    for (const seg of TB_SEGS) agg_tb[seg.key] = tbRows.reduce((s,r)=>s+(r[seg.key]||0),0);
    const tbLabels = [...tbRows.map(r=>`Q${r.idx}`), "ALL"];
    const tbDatasets = TB_SEGS.map(seg => ({
      label: seg.label, data: [...tbRows.map(r=>r[seg.key]||0), agg_tb[seg.key]||0],
      backgroundColor: seg.color+"cc", borderWidth: 0,
    }));
    document.getElementById("tb-nodata").style.display = "none";
    document.getElementById("cTimeBreak").style.display = "";
    mkBarStacked("cTimeBreak", tbLabels, tbDatasets);
  } else {
    document.getElementById("tb-nodata").style.display = "";
    document.getElementById("cTimeBreak").style.display = "none";
  }

  mkBarStacked("cTokens", lbl, [
    {label:"Input",  data:fin.map(r=>r.agg_input||0),  backgroundColor:CLR.in+"bb"},
    {label:"Output", data:fin.map(r=>r.agg_output||0), backgroundColor:CLR.out+"bb"},
  ], "tokens");
  mkBar("cTime", lbl, [{label:"Wall (s)", data:fin.map(r=>r.elapsed_s||0), backgroundColor:CLR.time+"bb"}]);

  const ie = fin.filter(r=>r.avg_e2el_ms!=null);
  mkBar("cE2el", ie.map(r=>`Q${r.idx}`), [
    {label:"avg E2EL",data:ie.map(r=>r.avg_e2el_ms),backgroundColor:CLR.e2el_a+"bb"},
    {label:"med E2EL",data:ie.map(r=>r.med_e2el_ms||0),backgroundColor:CLR.e2el_m+"88"},
  ]);

  const it = fin.filter(r=>r.avg_ttft_ms!=null);
  if (it.length) {
    document.getElementById("ttft-nodata").style.display="none";
    document.getElementById("cTtft").style.display="";
    mkBar("cTtft", it.map(r=>`Q${r.idx}`), [
      {label:"avg TTFT",data:it.map(r=>r.avg_ttft_ms),backgroundColor:CLR.ttft_a+"bb"},
      {label:"med TTFT",data:it.map(r=>r.med_ttft_ms||0),backgroundColor:CLR.ttft_m+"88"},
    ]);
  } else { document.getElementById("ttft-nodata").style.display=""; document.getElementById("cTtft").style.display="none"; }

  const ii = fin.filter(r=>r.avg_itl_ms!=null);
  if (ii.length) {
    document.getElementById("itl-nodata").style.display="none";
    document.getElementById("cItl").style.display="";
    mkBar("cItl", ii.map(r=>`Q${r.idx}`), [
      {label:"avg ITL",data:ii.map(r=>r.avg_itl_ms),backgroundColor:CLR.itl_a+"bb"},
      {label:"med ITL",data:ii.map(r=>r.med_itl_ms||0),backgroundColor:CLR.itl_m+"88"},
    ]);
  } else { document.getElementById("itl-nodata").style.display=""; document.getElementById("cItl").style.display="none"; }

  const ip = fin.filter(r=>r.avg_tps_out!=null);
  mkBar("cTps", ip.map(r=>`Q${r.idx}`), [
    {label:"avg TPS",data:ip.map(r=>r.avg_tps_out),backgroundColor:CLR.tps_a+"bb"},
    {label:"med TPS",data:ip.map(r=>r.med_tps_out||0),backgroundColor:CLR.tps_m+"88"},
  ]);
}

// ── Reports ───────────────────────────────────────────────────
function renderReports(d) {
  document.getElementById("reports-list").innerHTML = d.rows.map(r => {
    const link = r.has_report
      ? `<a href="/report/${r.session_id}" target="_blank" style="color:#58a6ff;text-decoration:none;font-weight:500;white-space:nowrap">Open &#x2197;</a>`
      : "";
    const badge = r.has_report
      ? `<span style="background:#1a3a2a;color:#3fb950;border:1px solid #2d6a3f;padding:2px 8px;border-radius:10px;font-size:.72em">READY</span>`
      : `<span style="background:#161b22;color:#484f58;border:1px solid #30363d;padding:2px 8px;border-radius:10px;font-size:.72em">NO REPORT</span>`;
    const src = r.input_source || "";
    const srcBadge = src==="ui"
      ? `<span style="background:#1a3a2a;color:#3fb950;border:1px solid #2d6a3f;padding:1px 7px;border-radius:8px;font-size:.72em">UI</span>`
      : src==="cli"
      ? `<span style="background:#0d2040;color:#58a6ff;border:1px solid #1d3d6e;padding:1px 7px;border-radius:8px;font-size:.72em">CLI</span>`
      : `<span style="background:#21262d;color:#8b949e;padding:1px 7px;border-radius:8px;font-size:.72em">${src||"?"}</span>`;
    return `<div class="report-item">
      <span style="color:#58a6ff;font-weight:600;min-width:26px">Q${r.idx}</span>
      ${srcBadge}
      <span style="flex:1;color:#c9d1d9;font-size:.88em">${r.query}</span>
      ${badge} ${link}
    </div>`;
  }).join("");
}

// ── Architecture diagram ──────────────────────────────────────
const ARCH_DEF = `flowchart TD
    Q(["User Query"])
    Q --> CLF

    subgraph CLF["Classify"]
        CLLM["LLM / JSON mode"]
    end

    CLF -->|"Cat 1: Factual"| ANS1["Direct Answer / LLM streaming"]
    CLF -->|"Cat 2: Search"| C2PATH
    CLF -->|"Cat 3: Complex"| LOOP

    subgraph C2PATH["Search + Answer"]
        WS2["web_search"] --> ANS2["LLM streaming"]
    end

    subgraph LOOP["ReAct Loop (max 15 iter)"]
        direction LR
        AGN["Agent LLM / tool-calling"] -->|tool calls| TOOLS
        TOOLS -->|results| AGN
    end

    subgraph TOOLS["Tools"]
        direction TB
        T1("web_search")
        T2("evaluate_sources")
        T3("analyze_gaps")
        T4("extract_claims")
        T5("write_report")
    end

    LOOP -->|write_report| WRP

    subgraph WRP["Report Generation"]
        direction LR
        MAP["Map summaries"] --> OTL["Outline"] --> RED["Reduce / synthesis"]
    end

    ANS1 & ANS2 & RED --> OUT(["Report"])

    classDef llm fill:#1d1f6e,stroke:#8957e5,color:#c9d1d9
    classDef tool fill:#3d2a00,stroke:#d29922,color:#c9d1d9
    classDef io fill:#0d2040,stroke:#58a6ff,color:#c9d1d9
    classDef out fill:#0f2d1c,stroke:#3fb950,color:#c9d1d9
    class CLLM,AGN,ANS1,ANS2,MAP,OTL,RED llm
    class WS2,T1,T2,T3,T4,T5 tool
    class Q io
    class OUT out
`;

async function renderArch() {
  try {
    mermaid.initialize({
      startOnLoad: false, theme: "dark",
      themeVariables: {
        primaryColor:"#161b22", primaryTextColor:"#c9d1d9",
        primaryBorderColor:"#30363d", lineColor:"#58a6ff",
        secondaryColor:"#21262d", tertiaryColor:"#0d1117",
        edgeLabelBackground:"#161b22", clusterBkg:"#161b22", clusterBorder:"#30363d",
      },
      flowchart: {curve:"basis", nodeSpacing:50, rankSpacing:60},
    });
    const {svg} = await mermaid.render("arch-svg", ARCH_DEF);
    document.getElementById("arch-wrap").innerHTML = svg;
    _archDone = true;
  } catch(e) {
    document.getElementById("arch-wrap").innerHTML =
      `<div class="no-data">Diagram error: ${e.message}</div>`;
  }
}

// ── Poll ──────────────────────────────────────────────────────
async function poll() {
  try {
    const resp = await fetch(location.origin + "/status.json", {cache:"no-store"});
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const d = await resp.json();
    _lastData = d;
    _updateWsDropdown(d);
    const fd = _filteredData(d);
    const m = d.meta;
    document.getElementById("meta").textContent =
      m.total + " quer" + (m.total===1?"y":"ies") + "  \u00B7  Updated " + m.last_updated;
    renderTable(fd);
    if (document.getElementById("panel-charts").classList.contains("active")) renderCharts(fd);
    if (document.getElementById("panel-reports").classList.contains("active")) renderReports(fd);
  } catch(e) {
    document.getElementById("meta").textContent =
      "\u26a0\ufe0f fetch failed: " + e.message + " \u2014 " + new Date().toLocaleTimeString();
  }
}

buildToggles();
poll();
setInterval(poll, 3000);
</script>
</body></html>
"""


# ── HTTP server ────────────────────────────────────────────────

_VENDOR = {
    "/vendor/chart.js":   ("/tmp/chart.umd.min.js",   "application/javascript"),
    "/vendor/mermaid.js": ("/tmp/mermaid.min.js",      "application/javascript"),
}


class _Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8",
                       _DASHBOARD_HTML.encode())

        elif self.path == "/status.json":
            body = json.dumps(_get_status_json()).encode()
            self._send(200, "application/json", body,
                       extra_headers={"Cache-Control": "no-cache"})

        elif self.path.startswith("/report/"):
            session_id = self.path.removeprefix("/report/").strip("/")
            # Search all live dirs for the report
            fp = None
            for d in LIVE_DIRS:
                candidate = d / f"{session_id}.json"
                try:
                    if candidate.exists():
                        fp = candidate
                        break
                except PermissionError:
                    pass
            if fp:
                try:
                    m = json.loads(fp.read_text())
                    md = m.get("report_markdown", "")
                    if md:
                        body = _markdown_page(session_id, md, m.get("query", "")).encode()
                        self._send(200, "text/html; charset=utf-8", body)
                        return
                except Exception:
                    pass
            self.send_error(404, "Report not available")

        elif self.path in _VENDOR:
            fpath, ctype = _VENDOR[self.path]
            try:
                body = open(fpath, "rb").read()
                self._send(200, ctype, body,
                           extra_headers={"Cache-Control": "public, max-age=3600"})
            except OSError:
                self.send_error(404, f"Vendor file not found: {fpath}")

        else:
            self.send_error(404)

    def _send(self, code: int, ctype: str, body: bytes, extra_headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silence access logs


# ── Entry point ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Research metrics live dashboard")
    parser.add_argument("--port",      type=int, default=8082)
    parser.add_argument("--clear",     action="store_true",
                        help="Delete all live metrics before starting")
    parser.add_argument("--serve-only", action="store_true",
                        help="(no-op, kept for backward compatibility)")
    args = parser.parse_args()

    if args.clear:
        if LIVE_DIR.exists():
            import shutil
            shutil.rmtree(LIVE_DIR)
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Cleared {LIVE_DIR}", file=sys.stderr)

    LIVE_DIR.mkdir(parents=True, exist_ok=True)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), _Handler)
    print(f"Dashboard: http://localhost:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
