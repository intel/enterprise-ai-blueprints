#!/usr/bin/env python3
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Launch the Coding Agent — LangGraph ReAct agent with FastAPI server.

Provides:
- POST /v1/chat/completions  — OpenAI-compatible chat endpoint
- POST /v1/responses         — OpenAI Responses API for DevUI/flow integration
- GET  /v1/entities          — Agent introspection for auto-discovery
- GET  /health               — Liveness check
- SSE streaming support for all inference endpoints
"""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Observability (Langfuse + OpenTelemetry)
# ---------------------------------------------------------------------------

def _is_truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_langfuse_handler = None


def _run_config(session_id: str) -> dict:
    """LangGraph run config: per-session thread + Langfuse callback when enabled."""
    config = {"configurable": {"thread_id": session_id}}
    if _langfuse_handler is not None:
        config["callbacks"] = [_langfuse_handler]
    return config


def setup_observability() -> None:
    """Configure Langfuse + OpenTelemetry if credentials are available."""
    has_langfuse_keys = bool(os.getenv("LANGFUSE_PUBLIC_KEY")) and bool(
        os.getenv("LANGFUSE_SECRET_KEY")
    )
    if not has_langfuse_keys:
        logger.info("Langfuse not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to enable tracing")
        return
    try:
        from langfuse import get_client
        langfuse = get_client()
        if not langfuse.auth_check():
            logger.warning("Langfuse auth check failed.")
            return
        # Langfuse v3+ LangChain integration (langfuse.callback was the v2 API).
        from langfuse.langchain import CallbackHandler
        global _langfuse_handler
        _langfuse_handler = CallbackHandler()
        os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "true")
        logger.info("Langfuse observability enabled")
    except Exception as e:
        logger.warning(f"Langfuse setup failed: {e}")


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_observability()
    logger.info("=" * 60)
    logger.info("Coding Agent — LangGraph")
    logger.info("=" * 60)
    logger.info("Features:")
    logger.info("  - Sandboxed code execution (K8s Agent Sandbox)")
    logger.info("  - Ray parallel execution (when enabled)")
    logger.info("  - Redis-backed conversation memory")
    logger.info("  - OpenAI-compatible API")
    logger.info("  - Langfuse observability (when configured)")
    logger.info("=" * 60)
    yield


app = FastAPI(title="Coding Agent", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = None
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    session_id: Optional[str] = None


class ResponsesRequest(BaseModel):
    model: Optional[str] = None
    input: str = ""
    stream: bool = False
    metadata: Optional[Dict[str, Any]] = None
    instructions: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

AUTH_TOKEN = os.getenv("DEVUI_AUTH_TOKEN", "")


def _check_auth(request: Request):
    if not AUTH_TOKEN:
        return
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if token == AUTH_TOKEN:
            return
    raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "healthy", "entities_count": 1, "framework": "langgraph"}


@app.get("/v1/entities")
async def list_entities(request: Request):
    """Agent introspection — used by agentic-ai-tools for auto-discovery."""
    _check_auth(request)
    from tools import ALL_TOOLS
    tools_info = [{"name": t.name, "description": t.description or ""} for t in ALL_TOOLS]
    model = os.getenv("MODEL_NAME", "gpt-4o")
    entities = [{
        "id": f"agent_langgraph_coding-agent_{uuid.uuid4().hex[:16]}",
        "type": "agent",
        "name": "coding-agent",
        "description": "LangGraph coding assistant with sandboxed execution",
        "framework": "langgraph",
        "tools": tools_info,
        "model": model,
        "chat_client_type": "ChatOpenAI",
    }]
    return {"entities": entities}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint."""
    _check_auth(request)
    from agent import agent, checkpointer

    session_id = body.session_id or "default"
    config = _run_config(session_id)

    messages = [("system" if m.role == "system" else "human" if m.role == "user" else "ai", m.content)
                for m in body.messages]

    if body.stream:
        return StreamingResponse(
            _stream_chat(messages, config),
            media_type="text/event-stream",
        )

    result = await agent.ainvoke({"messages": messages}, config=config)
    ai_messages = result.get("messages", [])
    content = ""
    for msg in reversed(ai_messages):
        if hasattr(msg, "content") and msg.type == "ai" and msg.content:
            content = msg.content
            break

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model or os.getenv("MODEL_NAME", "gpt-4o"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _stream_chat(messages, config):
    """Stream chat completions as SSE."""
    from agent import agent
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"

    async for event in agent.astream_events({"messages": messages}, config=config, version="v2"):
        if event["event"] == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk and hasattr(chunk, "content") and chunk.content:
                data = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "choices": [{"index": 0, "delta": {"content": chunk.content}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(data)}\n\n"

    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/responses")
async def responses_api(request: Request, body: ResponsesRequest):
    """OpenAI Responses API — used by DevUI / agentic-ai-tools flow view."""
    _check_auth(request)
    from agent import agent, checkpointer

    metadata = body.metadata or {}
    session_id = metadata.get("thread_id", metadata.get("entity_id", "default"))
    config = _run_config(session_id)

    input_text = body.input
    if not input_text:
        raise HTTPException(400, detail="input is required")

    if body.stream:
        return StreamingResponse(
            _stream_responses(input_text, config, body.model),
            media_type="text/event-stream",
        )

    result = await agent.ainvoke({"messages": [("user", input_text)]}, config=config)
    ai_messages = result.get("messages", [])

    output_items = []
    for msg in ai_messages:
        if hasattr(msg, "type"):
            if msg.type == "ai" and msg.content:
                output_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": msg.content}],
                })
            elif msg.type == "tool":
                output_items.append({
                    "type": "function_call_output",
                    "call_id": getattr(msg, "tool_call_id", ""),
                    "output": msg.content if isinstance(msg.content, str) else str(msg.content),
                })

    return {
        "id": f"resp-{uuid.uuid4().hex[:8]}",
        "object": "response",
        "output": output_items,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


async def _stream_responses(input_text: str, config: dict, model: Optional[str]):
    """Stream Responses API as SSE."""
    from agent import agent

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': f'resp-{uuid.uuid4().hex[:8]}', 'status': 'in_progress'}})}\n\n"

    full_content = ""
    async for event in agent.astream_events({"messages": [("user", input_text)]}, config=config, version="v2"):
        if event["event"] == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk and hasattr(chunk, "content") and chunk.content:
                full_content += chunk.content
                yield f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': chunk.content})}\n\n"
        elif event["event"] == "on_tool_start":
            tool_name = event.get("name", "")
            yield f"data: {json.dumps({'type': 'response.function_call_arguments.start', 'name': tool_name})}\n\n"
        elif event["event"] == "on_tool_end":
            tool_output = event.get("data", {}).get("output", "")
            if hasattr(tool_output, "content"):
                tool_output = tool_output.content
            yield f"data: {json.dumps({'type': 'response.function_call_output', 'output': str(tool_output)[:2000]})}\n\n"

    yield f"data: {json.dumps({'type': 'response.output_text.done', 'text': full_content})}\n\n"
    yield f"data: {json.dumps({'type': 'response.completed'})}\n\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.getenv("DEVUI_HOST", "0.0.0.0")  # nosec B104 - containerized service must bind all interfaces to be reachable via its Service
    port = int(os.getenv("DEVUI_PORT", "8090"))
    auto_open = os.getenv("AUTO_OPEN_BROWSER", "true").lower() != "false"

    if auto_open:
        import webbrowser
        webbrowser.open(f"http://localhost:{port}/health")

    uvicorn.run(app, host=host, port=port, log_level="info")
