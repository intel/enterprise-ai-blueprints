# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Talk to the banking agent, which speaks the OpenAI RESPONSES API, on behalf of clients that
speak CHAT COMPLETIONS.

The agent is served by Agent Framework DevUI (agent-framework-devui, pinned in
requirements.txt). DevUI exposes POST /v1/responses and no /v1/chat/completions at all, so the
gate translates in both directions:

    chat request   {"messages": [...], "stream": bool, ...}
      -> POST /v1/responses {"input": [{role, content}...], "metadata": {"entity_id": ...}}
    response       {"output": [function_call..., message{output_text}]}
      -> chat.completion {"choices": [{"message": {"role": "assistant", "content": ...}}]}
    stream         response.output_text.delta events -> chat.completion.chunk events

Three DevUI facts this module depends on, all verified against 1.0.0b261008:

  * The entity is chosen by `metadata.entity_id` -- NOT by `model`, whatever DevUI's own
    request docstring says. A request without it is a 400.
  * The entity id carries a random suffix generated per process
    ("agent_in_memory_banking-assistant_<hex>"), so it cannot be configured. It is looked up by
    NAME from GET /v1/entities, cached, and looked up again once if the agent answers 404 (it
    restarted and the id changed).
  * Every route needs the DevUI token. The customer's own Authorization header is used for all
    of them, so the gate holds no credential of its own.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from . import config

logger = logging.getLogger(__name__)

# Roles forwarded to the agent: the conversation itself, nothing that instructs it.
#   * "system" / "developer" from the CLIENT are dropped. The agent has its own instructions,
#     and it can move money: a client-supplied system message carries more weight with the
#     model than user text, so it is not a channel the gate offers anyone.
#   * "tool" messages, and assistant turns that carry only tool_calls, are artefacts of a
#     client's own tool loop; the agent runs its own tools, so they are not replayed either.
_ROLES = {"user", "assistant"}


class AgentError(Exception):
    """The agent could not be reached or refused the request. Carries an HTTP status."""

    def __init__(self, status: int, message: str, body: bytes | None = None,
                 media_type: str = "application/json"):
        super().__init__(message)
        self.status, self.body, self.media_type = status, body, media_type


def _text_of(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):   # chat content-parts form
        parts = [p["text"] for p in content
                 if isinstance(p, dict) and isinstance(p.get("text"), str)]
        return " ".join(parts) if parts else None
    return None


def to_responses_input(messages) -> list[dict[str, str]]:
    """Chat `messages` -> Responses `input`, keeping the whole conversation as history."""
    out = []
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict) or m.get("role") not in _ROLES:
            continue
        text = _text_of(m.get("content"))
        if text is None:          # e.g. an assistant turn that was only tool_calls
            continue
        out.append({"role": m["role"], "content": text})
    return out


def output_text(resp: dict) -> str:
    """The assistant's final text from a Responses `output`: the LAST message item, since
    function_call items and their intermediate messages come before it."""
    texts = []
    for item in resp.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            t = "".join(c.get("text", "") for c in item.get("content") or []
                        if isinstance(c, dict) and c.get("type") == "output_text")
            if t:
                texts.append(t)
    return texts[-1] if texts else ""


def tool_calls(resp: dict) -> list[str]:
    """Names of the tools the agent called while answering -- shadow mode's label signal."""
    return [i.get("name") for i in resp.get("output") or []
            if isinstance(i, dict) and i.get("type") == "function_call" and i.get("name")]


def _failure(resp: dict) -> str | None:
    """The agent's error message if the run failed, else None. A failed run must not come back
    as HTTP 200 with empty content -- that reads as an answer."""
    err = resp.get("error")
    if resp.get("status") == "failed" or err:
        if isinstance(err, dict):
            return err.get("message") or "the banking assistant failed"
        return str(err) if err else "the banking assistant failed"
    return None


def _finish(resp: dict) -> str:
    """'length' when the run was cut off (e.g. max_output_tokens), so a client knows the text is
    truncated; 'stop' otherwise."""
    return "length" if resp.get("status") == "incomplete" else "stop"


def to_chat_completion(resp: dict, model: str) -> dict:
    return {
        "id": "chatcmpl-%s" % (resp.get("id") or "agent"),
        "object": "chat.completion",
        "created": int(resp.get("created_at") or time.time()),
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": output_text(resp)},
                     "finish_reason": _finish(resp)}],
    }


def _chunk(cid: str, created: int, model: str, delta: dict, finish: str | None = None) -> bytes:
    return ("data: {}\n\n".format(json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}))).encode("utf-8")


class AgentClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str | None = None):
        self._http = http
        self.base_url = (base_url or config.AGENT_URL).rstrip("/")
        self._entity: str | None = None

    # --- entity discovery -----------------------------------------------------------------
    async def _lookup_entity(self, auth: dict[str, str]) -> str:
        r = await self._http.get(f"{self.base_url}/v1/entities", headers=auth)
        if r.status_code >= 400:
            raise AgentError(r.status_code, f"entity lookup failed ({r.status_code})",
                             r.content, r.headers.get("content-type", "application/json"))
        ents = [e for e in (r.json().get("entities") or []) if isinstance(e, dict)]
        named = [e for e in ents if e.get("name") == config.AGENT_ENTITY_NAME]
        pick = named or (ents if len(ents) == 1 else [])
        if not pick:
            raise AgentError(502, "agent serves no entity named {!r} (found {})".format(config.AGENT_ENTITY_NAME, [e.get("name") for e in ents]))
        self._entity = pick[0]["id"]
        logger.info("agent entity: %s", self._entity)
        return self._entity

    def _request(self, body: dict, entity: str) -> dict:
        req: dict[str, Any] = {"input": to_responses_input(body.get("messages")),
                               "metadata": {"entity_id": entity},
                               "stream": body.get("stream") is True}
        for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                         ("max_tokens", "max_output_tokens"),
                         ("max_completion_tokens", "max_output_tokens")):
            if isinstance(body.get(src), (int, float)) and not isinstance(body.get(src), bool):
                req[dst] = body[src]
        return req

    async def _send(self, body: dict, auth: dict[str, str]) -> httpx.Response:
        """POST /v1/responses, streamed. Re-resolves the entity once if the agent restarted."""
        for attempt in (0, 1):
            entity = self._entity or await self._lookup_entity(auth)
            req = self._http.build_request("POST", f"{self.base_url}/v1/responses",
                                           json=self._request(body, entity), headers=auth)
            r = await self._http.send(req, stream=True)
            if r.status_code == 404 and attempt == 0:
                await r.aclose()
                self._entity = None          # stale id after an agent restart: look it up again
                continue
            return r
        return r  # pragma: no cover - loop always returns

    # --- the two call shapes --------------------------------------------------------------
    async def complete(self, body: dict, auth: dict[str, str]) -> tuple[int, dict, list[str]]:
        """Non-streaming. Returns (status, chat.completion, tools the agent called)."""
        r = await self._send(body, auth)
        try:
            raw = await r.aread()
        finally:
            await r.aclose()
        if r.status_code >= 400:
            raise AgentError(r.status_code, f"agent returned {r.status_code}", raw,
                             r.headers.get("content-type", "application/json"))
        try:
            resp = json.loads(raw)
        except ValueError as e:
            raise AgentError(502, "agent returned a non-JSON body") from e
        failure = _failure(resp)
        if failure:
            raise AgentError(502, f"agent run failed: {failure[:200]}")
        model = body.get("model") if isinstance(body.get("model"), str) else config.AGENT_ENTITY_NAME
        return r.status_code, to_chat_completion(resp, model), tool_calls(resp)

    async def open_stream(self, body: dict, auth: dict[str, str]) -> httpx.Response:
        """Streaming: returns the OPEN upstream response; pass it to relay_stream()."""
        r = await self._send(body, auth)
        if r.status_code >= 400:
            try:
                raw = await r.aread()
            finally:
                await r.aclose()
            raise AgentError(r.status_code, f"agent returned {r.status_code}", raw,
                             r.headers.get("content-type", "application/json"))
        return r

    @staticmethod
    async def relay_stream(r: httpx.Response, model: str,
                           on_done=None) -> AsyncIterator[bytes]:
        """Responses SSE -> chat.completion.chunk SSE, as it arrives.

        Only text deltas become content; DevUI's lifecycle and trace events are dropped. A
        failed run ends the stream with an OpenAI-style error event rather than a silent cut.
        `on_done(tool_names)` is called once at the end with the tools the agent used.
        """
        cid, created, first, tools = "chatcmpl-agent", int(time.time()), True, []
        finished = False
        try:
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "response.created":
                    rid = (ev.get("response") or {}).get("id")
                    cid = f"chatcmpl-{rid}" if rid else cid
                elif kind == "response.output_text.delta" and isinstance(ev.get("delta"), str):
                    delta = {"content": ev["delta"]}
                    if first:
                        delta["role"], first = "assistant", False
                    yield _chunk(cid, created, model, delta)
                elif kind in ("response.completed", "response.incomplete"):
                    resp = ev.get("response") or {}
                    tools = tool_calls(resp)
                    yield _chunk(cid, created, model, {},
                                 "length" if kind == "response.incomplete" else _finish(resp))
                    finished = True
                elif kind in ("response.failed", "error"):
                    err = ev.get("error") or (ev.get("response") or {}).get("error") or {}
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    yield ("data: {}\n\n".format(json.dumps({"error": {
                        "message": msg or "the banking assistant failed", "type": "upstream_error"}}))
                           ).encode("utf-8")
                    finished = True
            if not finished:          # upstream ended without a terminal event: a truncated answer
                yield ("data: {}\n\n".format(json.dumps({"error": {
                    "message": "agent stream ended before a terminal response event",
                    "type": "upstream_error"}}))).encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            await r.aclose()
            if on_done is not None:
                try:
                    on_done(tools)
                except Exception:
                    logger.exception("stream completion hook failed")
