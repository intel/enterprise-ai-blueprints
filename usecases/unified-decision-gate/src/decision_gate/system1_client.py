# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Client for System-1: a structured-decisions model served by vLLM (config.SYSTEM1_URL).

Wire contract, from vLLM's docs (models/structured_decisions_models):

    POST /v1/systemone
      {"model": <SYSTEM1_MODEL>, "state": <json>, "questions": {qid: {...}},
       "instructions": <questions.SYSTEM_INSTRUCTIONS, the system message; only when set>}
    ->
      {"object": "structured_decision", "model": ...,
       "answers": {qid: {"type": "noul", "noul": <P(true)>,
                         "probabilities": {"yes": f, "no": f}, "confidence": f}},
       "usage": {...},
       "diagnostics": {qid: {"label_mass": f, "argmax_is_label": bool}}}

The client hands the gate each answer in the shape policy.decide reads: `noul` is P(yes), vLLM's
`confidence` becomes `answer_confidence`, and the question's `label_mass` from `diagnostics`, when
vLLM sends one, is copied onto the answer (policy.decide escalates on a low one).

vLLM must have the endpoint (v0.32.0 does; the 0.24.0 / 0.28.0 the platform deploys by default do
not, and answer 404) and must serve the model with `--logprobs-mode raw_logprobs` (or `processed_logprobs`), or it
has no way to read the label probabilities (501). A 400 is a request vLLM rejects: a label that is
not a single token, or a field it does not accept. All three are deployment mistakes rather than
load, and are named as such; no response body ever reaches the decision log.

SYSTEM1_TIMEOUT_S is a hard deadline for the whole call (asyncio.wait_for), not httpx's per-phase
timeout, which would let connect + read take twice as long before the gate fails open.

