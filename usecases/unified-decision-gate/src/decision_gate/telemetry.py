# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Per-request telemetry — the thing that makes "improve it in v2" real.

We are shipping v1 without two numbers we would normally insist on: p, the share of real
traffic that genuinely needs account access, and a labelled set for the routed yes/no question.
Neither can be invented honestly. But both fall out of a few weeks of production logs if, and
only if, v1 records the right fields from the start. That is all this module does.

What each field is for:

  question_text     the customer's words. The labelled set for v2 is built by reading these
                    alongside `decision`, so this is the single most valuable field here.
                    HOLDS CUSTOMER TEXT -- see the privacy note below.
  scores            qid -> P(true). The distribution of these IS the base-rate estimate: the
                    share above 0.5 approximates p without anyone having to hand-count traffic.
  confidences       qid -> answer_confidence. With `scores`, this tells us where the display cut-offs should
                    actually sit, rather than the conservative guess v1 ships with.
  route, reason     which rule spent the traffic. Group by `reason` to see whether the gate is
                    degenerating into "escalate everything" -- the most likely failure mode, and
                    one that looks healthy on an accuracy metric.
  routing_model     the checkpoint that served System-1's answers. Guards against a silent switch on
                    non-English text.
  session_id        the client's X-Session-Id, when it sends one, to group rows into
                    conversations. Null when the client does not send it.
  system1_answer        "yes"/"no": what System-1 answered to the customer's question, displayed or not.
  displayed         whether that answer was shown to the customer. system1_answer on rows where it
                    was NOT displayed (escalated, shadow) is the calibration material: pair it
                    with what the agent then said and the tools it called.
  agent_tools       the tools the agent called while answering (null when the agent did not
                    serve the request). This is shadow mode's label signal: a query the gate
                    would have answered cheaply but for which the agent called an account tool
                    is a candidate miss.
  system1_ms, total_ms  the latency case, measured rather than quoted. Streamed rows are written
                    when the stream ENDS, so total_ms is the full response time for them too;
                    `streamed` still separates them, since their latency profile differs.

PRIVACY. `question_text` is customer content, so logging it is opt-in via
GATE_LOG_QUESTION_TEXT and off by default; with it off we still record a length and a hash so
volumes and repeats stay analysable. The hash (`question_hash`) is an HMAC-SHA256 keyed by
GATE_TELEMETRY_HASH_KEY, because an unkeyed hash of a short query is reversible by dictionary. Decide this with whoever owns the deployment before
turning it on, and keep the file inside whatever retention policy applies to the chat logs
themselves.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from . import config
from .policy import Decision

logger = logging.getLogger(__name__)

_HASH_KEY = (config.TELEMETRY_HASH_KEY or secrets.token_hex(32)).encode("utf-8")

_lock = threading.Lock()
_fh = None
_failed = False      # the sink could not be opened; do not retry and re-warn on every request


def _handle():
    """Open the sink lazily; a telemetry failure must never take a request down."""
    global _fh, _failed
    if _fh is not None or _failed or not config.TELEMETRY_ENABLED:
        return _fh
    try:
        os.makedirs(os.path.dirname(config.TELEMETRY_PATH), exist_ok=True)
        _fh = open(config.TELEMETRY_PATH, "a", buffering=1)  # noqa: SIM115 - the log's long-lived, line-buffered sink
        _fh.write(json.dumps({"event": "start", "ts": time.time(),
                              "config": config.snapshot(),
                              "log_question_text": config.LOG_QUESTION_TEXT}) + "\n")
    except OSError as e:
        logger.warning("telemetry disabled (cannot open %s): %s", config.TELEMETRY_PATH, e)
        _fh = None
        _failed = True
    return _fh


def _hash(v: str) -> str:
    return hmac.new(_HASH_KEY, v.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def record(decision: Decision, question_text: str, total_ms: float,
           upstream_status: int | None = None, model: str | None = None,
           routing_model: str | None = None, session_id: str | None = None,
           streamed: bool = False, agent_tools: list | None = None) -> None:
    if not config.TELEMETRY_ENABLED:
        return
    row = {
        "event": "decision",
        "ts": round(time.time(), 3),
        "route": decision.route,
        "reason": decision.reason,
        "confident": decision.confident,
        "scores": {k: round(v, 4) for k, v in decision.scores.items()},
        "confidences": {k: round(v, 4) for k, v in decision.confidences.items()},
        "system1_ms": decision.system1_ms,
        "total_ms": round(total_ms, 1),
        "upstream_status": upstream_status,
        "model": model,
        "routing_model": routing_model or decision.routing_model,
        "session_id": session_id,
        "question_chars": len(question_text or ""),
        "question_hash": _hash(question_text or ""),
        "streamed": streamed,
        "agent_tools": agent_tools,
        # System-1's answer to the customer's question -- recorded even when it was NOT shown
        # (escalated, or shadow mode) so it can be scored against the agent and calibrated.
        "system1_answer": decision.answer,
        "displayed": decision.route == "answer",
        "shadow": config.SHADOW_MODE,
    }
    if config.LOG_QUESTION_TEXT:
        row["question_text"] = question_text
    fh = _handle()
    if fh is None:
        return
    try:
        with _lock:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, ValueError) as e:
        logger.warning("telemetry write failed: %s", e)
