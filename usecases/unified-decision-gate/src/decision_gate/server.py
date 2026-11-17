# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
The gate: an OpenAI-compatible front door that decides whether a banking query needs the
full agent.

                      /v1/chat/completions
    customer ---->  gate :8090
                       |  1. a reply to the agent?             -> agent (System-1 not asked)
                       |     not a plain yes/no question?      -> agent (System-1 not asked):
                       |       wh-question, "can you explain...", about MY account,
                       |       needs a calculation, or two questions in one
                       |  2. one System-1 call over the banking question:
                       |       is it a request to DO something?  -> agent if so (veto)
                       |       what is the answer, yes or no?
                       |
                       +-- System-1 confident -----> "Yes." / "No." shown as the reply  (no LLM call)
                       |
                       +-- anything else -----> banking-assistant :8091            (agent + MCP)
                                                 via /v1/responses (see agent_client.py)

    any other path  ---------------------------> banking-assistant, proxied unchanged
                                                 (DevUI web UI, /v1/responses, /v1/conversations,
                                                  /v1/entities ...)

The gate OWNS PORT 8090, which the agent used to serve, and the agent moves to 8091. Clients
keep speaking chat completions to :8090; the agent speaks only the Responses API, so the gate
translates in both directions. Every other DevUI route is proxied as-is, so the web UI and the
native Responses API stay reachable through the same port.

