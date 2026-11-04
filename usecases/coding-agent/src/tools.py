# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Coding Agent Tools — Sandboxed code execution via Agent Sandbox + Ray parallel.

All code execution happens in isolated Kubernetes sandbox pods managed by the
Agent Sandbox controller. Each agent session gets its own sandbox pod with
persistent state (packages, variables, files) across tool calls.
"""

import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List

from langchain_core.tools import tool

# Configuration
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "workspace"))
WORKSPACE_DIR.mkdir(exist_ok=True, parents=True)
SANDBOX_NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "agent-sandbox-system")
SANDBOX_TEMPLATE = os.getenv("SANDBOX_TEMPLATE", "python-sandbox-template")
# Warm pool to adopt sandboxes from (agent-toolkit ships "python-pool"); empty relies
# on the controller creating a shadow pool for SANDBOX_TEMPLATE.
SANDBOX_WARMPOOL = os.getenv("SANDBOX_WARMPOOL", "")
# Router bearer token (secret agent-sandbox-system/sandbox-router-auth).
SANDBOX_ROUTER_TOKEN = os.getenv("SANDBOX_ROUTER_TOKEN", "")

# ---------------------------------------------------------------------------
# Session Sandbox — one sandbox pod per agent process lifetime.
# State (installed packages, defined variables) persists across tool calls.
# SandboxDirectConnectionConfig routes through sandbox-router, which is allowed
# by the controller's NetworkPolicy. Direct pod connections (InCluster config)
# are blocked because the NetworkPolicy only permits ingress from the router.
# ---------------------------------------------------------------------------

_sandbox_client = None
_session_sandbox = None
_sandbox_available = False

try:
    from k8s_agent_sandbox import SandboxClient
    from k8s_agent_sandbox.models import SandboxDirectConnectionConfig
    _sandbox_available = True
except ImportError:
    pass


# Inject the router bearer token into the SDK's HTTP session — the
# k8s-agent-sandbox SDK has no built-in option for it. No-op when unset.
def _patch_router_auth() -> None:
    if not (_sandbox_available and SANDBOX_ROUTER_TOKEN):
        return
    try:
        import k8s_agent_sandbox.connector as _conn
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[Sandbox] WARNING: could not patch sandbox-router auth (import failed): {exc}")
        return
    _orig_init = _conn.SandboxConnector.__init__

    def _init_with_auth(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        try:
            self.session.headers["Authorization"] = f"Bearer {SANDBOX_ROUTER_TOKEN}"
        except Exception:  # pragma: no cover - defensive
            pass

    _conn.SandboxConnector.__init__ = _init_with_auth
    print("[Sandbox] Sandbox-router bearer-token auth enabled.")


_patch_router_auth()


def _get_sandbox():
    """Return the active session sandbox, creating one if needed."""
    global _sandbox_client, _session_sandbox
    if not _sandbox_available:
        return None
    if _sandbox_client is None:
        _sandbox_client = SandboxClient(
            connection_config=SandboxDirectConnectionConfig(
                api_url=os.getenv(
                    "SANDBOX_ROUTER_URL",
                    "http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080",
                )
            )
        )
    if _session_sandbox is None or not _session_sandbox.is_active:
        create_kwargs = {"template": SANDBOX_TEMPLATE, "namespace": SANDBOX_NAMESPACE}
        if SANDBOX_WARMPOOL:  # adopt from the named warm pool
            create_kwargs["warmpool"] = SANDBOX_WARMPOOL
        _session_sandbox = _sandbox_client.create_sandbox(**create_kwargs)
    return _session_sandbox


# ---------------------------------------------------------------------------
# Ray distributed-computing (optional)
# ---------------------------------------------------------------------------
_RAY_ENABLED: bool = os.getenv("RAY_ENABLED", "false").lower() == "true"
_RAY_ADDRESS: str = os.getenv("RAY_ADDRESS", "")
_ray_ready: bool = False

if _RAY_ENABLED and _RAY_ADDRESS:
    try:
        import ray
        if not ray.is_initialized():
            ray.init(address=_RAY_ADDRESS, ignore_reinit_error=True, logging_level="error")
        _ray_ready = True
    except Exception as _ray_init_err:
        print(f"[Ray] WARNING: Could not connect to Ray cluster at {_RAY_ADDRESS}: {_ray_init_err}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def execute_python_code(code: str) -> str:
    """Execute Python code in an isolated Kubernetes sandbox pod and return the output.

    The sandbox persists for the lifetime of the agent process, so state is
    preserved across calls — installed packages, defined variables, and written
    files all remain available in subsequent executions.
    Use reset_sandbox() to start a completely fresh environment.

    Falls back to local execution if the sandbox infrastructure is not available.
    """
    code = re.sub(r'^```[a-zA-Z]*\n?', '', code.strip())
    code = re.sub(r'\n?```$', '', code.strip())

    sandbox = _get_sandbox()
    if sandbox is not None:
        try:
            encoded = base64.b64encode(code.encode()).decode()
            result = sandbox.commands.run(
                f"sh -c 'echo {encoded} | base64 -d > /tmp/_exec.py && python3 /tmp/_exec.py'"
            )
            output = []
            if result.stdout:
                output.append(result.stdout)
            if result.stderr:
                output.append(f"Errors:\n{result.stderr}")
            if result.exit_code != 0:
                output.append(f"Exit code: {result.exit_code}")
            return "\n\n".join(output) if output else "Code executed successfully with no output"
        except Exception as e:
            return f"Error executing code in sandbox: {str(e)}"

    # Local fallback when sandbox is not available
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, dir=WORKSPACE_DIR) as f:
            temp_file = f.name
            f.write(code)
        result = subprocess.run(
            [sys.executable, temp_file],
            capture_output=True, text=True, timeout=30, cwd=WORKSPACE_DIR,
        )
        os.unlink(temp_file)
        output = []
        if result.stdout:
            output.append(f"Output:\n{result.stdout}")
        if result.stderr:
            output.append(f"Errors:\n{result.stderr}")
        if result.returncode != 0:
            output.append(f"Exit code: {result.returncode}")
        return "\n\n".join(output) if output else "Code executed successfully with no output"
    except subprocess.TimeoutExpired:
        return "Error: Code execution timed out (30 seconds)"
    except Exception as e:
        return f"Error executing code: {str(e)}"


@tool
def execute_python_code_parallel(tasks: List[str]) -> str:
    """Execute multiple independent Python code snippets in parallel using Ray distributed computing.

    Each snippet in `tasks` runs as a separate Ray remote task on the cluster's worker
    nodes simultaneously. Use this when you have N independent computations (e.g.
    benchmarks, data transforms, simulations) that would otherwise run sequentially.

    Falls back to sequential local execution when Ray is not configured.
    """
    if not tasks:
        return "No tasks provided."

    if _ray_ready:
        try:
            import ray

            @ray.remote
            def _run_task(code: str, idx: int) -> dict:
                import subprocess, sys, tempfile, os, time as _time
                start = _time.monotonic()
                workspace = os.getenv("WORKSPACE_DIR", "/tmp")
                with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=workspace) as f:
                    f.write(code)
                    tmp = f.name
                proc = subprocess.run(
                    [sys.executable, tmp],
                    capture_output=True, text=True, timeout=60, cwd=workspace,
                )
                os.unlink(tmp)
                elapsed = _time.monotonic() - start
                return {"idx": idx, "stdout": proc.stdout, "stderr": proc.stderr,
                        "returncode": proc.returncode, "elapsed": elapsed}

            wall_start = time.monotonic()
            futures = [_run_task.remote(code, i) for i, code in enumerate(tasks)]
            results = ray.get(futures)
            wall_elapsed = time.monotonic() - wall_start

            lines = [f"Ray parallel execution — {len(tasks)} tasks, wall time {wall_elapsed:.2f}s\n"]
            for r in sorted(results, key=lambda x: x["idx"]):
                lines.append(f"--- Task {r['idx'] + 1} ({r['elapsed']:.2f}s, exit {r['returncode']}) ---")
                if r["stdout"]:
                    lines.append(r["stdout"].rstrip())
                if r["stderr"]:
                    lines.append(f"[stderr] {r['stderr'].rstrip()}")
            return "\n".join(lines)
        except Exception as e:
            pass  # fall through to sequential

    # Sequential fallback
    lines = [f"Sequential fallback — Ray not connected ({len(tasks)} tasks)\n"]
    for i, code in enumerate(tasks):
        start = time.monotonic()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=WORKSPACE_DIR) as f:
            tmp = f.name
            f.write(code)
        proc = subprocess.run(
            [sys.executable, tmp],
            capture_output=True, text=True, timeout=60, cwd=WORKSPACE_DIR,
        )
        os.unlink(tmp)
        elapsed = time.monotonic() - start
        lines.append(f"--- Task {i + 1} ({elapsed:.2f}s, exit {proc.returncode}) ---")
        if proc.stdout:
            lines.append(proc.stdout.rstrip())
        if proc.stderr:
            lines.append(f"[stderr] {proc.stderr.rstrip()}")
    return "\n".join(lines)


@tool
def write_file(filename: str, content: str) -> str:
    """Write content to a file in the workspace directory."""
    try:
        file_path = WORKSPACE_DIR / filename
        if not file_path.resolve().is_relative_to(WORKSPACE_DIR.resolve()):
            return "Error: Cannot write outside workspace directory"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Successfully wrote to {filename}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


@tool
def read_file(filename: str) -> str:
    """Read content from a file in the workspace directory."""
    try:
        file_path = WORKSPACE_DIR / filename
        if not file_path.resolve().is_relative_to(WORKSPACE_DIR.resolve()):
            return "Error: Cannot read outside workspace directory"
        if not file_path.exists():
            return f"Error: File {filename} does not exist"
        return file_path.read_text()
    except Exception as e:
        return f"Error reading file: {str(e)}"


@tool
def list_files(directory: str = ".") -> str:
    """List files in the workspace directory."""
    try:
        dir_path = WORKSPACE_DIR / directory
        if not dir_path.resolve().is_relative_to(WORKSPACE_DIR.resolve()):
            return "Error: Cannot list outside workspace directory"
        if not dir_path.exists():
            return f"Error: Directory {directory} does not exist"
        files = []
        for item in sorted(dir_path.iterdir()):
            if item.is_file():
                files.append(f"  {item.name}")
            elif item.is_dir():
                files.append(f"  {item.name}/")
        return "\n".join(files) if files else "(empty directory)"
    except Exception as e:
        return f"Error listing files: {str(e)}"


@tool
def install_package(package: str) -> str:
    """Install a Python package inside the session sandbox using pip."""
    sandbox = _get_sandbox()
    if sandbox is not None:
        try:
            result = sandbox.commands.run(f"pip install {package}")
            if result.exit_code == 0:
                return f"Successfully installed {package}"
            else:
                return f"Error installing {package}:\n{result.stderr}"
        except Exception as e:
            return f"Error installing package in sandbox: {str(e)}"

    # Local fallback
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return f"Successfully installed {package}"
        return f"Error installing {package}:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "Error: Package installation timed out"
    except Exception as e:
        return f"Error installing package: {str(e)}"


@tool
def reset_sandbox() -> str:
    """Terminate the current sandbox session and start a completely fresh environment.

    All installed packages and defined variables from the current session will be lost.
    """
    global _session_sandbox
    if not _sandbox_available:
        return "Sandbox not available — running in local mode."
    try:
        if _session_sandbox is not None:
            _session_sandbox.terminate()
            _session_sandbox = None
        return "Sandbox reset. A fresh sandbox will be created on the next code execution."
    except Exception as e:
        return f"Error resetting sandbox: {str(e)}"


ALL_TOOLS = [
    execute_python_code,
    execute_python_code_parallel,
    write_file,
    read_file,
    list_files,
    install_package,
    reset_sandbox,
]
