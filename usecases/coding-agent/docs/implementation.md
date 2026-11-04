# Coding Agent (LangGraph) -- Implementation Details

This document covers the architecture, tools, conversation state, LLM
integration, API server, observability, and security considerations of the
LangGraph Coding Agent (`src/agent.py`, `src/tools.py`, `src/main.py`).

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                               main.py                                     │
│  FastAPI app (uvicorn, DEVUI_HOST:DEVUI_PORT = 0.0.0.0:8090)              │
│    GET  /health                 (public)                                  │
│    GET  /v1/entities            ┐                                         │
│    POST /v1/chat/completions    ├─ Bearer DEVUI_AUTH_TOKEN (_check_auth)  │
│    POST /v1/responses           ┘                                         │
│  Langfuse CallbackHandler (when LANGFUSE_* keys are set)                  │
└─────────────────────────────────────┬────────────────────────────────────┘
                                      │ agent.ainvoke / astream_events
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                               agent.py                                    │
│  ChatOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY,             │
│             model=MODEL_NAME, temperature=0)                              │
│  create_react_agent(model, tools=ALL_TOOLS, prompt=SYSTEM_PROMPT,         │
│                     checkpointer=<RedisSaver | InMemorySaver>)            │
└─────────────────────────────────────┬────────────────────────────────────┘
                                      │ tool calls
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                               tools.py                                    │
│  execute_python_code, install_package, reset_sandbox ──▶ Agent Sandbox    │
│  execute_python_code_parallel ──────────────────────────▶ Ray (optional)  │
│  write_file, read_file, list_files ─────────────────────▶ /app/workspace  │
└──────────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Role |
|-----------|------|
| `main.py` | FastAPI server: OpenAI-compatible endpoints, SSE streaming, bearer-token auth, Langfuse setup |
| `agent.py` | LLM client, system prompt, checkpointer selection, `create_react_agent` graph; `invoke`/`ainvoke`/`astream` helpers |
| `tools.py` | LangChain `@tool` functions; Agent Sandbox client (router auth, warm pool); optional Ray connection |
| `langgraph` | Prebuilt ReAct loop: model → tool calls → tool results → model, until a final answer |
| `langchain-openai` | `ChatOpenAI` client pointed at LiteLLM |
| `k8s-agent-sandbox` | Sandbox SDK: creates SandboxClaims and executes commands through the sandbox router |

## LLM Integration

`create_agent()` builds a `ChatOpenAI` client from `OPENAI_BASE_URL`,
`OPENAI_API_KEY` and `MODEL_NAME` with `temperature=0`. On Intel® Agent Toolkit this is
LiteLLM at `http://litellm.litellm.svc.cluster.local:4000/v1`; the model
(default `qwen2-5-coder-14b`) must be registered in LiteLLM and support native
tool/function calling, because the ReAct loop relies on structured tool calls.