FAIL OPEN. Every System-1-side failure -- down, slow, busy, malformed, wrong checkpoint, gate
disabled -- forwards to the agent: the assistant must never
degrade because System-1 is unhappy. The only errors the gate returns itself are a 400 for a request that is not a chat
completion at all (the agent could not accept it either) and a 502 when the agent itself is
unreachable.
"""

import asyncio
import hmac
import logging
import json
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from . import config, policy, questions, telemetry
from .agent_client import AgentClient, AgentError, _chunk
from .system1_client import System1Client, System1Unavailable

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

_system1: System1Client = None       # type: ignore[assignment]
_http: httpx.AsyncClient = None   # type: ignore[assignment]
_agent: AgentClient = None   # type: ignore[assignment]

# System-1 outages are logged at WARNING at most once per window, with how many requests failed open
# since the last warning: a deployment where System-1 is unreachable (a proxy or network mistake) must
# not look healthy while every question silently goes to the agent.
_SYSTEM1_WARN_EVERY_S = 60.0
_system1_fail = {"since_warn": 0, "busy_since_warn": 0, "last_warn": 0.0}


def _note_system1_unavailable(detail: str) -> None:
    # "busy (503)" is the System-1 server's ordinary load signal (it serves one request at a time), not an
    # outage: counted, but never a warning on its own, or normal traffic would page someone.
    if detail.startswith("busy"):
        _system1_fail["busy_since_warn"] += 1
        return
    _system1_fail["since_warn"] += 1
    now = time.monotonic()
    if _system1_fail["last_warn"] == 0.0 or now - _system1_fail["last_warn"] >= _SYSTEM1_WARN_EVERY_S:
        logger.warning("System-1 unavailable: %d request(s) failed open to the agent since the last "
                       "warning, plus %d busy (latest: %s)",
                       _system1_fail["since_warn"], _system1_fail["busy_since_warn"], detail)
        _system1_fail.update(since_warn=0, busy_since_warn=0, last_warn=now)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _system1, _http, _agent
    _system1 = System1Client()
    _http = httpx.AsyncClient(timeout=config.UPSTREAM_TIMEOUT_S)
    _agent = AgentClient(_http)
    logger.info("=" * 68)
    logger.info("gate on :%s", config.PORT)
    for k, v in config.snapshot().items():
        logger.info("  %-22s %s", k, v)
    if config.SHADOW_MODE:
        logger.info("  SHADOW MODE: decisions are logged but every request goes to the agent")
    for problem in config.invalid_settings():
        logger.error("  invalid setting: %s. System-1 answers are OFF; every question goes to the agent "
                     "until it is fixed.", problem)
    if not config.CLIENT_TOKEN:
        logger.warning("  DEVUI_AUTH_TOKEN is unset: /v1/chat/completions accepts ANY caller")
    if config.GATE_ENABLED and not config.SYSTEM1_URL.strip():
        logger.warning("  SYSTEM1_URL is EMPTY: System-1 is never reached, so every question goes to the agent. "
                       "Set it to System-1's base URL without /v1 (README, Configuration).")
    elif config.GATE_ENABLED:
        # A real read at startup. A brand-new pod's first connections through the mesh can take a
        # few seconds, so retry briefly before warning. Fail-open is unchanged: the gate still starts.
        err = None
        for attempt in range(3):
            try:
                await _system1.health()
                err = None
                break
            except Exception as e:  # noqa: BLE001 - a warning, never a startup failure
                err = e
                if attempt < 2:
                    await asyncio.sleep(2.0)
        if err is not None:
            logger.warning("  System-1 is NOT reachable at %s (%s): every question will go to the agent "
                           "until it is. Check SYSTEM1_URL and no_proxy.", config.SYSTEM1_URL, str(err)[:160])
        else:
            logger.info("  System-1 reachable at %s", config.SYSTEM1_URL)
    logger.info("=" * 68)
    yield
    await _system1.aclose()
    await _http.aclose()


# No docs of the gate's own: they would shadow DevUI's /docs and /openapi.json, which the
# catch-all proxy below is meant to reach.
app = FastAPI(title="Unified Decision Gate", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _last_user_text(body) -> str:
    """The customer's most recent message, which is what System-1 scores.

    Only the LAST user message is ever read. If it has no usable text (content None, an image-
    only parts list, a malformed entry) the answer is "", which escalates -- scoring an older
    message instead would answer a question the customer is no longer asking. Never raises: a
    body of the wrong shape is the agent's to reject, not ours.
    """
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list):
        return ""
    for m in reversed(msgs):
        if not isinstance(m, dict):
            return ""
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):   # OpenAI content-parts form
            return " ".join(p["text"] for p in c
                            if isinstance(p, dict) and isinstance(p.get("text"), str))
        return ""
    return ""


# Replies that only make sense against the previous turn. Scored alone they look like nothing --
# measured with a System-1 model, "yes" after a transfer confirmation scored P(action) 0.27 -- so they are
# never put to System-1 at all.
_SHORT_REPLY = {
    "yes", "yeah", "yep", "yup", "y", "sure", "ok", "okay", "k", "fine", "right", "correct",
    "confirm", "confirmed", "proceed", "go", "ahead", "do", "it", "please", "pls", "that",
    "no", "nope", "nah", "n", "cancel", "stop", "dont", "don't", "not", "wait", "hold", "on",
    "thanks", "thank", "you", "great", "perfect", "sounds", "good", "agreed", "approve",
}


_CONSENT_START = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirmed", "approved",
                  "agreed", "absolutely", "definitely", "please", "go"}


def _followup_reason(body) -> str | None:
    """Why the last user message must go to the agent without asking System-1, or None.

    Two rules, both conservative:
      * ANY earlier assistant turn. A follow-up can only be judged against what the agent said,
        and System-1 sees the last message alone. Matching the agent's wording does not work: LLM
        phrasing varies too much ("**Shall I proceed?**", "Reply yes or no.", "Please confirm
        and I'll go ahead.", an emoji after the "?"), and every variant that slipped past a
        trailing-"?" rule would let "yes" be judged out of context. This costs cheap-path
        coverage only on follow-up turns -- exactly the turns System-1 cannot judge.
      * a first message that is only yes/no/confirm words ("ok do it"), which has no question
        in it for System-1 to judge either.
    """
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list) or not msgs:
        return None
    last_user = None
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], dict) and msgs[i].get("role") == "user":
            last_user = i
            break
    if last_user is None:
        return None
    if any(isinstance(m, dict) and m.get("role") == "assistant" for m in msgs[:last_user]):
        return "followup:after_assistant_turn"
    words = re.findall(r"[a-z']+", _content_text(msgs[last_user].get("content")).lower())
    if words and len(words) <= 5 and all(w in _SHORT_REPLY for w in words):
        return "followup:short_reply"
    # A short message that is CONSENT, even with other words in it ("yes I confirm",
    # "confirmed, send it"): only meaningful as a reply, and a router that sends just the latest
    # message would otherwise hand it to System-1 with no history to fail the first rule. Keyed on
    # the first word or a confirm-verb, so a short question ("is there no fee?") is untouched.
    if words and len(words) <= 8 and (words[0] in _CONSENT_START
                                      or any(w.startswith(("confirm", "proceed")) for w in words)):
        return "followup:consent_reply"
    return None


def _content_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p["text"] for p in c if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _session(headers) -> str | None:
    """The client's conversation id, if it sends one, so telemetry rows can be grouped into
    conversations. Clients opt in with an X-Session-Id header; nothing is inferred."""
    v = (headers or {}).get("x-session-id")
    return _hdr(v)[:128] if v else None


def _wants_stream(body) -> bool:
    return isinstance(body, dict) and body.get("stream") is True


@app.get("/live")
async def live():
    """For liveness and readiness probes: the gate itself, never System-1. The gate fails open, so a
    slow or absent System-1 must not make it NotReady or get it restarted; /health is for monitoring."""
    return {"status": "ok", "gate_enabled": config.GATE_ENABLED, "shadow": config.SHADOW_MODE}


@app.get("/health")
async def health():
    """Liveness for the gate, plus whatever System-1 reports. Never fails on System-1's account.
    For monitoring: each call makes a real /v1/systemone read, so poll it sparingly; probes use /live."""
    out = {"status": "ok", "gate_enabled": config.GATE_ENABLED, "shadow": config.SHADOW_MODE}
    try:
        out["system1"] = await _system1.health()
        out["system1_ok"] = True
    except Exception as e:  # noqa: BLE001 - health must not raise
        out["system1"] = {"status": "unreachable", "detail": str(e)[:200]}
        # For monitoring to alert on. "status" stays "ok" on purpose: the gate is alive and fails
        # open, so liveness must not restart it for System-1's problem.
        out["system1_ok"] = False
    return out


def _authorized(headers) -> bool:
    if not config.CLIENT_TOKEN:
        return True
    got = (headers.get("authorization") or "").encode("utf-8")
    return hmac.compare_digest(got, (f"Bearer {config.CLIENT_TOKEN}").encode())


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    t0 = time.perf_counter()
    if not _authorized(request.headers):
        # Checked before System-1 is asked anything: an unauthenticated caller costs no forward pass.
        return JSONResponse(status_code=401, content={"error": {
            "message": "missing or invalid bearer token", "type": "authentication_error"}})
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        # Not JSON, or JSON that is not a chat request (a list, a string...). The agent speaks
        # the Responses API, so there is nothing to translate it into: say so, with a 400.
        return _bad_request("request body must be a JSON chat-completions object", t0,
                            dict(request.headers), "unexpected_body_shape")

    headers = dict(request.headers)
    text = _last_user_text(body)
    followup = _followup_reason(body)
    if followup:
        # A reply to the agent: System-1 is not asked, so "yes" can never be answered out of context.
        decision = policy.Decision(route=policy.ESCALATE, reason=followup, confident=False)
    else:
        decision = await _decide(text)
    if config.SHADOW_MODE:
        decision = policy.apply_shadow(decision)

    if decision.route == policy.ANSWERED:
        return _answer(body, headers, decision, t0, text)
    return await _forward_agent(body, headers, decision, t0, text)


async def _decide(text: str) -> policy.Decision:
    if not config.GATE_ENABLED:
        return policy.decision_for_disabled()
    if config.invalid_settings():
        return policy.Decision(route=policy.ESCALATE, reason="config:invalid_settings", confident=False)
    if not text.strip():
        return policy.decision_for_failure("empty_query")
    rule = _llm_rule(text)
    if rule:
        # Decided by text rules, which are exact and free -- System-1's own read of "does this need
        # more than a yes/no" measured AUC ~0.84 only. System-1 is not asked.
        return policy.Decision(route=policy.ESCALATE, reason=rule, confident=False)
    # The action question only while its veto can fire: each question is one vLLM read, and at
    # SYSTEM1_MAX_P_ACTION >= 1.0 (off) its score could never change a decision.
    qs = questions.select(None if config.MAX_P_ACTION < 1.0 else (questions.ANSWER,))
    try:
        # One call, both questions, over the banking question itself: the answer, and the
        # is_action_request veto that keeps "Can you transfer $200?" from getting a "Yes.".
        answers, meta = await _system1.ask(questions.build_state(text), qs)
        # decide() is inside the try as well: whatever System-1 sent back, judging it must not be
        # able to turn the request into a 500.
        d = policy.decide(answers, asked=qs, system1_ms=meta.get("elapsed_ms"))
        d.routing_model = meta.get("routing_model")
        return d
    except System1Unavailable as e:
        logger.info("system1 unavailable (%s) -> escalating", e)
        _note_system1_unavailable(str(e)[:120])
        return policy.decision_for_failure(str(e)[:120])
    except Exception as e:
        logger.exception("unexpected System-1 error -> escalating")
        return policy.decision_for_failure(f"unexpected:{str(e)[:80]}")


# A yes/no question opens with an auxiliary verb ("Is a mortgage a loan?", "Can I ...?"). One
# that opens with what/how/why/... -- or with anything else -- asks for more than a yes or no.
_YES_NO_START = {"is", "are", "am", "was", "were", "do", "does", "did", "can", "could", "will",
                 "would", "should", "shall", "may", "might", "must", "has", "have", "had"}


def _is_yes_no_question(text: str) -> bool:
    words = re.findall(r"[a-z']+", text.lower())
    return bool(words) and words[0] in _YES_NO_START


# The text rules: questions that go to the LLM agent without asking System-1 ("Is my card blocked?",
# "Is 5% on $1000 more than $40?"). They live in a file, text_rules.json beside this module, so a
# deployment can add its own regexes without touching the code; GATE_TEXT_RULES points to another
# file. Loaded once at start; a malformed rule stops the gate rather than being skipped.
_REASON_TOKEN = re.compile(r"^[a-z0-9_:.-]+$")      # reasons go into a header and the decision log


def _load_text_rules(path: str) -> list[tuple[str, str, list[re.Pattern]]]:
    """Read and check the rules file; refuse anything that could not work as written, naming it."""
    def bad(msg):
        raise SystemExit(f"text rules: {path}: {msg}")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        bad(f"cannot read: {e}")
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, list):
        bad("'rules' must be a list of rules")
    out, seen = [], set()
    for k, r in enumerate(rules):
        if not isinstance(r, dict):
            bad(f"rule {k} must be an object")
        reason = r.get("reason")
        if not isinstance(reason, str) or not _REASON_TOKEN.match(reason):
            bad(f"rule {k}: 'reason' must be a short lower-case token (letters, digits, _ : . -), got {reason!r}")
        kinds = [x for x in ("any", "all") if x in r]
        if len(kinds) != 1:
            bad(f"rule {k} ({reason}): give exactly one of 'any' or 'all'")
        kind, pats = kinds[0], r[kinds[0]]
        if not isinstance(pats, list) or not pats or not all(isinstance(x, str) and x for x in pats):
            bad(f"rule {k} ({reason}): '{kind}' must be a non-empty list of patterns")
        for x in pats:
            # the question is lower-cased before matching, so an upper-case letter (outside an escape
            # such as \S or \W) could never match
            if re.search(r"[A-Z]", re.sub(r"\\.", "", x)):
                bad(f"rule {k} ({reason}): pattern {x!r} has upper-case letters; questions are matched lower-cased")
        try:
            compiled = [re.compile(x) for x in pats]
        except re.error as e:
            bad(f"rule {k} ({reason}): bad pattern: {e}")
        if reason in seen:
            logger.warning("text rules: %s: reason %r is used by more than one rule; the decision log "
                           "cannot tell them apart", path, reason)
        seen.add(reason)
        out.append((reason, kind, compiled))
    if not out:
        logger.warning("text rules: %s has no rules: every plain yes/no question goes to System-1", path)
    return out


TEXT_RULES = _load_text_rules(config.GATE_TEXT_RULES)


def _llm_rule(text: str) -> str | None:
    """The reason a question must go to the LLM without asking System-1, or None.

    Every rule errs toward the LLM: a question it wrongly catches only costs one LLM call, a
    question it wrongly lets through gets a yes/no displayed that may be meaningless.
    """
    if not _is_yes_no_question(text):
        return "not_yes_no_question"                 # "What is a mortgage?"
    low = text.lower().strip()
    for reason, kind, pats in TEXT_RULES:
        hits = (p.search(low) for p in pats)
        if (all(hits) if kind == "all" else any(hits)):
            return reason
    return None


ANSWER_TEXT = {"yes": "Yes.", "no": "No."}


def _answer(body: dict, headers: dict, decision: policy.Decision, t0: float, text: str):
    """Display System-1's yes/no: a standard chat completion (or event stream), no LLM call."""
    content = ANSWER_TEXT[decision.answer]
    model = body.get("model") if isinstance(body.get("model"), str) else "system1"
    cid, created = f"chatcmpl-system1-{int(time.time() * 1000)}", int(time.time())
    total_ms = 1000 * (time.perf_counter() - t0)
    telemetry.record(decision, text, total_ms, upstream_status=200, session_id=_session(headers),
                     streamed=_wants_stream(body))
    marks = _marks(decision, total_ms)
    if _wants_stream(body):
        def events():
            yield _chunk(cid, created, model, {"role": "assistant", "content": content})
            yield _chunk(cid, created, model, {}, "stop")
            yield b"data: [DONE]\n\n"
        return StreamingResponse(events(), media_type="text/event-stream", headers=marks)
    return JSONResponse(headers=marks, content={
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}]})


