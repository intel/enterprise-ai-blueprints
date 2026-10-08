# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Coding Agent with Sandboxed Code Execution via Agent Sandbox
Uses Agent Framework with DevUI and Redis for memory.
Code execution is isolated in Kubernetes-managed sandbox pods.
Uses SandboxDirectConnectionConfig: routes command execution through the
sandbox-router service (sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080),
which is permitted by the controller's auto-generated NetworkPolicy.
SandboxInClusterConnectionConfig bypasses the router and connects directly
to pod IPs, which are blocked by that same NetworkPolicy.
"""

import base64
import os
import re
from pathlib import Path
from typing import Annotated, Any, List

from agent_framework import tool
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework.redis import RedisContextProvider
from dotenv import load_dotenv
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig
# from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig
from pydantic import Field

# Load environment variables
load_dotenv()

# Configuration
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "workspace"))
WORKSPACE_DIR.mkdir(exist_ok=True, parents=True)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SANDBOX_NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "agent-sandbox-system")
SANDBOX_TEMPLATE = os.getenv("SANDBOX_TEMPLATE", "python-sandbox-template")
# Warm pool to adopt sandboxes from (toolkit ships "python-pool"); empty relies
# on the controller creating a shadow pool for SANDBOX_TEMPLATE.
SANDBOX_WARMPOOL = os.getenv("SANDBOX_WARMPOOL", "")
# Router bearer token, needed when the router requires auth.
SANDBOX_ROUTER_TOKEN = os.getenv("SANDBOX_ROUTER_TOKEN", "")


# Inject the router bearer token into the SDK's HTTP session — the
# k8s-agent-sandbox SDK has no built-in option for it. No-op when unset.
def _patch_router_auth() -> None:
    if not SANDBOX_ROUTER_TOKEN:
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


# ============================================================================
# Session Sandbox — one sandbox pod per agent process lifetime.
# State (installed packages, defined variables) persists across tool calls.
# SandboxDirectConnectionConfig routes through sandbox-router, which is allowed
# by the controller's NetworkPolicy. Direct pod connections (InCluster config)
# are blocked because the NetworkPolicy only permits ingress from the router.
# ============================================================================

_sandbox_client: SandboxClient | None = None
_session_sandbox = None


def _get_sandbox():
    """Return the active session sandbox, creating one if needed."""
    global _sandbox_client, _session_sandbox
    if _sandbox_client is None:
        _sandbox_client = SandboxClient(
            connection_config=SandboxDirectConnectionConfig(
                api_url=os.getenv("SANDBOX_ROUTER_URL", "http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080")
            )
        )
    if _session_sandbox is None or not _session_sandbox.is_active:
        create_kwargs = {
            "template": SANDBOX_TEMPLATE,
            "namespace": SANDBOX_NAMESPACE,
        }
        if SANDBOX_WARMPOOL:  # adopt from the named warm pool
            create_kwargs["warmpool"] = SANDBOX_WARMPOOL
        _session_sandbox = _sandbox_client.create_sandbox(**create_kwargs)
    return _session_sandbox

# ---------------------------------------------------------------------------
# Ray distributed-computing (optional)
#
# Ray is enabled at runtime only when both environment variables are set:
#   RAY_ENABLED=true
#   RAY_ADDRESS=ray://<head-svc>.<namespace>.svc.cluster.local:10001
#
# The import is deferred so the container starts normally even when Ray is
# not deployed (ray package may still be installed but the cluster is absent).
# ---------------------------------------------------------------------------
_RAY_ENABLED: bool = os.getenv("RAY_ENABLED", "false").lower() == "true"
_RAY_ADDRESS: str = os.getenv("RAY_ADDRESS", "")
_ray_ready: bool = False

if _RAY_ENABLED and _RAY_ADDRESS:
    try:
        import ray  # noqa: E402  (deferred import)
        if not ray.is_initialized():
            ray.init(address=_RAY_ADDRESS, ignore_reinit_error=True, logging_level="error")
        _ray_ready = True
    except Exception as _ray_init_err:
        print(f"[Ray] WARNING: Could not connect to Ray cluster at {_RAY_ADDRESS}: {_ray_init_err}")
        print("[Ray] Parallel execution tool will fall back to sequential execution.")


# ============================================================================
# Redis Context Provider (sanitizes special chars that break RediSearch queries)
# ============================================================================

class SanitizedRedisContextProvider(RedisContextProvider):
    """RedisContextProvider that strips RediSearch special characters from search text.

    Characters like '?', '!', '*', '~' etc. are special in RediSearch query syntax.
    When present in user messages they cause 'Syntax error' responses from Redis.

    Also filters search results to user messages only. Returning assistant responses
    as memories causes them to be injected back as user-role context, which misleads
    the model into repeating previous incorrect answers (e.g. an old "I don't know
    your name" response being treated as a user instruction to withhold personal info).
    """

    async def before_run(self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]) -> None:
        """Retrieve session-scoped context from Redis and add to the session context."""
        from agent_framework import Message  # noqa: PLC0415
        self._validate_filters()
        input_text = "\n".join(
            msg.text for msg in context.input_messages if msg and msg.text and msg.text.strip()
        )
        if not input_text.strip():
            return
        # Pass session_id so search is scoped to the current session only
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
        # Strip characters that have special meaning in RediSearch query syntax
        sanitized = re.sub(r'[?!@{}\[\]()|~*^=+<>&,]', ' ', text)
        sanitized = ' '.join(sanitized.split())  # collapse whitespace
        if not sanitized.strip():
            return []
        results = await super()._redis_search(sanitized, **kwargs)
        # Only surface user messages as injected memories. Assistant responses must
        # not be re-injected: before_run wraps all results in a single user-role
        # message, so an old assistant reply like "I don't have access to your name"
        # would appear to the LLM as the user instructing it to withhold information.
        return [r for r in results if r.get("role") == "user"]


# ============================================================================
# Code Execution Tools (Sandboxed via Agent Sandbox)
# ============================================================================


@tool(approval_mode="never_require")
def execute_python_code(
    code: Annotated[str, Field(description="Python code to execute")]
) -> str:
    """
    Execute Python code in an isolated Kubernetes sandbox pod and return the output.

    The sandbox persists for the lifetime of the agent process, so state is
    preserved across calls — installed packages, defined variables, and written
    files all remain available in subsequent executions.
    Use reset_sandbox() to start a completely fresh environment.
    """
    try:
        # Strip markdown code fences (e.g. ```python ... ``` or ``` ... ```)
        # that models sometimes include when generating code blocks.
        code = re.sub(r'^```[a-zA-Z]*\n?', '', code.strip())
        code = re.sub(r'\n?```$', '', code.strip())

        sandbox = _get_sandbox()
        # Encode code as base64 to avoid shell-escaping issues with quotes,
        # backslashes, or special characters in the code string.
        encoded = base64.b64encode(code.encode()).decode()
        result = sandbox.commands.run(
            f"sh -c 'echo {encoded} | base64 -d > /tmp/_exec.py && python3 /tmp/_exec.py'"
        )
        output = []
        if result.stdout:
            output.append(f"{result.stdout}")
        if result.stderr:
            output.append(f"Errors:\n{result.stderr}")
        if result.exit_code != 0:
            output.append(f"Exit code: {result.exit_code}")
        return "\n\n".join(output) if output else "Code executed successfully with no output"
    except Exception as e:
        return f"Error executing code in sandbox: {str(e)}"


@tool(approval_mode="never_require")
def write_file(
    filename: Annotated[str, Field(description="Name of file to write")],
    content: Annotated[str, Field(description="Content to write to file")]
) -> str:
    """Write content to a file in the workspace directory."""
    try:
        file_path = WORKSPACE_DIR / filename

        # Ensure we're writing within workspace
        if not file_path.resolve().is_relative_to(WORKSPACE_DIR.resolve()):
            return "Error: Cannot write outside workspace directory"

        file_path.write_text(content)
        return f"Successfully wrote to {filename}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


@tool(approval_mode="never_require")
def read_file(
    filename: Annotated[str, Field(description="Name of file to read")]
) -> str:
    """Read content from a file in the workspace directory."""
    try:
        file_path = WORKSPACE_DIR / filename

        # Ensure we're reading from workspace
        if not file_path.resolve().is_relative_to(WORKSPACE_DIR.resolve()):
            return "Error: Cannot read outside workspace directory"

        if not file_path.exists():
            return f"Error: File {filename} does not exist"

        content = file_path.read_text()
        return content
    except Exception as e:
        return f"Error reading file: {str(e)}"


@tool(approval_mode="never_require")
def list_files(
    directory: Annotated[str, Field(description="Directory to list (relative to workspace)")] = "."
) -> List[str]:
    """List files in the workspace directory."""
    try:
        dir_path = WORKSPACE_DIR / directory

        # Ensure we're listing within workspace
        if not dir_path.resolve().is_relative_to(WORKSPACE_DIR.resolve()):
            return ["Error: Cannot list outside workspace directory"]

        if not dir_path.exists():
            return [f"Error: Directory {directory} does not exist"]

        files = []
        for item in dir_path.iterdir():
            if item.is_file():
                files.append(f"📄 {item.name}")
            elif item.is_dir():
                files.append(f"📁 {item.name}/")

        return sorted(files) if files else ["(empty directory)"]
    except Exception as e:
        return [f"Error listing files: {str(e)}"]


@tool(approval_mode="never_require")
def install_package(
    package: Annotated[str, Field(description="Python package to install via pip")]
) -> str:
    """Install a Python package inside the session sandbox using pip."""
    try:
        sandbox = _get_sandbox()
        result = sandbox.commands.run(f"pip install {package}")
        if result.exit_code == 0:
            return f"Successfully installed {package}"
        else:
            return f"Error installing {package}:\n{result.stderr}"
    except Exception as e:
        return f"Error installing package: {str(e)}"


@tool(approval_mode="never_require")
def execute_python_code_parallel(
    tasks: Annotated[List[str], Field(description="List of independent Python code snippets to execute in parallel via Ray")]
) -> str:
    """
    Execute multiple independent Python code snippets in parallel using Ray distributed computing.

    Each snippet in `tasks` runs as a separate Ray remote task on the cluster's worker
    nodes simultaneously.  Use this when you have N independent computations (e.g.
    benchmarks, data transforms, simulations) that would otherwise run sequentially.

    Requirements:
    - deploy_kuberay=on must be set in agentic-config.cfg
    - ray.enabled=true and ray.address must be configured in the Coding Agent Helm values
    - The Ray cluster must be reachable from inside the Kubernetes pod

    Falls back to sequential local execution when Ray is not configured, so the tool
    is always safe to call regardless of cluster state.
    """
    import subprocess
    import sys
    import tempfile
    import time

    if not tasks:
        return "No tasks provided."

    # ── Ray parallel path ───────────────────────────────────────────────────
    if _ray_ready:
        try:
            import ray  # already initialised at module load

            @ray.remote
            def _run_task(code: str, idx: int) -> dict:
                """Ray remote wrapper — executes one code snippet in a worker."""
                import os
                import subprocess
                import sys
                import tempfile
                import time
                start = time.monotonic()
                workspace = os.getenv("WORKSPACE_DIR", tempfile.gettempdir())
                with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=workspace) as f:
                    f.write(code)
                    tmp = f.name
                proc = subprocess.run(
                    [sys.executable, tmp],
                    capture_output=True, text=True, timeout=60,
                    cwd=workspace,
                )
                os.unlink(tmp)
                elapsed = time.monotonic() - start
                return {
                    "idx": idx,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "returncode": proc.returncode,
                    "elapsed": elapsed,
                }

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
            return f"[Ray] Parallel execution failed: {e}\nFalling back to sequential execution.\n" + \
                   execute_python_code_parallel.__wrapped__(tasks)  # type: ignore[attr-defined]

    # ── Sequential fallback (Ray not available) ─────────────────────────────
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
def reset_sandbox() -> str:
    """
    Terminate the current sandbox session and start a completely fresh environment.
    Use this when you want a clean slate — all installed packages and defined
    variables from the current session will be lost.
    """
    global _session_sandbox
    try:
        if _session_sandbox is not None:
            _session_sandbox.terminate()
            _session_sandbox = None
        return "Sandbox reset. A fresh sandbox will be created on the next code execution."
    except Exception as e:
        return f"Error resetting sandbox: {str(e)}"


# ============================================================================
# Agent Creation
# ============================================================================


def create_coding_agent():
    """Create the coding agent with all tools and Redis memory."""

    # Create Redis context provider for persistent memory (text-only, no embeddings)
    context_provider = SanitizedRedisContextProvider(
        source_id="coding_agent_context",
        redis_url=REDIS_URL,
        index_name="coding_agent_memory",
        application_id="coding_agent_app",
        agent_id="coding_agent",
        user_id="default_user",
    )

    # Agent instructions
    instructions = """You are an expert coding assistant that helps users write and execute Python code.

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
- Remember context from previous conversations (stored in Redis)

When users ask you to create or run code:
1. Understand the requirements clearly
2. Write the Python code
3. Execute it using execute_python_code (dont send language indicators) (runs in a secure sandbox pod)
4. Show the results and explain any issues
5. Iterate based on feedback

You have access to persistent memory, so you can recall information from previous conversations.
"""

    # Create OpenAI-compatible chat client
    # Can point to OpenAI, Azure OpenAI, Ollama, or any compatible endpoint
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("OPENAI_API_KEY", "not-needed")
    model = os.getenv("MODEL_NAME", "gpt-4o")

    # Create agent using OpenAIChatCompletionClient
    agent = OpenAIChatCompletionClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
    ).as_agent(
        name="coding-agent",
        instructions=instructions,
        tools=[
            execute_python_code,
            execute_python_code_parallel,
            write_file,
            read_file,
            list_files,
            install_package,
            reset_sandbox,
        ],
        context_providers=[context_provider],
    )

    return agent


# Export agent for DevUI discovery
agent = create_coding_agent()
