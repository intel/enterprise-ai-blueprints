# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import os
import httpx
from langchain_openai import ChatOpenAI

from config import settings

logger = logging.getLogger(__name__)


def get_agent_hardware() -> str:
    """Return the hardware label for the LLM used by the current agent configuration."""
    modules = {m.strip() for m in settings.secondary_llm_modules.split(",") if m.strip()}
    if "agent" in modules and settings.secondary_llm_base_url and settings.secondary_llm_model:
        return settings.secondary_llm_hardware
    return settings.llm_hardware


def get_agent_model() -> str:
    """Return the model ID for the LLM used by the current agent configuration."""
    modules = {m.strip() for m in settings.secondary_llm_modules.split(",") if m.strip()}
    if "agent" in modules and settings.secondary_llm_base_url and settings.secondary_llm_model:
        return settings.secondary_llm_model
    return settings.llm_model


def create_agent_llm() -> ChatOpenAI:
    """Create a ChatOpenAI instance pointed at the local vLLM server.

    - trust_env=False on the httpx client prevents http_proxy routing localhost
      requests through the corporate proxy.
    - ALL_PROXY/all_proxy are temporarily unset before constructing ChatOpenAI
      because langchain_openai reads proxy env vars at __init__ time and rejects
      the socks:// scheme with a ValidationError.

    If "agent" is listed in SECONDARY_LLM_MODULES and the secondary LLM is
    configured, uses the secondary model instead.
    """
    modules = {m.strip() for m in settings.secondary_llm_modules.split(",") if m.strip()}
    if "agent" in modules and settings.secondary_llm_base_url and settings.secondary_llm_model:
        logger.info("Using secondary LLM for agent")
        base_url = settings.secondary_llm_base_url
        model = settings.secondary_llm_model
        api_key = settings.secondary_llm_api_key
    else:
        base_url = settings.llm_base_url
        model = settings.llm_model
        api_key = settings.llm_api_key

    extra_body: dict = {}
    if settings.llm_top_k > 0:
        extra_body["top_k"] = settings.llm_top_k
    if settings.llm_min_p > 0.0:
        extra_body["min_p"] = settings.llm_min_p
    if settings.llm_repetition_penalty != 1.0:
        extra_body["repetition_penalty"] = settings.llm_repetition_penalty

    # Temporarily suppress SOCKS proxy env vars that ChatOpenAI reads at init time
    _saved = {}
    for key in ("ALL_PROXY", "all_proxy"):
        if key in os.environ:
            _saved[key] = os.environ.pop(key)
    try:
        llm = ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=settings.llm_temperature,
            max_tokens=settings.agent_max_tokens,
            top_p=settings.llm_top_p,
            presence_penalty=settings.llm_presence_penalty,
            http_async_client=httpx.AsyncClient(trust_env=False),
            model_kwargs={"stream_options": {"include_usage": True}},
            extra_body=extra_body or None,
            max_retries=10,
            request_timeout=180,
        )
    finally:
        os.environ.update(_saved)
    return llm
