# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""FastAPI server exposing the agent as an API for benchmarking."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from config import API_PORT
from backend.database import init_db
from backend.seed_data import seed_all
from tools.registry import import_all_tools, list_tools, execute_tool
from agent.agent_loop import run_agent, AgentResponse
from agent import memory as memory_store

app = FastAPI(title="TechGadgets Customer Support Agent API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup():
    init_db()
    seed_all()
    import_all_tools()
    # Initialize long-term memory (RediSearch + gateway embeddings). Fail-open:
    # if Redis/embeddings are unavailable the agent still runs without memory.
    memory_store.init_memory()


def _augment_with_memory(request: "ChatRequest") -> str:
    """Retrieve relevant past interactions for this customer, merged with any
    memory_context supplied by the caller."""
    recalled = ""
    if memory_store.is_available():
        recalled = memory_store.search_memory(request.customer_id, request.message)
    parts = [p for p in (request.memory_context, recalled) if p]
    return "\n".join(parts)


def _remember_interaction(customer_id: str, user_msg: str, agent_reply: str) -> None:
    """Persist a compact summary of the exchange for future recall. Fail-open."""
    if not memory_store.is_available():
        return
    summary = f"User asked: {user_msg.strip()[:400]} | Agent answered: {agent_reply.strip()[:400]}"
    memory_store.add_memory(customer_id, summary)


# ─── Request/Response Models ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    customer_id: str = ""
    conversation_id: str = ""
    conversation_history: List[Dict[str, str]] = Field(default_factory=list)
    memory_context: str = ""
    max_turns: int = 8


class ToolCallResponse(BaseModel):
    name: str
    arguments: Dict[str, Any]
    result: Dict[str, Any]
    duration_ms: float


class ChatResponse(BaseModel):
    response: str
    tool_calls: List[ToolCallResponse]
    metadata: Dict[str, Any]


class ToolRequest(BaseModel):
    arguments: Dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    model: str
    tools_count: int
    memory_enabled: bool = False
    executor_backend: str = "local"


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/v1/health", response_model=HealthResponse)
def health():
    from config import LLM_MODEL, TOOL_EXECUTOR
    tools = list_tools()
    return HealthResponse(
        status="healthy",
        model=LLM_MODEL,
        tools_count=len(tools),
        memory_enabled=memory_store.is_available(),
        executor_backend=TOOL_EXECUTOR,
    )


@app.get("/api/v1/tools")
def get_tools():
    return list_tools()


@app.post("/api/v1/tools/{tool_name}")
def call_tool(tool_name: str, request: ToolRequest):
    result = execute_tool(tool_name, request.arguments)
    if "error" in result and not result.get("success"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/v1/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    """Send a message to the agent and get a response with tool call details."""
    memory_context = _augment_with_memory(request)

    response: AgentResponse = run_agent(
        query=request.message,
        customer_id=request.customer_id,
        memory_context=memory_context,
        conversation_history=request.conversation_history,
    )

    _remember_interaction(request.customer_id, request.message, response.content)

    tool_calls = [
        ToolCallResponse(
            name=tc.name,
            arguments=tc.arguments,
            result=tc.result,
            duration_ms=tc.duration_ms,
        )
        for tc in response.tool_calls
    ]

    return ChatResponse(
        response=response.content,
        tool_calls=tool_calls,
        metadata={
            "turns": response.turns,
            "total_tokens": response.total_tokens,
            "latency_ms": round(response.total_latency_ms, 1),
            "tools_called": len(response.tool_calls),
            "parallel_batches": response.parallel_batches,
            "max_batch_size": response.max_batch_size,
            "executor_backend": response.executor_backend,
            "memory_used": bool(memory_context),
        }
    )


@app.post("/api/v1/conversation")
def conversation(request: ChatRequest):
    """Multi-turn conversation endpoint. Pass conversation_history for context."""
    memory_context = _augment_with_memory(request)

    response: AgentResponse = run_agent(
        query=request.message,
        customer_id=request.customer_id,
        memory_context=memory_context,
        conversation_history=request.conversation_history,
    )

    _remember_interaction(request.customer_id, request.message, response.content)

    # Build updated conversation history
    updated_history = list(request.conversation_history)
    updated_history.append({"role": "user", "content": request.message})
    updated_history.append({"role": "assistant", "content": response.content})

    return {
        "response": response.content,
        "conversation_history": updated_history,
        "tool_calls": [
            {"name": tc.name, "arguments": tc.arguments, "result": tc.result, "duration_ms": tc.duration_ms}
            for tc in response.tool_calls
        ],
        "metadata": {
            "turns": response.turns,
            "total_tokens": response.total_tokens,
            "latency_ms": round(response.total_latency_ms, 1),
        }
    }


# ─── OpenAI-Compatible Endpoint (for benchmarking without /v1/entities) ─────

class ChatCompletionMessage(BaseModel):
    role: str
    content: str = ""

class ChatCompletionRequest(BaseModel):
    model: str = ""
    messages: List[ChatCompletionMessage] = Field(default_factory=list)
    temperature: float = 0.3
    max_tokens: int = 1500
    stream: bool = False
    customer_id: str = ""


@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint.

    This allows the Unified Benchmark to call this agent using the standard
    'chat_completions' protocol without needing /v1/entities detection.
    Just point to http://<host>:8502/v1/chat/completions with protocol=chat_completions.
    Supports both stream=true (SSE) and stream=false (JSON) modes.
    """
    from config import LLM_MODEL
    import time as _time
    import uuid
    import json as _json

    # Extract the user query (last user message)
    user_messages = [m for m in request.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message found in messages")
    query = user_messages[-1].content

    # Extract system prompt for customer_id if embedded
    customer_id = request.customer_id
    if not customer_id:
        for m in request.messages:
            if m.role == "system" and "CUST-" in m.content:
                import re
                match = re.search(r'(CUST-\d+)', m.content)
                if match:
                    customer_id = match.group(1)
                    break
        if not customer_id:
            customer_id = "CUST-0001"

    # Build conversation history from prior messages (exclude system)
    conversation_history = []
    for m in request.messages[:-1]:
        if m.role in ("user", "assistant"):
            conversation_history.append({"role": m.role, "content": m.content})

    # Run the agent
    response: AgentResponse = run_agent(
        query=query,
        customer_id=customer_id,
        conversation_history=conversation_history,
    )

    # Build agent_trace for benchmark tool-call tracking
    agent_trace = {
        "framework": "custom_agent_loop",
        "llm_calls": response.turns,
        "tool_calls_made": [
            {
                "name": tc.name,
                "args": tc.arguments,
                "output": tc.result,
                "duration_ms": tc.duration_ms,
            }
            for tc in response.tool_calls
        ],
        "total_latency_ms": response.total_latency_ms,
    }

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(_time.time())

    # If streaming requested, return SSE format
    if request.stream:
        from fastapi.responses import StreamingResponse

        def generate_sse():
            # Send content as a single chunk (agent already completed)
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": LLM_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": response.content},
                        "finish_reason": None,
                    }
                ],
                "agent_trace": agent_trace,
            }
            yield f"data: {_json.dumps(chunk)}\n\n"

            # Send finish chunk
            done_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": LLM_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": response.total_tokens // 2,
                    "completion_tokens": response.total_tokens // 2,
                    "total_tokens": response.total_tokens,
                },
            }
            yield f"data: {_json.dumps(done_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate_sse(), media_type="text/event-stream")

    # Non-streaming: return standard JSON
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": LLM_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response.content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": response.total_tokens // 2,
            "completion_tokens": response.total_tokens // 2,
            "total_tokens": response.total_tokens,
        },
        "agent_trace": agent_trace,
    }


# ─── Discovery Endpoints (for Unified Benchmark auto-detect) ────────────────

@app.get("/v1/entities")
def list_entities():
    """Agent discovery endpoint — returns entities list for benchmark detection."""
    from config import LLM_MODEL
    tools = list_tools()
    return {
        "entities": [
            {
                "id": "techgadgets-support-agent",
                "name": "TechGadgets Customer Support Agent",
                "type": "agent",
                "model": LLM_MODEL,
                "object": "agent",
                "tools_count": len(tools),
                "description": "AI customer support agent with 40 tools for order management, returns, product search, warranties, and more.",
            }
        ]
    }


@app.get("/v1/models")
def list_models_v1():
    """OpenAI-compatible models endpoint for benchmark detection."""
    from config import LLM_MODEL
    return {
        "object": "list",
        "data": [
            {
                "id": "techgadgets-support-agent",
                "name": "TechGadgets Customer Support Agent",
                "type": "agent",
                "model": LLM_MODEL,
                "object": "model",
                "owned_by": "custom",
            }
        ]
    }


@app.get("/models")
def list_models():
    """Alternate models endpoint for benchmark detection."""
    return list_models_v1()


@app.get("/meta")
def get_meta():
    """Framework metadata for benchmark detection."""
    from config import LLM_MODEL
    tools = list_tools()
    return {
        "framework": "Custom Agent Loop",
        "version": "1.0.0",
        "model": LLM_MODEL,
        "tools_count": len(tools),
        "max_turns": 50,
        "capabilities": ["tool_calling", "multi_turn", "streaming"],
        "endpoints": {
            "chat": "/api/v1/chat",
            "tools": "/api/v1/tools",
            "health": "/api/v1/health",
        }
    }


# ─── UI Serving ─────────────────────────────────────────────────────────────

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


@app.get("/")
def serve_ui():
    return FileResponse(os.path.join(UI_DIR, "index.html"))


@app.get("/ui/{path:path}")
def serve_ui_static(path: str):
    file_path = os.path.join(UI_DIR, path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
