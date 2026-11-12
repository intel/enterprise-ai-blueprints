# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Per-session cumulative token usage tracking.

Uses a ``ContextVar`` so callers (LLMClient, agent node) don't need to
thread ``research_id`` through every call — it's picked up automatically
from the async context set at the start of each research session.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Set this at the start of each research session's async task.
current_research_id: ContextVar[str] = ContextVar(
    "current_research_id", default=""
)

# Optional async callable ``(ProgressEvent) -> None`` — set per-request so
# llm_client can emit SSE events without threading the callback everywhere.
current_progress_callback: ContextVar[Optional[Callable]] = ContextVar(
    "current_progress_callback", default=None
)

# Per-request override for webpage summarization, used by the split-screen
# comparison so two concurrent runs can differ without touching the global
# ``settings`` singleton. ``None`` = use settings as-is; ``True`` = force ON
# (configured method); ``False`` = force OFF (skip summarize_sources).
current_summarize_override: ContextVar[Optional[bool]] = ContextVar(
    "current_summarize_override", default=None
)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# Hardware labels that are remote/cloud-hosted. For these, TTFT is dominated by
# network round-trip (gateway -> cloud provider), not prefill compute, so
# prefill_tps ("input tokens / TTFT") is not physically meaningful and is hidden.
_CLOUD_HARDWARE = {"bedrock", "openai", "anthropic", "azure", "cloud", "vertex", "gemini"}


@dataclass
class HardwareStats:
    input_tokens: int = 0
    output_tokens: int = 0
    total_elapsed_s: float = 0.0
    call_count: int = 0
    model: str = ""
    total_ttft_ms: float = 0.0
    ttft_count: int = 0       # calls that had a measurable TTFT
    ttft_input_tokens: int = 0  # input tokens from ONLY the TTFT-bearing calls (for prefill_tps)
    total_itl_ms: float = 0.0
    itl_count: int = 0        # calls that had a measurable ITL


@dataclass
class CallRecord:
    seq: int = 0
    caller: str = ""
    latency_s: float = 0.0
    ttft_ms: Optional[float] = None
    itl_ms: Optional[float] = None
    input_tokens: int = 0
    output_tokens: int = 0
    tps_out: float = 0.0
    hw: str = ""


