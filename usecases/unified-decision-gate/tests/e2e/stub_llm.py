# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""A stand-in LLM for the end-to-end smoke test: an OpenAI-compatible /v1/chat/completions.

  * a question mentioning "balance" -> the stub asks for the real MCP tool get_balances(U1001);
    once the tool result comes back it replies with that result quoted, so the smoke test can
    prove the agent really called the banking MCP server
  * anything else -> a fixed, recognisable sentence
  * POST /v1/systemone (as model "stub-system1") -> a stand-in for the System-1 vLLM: every question
    a confident yes, never a request to act. The gate's text rules decide what reaches it

It exists so the REAL agent image -- DevUI, the agent, the MCP sidecar -- can be exercised end to
end without a LiteLLM gateway. It proves the plumbing, not the model: nothing here is a quality
test. Streaming and non-streaming are both supported, because the agent may use either.
"""
import asyncio
import json
import os
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

REPLY = "STUB-LLM reply: the banking assistant pipeline is connected."
# Simulated model time per completion, for latency tests: a real LLM takes far longer than the
# stub. 0 (default) = answer immediately.
DELAY_S = float(os.environ.get("STUB_LLM_DELAY_MS", "0")) / 1000.0
app = FastAPI()
SEEN = []          # every prompt the agent sent, so the smoke test can see what the LLM saw


@app.get("/seen")
async def seen():
    return {"requests": SEEN[-20:], "total": len(SEEN)}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "stub-model", "object": "model"},
                                       {"id": "stub-system1", "object": "model"}]}


@app.get("/health")
async def health():
    return {}


@app.post("/v1/systemone")
async def systemone(request: Request):
    body = await request.json()
    p = {"answer": 0.999, "is_action_request": 0.03}   # clear of the default cut-offs (0.9952 / 0.9906)
    answers = {q: {"type": "noul", "noul": p.get(q, 0.5), "confidence": max(p.get(q, 0.5), 1 - p.get(q, 0.5))}
               for q in (body.get("questions") or {})}
    return {"object": "structured_decision", "model": body.get("model"), "answers": answers,
            "usage": {"input_tokens": 0, "output_tokens": 1},
            "diagnostics": {q: {"label_mass": 0.99, "argmax_is_label": True} for q in answers}}


def _last(msgs, role):
    return next((m for m in reversed(msgs) if m.get("role") == role), None)


def _text(m):
    c = (m or {}).get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    if DELAY_S:
        await asyncio.sleep(DELAY_S)
    model = body.get("model") or "stub-model"
    created = int(time.time())
    msgs = body.get("messages") or []
    SEEN.append([{"role": m.get("role"), "text": _text(m)} for m in msgs])
    tools = {t.get("function", {}).get("name") for t in body.get("tools") or []}
    if msgs and msgs[-1].get("role") == "tool":
        reply = f"STUB-LLM tool result: {_text(msgs[-1])[:300]}"
        return _completion(reply, model, created, body.get("stream"))
    if "balance" in _text(_last(msgs, "user")).lower() and "get_balances" in tools:
        call = {"id": "call_stub_1", "type": "function",
                "function": {"name": "get_balances", "arguments": json.dumps({"user_id": "U1001"})}}
        return _tool_call(call, model, created, body.get("stream"))
    return _completion(REPLY, model, created, body.get("stream"))


def _tool_call(call, model, created, stream):
    if stream:
        def events():
            first = {"role": "assistant", "content": None,
                     "tool_calls": [dict(call, index=0)]}
            yield "data: {}\n\n".format(json.dumps({"id": "stub-t", "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": first, "finish_reason": None}]}))
            yield "data: {}\n\n".format(json.dumps({"id": "stub-t", "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}))
            yield "data: [DONE]\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")
    return {"id": "stub-t", "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                                                 "tool_calls": [call]},
                         "finish_reason": "tool_calls"}]}


def _completion(REPLY, model, created, stream):
    if stream:
        def events():
            for i, piece in enumerate(REPLY.split(" ")):
                delta = {"role": "assistant", "content": piece} if i == 0 else {"content": " " + piece}
                chunk = {"id": "stub-1", "object": "chat.completion.chunk", "created": created,
                         "model": model, "choices": [{"index": 0, "delta": delta,
                                                      "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            done = {"id": "stub-1", "object": "chat.completion.chunk", "created": created,
                    "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")
    return {"id": "stub-1", "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 9, "total_tokens": 10}}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4000, log_level="warning")  # nosec B104 - test stub in a container, reached by the other containers
