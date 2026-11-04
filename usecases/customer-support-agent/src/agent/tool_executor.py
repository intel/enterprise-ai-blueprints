# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Parallel tool execution — local thread pool and/or KubeRay dispatch.

The agent can now request several tools in a single turn; this module runs a
batch of tool calls concurrently and returns their results in order.

Two backends (config.TOOL_EXECUTOR):
- "local": ThreadPoolExecutor in-process. Best latency for these tools (they
  read local SQLite, i.e. sub-millisecond) and no external dependency.
- "ray": dispatch each call as a task on the agent-toolkit KubeRay cluster (kuberay_enabled).

Honest note on the Ray path: the tools read a SQLite DB that lives only in this
pod, so a Ray worker on another node can't execute them directly. Each Ray task
therefore calls back to this agent's own /api/v1/tools/{name} endpoint over
in-cluster DNS. This genuinely exercises KubeRay (tasks show up in the Ray
dashboard) and demonstrates distributed orchestration, but it is a *toolkit
showcase*, not a speedup — it adds a network hop. Use "local" for production
latency, "ray" to demonstrate the toolkit. The Ray path auto-falls back to
local if the cluster is unreachable.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple

from config import TOOL_EXECUTOR, TOOL_MAX_PARALLEL, RAY_ADDRESS, SELF_API_URL
from tools.registry import execute_tool

# Ray connection state (lazy, shared)
_ray_ready = False
_ray_attempted = False


def _try_init_ray() -> bool:
    """Connect to the KubeRay cluster once. Returns False on any failure."""
    global _ray_ready, _ray_attempted
    if _ray_attempted:
        return _ray_ready
    _ray_attempted = True
    try:
        import ray

        if not ray.is_initialized():
            ray.init(address=RAY_ADDRESS, ignore_reinit_error=True, log_to_driver=False)
        _ray_ready = True
        print(f"[tool_executor] connected to Ray at {RAY_ADDRESS}", flush=True)
    except Exception as e:  # noqa: BLE001 - fall back to local
        print(f"[tool_executor] Ray unavailable ({e}); falling back to local", flush=True)
        _ray_ready = False
    return _ray_ready


def _run_tool_via_api(name: str, arguments: Dict[str, Any], api_url: str) -> Dict[str, Any]:
    """Execute a tool by calling the owning agent's HTTP API (used by Ray tasks)."""
    import urllib.request
    import json as _json

    if not api_url.startswith(("http://", "https://")):
        return {"error": f"refusing non-HTTP SELF_API_URL: {api_url}"}
    try:
        req = urllib.request.Request(
            f"{api_url}/api/v1/tools/{name}",
            data=_json.dumps({"arguments": arguments}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - scheme checked above
            return _json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"error": f"remote tool execution failed: {e}"}


def _execute_local(calls: List[Tuple[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if len(calls) == 1:
        name, args = calls[0]
        return [execute_tool(name, args)]
    results: List[Dict[str, Any]] = [None] * len(calls)  # type: ignore
    with ThreadPoolExecutor(max_workers=min(TOOL_MAX_PARALLEL, len(calls))) as pool:
        futures = {pool.submit(execute_tool, name, args): i for i, (name, args) in enumerate(calls)}
        for fut in futures:
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[idx] = {"error": f"tool execution failed: {e}"}
    return results


def _execute_ray(calls: List[Tuple[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    import ray

    api_url = SELF_API_URL

    @ray.remote
    def _remote(name: str, arguments: Dict[str, Any], url: str) -> Dict[str, Any]:
        import urllib.request
        import json as _json

        if not url.startswith(("http://", "https://")):
            return {"error": f"refusing non-HTTP SELF_API_URL: {url}"}
        try:
            req = urllib.request.Request(
                f"{url}/api/v1/tools/{name}",
                data=_json.dumps({"arguments": arguments}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - scheme checked above
                return _json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001
            return {"error": f"remote tool execution failed: {e}"}

    refs = [_remote.remote(name, args, api_url) for name, args in calls]
    return ray.get(refs)


def execute_tools(calls: List[Tuple[str, Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], str]:
    """Execute a batch of (tool_name, arguments) concurrently.

    Returns (results_in_order, backend_used) where backend_used is
    "local" or "ray".
    """
    if not calls:
        return [], "local"

    if TOOL_EXECUTOR == "ray" and _try_init_ray():
        try:
            return _execute_ray(calls), "ray"
        except Exception as e:  # noqa: BLE001
            print(f"[tool_executor] Ray dispatch failed ({e}); using local", flush=True)

    return _execute_local(calls), "local"