LiteLLM is protected by an Istio AuthorizationPolicy; the `coding-agent`
namespace must be in `litellm_allowed_namespaces` (see the
[README](../README.md#prerequisites)).

The system prompt (`SYSTEM_PROMPT` in `agent.py`) instructs the model to always
execute the code it writes via `execute_python_code`, reuse sandbox state across
calls, call `reset_sandbox()` for a clean environment, and use
`execute_python_code_parallel` for independent tasks when Ray is enabled.

## Agent Tools

All tools are LangChain `@tool` functions collected in `ALL_TOOLS`. They run
automatically when the model calls them.

### execute_python_code

Strips Markdown code fences, base64-encodes the code, and runs
`echo <b64> | base64 -d > /tmp/_exec.py && python3 /tmp/_exec.py` inside the
session sandbox via `sandbox.commands.run(...)`. Returns stdout, stderr
(`Errors:`) and a non-zero exit code. If the `k8s-agent-sandbox` SDK is not
importable, it falls back to running the code locally in the agent container
(30 s timeout) — the shipped image includes the SDK, so the sandbox path is used.

### execute_python_code_parallel

Takes a list of code snippets. When `RAY_ENABLED=true` and `RAY_ADDRESS` is
reachable at import time, each snippet runs as a Ray remote task (60 s timeout
each) and the results are reported with per-task and wall-clock timings.
Otherwise (or on any Ray error) the snippets run **sequentially in the agent
container** — not in the sandbox.

### install_package

Runs `pip install <package>` in the session sandbox, so the package is available
to later `execute_python_code` calls. Sandbox pods have public-IP-only egress, so
this needs direct internet access from the sandbox.

### reset_sandbox

Terminates the current sandbox; the next execution creates a new one.

### write_file / read_file / list_files

Operate on `WORKSPACE_DIR` (`/app/workspace`, a 5Gi PVC in Kubernetes). Paths are
resolved and rejected if they escape the workspace (`is_relative_to` check).
These files live in the **agent pod**, not in the sandbox.

## Agent Sandbox Integration

```
tools._get_sandbox()
   │  SandboxClient(SandboxDirectConnectionConfig(api_url=SANDBOX_ROUTER_URL))
   │  create_sandbox(template=SANDBOX_TEMPLATE, namespace=SANDBOX_NAMESPACE,
   │                 warmpool=SANDBOX_WARMPOOL)
   ▼
sandbox-router-svc.agent-sandbox-system:8080  ──▶  sandbox pod (python-pool)
```

- **One sandbox per agent process.** `_session_sandbox` is a module-level
  singleton created lazily on first use and re-created if it is no longer active.
  All sessions served by the same pod share it.
- **Router connection.** `SandboxDirectConnectionConfig` routes command
  execution through the sandbox router — the sandbox NetworkPolicy only admits
  ingress from the router, so direct pod connections would be blocked.
- **Router auth.** The SDK has no option for a bearer token, so
  `_patch_router_auth()` wraps `SandboxConnector.__init__` to add
  `Authorization: Bearer $SANDBOX_ROUTER_TOKEN` to its HTTP session (no-op when
  the token is empty). `deploy.sh` reads the token from
  `agent-sandbox-system/sandbox-router-auth`.
- **Warm pool.** With `SANDBOX_WARMPOOL=python-pool` (chart default) sandboxes are
  adopted from the pre-warmed pool for near-instant start.
- **RBAC.** The SDK creates SandboxClaims through the Kubernetes API, so the chart
  ships a Role/RoleBinding in `agent-sandbox-system` granting the agent's
  ServiceAccount `create/get/list/watch/update/patch/delete` on
  `extensions.agents.x-k8s.io/sandboxclaims` and read access to
  `agents.x-k8s.io/sandboxes` (`serviceAccount.sandboxAccess: true`).

## Conversation State (Checkpointer)

`_build_checkpointer()` tries `langgraph.checkpoint.redis.RedisSaver(REDIS_URL)`
and falls back to `InMemorySaver` on any error. The Redis saver comes from the
`langgraph-checkpoint-redis` package, which is **not** in `requirements.txt`, so
the import fails and the agent always uses **`InMemorySaver`**. Consequences:

- Conversation history is kept per `thread_id` in the agent process only.
- It is lost on pod restart and not shared across replicas.

Adding `langgraph-checkpoint-redis` to `requirements.txt` (with the authenticated
`REDIS_URL` that `deploy.sh` already passes) would make the existing code use
Redis.

Thread selection:

| Endpoint | `thread_id` |
|---|---|
| `/v1/chat/completions` | `session_id` body field, default `default` |
| `/v1/responses` | `metadata.thread_id`, else `metadata.entity_id`, else `default` |

Because the checkpointer accumulates messages per thread, callers that send the
full history to `/v1/chat/completions` with the same `session_id` will see it
appended to the stored thread; use distinct `session_id`s for independent
conversations.

## API Server

`main.py` creates a FastAPI app with permissive CORS and runs it with uvicorn on
`DEVUI_HOST:DEVUI_PORT` (default `0.0.0.0:8090`).

- **Auth.** `_check_auth()` compares `Authorization: Bearer <token>` with
  `DEVUI_AUTH_TOKEN` on every `/v1/*` route and returns `401` otherwise. If
  `DEVUI_AUTH_TOKEN` is empty, auth is disabled (local dev only — the chart
  requires a token and `deploy.sh` generates one).
- **`/v1/chat/completions`** maps OpenAI messages to LangGraph tuples
  (`system`/`human`/`ai`), runs `agent.ainvoke`, and returns the last AI message
  as an OpenAI `chat.completion` (usage counters are zero). With `stream: true`
  it forwards `on_chat_model_stream` events as `chat.completion.chunk` SSE.
- **`/v1/responses`** runs the agent on `input` and returns `output[]` with
  `message` items (assistant text) and `function_call_output` items (tool
  results). With `stream: true` it emits `response.created`,
  `response.output_text.delta`, `response.function_call_arguments.start`,
  `response.function_call_output`, `response.output_text.done` and
  `response.completed` events.
- **`/v1/entities`** describes the agent (framework `langgraph`, model, tool
  names and descriptions).
- **`/health`** returns `{"status":"healthy","entities_count":1,"framework":"langgraph"}`
  and backs the liveness/readiness probes.

## Observability

At startup `setup_observability()` checks for `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY`. If present and `get_client().auth_check()` succeeds, it
creates a `langfuse.langchain.CallbackHandler`; `_run_config()` adds it to every
run's `callbacks`, so each request produces a Langfuse trace with the LLM calls
and tool steps. `deploy.sh` auto-discovers the keys from
`langfuse/langfuse-secrets` and sets `LANGFUSE_HOST` to
`http://langfuse-web.langfuse.svc.cluster.local:3000`. LiteLLM additionally
traces every model call on the platform side. See the
[Observability guide](../../../docs/observability-langfuse.md).

## Security Considerations

- **Code isolation.** Model-generated code runs in Agent Sandbox pods, not in the
  agent pod. Sandbox pods accept traffic only from the router and can reach public
  IPs only — they cannot call LiteLLM, Redis or other in-cluster services.
- **Exceptions.** `execute_python_code_parallel` without Ray, and the local
  fallback of `execute_python_code` when the SDK is missing, run code inside the
  agent container. The container runs as non-root UID 10001 with all capabilities
  dropped, no privilege escalation and seccomp `RuntimeDefault` (Pod Security
  `restricted`), but it does have in-cluster network access.
- **API access.** A single shared bearer token protects `/v1/*`; there is no
  per-user identity. The HTTPRoute opts out of the gateway's LiteLLM-key check
  (`httpRoute.bypassGatewayAuth: true`) because the token protects the API.
- **Path traversal.** File tools refuse paths outside `WORKSPACE_DIR`.
- **Prompt injection.** Model output drives tool calls; treat sandbox output and
  generated code as untrusted.

### Recommendations

1. Use a LiteLLM virtual key scoped to the model instead of the master key.
2. Rotate `DEVUI_AUTH_TOKEN` by redeploying with a new value.
3. Keep `serviceAccount.sandboxAccess` limited to the sandbox namespace (the
   chart's Role is namespaced).
4. Add `langgraph-checkpoint-redis` if conversation history must survive
   restarts.
