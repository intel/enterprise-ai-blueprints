# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Configuration for the gate.

Every threshold is an environment variable with a deliberately CONSERVATIVE default: when in
doubt the gate escalates to the full agent. v1 is expected to call the LLM more often than it
strictly must; the telemetry (see telemetry.py) is what lets v2 tighten these with real numbers
instead of guesses.
"""

import math
import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


# --- upstreams ---------------------------------------------------------------
# The agent that holds the MCP banking tools. The gate forwards here whenever System-1 cannot answer
# yes/no with confidence, or whenever anything is unclear.
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8091")
# The DevUI entity to send escalations to, by NAME: its id has a per-process random suffix.
AGENT_ENTITY_NAME = os.getenv("AGENT_ENTITY_NAME", "banking-assistant")
# System-1 is a structured-decisions model served by vLLM through Intel Inference Toolkit's Model
# Manager, like the agent's LLM: one forward pass per question over POST /v1/systemone, no text
# generated (docs.vllm.ai, "Structured Decisions Models"). SYSTEM1_URL is the model's base URL
# without /v1: on the platform, its route on the inference AI gateway (the default below); in local
# Docker mode, the vLLM container. SYSTEM1_API_KEY is that server's --api-key, if it has one.
SYSTEM1_URL = os.getenv("SYSTEM1_URL",
                        "http://ai-gateway.envoy-gateway-system.svc.cluster.local/llm-inference/qwen3-0-6b-system1")
SYSTEM1_API_KEY = os.getenv("SYSTEM1_API_KEY", "")
# The text rules (questions sent to the LLM without asking System-1): a JSON file of regexes. The
# default ships beside the code; point this at your own file to add or change rules.
GATE_TEXT_RULES = os.getenv("GATE_TEXT_RULES") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "text_rules.json")
# The token customers must present, as "Authorization: Bearer <token>". It is the agent's own
# DevUI token: before the gate existed DevUI enforced it on every request, and a request the
# gate answers itself never reaches DevUI, so the gate must enforce it or its answers are open
# to anyone. Unset disables the check (local tests only).
CLIENT_TOKEN = os.getenv("DEVUI_AUTH_TOKEN", "")
# The model name vLLM serves System-1 under, sent in every request: Model Manager serves a catalog
# model under its catalog name (qwen3-0-6b-system1, Qwen/Qwen3-0.6B); a plain `vllm serve` uses the
# HuggingFace id. A vLLM server that does not serve it answers 404 and the gate fails open.
SYSTEM1_MODEL = os.getenv("SYSTEM1_MODEL", "qwen3-0-6b-system1")
# vLLM reports label_mass per question: the share of the whole vocabulary's probability that fell on
# the label tokens (yes / no). A low one means the model's first token was mostly NOT yes or no --
# it did not take the question as a yes/no one -- so its P(yes) is not worth displaying. Below this,
# the gate escalates. 0 turns the check off; it is unmeasured, so fit it with the cut-offs.
MIN_LABEL_MASS = _f("SYSTEM1_MIN_LABEL_MASS", 0.0)

# --- timeouts ----------------------------------------------------------------
# vLLM batches System-1's calls with whatever else it serves, so under load a call can queue. The
# timeout is short on purpose: if System-1 is slower than this, the agent answers instead.
SYSTEM1_TIMEOUT_S = _f("SYSTEM1_TIMEOUT_S", 2.0)
UPSTREAM_TIMEOUT_S = _f("UPSTREAM_TIMEOUT_S", 120.0)

# --- the gate ----------------------------------------------------------------
# The display cut-offs on P(yes) -- the CALIBRATION knobs:
#     P(yes) >= YES_MIN  -> display "Yes."
#     P(yes) <= NO_MAX   -> display "No."
#     anything between   -> the LLM agent
# These code fallbacks (0.90 / 0.10) display only when System-1 is 90% sure either way; Docker, Helm
# and config/agent-config.env set the fitted values for Qwen3-0.6B (0.9952 / 0.9906). A model's P(yes)
# is shifted for a given question style (Qwen3-0.6B puts almost every banking question near 1), so a
# deployment fits these two numbers on its own labelled questions instead of using 0.5.
# Crossed or out-of-range cut-offs (see invalid_settings below) leave no "unsure -> LLM" band, or
# display everything. A calibration typo must not do that silently: the gate then sends every
# question to the LLM, and says so at startup.
YES_MIN = _f("SYSTEM1_YES_MIN", 0.90)
NO_MAX = _f("SYSTEM1_NO_MAX", 0.10)
# The action VETO: escalate when P(asking for something to be done) is above this. It is a SECOND
# layer: explicit requests ("Can you transfer ...?", "Please block my card", anything with an
# amount) are already sent to the LLM by the text rules in server.py. At 0.40 it also vetoed
# informational banking Q&A that merely uses an action verb ("Can an individual open a Current
# Account?", "Can the ATM PIN be changed?"); 0.60 was chosen on a development split of the
# benchmark questions, where coverage gains flattened above it.
MAX_P_ACTION = _f("SYSTEM1_MAX_P_ACTION", 0.60)
# Master switch. Off => behave exactly like the un-gated assistant (everything to the agent).
GATE_ENABLED = _b("GATE_ENABLED", True)
# Shadow mode: ask System-1 and log the answer it WOULD display, but ALWAYS serve from the agent.
# The recommended first deployment: it logs what System-1 would answer, at zero customer risk.
SHADOW_MODE = _b("GATE_SHADOW_MODE", False)

# --- what the gate exposes ----------------------------------------------------
# X-Gate-* response headers (route, reason, per-question scores). Internal detail, and a reason
# can carry upstream error text, so they are opt-in -- for debugging and the tests.
DEBUG_HEADERS = _b("GATE_DEBUG_HEADERS", False)
# Proxy every other path to the agent's DevUI (web UI, /v1/responses, /v1/conversations...).
# Convenient in development; turn off where only chat completions should be reachable.
PROXY_DEVUI = _b("GATE_PROXY_DEVUI", True)

# --- serving -----------------------------------------------------------------
HOST = os.getenv("GATE_HOST", "0.0.0.0")  # nosec B104 - containerized service must bind all interfaces to be reachable
PORT = _i("GATE_PORT", 8090)
TELEMETRY_PATH = os.getenv("GATE_TELEMETRY_PATH", "/app/telemetry/gate.jsonl")
TELEMETRY_ENABLED = _b("GATE_TELEMETRY_ENABLED", True)
# Customer text in the decision log: opt-in, see the privacy note in telemetry.py.
LOG_QUESTION_TEXT = _b("GATE_LOG_QUESTION_TEXT", False)
# Key for the question hash. A plain hash of a short query can be reversed with a dictionary,
# so it is an HMAC; without a key one is generated per process, which keeps repeats linkable
# within a run but not across restarts. Set it to link across restarts.
TELEMETRY_HASH_KEY = os.getenv("GATE_TELEMETRY_HASH_KEY", "")


def invalid_settings() -> list[str]:
    """The decision settings that are out of range, as short messages; [] when all are valid.

    Checked on every decision (and logged at start-up): with any of them invalid, System-1's answers
    are OFF and every question goes to the agent -- a typo like SYSTEM1_YES_MIN=1.2 must not turn into
    "No." for everything. Parsing already falls back to the default on a non-number; this catches
    numbers that parse but make no sense (NaN, inf, out of range, crossed cut-offs)."""
    bad = []

    def prob(name, v):
        if not (math.isfinite(v) and 0.0 <= v <= 1.0):
            bad.append(f"{name}={v} is not a probability in [0, 1]")

    prob("SYSTEM1_NO_MAX", NO_MAX)
    prob("SYSTEM1_YES_MIN", YES_MIN)
    prob("SYSTEM1_MAX_P_ACTION", MAX_P_ACTION)
    prob("SYSTEM1_MIN_LABEL_MASS", MIN_LABEL_MASS)
    if math.isfinite(NO_MAX) and math.isfinite(YES_MIN) and not NO_MAX < YES_MIN:
        bad.append(f"SYSTEM1_NO_MAX={NO_MAX} is not below SYSTEM1_YES_MIN={YES_MIN} (no unsure band)")
    for name, v in (("SYSTEM1_TIMEOUT_S", SYSTEM1_TIMEOUT_S), ("UPSTREAM_TIMEOUT_S", UPSTREAM_TIMEOUT_S)):
        if not (math.isfinite(v) and v > 0):
            bad.append(f"{name}={v} is not a positive number of seconds")
    return bad


def snapshot() -> dict:
    """Everything that affects a decision, for the startup log and the telemetry header."""
    return {
        "agent_url": AGENT_URL, "agent_entity": AGENT_ENTITY_NAME,
        "system1_url": SYSTEM1_URL, "system1_model": SYSTEM1_MODEL,
        "min_label_mass": MIN_LABEL_MASS,
        "yes_min": YES_MIN, "no_max": NO_MAX, "settings_valid": not invalid_settings(),
        "max_p_action": MAX_P_ACTION,
        "system1_timeout_s": SYSTEM1_TIMEOUT_S,
        "gate_enabled": GATE_ENABLED, "shadow_mode": SHADOW_MODE,
        "client_auth_enforced": bool(CLIENT_TOKEN),
        "debug_headers": DEBUG_HEADERS, "proxy_devui": PROXY_DEVUI,
        "log_question_text": LOG_QUESTION_TEXT,
        "telemetry_hash_keyed": bool(TELEMETRY_HASH_KEY),
    }
