# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ResearchSessionRow, SearchCacheRow
from models import ResearchSession

logger = logging.getLogger(__name__)


class SessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def save(self, session: ResearchSession) -> None:
        """Upsert a research session."""
        row = await self.db.get(ResearchSessionRow, session.id)
        data = session.model_dump()

        if row is None:
            row = ResearchSessionRow(
                id=session.id,
                query=session.query,
                status=session.status.value,
                client_id=getattr(session, "client_id", ""),
                decomposition=data.get("decomposition"),
                search_results=data.get("search_results"),
                evaluated_sources=data.get("evaluated_sources"),
                citations=data.get("citations"),
                claims=data.get("claims"),
                report_markdown=session.report_markdown,
                error=session.error,
            )
            self.db.add(row)
        else:
            row.status = session.status.value
            row.client_id = getattr(session, "client_id", row.client_id)
            row.decomposition = data.get("decomposition")
            row.search_results = data.get("search_results")
            row.evaluated_sources = data.get("evaluated_sources")
            row.citations = data.get("citations")
            row.claims = data.get("claims")
            row.report_markdown = session.report_markdown
            row.error = session.error
            row.updated_at = datetime.now(timezone.utc)

        await self.db.commit()

    async def get(self, session_id: str) -> ResearchSession | None:
        row = await self.db.get(ResearchSessionRow, session_id)
        if row is None:
            return None
        return self._row_to_session(row)

    async def list_sessions(self, limit: int = 50, client_id: str = "") -> list[dict]:
        """Return a summary list of sessions (id, query, status, created_at).

        If ``client_id`` is non-empty, returns only sessions for that client.
        """
        stmt = select(
            ResearchSessionRow.id,
            ResearchSessionRow.query,
            ResearchSessionRow.status,
            ResearchSessionRow.created_at,
        )
        if client_id:
            stmt = stmt.where(ResearchSessionRow.client_id == client_id)
        stmt = stmt.order_by(ResearchSessionRow.created_at.desc()).limit(limit)
        result = await self.db.execute(stmt)
        return [
            {
                "id": r.id,
                "query": r.query,
                "status": r.status,
                "created_at": r.created_at.isoformat() if isinstance(r.created_at, datetime) else str(r.created_at),
            }
            for r in result
        ]

    async def delete_all(self, client_id: str = "") -> int:
        """Delete sessions. If ``client_id`` is given, only delete that client's sessions."""
        if client_id:
            stmt = delete(ResearchSessionRow).where(ResearchSessionRow.client_id == client_id)
        else:
            stmt = delete(ResearchSessionRow)
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount

    @staticmethod
    def _row_to_session(row: ResearchSessionRow) -> ResearchSession:
        return ResearchSession.model_validate({
            "id": row.id,
            "query": row.query,
            "status": row.status,
            "decomposition": row.decomposition,
            "search_results": row.search_results or [],
            "evaluated_sources": row.evaluated_sources or [],
            "citations": row.citations or [],
            "claims": row.claims or [],
            "report_markdown": row.report_markdown or "",
            "error": row.error,
            "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else str(row.created_at),
            "client_id": row.client_id or "",
        })


class SearchCacheRepository:
    """Persistent search-result cache backed by SQLite."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, key: str) -> list | None:
        """Return cached results for ``key`` if not expired, otherwise None."""
        now = datetime.now(timezone.utc)
        row = await self.db.get(SearchCacheRow, key)
        if row is None:
            return None
        if row.expires_at.replace(tzinfo=timezone.utc) <= now:
            # Expired — delete lazily
            await self.db.delete(row)
            await self.db.commit()
            return None
        try:
            return json.loads(row.results_json)
        except Exception:
            return None

    async def set(self, key: str, query_text: str, results: list, ttl_seconds: int) -> None:
        """Upsert a cache entry with ``ttl_seconds`` TTL."""
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        results_json = json.dumps(results)
        row = await self.db.get(SearchCacheRow, key)
        if row is None:
            row = SearchCacheRow(
                key=key,
                query_text=query_text,
                results_json=results_json,
                expires_at=expires_at,
            )
            self.db.add(row)
        else:
            row.query_text = query_text
            row.results_json = results_json
            row.expires_at = expires_at
        await self.db.commit()

    async def load_all_valid(self) -> dict[str, list]:
        """Load all non-expired entries. Used at startup to warm the in-memory cache."""
        now = datetime.now(timezone.utc)
        stmt = select(SearchCacheRow).where(SearchCacheRow.expires_at > now)
        result = await self.db.execute(stmt)
        out: dict[str, list] = {}
        for row in result.scalars():
            try:
                out[row.key] = json.loads(row.results_json)
            except Exception:
                pass
        return out

    async def clear(self) -> int:
        """Delete all cache entries."""
        result = await self.db.execute(delete(SearchCacheRow))
        await self.db.commit()
        return result.rowcount

    async def stats(self) -> dict:
        """Return counts of total and expired entries."""
        now = datetime.now(timezone.utc)
        total_stmt = select(SearchCacheRow)
        total_result = await self.db.execute(total_stmt)
        rows = list(total_result.scalars())
        total = len(rows)
        expired = sum(1 for r in rows if r.expires_at.replace(tzinfo=timezone.utc) <= now)
        return {"total": total, "expired": expired, "valid": total - expired}
