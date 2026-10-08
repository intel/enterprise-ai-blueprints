#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Launch the Banking Assistant with DevUI.

Starts a web interface for interacting with the banking agent. The agent routes
natural-language banking requests to a GenAI-gateway model and uses a remote MCP
server (the banking sidecar) for all account data and operations.
"""

import logging
import os

from agent_framework.observability import configure_otel_providers
from agent_framework.devui import serve
from dotenv import load_dotenv
from langfuse import get_client

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _is_truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setup_observability() -> None:
    """Configure Langfuse + OpenTelemetry if credentials are available."""
    has_langfuse_keys = bool(os.getenv("LANGFUSE_PUBLIC_KEY")) and bool(
        os.getenv("LANGFUSE_SECRET_KEY")
    )
    if not has_langfuse_keys:
        logger.info(
            "Langfuse not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to enable tracing"
        )
        return

    langfuse = get_client()
    if not langfuse.auth_check():
        logger.warning("Langfuse auth check failed. Verify LANGFUSE keys and base URL.")
        return

    enable_sensitive_data = _is_truthy(
        os.getenv("LANGFUSE_ENABLE_SENSITIVE_DATA"), default=True
    )
    configure_otel_providers(enable_sensitive_data=enable_sensitive_data)
    logger.info(
        "Langfuse observability enabled (sensitive_data=%s)", enable_sensitive_data
    )


def main():
    """Launch DevUI with the banking agent."""
    setup_observability()
    from agent import agent, MCP_SERVER_URL

    logger.info("=" * 60)
    logger.info("Banking Assistant with DevUI")
    logger.info("=" * 60)
    logger.info("Starting web interface at http://localhost:8090")
    logger.info("")
    logger.info("Features:")
    logger.info("  • Natural-language banking queries")
    logger.info("  • Banking tools via MCP server: %s", MCP_SERVER_URL)
    logger.info("  • Persistent memory via Redis (text-only)")
    logger.info("  • OpenAI-compatible API (GenAI Gateway / LiteLLM)")
    logger.info("")
    logger.info("Prerequisites:")
    logger.info("  1. Banking MCP server reachable at MCP_SERVER_URL")
    logger.info("  2. Redis: docker run -d -p 6379:6379 redis/redis-stack-server")
    logger.info("  3. Configure .env with your LLM endpoint")
    logger.info("")
    logger.info("=" * 60)

    auto_open = os.getenv("AUTO_OPEN_BROWSER", "true").lower() != "false"
    host = os.getenv("DEVUI_HOST", "0.0.0.0")  # nosec B104 - containerized service must bind all interfaces to be reachable
    serve(entities=[agent], port=8090, host=host, auto_open=auto_open)


if __name__ == "__main__":
    main()