def _bad_request(message: str, t0: float, headers: dict, reason: str):
    d = policy.decision_for_failure(reason)
    total_ms = 1000 * (time.perf_counter() - t0)
    telemetry.record(d, "", total_ms, upstream_status=400, session_id=_session(headers))
    return JSONResponse(status_code=400, headers=_marks(d, total_ms),
                        content={"error": {"message": message, "type": "invalid_request_error"}})


def _auth(headers) -> dict:
    """The customer's credentials, for every call the gate makes to the agent on their behalf."""
    return {k: v for k, v in (headers or {}).items() if k.lower() == "authorization"}


async def _forward_agent(body: dict, headers, decision, t0: float, text: str):
    """Serve the request from the agent, which holds the MCP tools, translating chat
    completions to the Responses API and back (agent_client.py)."""
    auth = _auth(headers)
    session = _session(headers)
    model = body.get("model") if isinstance(body.get("model"), str) else config.AGENT_ENTITY_NAME

    def failed(e: Exception, status: int):
        total_ms = 1000 * (time.perf_counter() - t0)
        telemetry.record(decision, text, total_ms, upstream_status=status, session_id=session)
        if isinstance(e, AgentError) and e.body is not None and e.status < 500:
            # The agent's own refusal (401 bad token, 422...): pass it through untouched.
            return Response(content=e.body, status_code=e.status, media_type=e.media_type,
                            headers=_marks(decision, total_ms))
        logger.error("agent request failed: %s", str(e)[:200])
        msg = ("the banking assistant could not complete the request"
               if isinstance(e, AgentError) and str(e).startswith("agent run failed")
               else "banking assistant unreachable")
        return JSONResponse(status_code=502, headers=_marks(decision, total_ms),
                            content={"error": {"message": msg, "type": "upstream_error"}})

    if _wants_stream(body):
        try:
            r = await _agent.open_stream(body, auth)
        except AgentError as e:
            return failed(e, e.status)
        except Exception as e:  # noqa: BLE001
            return failed(e, 599)
        marks = _marks(decision, 1000 * (time.perf_counter() - t0))

        def done(tools):
            # Recorded when the stream ENDS, so total_ms is the full response time here too.
            telemetry.record(decision, text, 1000 * (time.perf_counter() - t0),
                             upstream_status=r.status_code, session_id=session, streamed=True,
                             agent_tools=tools)
        return StreamingResponse(AgentClient.relay_stream(r, model, on_done=done),
                                 media_type="text/event-stream", headers=marks)

    try:
        status, completion, tools = await _agent.complete(body, auth)
    except AgentError as e:
        return failed(e, e.status)
    except Exception as e:  # noqa: BLE001
        return failed(e, 599)
    total_ms = 1000 * (time.perf_counter() - t0)
    telemetry.record(decision, text, total_ms, upstream_status=status, session_id=session,
                     agent_tools=tools)
    return JSONResponse(content=completion, status_code=status, headers=_marks(decision, total_ms))


