# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import settings

logger = logging.getLogger(__name__)

# Paths that don't require authentication
_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Simple header-based API key validation.

    If settings.api_keys is empty, auth is disabled (all requests pass).
    Otherwise, requests must include `X-API-Key` header with a valid key.
    Static files (frontend) are excluded from auth.
    """

    async def dispatch(self, request: Request, call_next):
        valid_keys = {
            k.strip() for k in settings.api_keys.split(",") if k.strip()
        }

        # If no keys configured, auth is disabled
        if not valid_keys:
            return await call_next(request)

        path = request.url.path

        # Allow public paths
        if path in _PUBLIC_PATHS:
            return await call_next(request)

        # Allow static files (frontend)
        if not path.startswith("/api/"):
            return await call_next(request)

        # Check API key
        api_key = request.headers.get("X-API-Key", "")
        if api_key not in valid_keys:
            logger.warning("Unauthorized API request from %s to %s", request.client.host, path)
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )

        return await call_next(request)
