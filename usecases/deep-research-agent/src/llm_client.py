# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TypeVar

import httpx
import instructor
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings
from models import ProgressEvent
from token_tracker import current_progress_callback, current_research_id, tracker

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


async def _emit_safe(cb, event: ProgressEvent) -> None:
    """Fire-and-forget wrapper so the streaming loop never blocks on SSE delivery."""
    try:
        await cb(event)
    except Exception:
        pass


def _fire(cb, event: ProgressEvent) -> None:
    """Schedule SSE emission as a background task (non-blocking)."""
    asyncio.ensure_future(_emit_safe(cb, event))

# Exceptions worth retrying on.
# openai.NotFoundError (404) is included because vLLM can transiently return
# "model not found" while the server is restarting or reloading the model.
# InternalServerError (500) and APIStatusError (covers 503 via litellm gateway)
# are included because the hosted endpoint is occasionally flaky.
_RETRYABLE = (
    ConnectionError,
    TimeoutError,
    OSError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.NotFoundError,
    openai.InternalServerError,
    openai.APIStatusError,
)


def _make_openai_client() -> AsyncOpenAI:
    """Return an AsyncOpenAI client that bypasses the system proxy for localhost.

    Without this, httpx picks up http_proxy/https_proxy from the environment and
    routes requests to http://localhost:8000 through the Intel corporate proxy,
    which cannot reach internal endpoints.  trust_env=False disables all env-based
    proxy detection (the correct httpx idiom; proxy=None does NOT disable it).
    """
    http_client = httpx.AsyncClient(trust_env=False)
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        http_client=http_client,
    )


