# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Coding Agent — LangGraph ReAct agent with sandboxed execution and Redis memory.

Architecture:
  User prompt → LangGraph ReAct loop → Tool calls (sandbox/Ray) → Response
                      ↕
              Redis checkpointer (conversation memory)
"""

import os
import re
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from tools import ALL_TOOLS

load_dotenv()

# ---------------------------------------------------------------------------
# Redis-backed checkpointer (conversation memory across sessions)
# Falls back to in-memory if Redis is unavailable.
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


def _build_checkpointer():
    """Build a Redis checkpointer, falling back to in-memory."""
    try:
        from langgraph.checkpoint.redis import RedisSaver
        saver = RedisSaver(conn=REDIS_URL)
        saver.setup()
        return saver
    except Exception:
        return InMemorySaver()


checkpointer = _build_checkpointer()

# ---------------------------------------------------------------------------
# LLM Configuration
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert coding assistant that helps users write and execute Python code.

Your capabilities:
1. Write Python code based on user requests
2. Execute code in an isolated Kubernetes sandbox pod using the execute_python_code tool
3. Read from and write to files in the workspace
4. Install Python packages inside the sandbox as needed
5. Debug and fix code errors
6. Explain code and programming concepts
7. Run multiple independent tasks in parallel using execute_python_code_parallel (when Ray is enabled)

Guidelines:
- Always test your code by executing it
- Handle errors gracefully and provide clear explanations
- Write clean, well-commented code
- Explain your approach before writing complex code
- Use the workspace directory for storing files and artifacts
- The sandbox session persists across your calls — installed packages and defined
  variables carry over between execute_python_code calls within the same session
- Use reset_sandbox() when a completely fresh environment is needed
- Remember context from previous conversations (stored via checkpointer)

When users ask you to create or run code:
1. Understand the requirements clearly
2. Write the Python code
3. Execute it using execute_python_code (dont send language indicators) (runs in a secure sandbox pod)
4. Show the results and explain any issues
5. Iterate based on feedback
"""


def create_agent():
    """Create the LangGraph ReAct coding agent."""
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("OPENAI_API_KEY", "not-needed")
    model = os.getenv("MODEL_NAME", "gpt-4o")

    llm = ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=0,
    )

    agent = create_react_agent(
        model=llm,
        tools=ALL_TOOLS,
        checkpointer=checkpointer,
        prompt=SYSTEM_PROMPT,
    )

    return agent


agent = create_agent()


# ---------------------------------------------------------------------------
# Convenience helpers for running the agent
# ---------------------------------------------------------------------------


def invoke(prompt: str, session_id: str = "default") -> str:
    """Run the agent synchronously and return the final assistant message."""
    config = {"configurable": {"thread_id": session_id}}
    result = agent.invoke({"messages": [("user", prompt)]}, config=config)
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.type == "ai" and msg.content:
            return msg.content
    return ""


async def ainvoke(prompt: str, session_id: str = "default") -> str:
    """Run the agent asynchronously and return the final assistant message."""
    config = {"configurable": {"thread_id": session_id}}
    result = await agent.ainvoke({"messages": [("user", prompt)]}, config=config)
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if hasattr(msg, "content") and msg.type == "ai" and msg.content:
            return msg.content
    return ""


async def astream(prompt: str, session_id: str = "default"):
    """Stream agent events asynchronously. Yields event dicts."""
    config = {"configurable": {"thread_id": session_id}}
    async for event in agent.astream_events(
        {"messages": [("user", prompt)]}, config=config, version="v2"
    ):
        yield event
