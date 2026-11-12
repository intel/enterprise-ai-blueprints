# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from agent.graph import build_graph
from cache import clear_search_cache, get_cache_stats, load_cache_from_db
from config import settings
from db import SearchCacheRepository, SessionRepository, get_session, init_db
from middleware import ApiKeyMiddleware
from models import (
    Citation,
    EvaluatedSource,
    FactualClaim,
    ProgressEvent,
    ResearchSession,
    ResearchStatus,
    SearchResult,
)
from token_tracker import (
    current_progress_callback,
    current_research_id,
    current_summarize_override,
    tracker,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await load_cache_from_db()
    yield


app = FastAPI(title="Deep Research", lifespan=lifespan)

app.add_middleware(ApiKeyMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory state for active sessions and SSE queues (ephemeral)
active_sessions: dict[str, ResearchSession] = {}
queues: dict[str, asyncio.Queue[ProgressEvent | None]] = {}


class ResearchRequest(BaseModel):
    query: str
    # Split-screen comparison override: True = force summarization ON,
    # False = force OFF, None = use server default (settings).
    summarize: bool | None = None


class ResearchResponse(BaseModel):
    research_id: str
    status: ResearchStatus


@app.get("/health")
async def health_check() -> dict:
    checks: dict[str, str] = {}

    # Check vLLM connectivity
    try:
        async with httpx.AsyncClient() as client:
            # LiteLLM (Intel Inference Toolkit) requires a key on /models.
            resp = await client.get(
                f"{settings.llm_base_url}/models",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else None,
                timeout=5.0,
            )
            resp.raise_for_status()
            checks["vllm"] = "ok"
    except Exception as e:
        checks["vllm"] = f"error: {e}"

    # Check Tavily API reachability
    if settings.tavily_api_key:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": settings.tavily_api_key, "query": "test", "max_results": 1},
                    timeout=10.0,
                )
                checks["tavily"] = "ok" if resp.status_code == 200 else f"http {resp.status_code}"
        except Exception as e:
            checks["tavily"] = f"error: {e}"
    else:
        checks["tavily"] = "not configured"

    overall = "healthy" if all(v == "ok" for v in checks.values() if v != "not configured") else "degraded"
    return {"status": overall, "checks": checks}


@app.get("/api/system/status")
async def system_status() -> dict:
    """Return health status of the primary and secondary model backends.

    Both slots route through the Intel Inference Toolkit LiteLLM gateway. The gateway's
    authenticated `/health?model=<name>` endpoint actually pings the upstream
    backend (Bedrock / Xeon vLLM) and reports it under healthy/unhealthy_endpoints,
    so a green light means "this model is reachable *through LiteLLM*",
    not merely that the gateway process is up. For a plain (non-gateway) vLLM the
    same call falls back to a simple /health 200 check.
    """
    async def check(base_url: str, api_key: str, model: str, name: str) -> dict:
        # Ask the gateway about this specific model so the dot reflects the real
        # upstream backend, not the gateway itself.
        url = base_url.removesuffix("/v1") + "/health"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        params = {"model": model} if model else None
        try:
            # trust_env=False bypasses corporate proxy for the gateway IP (Rule #8).
            # Bedrock's upstream ping can be slow, so allow a generous timeout.
            async with httpx.AsyncClient(trust_env=False, timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                return {"name": name, "status": "down", "detail": f"HTTP {resp.status_code}"}
            # LiteLLM gateway: green only if this model is in healthy_endpoints.
            try:
                data = resp.json()
            except Exception:
                data = None
            if isinstance(data, dict) and ("healthy_endpoints" in data or "unhealthy_endpoints" in data):
                if data.get("healthy_endpoints"):
                    return {"name": name, "status": "up"}
                unhealthy = data.get("unhealthy_endpoints") or [{}]
                detail = str(unhealthy[0].get("error", "unreachable"))[:80]
                return {"name": name, "status": "down", "detail": detail}
            # Plain vLLM (or any non-gateway) /health returned 200 → up.
            return {"name": name, "status": "up"}
        except Exception as e:
            return {"name": name, "status": "down", "detail": str(e)[:80]}

    tasks = [check(settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_hardware)]
    if settings.secondary_llm_base_url:
        tasks.append(check(
            settings.secondary_llm_base_url,
            settings.secondary_llm_api_key,
            settings.secondary_llm_model,
            settings.secondary_llm_hardware,
        ))
    results = await asyncio.gather(*tasks)
    return {"services": list(results)}


@app.post("/api/research", response_model=ResearchResponse)
async def start_research(
    req: ResearchRequest,
    x_client_id: str = Header(default=""),
) -> ResearchResponse:
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    session = ResearchSession(query=req.query.strip(), input_source="ui")
    session.client_id = x_client_id  # "" for anonymous / non-browser callers
    summarize_override = req.summarize  # None = server default; True/False = force
    logger.info(
        "New research session %s: query=%r source=ui client=%r summarize=%s",
        session.id, session.query, session.client_id[:8] or "anon", summarize_override,
    )
    active_sessions[session.id] = session
    queues[session.id] = asyncio.Queue()

    # Persist initial session
    async with get_session() as db:
        repo = SessionRepository(db)
        await repo.save(session)

    async def progress_callback(event: ProgressEvent) -> None:
        q = queues.get(event.research_id)
        if q:
            await q.put(event)

    async def run_and_signal_done() -> None:
        current_research_id.set(session.id)
        current_progress_callback.set(progress_callback)
        current_summarize_override.set(summarize_override)
        t_start = time.monotonic()
        _final_state: dict = {}
        try:
            # Emit initial thinking status
            await progress_callback(
                ProgressEvent(
                    research_id=session.id,
                    status=ResearchStatus.THINKING,
                    message="Agent is analyzing the query...",
                    progress_pct=5.0,
                )
            )

            graph = build_graph()

            initial_state = {
                "messages": [HumanMessage(content=session.query)],
                "query": session.query,
                "research_id": session.id,
                "search_results": [],
                "evaluated_sources": [],
                "claims": [],
                "citations": [],
                "report_markdown": "",
                "search_count": 0,
                "iteration_count": 0,
                "completed_tools": [],
                "search_queries_history": [],
                "search_evaluate_cycles": 0,
                "query_category": 0,
            }

            config = {
                "configurable": {
                    "progress_callback": progress_callback,
                },
            }

            final_state = await graph.ainvoke(initial_state, config=config)
            _final_state = final_state

            # Extract results from final state into session
            _populate_session_from_state(session, final_state)

            usage = tracker.get(session.id)
            hw_summary = tracker.get_hw_summary(session.id)
            logger.info(
                "Research session %s complete: %d searches, %d iterations, "
                "%d sources, %d claims, %d citations, report=%d chars, "
                "total_input_tokens=%d, total_output_tokens=%d, "
                "total_tokens=%d, llm_calls=%d",
                session.id,
                final_state.get("search_count", 0),
                final_state.get("iteration_count", 0),
                len(final_state.get("search_results", [])),
                len(final_state.get("claims", [])),
                len(final_state.get("citations", [])),
                len(final_state.get("report_markdown", "")),
                usage.input_tokens,
                usage.output_tokens,
                usage.total_tokens,
                usage.call_count,
            )

            # Treat "no sources found" as a failure — all searches timed out / failed
            _report = final_state.get("report_markdown", "")
            if not final_state.get("search_results") and _report.startswith("Unable to complete research"):
                session.status = ResearchStatus.FAILED
                session.error = "No search results — all searches failed or returned empty."
            else:
                session.status = ResearchStatus.COMPLETE
            await progress_callback(
                ProgressEvent(
                    research_id=session.id,
                    status=session.status,
                    message="Research complete!" if session.status == ResearchStatus.COMPLETE else "Research failed — no sources found.",
                    progress_pct=100.0,
                    hw_summary=hw_summary,
                    input_source=session.input_source,
                )
            )

        except Exception as exc:
            logger.exception("Research agent failed")
            session.status = ResearchStatus.FAILED
            session.error = str(exc)
            await progress_callback(
                ProgressEvent(
                    research_id=session.id,
                    status=ResearchStatus.FAILED,
                    message=f"Research failed: {exc}",
                )
            )

        finally:
            elapsed_s = time.monotonic() - t_start
            # Persist final state
            async with get_session() as db:
                repo = SessionRepository(db)
                await repo.save(session)
            # Write live metrics for the dashboard (before clearing tracker)
            try:
                tracker.write_metrics(
                    session.id,
                    session.query,
                    _final_state.get("query_category", 0),
                    session.status.value,
                    elapsed_s,
                    iterations=_final_state.get("iteration_count", 0),
                    source=session.input_source,
                    report_markdown=_final_state.get("report_markdown", ""),
                )
            except Exception:
                logger.exception("Failed to write live metrics")
            q = queues.get(session.id)
            if q:
                await q.put(None)  # sentinel to close SSE
            # Clean up in-memory state after a delay (give SSE time to flush)
            await asyncio.sleep(30)
            active_sessions.pop(session.id, None)
            queues.pop(session.id, None)
            tracker.clear(session.id)

    asyncio.create_task(run_and_signal_done())

    return ResearchResponse(research_id=session.id, status=session.status)


def _populate_session_from_state(
    session: ResearchSession, state: dict
) -> None:
    """Transfer accumulated artifacts from graph state into the session."""
    # Search results
    if state.get("search_results"):
        session.search_results = [
            SearchResult.model_validate(r) for r in state["search_results"]
        ]

    # Evaluated sources
    if state.get("evaluated_sources"):
        session.evaluated_sources = [
            EvaluatedSource.model_validate(e) for e in state["evaluated_sources"]
        ]

    # Claims
    if state.get("claims"):
        session.claims = [
            FactualClaim.model_validate(c) for c in state["claims"]
        ]

    # Citations
    if state.get("citations"):
        session.citations = [
            Citation.model_validate(c) for c in state["citations"]
        ]

    # Report
    if state.get("report_markdown"):
        session.report_markdown = state["report_markdown"]
    else:
        # Direct answer — agent answered without tools.
        # Use the last AI message content as the report.
        messages = state.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
                session.report_markdown = msg.content
                break


@app.get("/api/research/{research_id}/stream")
async def stream_progress(research_id: str) -> StreamingResponse:
    if research_id not in active_sessions and research_id not in queues:
        raise HTTPException(status_code=404, detail="Research session not found or already completed")

    q = queues.get(research_id)
    if q is None:
        raise HTTPException(status_code=404, detail="No event queue for session")

    async def event_generator():
        while True:
            event = await q.get()
            if event is None:
                break
            data = json.dumps(event.model_dump())
            yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/research/{research_id}")
async def get_research_session(research_id: str) -> dict:
    # Check in-memory first (active session)
    session = active_sessions.get(research_id)
    if session is not None:
        return session.model_dump()

    # Fall back to DB
    async with get_session() as db:
        repo = SessionRepository(db)
        session = await repo.get(research_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Research session not found")
    return session.model_dump()


@app.get("/api/sessions")
async def list_sessions(limit: int = 50, client_id: str = "") -> list[dict]:
    """Return session history. If ``client_id`` is provided, scoped to that browser client."""
    async with get_session() as db:
        repo = SessionRepository(db)
        return await repo.list_sessions(limit=limit, client_id=client_id)


@app.delete("/api/sessions")
async def clear_sessions(client_id: str = "") -> dict:
    """Delete session history. If ``client_id`` is provided, deletes only that client's sessions."""
    async with get_session() as db:
        repo = SessionRepository(db)
        deleted = await repo.delete_all(client_id=client_id)
    return {"deleted": deleted}


@app.get("/api/system/cache-stats")
async def cache_stats() -> dict:
    """Return in-memory and DB search cache statistics."""
    mem_stats = get_cache_stats()
    async with get_session() as db:
        repo = SearchCacheRepository(db)
        db_stats = await repo.stats()
    return {"memory": mem_stats, "db": db_stats}


@app.delete("/api/cache")
async def clear_cache() -> dict:
    """Clear the search cache (both in-memory and persistent DB)."""
    mem_cleared = clear_search_cache()
    async with get_session() as db:
        repo = SearchCacheRepository(db)
        db_cleared = await repo.clear()
    return {"memory_cleared": mem_cleared, "db_cleared": db_cleared}


# Mount frontend static files — must be last so it doesn't shadow API routes
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