Any failure here raises System1Unavailable and the caller escalates. The gate must fail OPEN: the
assistant never breaks because the cheap tier is unhappy.
"""

import asyncio
import logging
import time

import httpx

from . import config
from .questions import SYSTEM_INSTRUCTIONS

logger = logging.getLogger(__name__)


class System1Unavailable(Exception):
    """System-1 could not give a usable answer. The caller escalates; this is not an error path."""


class System1Client:
    def __init__(self, base_url: str | None = None, timeout_s: float | None = None):
        self.base_url = (base_url or config.SYSTEM1_URL).rstrip("/")
        self.timeout_s = timeout_s or config.SYSTEM1_TIMEOUT_S
        headers = {"content-type": "application/json"}
        if config.SYSTEM1_API_KEY:
            headers["authorization"] = f"Bearer {config.SYSTEM1_API_KEY}"
        self._client = httpx.AsyncClient(timeout=self.timeout_s, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict:
        """Can System-1 answer? /v1/models must list SYSTEM1_MODEL, and a one-word /v1/systemone read
        must succeed: a vLLM that is up but has no such endpoint (v0.28.0) must not look healthy.
        Both calls run at once, under one SYSTEM1_TIMEOUT_S deadline. Raises when either fails."""
        async def models():
            r = await self._client.get(f"{self.base_url}/v1/models")
            r.raise_for_status()
            served = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
            if config.SYSTEM1_MODEL not in served:
                raise System1Unavailable(f"vLLM serves {served}, not SYSTEM1_MODEL={config.SYSTEM1_MODEL!r}")

        probe = {"probe": {"type": "noul", "instructions": "Is this a word?"}}
        # Both end together: when one call fails, the other is cancelled rather than left running in
        # the background until its own deadline.
        tasks = [asyncio.ensure_future(models()), asyncio.ensure_future(self._post("ok", probe))]
        done, pending = await asyncio.wait(tasks, timeout=self.timeout_s, return_when=asyncio.FIRST_EXCEPTION)
        for t in pending:
            t.cancel()
        failed = [t.exception() for t in done if t.exception() is not None]
        if failed:
            e = failed[0]
            raise e if isinstance(e, System1Unavailable) else System1Unavailable(f"health: {e}")
        if pending:
            raise System1Unavailable(f"health: no answer within {self.timeout_s:.1f}s")
        _, meta = tasks[1].result()
        return {"status": "ok", "model": config.SYSTEM1_MODEL, "probe_ms": meta["elapsed_ms"]}

    async def ask(self, state, questions: dict[str, dict]) -> tuple[dict[str, dict], dict]:
        """Score `questions` against `state`. Returns (answers, meta). Raises System1Unavailable.

        One forward pass per question, so cost scales with the number of questions asked, and
        with the state: two questions cost more than one, and a full customer snapshot far more
        than a short question. SYSTEM1_TIMEOUT_S must leave room for that.
        """
        try:
            return await asyncio.wait_for(self._post(state, questions), self.timeout_s)
        except asyncio.TimeoutError as e:
            raise System1Unavailable(f"timeout after {self.timeout_s:.1f}s") from e

    async def _post(self, state, questions: dict[str, dict]) -> tuple[dict[str, dict], dict]:
        body = {"state": state, "questions": questions, "model": config.SYSTEM1_MODEL}
        if SYSTEM_INSTRUCTIONS:
            body["instructions"] = SYSTEM_INSTRUCTIONS
        t0 = time.perf_counter()
        try:
            r = await self._client.post(f"{self.base_url}/v1/systemone", json=body)
        except httpx.TimeoutException as e:
            raise System1Unavailable(f"timeout after {self.timeout_s:.1f}s") from e
        except httpx.HTTPError as e:
            raise System1Unavailable(f"transport: {e}") from e
        elapsed_ms = round(1000 * (time.perf_counter() - t0), 1)

        if r.status_code == 503:
            raise System1Unavailable("busy (503)")
        if r.status_code == 501:
            raise System1Unavailable(f"http 501: {config.SYSTEM1_MODEL} has no /v1/systemone read strategy "
                                     "(is vLLM serving it with --logprobs-mode raw_logprobs?)")
        # The status and a fixed diagnosis only: an error body can echo the customer's question into
        # the decision log. The body is read only to tell the two 404s apart.
        if r.status_code == 404:
            if _is_unknown_route(r):
                raise System1Unavailable("http 404: no /v1/systemone on this vLLM (it needs a version that has "
                                         "it, e.g. v0.32.0; 0.24.0 and 0.28.0 do not)")
            raise System1Unavailable(f"http 404: vLLM does not serve SYSTEM1_MODEL={config.SYSTEM1_MODEL!r}")
        if r.status_code == 400:
            raise System1Unavailable("http 400: vLLM rejected the request (a label that is not a single "
                                     "token, or a field it does not accept)")
        if r.status_code >= 400:
            raise System1Unavailable(f"http {r.status_code}")

        try:
            payload = r.json()
        except ValueError as e:
            raise System1Unavailable("malformed json") from e

        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict) or not answers:
            raise System1Unavailable("no answers in response")

        diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
        for qid, a in answers.items():
            if not isinstance(a, dict) or a.get("confidence") is None:
                raise System1Unavailable(f"answer {qid!r} has no confidence")
            if a.get("type") == "noul" and a.get("noul") is None:
                raise System1Unavailable(f"noul answer {qid!r} has no probability")
            a["answer_confidence"] = a["confidence"]
            diag = diagnostics.get(qid)
            if isinstance(diag, dict) and "label_mass" in diag:
                a["label_mass"] = diag["label_mass"]

        meta = {"elapsed_ms": elapsed_ms, "routing_model": payload.get("model"),
                "usage": payload.get("usage") or {},
                "n_questions": len(questions)}
        return answers, meta


def _is_unknown_route(r: httpx.Response) -> bool:
    """FastAPI's own 404 for a route that does not exist is {"detail": "Not Found"}; vLLM's 404 for a
    model it does not serve is an OpenAI-style {"error": {...}}."""
    try:
        return r.json() == {"detail": "Not Found"}
    except ValueError:
        return True