# Hop-by-hop headers, and ones the proxy recomputes, are not copied across.
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
        "transfer-encoding", "upgrade", "host", "content-length"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_devui(path: str, request: Request):
    """Everything that is not /health or /v1/chat/completions goes to the agent unchanged: the
    DevUI web UI and its own API. Registered LAST, so the gate's routes take precedence.

    NOTE this includes DevUI's native /v1/responses, which does NOT go through System-1 or the
    decision log (DevUI still enforces its token, so it is not an auth hole). Set
    GATE_PROXY_DEVUI=false where only chat completions should be reachable.
    """
    if not config.PROXY_DEVUI:
        return JSONResponse(status_code=404, content={"error": {"message": "not found",
                                                                "type": "invalid_request_error"}})
    url = "{}/{}".format(config.AGENT_URL.rstrip("/"), path)
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    req = _http.build_request(request.method, url, params=request.query_params,
                              headers=fwd, content=await request.body())
    try:
        r = await _http.send(req, stream=True)
    except Exception as e:  # noqa: BLE001
        logger.error("agent unreachable at %s: %s", url, str(e)[:200])
        return JSONResponse(status_code=502, content={"error": {
            "message": "banking assistant unreachable", "type": "upstream_error"}})
    out = {k: v for k, v in r.headers.items() if k.lower() not in _HOP}
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out,
                             background=BackgroundTask(r.aclose))