class TokenTracker:
    """Accumulates token usage per research session."""

    def __init__(self) -> None:
        self._sessions: dict[str, TokenUsage] = {}
        self._hw: dict[str, dict[str, HardwareStats]] = {}
        self._calls: dict[str, list[CallRecord]] = {}
        self._tool_times: dict[str, dict[str, list[dict]]] = {}

    def track(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        research_id: str = "",
    ) -> None:
        rid = research_id or current_research_id.get("")
        if not rid:
            return
        if rid not in self._sessions:
            self._sessions[rid] = TokenUsage()
        usage = self._sessions[rid]
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        usage.call_count += 1
        logger.info(
            "Cumulative tokens [%s]: input=%d, output=%d, total=%d, calls=%d",
            rid[:8],
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            usage.call_count,
        )

    def track_hw(
        self,
        input_tokens: int,
        output_tokens: int,
        elapsed_s: float,
        hardware: str,
        *,
        research_id: str = "",
        model: str = "",
        ttft_ms: Optional[float] = None,
        itl_ms: Optional[float] = None,
    ) -> None:
        """Accumulate per-hardware token usage and timing for the UI summary line."""
        rid = research_id or current_research_id.get("")
        if not rid or not hardware:
            return
        hw_map = self._hw.setdefault(rid, {})
        stats = hw_map.setdefault(hardware, HardwareStats())
        stats.input_tokens += input_tokens
        stats.output_tokens += output_tokens
        stats.total_elapsed_s += elapsed_s
        stats.call_count += 1
        if model and not stats.model:
            stats.model = model
        if ttft_ms is not None:
            stats.total_ttft_ms += ttft_ms
            stats.ttft_count += 1
            stats.ttft_input_tokens += input_tokens
        if itl_ms is not None:
            stats.total_itl_ms += itl_ms
            stats.itl_count += 1
            stats.model = model

    def get_hw_summary(self, research_id: str) -> dict:
        """Return per-hardware stats suitable for the UI summary line."""
        hw_map = self._hw.get(research_id, {})
        result: dict = {}
        for hw, stats in hw_map.items():
            tps = (
                round(stats.output_tokens / stats.total_elapsed_s, 1)
                if stats.total_elapsed_s > 0
                else 0.0
            )
            # Prefill tok/s: input tokens processed per second during TTFT.
            # Use ONLY the TTFT-bearing (streamed) calls for both numerator and
            # denominator — non-streaming calls (e.g. Bedrock guided-decoding)
            # have no TTFT, so including their input here would inflate prefill.
            # Suppressed entirely for cloud hardware, where TTFT is network-bound
            # rather than prefill compute (see _CLOUD_HARDWARE).
            prefill_tps: Optional[float] = None
            if (
                hw.lower() not in _CLOUD_HARDWARE
                and stats.ttft_count > 0
                and stats.total_ttft_ms > 0
            ):
                avg_ttft_s = (stats.total_ttft_ms / stats.ttft_count) / 1000.0
                avg_in_per_call = stats.ttft_input_tokens / stats.ttft_count
                prefill_tps = round(avg_in_per_call / avg_ttft_s, 0)
            # Decode tok/s: output tokens per second during decode (1000 / avg_ITL)
            decode_tps: Optional[float] = None
            if stats.itl_count > 0 and stats.total_itl_ms > 0:
                avg_itl_ms = stats.total_itl_ms / stats.itl_count
                decode_tps = round(1000.0 / avg_itl_ms, 1)
            result[hw] = {
                "in_tokens": stats.input_tokens,
                "out_tokens": stats.output_tokens,
                "elapsed_s": round(stats.total_elapsed_s, 1),
                "tps": tps,
                "model": stats.model,
                "prefill_tps": prefill_tps,
                "decode_tps": decode_tps,
            }
        return result

    def get(self, research_id: str) -> TokenUsage:
        return self._sessions.get(research_id, TokenUsage())

    def get_call_log(self, research_id: str) -> list[dict]:
        """Return list of per-call records for the given session (JSON-serializable)."""
        calls = self._calls.get(research_id, [])
        return [
            {
                "seq": c.seq,
                "caller": c.caller,
                "latency_s": c.latency_s,
                "ttft_ms": c.ttft_ms,
                "itl_ms": c.itl_ms,
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "tps_out": c.tps_out,
                "hw": c.hw,
            }
            for c in calls
        ]

    def get_tool_times(self, research_id: str) -> dict[str, list[float]]:
        """Return dict of {tool_name: [elapsed_s, ...]} for the given session."""
        return dict(self._tool_times.get(research_id, {}))

    def track_call(
        self,
        caller: str,
        latency_s: float,
        ttft_ms: Optional[float],
        itl_ms: Optional[float],
        input_tokens: int,
        output_tokens: int,
        tps_out: float,
        *,
        hw: str = "",
        research_id: str = "",
    ) -> None:
        """Record a single LLM call for the given session."""
        rid = research_id or current_research_id.get("")
        if not rid:
            return
        calls = self._calls.setdefault(rid, [])
        calls.append(CallRecord(
            seq=len(calls) + 1,
            caller=caller,
            latency_s=round(latency_s, 3),
            ttft_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
            itl_ms=round(itl_ms, 2) if itl_ms is not None else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tps_out=round(tps_out, 1),
            hw=hw,
        ))

    def track_tool_time(
        self,
        tool_name: str,
        elapsed_s: float,
        *,
        hardware: str = "",
        research_id: str = "",
    ) -> None:
        """Record tool execution time and hardware for the given session."""
        rid = research_id or current_research_id.get("")
        if not rid:
            return
        entry = {"elapsed_s": round(elapsed_s, 3), "hw": hardware}
        self._tool_times.setdefault(rid, {}).setdefault(tool_name, []).append(entry)

    def write_metrics(
        self,
        research_id: str,
        query: str,
        category: int,
        status: str,
        elapsed_s: float,
        *,
        iterations: int = 0,
        source: str = "api",
        report_markdown: str = "",
    ) -> None:
        """Write per-session metrics to ``RESULTS_DIR``/{research_id}.json.

        RESULTS_DIR defaults to ``results/live`` (relative to the working
        directory) so local runs are unchanged. In a container it points at the
        mounted volume (e.g. /app/data/results/live) so metrics survive restarts
        and are picked up by the eval dashboard.
        """
        import json as _json
        import os as _os
        import pathlib as _pathlib
        from datetime import datetime as _dt, timezone as _tz

        live_dir = _pathlib.Path(_os.getenv("RESULTS_DIR") or "results/live")
        live_dir.mkdir(parents=True, exist_ok=True)

        usage = self.get(research_id)
        hw_summary = self.get_hw_summary(research_id)
        call_log = self.get_call_log(research_id)
        tool_times = self.get_tool_times(research_id)

        from config import settings as _settings
        metrics = {
            "session_id": research_id,
            "query": query,
            "category": category,
            "status": status,
            "timestamp_utc": _dt.now(_tz.utc).isoformat(),
            "elapsed_s": round(elapsed_s, 1),
            "agg_input": usage.input_tokens,
            "agg_output": usage.output_tokens,
            "agg_calls": usage.call_count,
            "agg_avg_input": round(usage.input_tokens / usage.call_count) if usage.call_count else 0,
            "agg_iterations": iterations,
            "source": source,
            "secondary_llm_modules": _settings.secondary_llm_modules,
            "hw_summary": hw_summary,
            "call_log": call_log,
            "tool_times": tool_times,
            "report_markdown": report_markdown,
        }

        out_path = live_dir / f"{research_id}.json"
        out_path.write_text(_json.dumps(metrics, indent=2))
        logger.info("Live metrics written: %s", out_path)

    def clear(self, research_id: str) -> None:
        self._sessions.pop(research_id, None)
        self._hw.pop(research_id, None)
        self._calls.pop(research_id, None)
        self._tool_times.pop(research_id, None)


# Global singleton
tracker = TokenTracker()
