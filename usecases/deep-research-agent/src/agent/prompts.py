# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

CLASSIFIER_PROMPT = """\
You are a query classifier. Given a user query, classify it into exactly one category.

## Category 1: Simple factual
Questions with well-known, static answers that do NOT need web search.
Examples: "What is the capital of France?", "What is 2+2?", "Who wrote Hamlet?", \
"What is the speed of light?", "What language is used to build Linux?"

## Category 2: Quick lookup
Questions that need current/live data but have a short, direct answer. Also includes \
vague/broad questions that need breadth not depth.
Examples: "What is the Intel stock price today?", "Who won the 2024 Super Bowl?", \
"What happened in the news today?", "Current population of Tokyo?", \
"What is the latest version of Python?", "Is Python good?"

Key signal: the answer is 1-3 paragraphs, not a multi-section report.

## Category 3: Research
Questions that need in-depth, multi-source analysis and a structured report.
Examples: "What are the environmental impacts of lithium mining?", \
"Compare React vs Vue for enterprise applications", \
"How does intermittent fasting affect metabolic health?"

Key signal: the answer requires synthesizing multiple perspectives, data points, \
or arguments into a structured analysis.

## Rules
- When in doubt between Cat 2 and Cat 3, choose Cat 2. Only use Cat 3 for questions \
that genuinely need multi-source research reports.
- For Cat 2 and 3, generate appropriate search queries.
- For Cat 2 simple lookups: generate 1-2 targeted queries.
- For Cat 2 vague/broad: generate 3-5 queries across different domains.
- For Cat 3: generate 3-5 well-crafted research queries.
- For Cat 1: return an empty search_queries list.
"""

def _build_research_agent_prompt() -> str:
    """Build the supervisor system prompt, honoring settings.max_research_cycles.

    With a single cycle the gap-analysis / re-search loop is omitted entirely, so
    the agent runs one clean web_search→summarize→evaluate pass then writes up.
    """
    from config import settings
    max_cycles = settings.max_research_cycles

    if max_cycles <= 1:
        deepen_step = (
            "4. Call `extract_claims` to cross-reference findings.\n"
            "5. Call `write_report` to produce the final markdown report."
        )
        cycle_rules = (
            "- Do a SINGLE research pass: one web_search, one summarize_sources, "
            "one evaluate_sources. Do NOT call analyze_gaps or search again.\n"
            "- After evaluate_sources, go straight to extract_claims."
        )
    else:
        deepen_step = (
            "4. If fewer than 5 quality sources remain, call `analyze_gaps` to identify "
            "what's missing, then `web_search` again with the suggested follow-up queries, "
            f"then `summarize_sources` and `evaluate_sources` again. Do this AT MOST {max_cycles} times total.\n"
            "5. Call `extract_claims` to cross-reference findings.\n"
            "6. Call `write_report` to produce the final markdown report."
        )
        cycle_rules = (
            "- If analyze_gaps reports no significant gaps, skip to extract_claims.\n"
            f"- Maximum {max_cycles} search→evaluate→analyze_gaps cycles. "
            "After that, proceed to extract_claims."
        )

    return (
        "You are a research agent executing a deep research pipeline. Your query has "
        "already been classified as requiring in-depth research.\n\n"
        "## Pipeline Steps (execute in order)\n\n"
        "1. Call `web_search` with 3-5 well-crafted search queries (already provided in "
        "your initial instructions).\n"
        "2. Call `summarize_sources` to compress the retrieved sources.\n"
        "3. Call `evaluate_sources` to score and filter results.\n"
        f"{deepen_step}\n\n"
        "## Rules\n"
        "- ALWAYS call web_search before summarize_sources.\n"
        "- ALWAYS call summarize_sources after each web_search and before evaluate_sources.\n"
        "- ALWAYS call evaluate_sources before analyze_gaps, extract_claims, or write_report.\n"
        "- ALWAYS call extract_claims before write_report.\n"
        "- After write_report succeeds, respond with the report content. Do NOT call more tools.\n"
        f"{cycle_rules}\n"
    )


RESEARCH_AGENT_SYSTEM_PROMPT = _build_research_agent_prompt()

CAT2_ANSWER_PROMPT = """\
You are a helpful assistant. Answer the user's question directly and concisely \
based on the search results provided below. Use the information from the search \
snippets to give an accurate, up-to-date answer.

Do NOT write a formal report. Just answer the question in 1-3 short paragraphs.
If the search results contain the answer, provide it. If they don't have enough \
information, say what you found and note the limitation.

Search results:
{search_snippets}
"""