def _hdr(v) -> str:
    """A header-safe value. Reasons can carry System-1 error text, which may echo the customer's
    query in any script; HTTP headers are Latin-1, so non-ASCII is escaped and CR/LF removed."""
    s = str(v).encode("ascii", "backslashreplace").decode("ascii")
    return s.replace("\r", " ").replace("\n", " ")[:200]


def _marks(d: policy.Decision, total_ms: float) -> dict:
    """Decision exposed in headers, so the response body keeps the exact OpenAI schema.

    OFF unless GATE_DEBUG_HEADERS is set: route, reason and per-question scores are internal, and
    a reason can carry upstream error text. The decision log has all of it regardless.
    """
    if not config.DEBUG_HEADERS:
        return {}
    h = {"X-Gate-Route": _hdr(d.route), "X-Gate-Reason": _hdr(d.reason),
         "X-Gate-Confident": str(d.confident).lower(),
         "X-Gate-Total-Ms": f"{total_ms:.1f}"}
    if d.system1_ms is not None:
        h["X-Gate-System1-Ms"] = f"{d.system1_ms:.1f}"
    for qid, p in d.scores.items():
        h["X-Gate-P-{}".format(_hdr(qid).replace("_", "-"))] = f"{p:.4f}"
    return h


def main():
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