class LLMClient:
    def __init__(self, secondary: bool = False) -> None:
        if secondary and settings.secondary_llm_base_url and settings.secondary_llm_model:
            base_url = settings.secondary_llm_base_url
            model = settings.secondary_llm_model
        else:
            base_url = settings.llm_base_url
            model = settings.llm_model

        # trust_env=False prevents http_proxy routing localhost requests through
        # the corporate proxy (see copilot-instructions Rule #8).
        self._raw_client = AsyncOpenAI(
            base_url=base_url,
            api_key=(settings.secondary_llm_api_key if secondary and settings.secondary_llm_base_url and settings.secondary_llm_model else settings.llm_api_key),
            http_client=httpx.AsyncClient(trust_env=False),
        )
        self._instructor_client = instructor.from_openai(
            AsyncOpenAI(
                base_url=base_url,
                api_key=(settings.secondary_llm_api_key if secondary and settings.secondary_llm_base_url and settings.secondary_llm_model else settings.llm_api_key),
                http_client=httpx.AsyncClient(trust_env=False),
            ),
            mode=instructor.Mode.JSON,
        )
        self.model = model
        self.hardware = settings.secondary_llm_hardware if secondary else settings.llm_hardware
        # Secondary slot may override temperature (e.g. 0 for speculative-decoding
        # models). -1 sentinel means "inherit the primary temperature".
        _using_secondary = bool(secondary and settings.secondary_llm_base_url and settings.secondary_llm_model)
        self._is_secondary = _using_secondary
        if _using_secondary and settings.secondary_llm_temperature >= 0:
            self.temperature = settings.secondary_llm_temperature
        else:
            self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        self.top_p = settings.llm_top_p
        self.presence_penalty = settings.llm_presence_penalty
        self._extra_body: dict[str, Any] = {}
        if settings.llm_top_k > 0:
            self._extra_body["top_k"] = settings.llm_top_k
        if settings.llm_min_p > 0.0:
            self._extra_body["min_p"] = settings.llm_min_p
        if settings.llm_repetition_penalty != 1.0:
            self._extra_body["repetition_penalty"] = settings.llm_repetition_penalty

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        caller: str = "",
    ) -> str:
        """Stream the completion to measure TTFT and ITL alongside E2EL."""
        t0 = time.perf_counter()
        ttft_s: float | None = None
        content_parts: list[str] = []
        chunk_times: list[float] = []  # timestamps of each content chunk for ITL
        in_tok = out_tok = 0

        # Per-request SSE streaming (if a progress callback is active)
        _cb  = current_progress_callback.get(None)
        _rid = current_research_id.get("")
        _buf = ""
        _last_flush = t0
        _total_chars = 0

        stream = await self._raw_client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens or self.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            top_p=self.top_p,
            presence_penalty=self.presence_penalty,
            extra_body=self._extra_body or None,
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
        )
        n_chunks = 0
        async for chunk in stream:
            now = time.perf_counter()
            if chunk.choices and chunk.choices[0].delta.content:
                txt = chunk.choices[0].delta.content
                if ttft_s is None:
                    ttft_s = now - t0
                chunk_times.append(now)
                content_parts.append(txt)
                n_chunks += 1
                # Emit buffered SSE thinking_chunk — fire-and-forget, never block the stream loop
                if _cb and _rid:
                    _buf += txt
                    _total_chars += len(txt)
                    if (now - _last_flush) >= 0.05 or len(_buf) >= 300:
                        _el = now - t0
                        _live_tps = round(_total_chars / 3.5 / _el, 1) if _el > 0.01 else 0.0
                        _fire(_cb, ProgressEvent(
                            research_id=_rid,
                            thinking_chunk=_buf,
                            hardware=self.hardware,
                            tps=_live_tps,
                            component=caller or "Processing",
                            model_name=self.model,
                        ))
                        _buf = ""
                        _last_flush = now
            if chunk.usage:
                in_tok = chunk.usage.prompt_tokens
                out_tok = chunk.usage.completion_tokens

        latency = time.perf_counter() - t0
        content = "".join(content_parts)

        # Flush any remaining SSE buffer
        if _cb and _rid and _buf:
            _el = latency
            _live_tps = round(_total_chars / 3.5 / _el, 1) if _el > 0.01 else 0.0
            _fire(_cb, ProgressEvent(
                research_id=_rid,
                thinking_chunk=_buf,
                hardware=self.hardware,
                tps=_live_tps,
                component=caller or "Processing",
                model_name=self.model,
            ))

        # TTFT/ITL are only meaningful when the response actually streamed
        # incrementally (>1 content chunk). Guided-decoding / non-streaming
        # backends deliver the whole response in one chunk, making "first token"
        # time equal total time — a misleading TTFT. Report n/a in that case.
        streamed = len(chunk_times) > 1
        ttft_ms = ttft_s * 1000 if (ttft_s is not None and streamed) else None
        itl_ms: float | None = None
        if streamed:
            intervals = [
                (chunk_times[j + 1] - chunk_times[j]) * 1000
                for j in range(len(chunk_times) - 1)
            ]
            itl_ms = sum(intervals) / len(intervals)

        if in_tok or out_tok:
            label = f" [{caller}]" if caller else ""
            tracker.track(in_tok, out_tok, research_id=_rid)
            tracker.track_hw(in_tok, out_tok, latency, self.hardware, model=self.model, research_id=_rid,
                             ttft_ms=ttft_ms, itl_ms=itl_ms)
            tps_out = out_tok / latency if latency > 0 and out_tok else 0
            ttft_str = f"{ttft_ms:.1f}" if ttft_ms is not None else "n/a"
            itl_str = f"{itl_ms:.2f}" if itl_ms is not None else "n/a"
            logger.info(
                "LLM usage%s: input_tokens=%d, output_tokens=%d, total_tokens=%d",
                label, in_tok, out_tok, in_tok + out_tok,
            )
            logger.info(
                "LLM timing%s: e2el=%.3fs ttft=%sms itl=%sms input=%d output=%d tps_out=%.1f chunks=%d",
                label, latency, ttft_str, itl_str, in_tok, out_tok, tps_out, n_chunks,
            )
            tracker.track_call(
                caller or "unknown",
                latency, ttft_ms, itl_ms, in_tok, out_tok, tps_out,
                hw=self.hardware,
                research_id=_rid,
            )
            # Emit live per-hardware summary so the UI updates after every call
            if _cb and _rid:
                _fire(_cb, ProgressEvent(
                    research_id=_rid,
                    hw_summary=tracker.get_hw_summary(_rid),
                    hardware=self.hardware,
                    tps=round(tps_out, 1),
                    component=caller or "Processing",
                    model_name=self.model,
                ))
        return content or ""

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def chat_structured(
        self,
        messages: list[dict[str, str]],
        response_model: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        caller: str = "",
    ) -> T:
        """Streaming structured call: collects TTFT/ITL then parses JSON with Pydantic.
        Falls back to instructor on parse failure."""
        _cb = current_progress_callback.get(None)
        _rid = current_research_id.get("")

        # Emit a "working" status event before blocking so the status bar stays live
        if _cb and _rid and caller:
            _fire(_cb, ProgressEvent(
                research_id=_rid,
                component=caller,
                hardware=self.hardware,
                model_name=self.model,
            ))

        t0 = time.perf_counter()
        ttft_s: float | None = None
        content_parts: list[str] = []
        chunk_times: list[float] = []
        in_tok = out_tok = 0

        # Prefer json_schema guided decoding (grammar-constrained output) over
        # plain json_object.  json_schema forces vLLM to only produce tokens that
        # conform to the Pydantic schema, preventing small models from echoing back
        # the schema definition instead of data.  Falls back to json_object via the
        # BadRequestError handler if the server doesn't support json_schema.
        _resp_fmt: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema(),
            },
        }

        # Speculative-decoding models on the vLLM CPU build corrupt STREAMED
        # structured output (deterministic: stream=True garbles, stream=False is
        # clean — the SD accept/reject emission path is buggy). So for the
        # secondary slot we issue a single non-streamed request; for the primary
        # we keep streaming to capture TTFT/ITL telemetry.
        _stream = not self._is_secondary

        async def _collect(resp_fmt: dict[str, Any]) -> None:
            """Run one structured request into content_parts/chunk_times/tokens.
            Streams for the primary slot; non-streams for the SD secondary slot."""
            nonlocal ttft_s, in_tok, out_tok
            if _stream:
                stream = await self._raw_client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature if temperature is not None else self.temperature,
                    max_tokens=max_tokens or self.max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                    response_format=resp_fmt,
                    top_p=self.top_p,
                    presence_penalty=self.presence_penalty,
                    extra_body=self._extra_body or None,
                    timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
                )
                async for chunk in stream:
                    now = time.perf_counter()
                    if chunk.choices and chunk.choices[0].delta.content:
                        txt = chunk.choices[0].delta.content
                        if ttft_s is None:
                            ttft_s = now - t0
                        chunk_times.append(now)
                        content_parts.append(txt)
                    if chunk.usage:
                        in_tok = chunk.usage.prompt_tokens
                        out_tok = chunk.usage.completion_tokens
            else:
                resp = await self._raw_client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature if temperature is not None else self.temperature,
                    max_tokens=max_tokens or self.max_tokens,
                    stream=False,
                    response_format=resp_fmt,
                    top_p=self.top_p,
                    presence_penalty=self.presence_penalty,
                    extra_body=self._extra_body or None,
                    timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
                )
                if resp.choices and resp.choices[0].message.content:
                    content_parts.append(resp.choices[0].message.content)
                if resp.usage:
                    in_tok = resp.usage.prompt_tokens
                    out_tok = resp.usage.completion_tokens

        try:
            await _collect(_resp_fmt)
        except openai.BadRequestError as exc:
            # json_schema not supported — retry with plain json_object before
            # falling all the way back to instructor.
            if _resp_fmt["type"] == "json_schema":
                logger.warning("%s: json_schema rejected (400), retrying with json_object: %s", caller, exc)
                _resp_fmt = {"type": "json_object"}
                try:
                    await _collect(_resp_fmt)
                except openai.BadRequestError as exc2:
                    logger.warning("%s: json_object also rejected (400), falling back to instructor: %s", caller, exc2)
                    content_parts = []
            else:
                logger.warning("%s: response_format rejected (400), falling back to instructor: %s", caller, exc)
                content_parts = []
        except Exception as exc:
            cause: BaseException | None = exc
            while cause is not None:
                if isinstance(cause, _RETRYABLE):
                    raise cause from exc
                cause = cause.__cause__ or cause.__context__
            raise

        latency = time.perf_counter() - t0
        content = "".join(content_parts)

        # Parse assembled JSON with Pydantic; fall back to schema-unwrap then instructor
        resp: T
        try:
            resp = response_model.model_validate_json(content)
        except Exception:
            # Detect schema echo-back: model returns the JSON Schema definition
            # ({"properties": {...}, "title": ..., "type": "object"} or {"$defs": ...})
            # instead of actual data. Small models (Qwen-3B, Llama-8B) do this
            # when they see the schema in the prompt and parrot it back.
            import json as _json
            _parsed = False
            try:
                _raw = _json.loads(content) if content.strip() else {}
                _is_schema = isinstance(_raw, dict) and (
                    "$defs" in _raw
                    or ("properties" in _raw and ("title" in _raw or "type" in _raw or "required" in _raw))
                )
                if _is_schema:
                    props = _raw.get("properties", {})
                    try:
                        resp = response_model.model_validate(props)
                        _parsed = True
                        logger.warning("%s: recovered from schema echo-back (unwrapped properties)", caller)
                    except Exception:
                        # Properties are schema descriptors, not values — return empty default
                        logger.warning("%s: schema echo-back detected, returning default instance", caller)
                        resp = response_model.model_construct()
                        _parsed = True
            except Exception:
                pass

            if not _parsed:
                logger.warning("%s: JSON parse failed, retrying via instructor", caller)
                try:
                    resp, completion = await self._instructor_client.chat.completions.create_with_completion(
                        model=self.model,
                        messages=messages,
                        response_model=response_model,
                        temperature=temperature if temperature is not None else self.temperature,
                        max_tokens=max_tokens or self.max_tokens,
                        max_retries=2,
                        top_p=self.top_p,
                        presence_penalty=self.presence_penalty,
                        extra_body=self._extra_body or None,
                    )
                    if completion.usage:
                        in_tok = completion.usage.prompt_tokens
                        out_tok = completion.usage.completion_tokens
                except Exception as exc2:
                    cause2: BaseException | None = exc2
                    while cause2 is not None:
                        if isinstance(cause2, _RETRYABLE):
                            raise cause2 from exc2
                        cause2 = cause2.__cause__ or cause2.__context__
                    raise

        # TTFT/ITL only meaningful for genuinely streamed responses (>1 chunk).
        # Structured/guided-decoding calls usually return one final chunk, so
        # "first token" == total time — report n/a instead of a misleading TTFT.
        streamed = len(chunk_times) > 1
        ttft_ms = ttft_s * 1000 if (ttft_s is not None and streamed) else None
        itl_ms: float | None = None
        if streamed:
            intervals = [(chunk_times[j + 1] - chunk_times[j]) * 1000 for j in range(len(chunk_times) - 1)]
            itl_ms = sum(intervals) / len(intervals)

        if in_tok or out_tok:
            label = f" [{caller}]" if caller else ""
            logger.info(
                "LLM usage%s (structured): input_tokens=%d, output_tokens=%d, total_tokens=%d, _rid=%r",
                label, in_tok, out_tok, in_tok + out_tok, _rid,
            )
            tracker.track(in_tok, out_tok, research_id=_rid)
            tracker.track_hw(in_tok, out_tok, latency, self.hardware, model=self.model, research_id=_rid,
                             ttft_ms=ttft_ms, itl_ms=itl_ms)
            tps_out = out_tok / latency if latency > 0 else 0
            ttft_str = f"{ttft_ms:.1f}" if ttft_ms is not None else "n/a"
            itl_str  = f"{itl_ms:.2f}"  if itl_ms  is not None else "n/a"
            logger.info(
                "LLM timing%s (structured): e2el=%.3fs ttft=%sms itl=%sms input=%d output=%d tps_out=%.1f",
                label, latency, ttft_str, itl_str, in_tok, out_tok, tps_out,
            )
            tracker.track_call(
                f"{caller} (structured)" if caller else "structured",
                latency, ttft_ms, itl_ms, in_tok, out_tok, tps_out,
                hw=self.hardware,
                research_id=_rid,
            )
            if _cb and _rid:
                _fire(_cb, ProgressEvent(
                    research_id=_rid,
                    hw_summary=tracker.get_hw_summary(_rid),
                    hardware=self.hardware,
                    tps=round(tps_out, 1),
                    component=caller or "Processing",
                    model_name=self.model,
                ))
        else:
            logger.warning("chat_structured: no usage data (caller=%r, rid=%r)", caller, _rid)
        return resp


def get_llm(module_name: str = "") -> LLMClient:
    """Return an LLMClient, using the secondary model when configured for *module_name*."""
    modules = {m.strip() for m in settings.secondary_llm_modules.split(",") if m.strip()}
    use_secondary = module_name in modules
    if use_secondary:
        logger.info("Using secondary LLM for module '%s'", module_name)
    return LLMClient(secondary=use_secondary)
