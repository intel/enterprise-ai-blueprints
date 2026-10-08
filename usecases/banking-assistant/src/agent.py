# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Banking Assistant agent.

A Microsoft Agent Framework agent that routes natural-language banking requests
to a GenAI-gateway model and uses a remote MCP server (the banking sidecar,
reached at MCP_SERVER_URL) for all factual account data and operations.

The agent connects to the MCP server as a client and exposes its tools to the
LLM. It keeps optional per-session memory in Redis (text-only), reusing the
sanitized context-provider pattern from the coding-agent usecase.
"""

import logging
import os
import re
from typing import Any

from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework.redis import RedisContextProvider
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")
ENABLE_MEMORY = os.getenv("ENABLE_MEMORY", "true").lower() != "false"


# ============================================================================
# Redis Context Provider (sanitizes special chars that break RediSearch queries)
# Mirrors coding-agent/src/agent.py so search text with '?', '*', etc. does not
# trigger RediSearch syntax errors, and only user messages are re-injected.
# ============================================================================

class SanitizedRedisContextProvider(RedisContextProvider):
    """RedisContextProvider that strips RediSearch special characters from search text."""

    async def before_run(self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]) -> None:
        from agent_framework import Message  # noqa: PLC0415
        self._validate_filters()
        input_text = "\n".join(
            msg.text for msg in context.input_messages if msg and msg.text and msg.text.strip()
        )
        if not input_text.strip():
            return
        memories = await self._redis_search(text=input_text, session_id=context.session_id)
        line_separated_memories = "\n".join(
            str(memory.get("content", "")) for memory in memories if memory.get("content")
        )
        if line_separated_memories:
            context.extend_messages(
                self.source_id,
                [Message(role="user", contents=[f"{self.context_prompt}\n{line_separated_memories}"])],
            )

    async def _redis_search(self, text: str, **kwargs: Any) -> list[dict[str, Any]]:
        sanitized = re.sub(r'[?!@{}\[\]()|~*^=+<>&,]', ' ', text)
        sanitized = ' '.join(sanitized.split())
        if not sanitized.strip():
            return []
        results = await super()._redis_search(sanitized, **kwargs)
        return [r for r in results if r.get("role") == "user"]


# ============================================================================
# MCP client factory
#
# The remote-MCP client class is provided by the Agent Framework. Its exact
# exported name can vary across framework versions, so we resolve it defensively
# and log loudly on failure — a wrong import must not silently yield a
# no-tools agent.
#
# To confirm the real name/signature in the build image, run:
#   python -c "import agent_framework; print([n for n in dir(agent_framework) if 'MCP' in n.upper()])"
#   python -c "import agent_framework, inspect; print(inspect.signature(agent_framework.MCPStreamableHTTPTool.__init__))"
# ============================================================================

def _resolve_mcp_tool_class():
    """Return the Agent Framework Streamable-HTTP MCP client class, or None."""
    import agent_framework  # noqa: PLC0415

    candidates = [
        "MCPStreamableHTTPTool",
        "MCPStreamableHttpTool",
    ]
    for name in candidates:
        cls = getattr(agent_framework, name, None)
        if cls is not None:
            logger.info("Resolved MCP client class: agent_framework.%s", name)
            return cls

    # Fall back to scanning the namespace for any MCP + HTTP tool export.
    for name in dir(agent_framework):
        upper = name.upper()
        if "MCP" in upper and "HTTP" in upper:
            logger.info("Resolved MCP client class by scan: agent_framework.%s", name)
            return getattr(agent_framework, name)

    logger.error(
        "Could not resolve an MCP Streamable-HTTP tool class in agent_framework. "
        "Available MCP-related exports: %s",
        [n for n in dir(agent_framework) if "MCP" in n.upper()],
    )
    return None


def build_banking_tools() -> list:
    """Build the remote MCP toolset the agent exposes to the LLM.

    Returns a single-element list holding the MCP tool object, or an empty list
    if the client class cannot be resolved (the agent then starts but reports it
    cannot access account data, rather than crashing).
    """
    mcp_tool_cls = _resolve_mcp_tool_class()
    if mcp_tool_cls is None:
        return []
    try:
        # approval_mode="never_require": the framework must not block waiting for
        # a human approval in the headless DevUI context. Confirmation for
        # money-moving operations is handled conversationally by the agent's
        # instructions instead.
        banking_mcp = mcp_tool_cls(
            name="banking",
            url=MCP_SERVER_URL,
            approval_mode="never_require",
        )
        logger.info("Banking MCP client configured for %s", MCP_SERVER_URL)
        return [banking_mcp]
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logger.exception("Failed to construct MCP client for %s: %s", MCP_SERVER_URL, exc)
        return []


# ============================================================================
# Agent Creation
# ============================================================================

INSTRUCTIONS = """You are a helpful, professional banking assistant for a demo bank.

You have access to banking tools via an MCP server. Use them for ANY factual
account information — never invent balances, transactions, loans, or account
numbers. If a tool returns an {"error": ...} payload, explain the problem to the
user plainly.

How to work:
1. Identify the customer first. If the user gives a user id (e.g. "U1001"), use
   it. If they give only a name or email, call find_user to resolve their user id
   and confirm you found the right person before continuing.
2. Use get_accounts / get_balances / get_transactions / get_loans /
   get_loan_summary to answer questions about the customer's finances.
3. For money-moving operations (transfer_funds, pay_loan): restate the exact
   details (amounts, source, destination) and ask the user to confirm BEFORE you
   call the tool. Only proceed after explicit confirmation.
4. Be concise and format currency clearly. Never expose full SSNs — the tools
   already mask them and you must keep them masked.

This is a demonstration using synthetic data only; no real money moves and no
real customers exist.
"""


def create_banking_agent():
    """Create the banking agent with MCP tools and optional Redis memory."""
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("OPENAI_API_KEY", "not-needed")
    model = os.getenv("MODEL_NAME", "gpt-4o")

    context_providers = []
    if ENABLE_MEMORY:
        try:
            context_providers.append(
                SanitizedRedisContextProvider(
                    source_id="banking_agent_context",
                    redis_url=REDIS_URL,
                    index_name="banking_agent_memory",
                    application_id="banking_agent_app",
                    agent_id="banking_agent",
                    user_id="default_user",
                )
            )
        except Exception as exc:  # pragma: no cover - memory is optional
            logger.warning("Redis memory disabled (could not initialize): %s", exc)

    tools = build_banking_tools()

    agent = OpenAIChatCompletionClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
    ).as_agent(
        name="banking-assistant",
        instructions=INSTRUCTIONS,
        tools=tools,
        context_providers=context_providers,
    )
    return agent


# Export agent for DevUI discovery
agent = create_banking_agent()
