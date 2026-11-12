# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""LLM-based query-focused summarization.

An alternative to ``extractive_summarizer.extractive_summarize`` that uses the
LLM served by the vLLM server instead of sentence-embedding extraction. Gated by
``settings.use_llm_summarizer`` and invoked from the ``summarize_sources`` tool;
the output is cached on ``SearchResult.compressed_content`` exactly like the
extractive path, so downstream tools reuse it transparently.
"""

from __future__ import annotations

import logging

from llm_client import get_llm

logger = logging.getLogger(__name__)


async def llm_summarize(document: str, query: str, token_budget: int = 4000) -> str:
    """Compress a document into a query-focused summary using the vLLM-served LLM.

    Args:
        document: The full document text to compress.
        query: The research query to focus the summary on.
        token_budget: Target/maximum token budget for the output. Enforced both
            as a soft target in the prompt and as a hard ``max_tokens`` cap.

    Returns:
        The summary text, or an empty string for empty input.
    """
    if not document or not document.strip():
        return ""

    llm = get_llm("summarize_sources")

    system_prompt = (
        "You are a research analyst. Produce a query-focused summary of the source "
        "document below. Preserve the specific facts, figures, names, dates, and "
        "claims that are relevant to the research query; drop boilerplate, "
        "navigation, and unrelated content. Do not add information that is not in "
        "the document, and do not editorialize.\n\n"
        f"Research query: {query}\n\n"
        f"Keep the summary within roughly {token_budget} tokens."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Source document:\n{document}"},
    ]

    return await llm.chat(messages, max_tokens=token_budget, caller="summarize_sources")
