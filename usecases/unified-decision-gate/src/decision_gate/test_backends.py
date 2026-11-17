# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the vLLM System-1 client, against canned vLLM /v1/systemone responses.

    cd src && python3 -m decision_gate.test_backends

vLLM's answers must come out in the shape policy.decide reads (confidence -> answer_confidence,
diagnostics' label_mass copied onto the answer); health() must check that vLLM serves SYSTEM1_MODEL
AND has /v1/systemone; SYSTEM1_TIMEOUT_S must bound the whole call; and every malformed or refused
response must raise System1Unavailable, with the cause named and no body, so the gate fails open.
"""
import asyncio
import os
import time

os.environ.setdefault("DEVUI_AUTH_TOKEN", "unused")

import httpx  # noqa: E402

from . import config  # noqa: E402
from .system1_client import System1Client, System1Unavailable  # noqa: E402

QUESTIONS = {"answer": {"type": "noul"}, "is_action_request": {"type": "noul"}}
MODEL = "Qwen/Qwen3-0.6B"


def _client(handler):
    client = System1Client(base_url="http://vllm")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def run(payload, status=200, seen=None):
    """One ask() against a vLLM that returns `payload`; `seen` collects the request bodies."""
    config.SYSTEM1_MODEL = MODEL

    def handler(req):
        if seen is not None:
            seen.append((req.url.path, req.read()))
        return httpx.Response(status, json=payload)
    client = _client(handler)
    try:
        return asyncio.run(client.ask({"question": "q"}, QUESTIONS))
    finally:
        asyncio.run(client.aclose())


def vllm_like(p, mass=0.93, diagnostics=True):
    def noul(x):
        return {"type": "noul", "noul": x, "probabilities": {"yes": x, "no": 1 - x}, "confidence": max(x, 1 - x)}
    out = {"object": "structured_decision", "model": MODEL,
           "answers": {"answer": noul(p), "is_action_request": noul(0.1)},
           "usage": {"input_tokens": 40, "output_tokens": 2}}
    if diagnostics:
        out["diagnostics"] = {"answer": {"label_mass": mass, "argmax_is_label": True}}
    return out


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def raises(fn):
    try:
        fn()
    except System1Unavailable as e:
        return str(e)
    return None


@case
def request_names_the_model():
    seen = []
    run(vllm_like(0.8), seen=seen)
    import json
    path, body = seen[0]
    body = json.loads(body)
    assert path == "/v1/systemone" and body["model"] == MODEL and set(body["questions"]) == set(QUESTIONS)


@case
def confidence_becomes_answer_confidence():
    answers, meta = run(vllm_like(0.83))
    assert answers["answer"]["noul"] == 0.83 and abs(answers["answer"]["answer_confidence"] - 0.83) < 1e-9
    assert meta["routing_model"] == MODEL


@case
def label_mass_is_copied_when_sent():
    answers, _ = run(vllm_like(0.83, mass=0.41))
    assert answers["answer"]["label_mass"] == 0.41
    assert "label_mass" not in answers["is_action_request"], "invented a label_mass vLLM did not send"
    answers, _ = run(vllm_like(0.83, diagnostics=False))
    assert "label_mass" not in answers["answer"]


@case
def malformed_answers_fail_open():
    bad = vllm_like(0.9)
    bad["answers"]["answer"]["noul"] = None
    assert raises(lambda: run(bad)), "a missing probability passed"
    bad = vllm_like(0.9)
    del bad["answers"]["answer"]["confidence"]
    assert raises(lambda: run(bad)), "a missing confidence passed"
    assert raises(lambda: run({"model": MODEL, "answers": {}})), "no answers passed"


@case
def refusals_fail_open_and_name_the_cause():
    assert "busy" in raises(lambda: run({}, status=503))
    err = raises(lambda: run({"detail": "not implemented"}, status=501))
    assert err and "logprobs-mode" in err, f"501 not diagnosed: {err!r}"
    err = raises(lambda: run({"error": {"message": "label is not a single token"}}, status=400))
    assert err and "single token" in err and "label is not" not in err, f"400 not diagnosed, or body leaked: {err!r}"
    err = raises(lambda: run({"detail": "Not Found"}, status=404))
    assert err and "no /v1/systemone" in err and "0.28.0" in err, f"missing endpoint not diagnosed: {err!r}"
    err = raises(lambda: run({"error": {"message": "The model `x` does not exist."}}, status=404))
    assert err and "does not serve SYSTEM1_MODEL" in err, f"unknown model not diagnosed: {err!r}"


def vllm_server(models, systemone_status=200, delay_s=0.0):
    """A canned vLLM: /v1/models lists `models`; /v1/systemone answers with `systemone_status`."""
    async def handler(req):
        if delay_s:
            await asyncio.sleep(delay_s)
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in models]})
        if req.url.path == "/v1/systemone":
            if systemone_status != 200:
                return httpx.Response(systemone_status, json={"detail": "Not Found"})
            return httpx.Response(200, json=vllm_like(0.7))
        return httpx.Response(200)
    return handler


def health_of(handler, timeout_s=None):
    config.SYSTEM1_MODEL = MODEL
    client = System1Client(base_url="http://vllm", timeout_s=timeout_s)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        return asyncio.run(client.health())
    finally:
        asyncio.run(client.aclose())


@case
def health_checks_the_served_model_and_the_endpoint():
    out = health_of(vllm_server([MODEL, "Qwen/Qwen3-4B-Instruct-2507"]))
    assert out["model"] == MODEL and "probe_ms" in out
    err = raises(lambda: health_of(vllm_server(["Qwen/Qwen3-4B-Instruct-2507"])))
    assert err and "not SYSTEM1_MODEL" in err, f"health passed a vLLM that does not serve the model: {err!r}"
    err = raises(lambda: health_of(vllm_server([MODEL], systemone_status=404)))
    assert err and "no /v1/systemone" in err, f"health passed a vLLM with no /v1/systemone: {err!r}"


@case
def the_timeout_is_one_deadline_for_the_whole_call():
    """httpx's timeout is per phase; the gate's must bound the whole call (and health's two calls)."""
    t0 = time.perf_counter()
    err = raises(lambda: health_of(vllm_server([MODEL], delay_s=0.5), timeout_s=0.2))
    assert err and "0.2s" in err and time.perf_counter() - t0 < 0.45, f"health outlived its deadline: {err!r}"
    config.SYSTEM1_MODEL = MODEL
    client = System1Client(base_url="http://vllm", timeout_s=0.2)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(vllm_server([MODEL], delay_s=0.5)))
    try:
        t0 = time.perf_counter()
        err = raises(lambda: asyncio.run(client.ask("q", QUESTIONS)))
        assert err and "timeout" in err and time.perf_counter() - t0 < 0.45, f"ask outlived its deadline: {err!r}"
    finally:
        asyncio.run(client.aclose())


@case
def health_ends_both_calls_when_one_fails():
    """A fast failure (the model not served) must end /health at once, not wait for the slow read."""
    async def handler(req):
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "other"}]})
        await asyncio.sleep(1.0)
        return httpx.Response(200, json=vllm_like(0.7))
    t0 = time.perf_counter()
    err = raises(lambda: health_of(handler, timeout_s=2.0))
    assert err and "not SYSTEM1_MODEL" in err and time.perf_counter() - t0 < 0.5, \
        f"health waited for the slow call: {err!r} after {time.perf_counter() - t0:.2f}s"


def main():
    saved = config.SYSTEM1_MODEL
    failures = []
    try:
        for fn in CASES:
            try:
                fn()
                print(f"ok    {fn.__name__}")
            except AssertionError as e:
                failures.append(fn.__name__)
                print(f"FAIL  {fn.__name__}: {e}")
    finally:
        config.SYSTEM1_MODEL = saved
    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
