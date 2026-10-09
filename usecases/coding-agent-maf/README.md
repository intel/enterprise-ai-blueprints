# Coding Agent

An AI-powered coding assistant that writes, executes, and debugs Python code.

Code execution runs in isolated **Kubernetes sandbox pods** managed by the
[Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox) controller —
each agent session gets its own sandbox pod, keeping installed packages and
defined variables alive across tool calls.

This use case runs on top of the
[Agentic AI Stack](https://github.com/intel/enterprise-agent-toolkit)
infrastructure.

---

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
  - [Option A — Kubernetes (Cluster)](#option-a--kubernetes-cluster)
  - [Option B — Docker Compose (Local Dev)](#option-b--docker-compose-local-dev)
- [Configuration](#configuration)
  - [Environment Variables](#environment-variables)
  - [Helm Values](#helm-values-key-overrides)
- [API Reference](#api-reference)
- [Running Examples](#running-examples)
- [Memory](#memory)
- [Extending](#extending)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
User / Browser (DevUI)
        │
        ▼
Coding Agent Pod  ──(cluster DNS)──▶  sandbox-router-svc  ──▶  Sandbox Pod
  (coding-agent ns)                 (agent-sandbox-system ns)      (ephemeral)
        │
        ▼
GenAI Gateway (LiteLLM)           Redis (memory)
  (genai-gateway ns)               (redis ns)
```

- The coding agent talks to the **GenAI Gateway** for all LLM inference.
- The coding agent calls the **sandbox-router** over in-cluster DNS — no
  port-forward or extra RBAC needed on the coding-agent pod.
- The sandbox-router owns all Kubernetes API interactions (create/delete SandboxClaims).
- Each session reuses one sandbox pod — state (installed packages, defined variables,
  `/tmp` files) persists across `execute_python_code` and `install_package` calls.
  Call `reset_sandbox()` to start fresh.

---

## Prerequisites

The following stack components must be running **before** deploying this use case.
See the [Agentic AI Stack setup guide](../../docs/agentic-stack-setup.md) or the
[infrastructure repo README](https://github.com/intel/enterprise-agent-toolkit).

| Component | Namespace | Config flag |
|---|---|---|
| Kubernetes | — | `deploy_kubernetes_fresh=on` |
| GenAI Gateway (LiteLLM) | `genai-gateway` | `deploy_genai_gateway=on` |
| Redis Stack | `redis` | `deploy_redis=on` |
| Agent Sandbox controller + router | `agent-sandbox-system` | `deploy_agent_sandbox=on` |
| LLM model | `llm-model-*` | `deploy_llm_models=on`, `models=cpu-qwen3-coder-30b` |

Verify all prerequisites are up:

```bash
kubectl get pods -n genai-gateway
kubectl get pods -n redis
kubectl get pods -n agent-sandbox-system
```

---

## Quick Start

### Option A — Kubernetes (Cluster)

```bash
# From the repo root
./deploy-usecase.sh coding-agent-maf
```

Or using the use case's own deploy script:

```bash
cd usecases/coding-agent-maf
./deploy.sh
```

Both scripts build the image (into the containerd `k8s.io` namespace via
`nerdctl`), prompt for your stack details, and deploy via Helm. They
auto-discover the authenticated Redis URL and the sandbox-router auth token from
the toolkit's secrets.

**What the deploy does:**
1. Builds the agent image (into the containerd `k8s.io` namespace via `nerdctl`)
2. Creates the `coding-agent-maf` namespace
3. Auto-discovers the model, authenticated Redis URL, and sandbox-router token
4. Optionally installs a TLS secret from your cert/key files
5. Runs `helm upgrade --install` with your configuration and waits for readiness
6. Prints the access URL and the DevUI auth token

**Expected output:**
```
[OK]    Auto-discovered model: Qwen/Qwen2.5-Coder-14B-Instruct
[OK]    Coding Agent deployed — namespace: coding-agent-maf

  Access the Coding Agent:
    https://coding-agent.api.example.com      (or port-forward — see below)
    DevUI login token (enter in the browser login prompt): <token>
```

The agent's API is the OpenAI **Responses** API on port 8090 — see
[API Reference](#api-reference).

#### Try it — example prompts

Port-forward and open the DevUI:

```bash
kubectl port-forward -n coding-agent-maf svc/coding-agent 8090:8090
#   → http://localhost:8090   (enter the DevUI token printed by deploy.sh)
```

Type any of these to verify code execution in the sandbox works:

```text
Compute the sum of all prime numbers below 50, run it in the sandbox, and print the result.
Write a Python function for the nth Fibonacci number, run it, and print the 15th.
Install the 'sympy' package in the sandbox and list the first 10 primes with it.
```

**Healthy response:** the agent calls `execute_python_code`, runs it in a sandbox
pod, and reports the actual computed result (e.g. sum of primes below 50 = 328).

---

### Option B — Docker Compose (Local Dev)

Runs the agent and Redis locally. Sandbox execution requires the Agent Sandbox
controller to be running in a reachable Kubernetes cluster, with the sandbox
router port-forwarded locally.

```bash
# Port-forward the sandbox router from your cluster
kubectl port-forward -n agent-sandbox-system svc/sandbox-router-svc 8080:8080 &

# Set your LiteLLM URL and API key
export LITELLM_URL=https://api.example.com/v1
export LITELLM_API_KEY=sk-your-key
export SANDBOX_ROUTER_URL=http://localhost:8080

# Start the stack
docker compose up --build
```

The DevUI is available at **http://localhost:8090**.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `http://genai-gateway-service.genai-gateway.svc.cluster.local:4000/v1` | LLM endpoint (LiteLLM / OpenAI-compatible) |
| `OPENAI_API_KEY` | — | API key for the LLM endpoint (**required**) |
| `MODEL_NAME` | — | Model ID registered in LiteLLM (**required**) |
| `REDIS_URL` | `redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379` | Redis for persistent memory. **Must include credentials** — the shared Redis requires auth (see [config.env](../../config.env)) |
| `SANDBOX_ROUTER_URL` | `http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080` | Sandbox router in-cluster URL |
| `SANDBOX_NAMESPACE` | `agent-sandbox-system` | Namespace where sandbox pods are created |
| `SANDBOX_WARMPOOL` | `python-pool` | Warm pool to adopt pre-warmed sandboxes from |
| `SANDBOX_ROUTER_TOKEN` | — | Bearer token for the router when `ALLOW_UNAUTHENTICATED_ROUTER=false` |
| `SANDBOX_TEMPLATE` | `python-sandbox-template` | SandboxTemplate name to use |
| `WORKSPACE_DIR` | `/app/workspace` | Workspace volume mount path |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse public key (optional — enables tracing) |
| `LANGFUSE_SECRET_KEY` | — | Langfuse secret key (optional) |
| `LANGFUSE_HOST` | — | Langfuse URL (optional) — see [Observability guide](../../docs/observability-langfuse.md) |
| `DEVUI_AUTH_TOKEN` | — | Auth token for the DevUI web interface |
| `AUTO_OPEN_BROWSER` | `true` | Set to `false` in containers |

### Helm Values (key overrides)

```yaml
agent:
  modelName: "Qwen/Qwen2.5-Coder-14B-Instruct"     # required — must match /v1/models
  openaiApiKey: "your-key"                            # required
  openaiBaseUrl: "https://api.example.com/v1"
  devuiAuthToken: "your-token"                        # required for web UI
  sandboxNamespace: "agent-sandbox-system"
  sandboxTemplate: "python-sandbox-template"
  sandboxWarmPool: "python-pool"                      # warm pool to adopt from
  sandboxRouterUrl: "http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080"
  sandboxRouterToken: "your-router-token"             # when router auth is enabled

# Authenticated Redis URL (from redis-stack-server-credentials secret)
redisUrl: "redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379"

ingress:
  enabled: true
  host: "coding-agent.api.example.com"
  tls:
    enabled: true
    secretName: "coding-agent-tls"
```

Full values reference: [`helm-chart/values.yaml`](helm-chart/values.yaml)

---

## API Reference

The agent is served by **Agent Framework DevUI** on port **8090**, which exposes
the **OpenAI Responses API** (there is no `/v1/chat/completions` or `/v1/code`
route). All routes except `/health` require `Authorization: Bearer <DEVUI_AUTH_TOKEN>`
(the token `deploy.sh` prints).

### `GET /health`

Liveness check (JSON, no auth).

```bash
curl https://coding-agent.api.example.com/health
# {"status":"healthy","entities_count":1,"framework":"agent_framework"}
```

### `GET /v1/entities` — list agent entities

```bash
curl -s https://coding-agent.api.example.com/v1/entities \
  -H "Authorization: Bearer $DEVUI_AUTH_TOKEN"
```

Grab the `entities[0].id` — you pass it as `metadata.entity_id` below.

### `POST /v1/responses` — run the agent (generate + execute code)

```bash
AGENT=https://coding-agent.api.example.com
EID=$(curl -s "$AGENT/v1/entities" -H "Authorization: Bearer $DEVUI_AUTH_TOKEN" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['entities'][0]['id'])")

curl -s -X POST "$AGENT/v1/responses" \
  -H "Authorization: Bearer $DEVUI_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"input\":\"Find all primes up to 50 with the Sieve of Eratosthenes, run it, and print them.\",\"metadata\":{\"entity_id\":\"$EID\"}}"
```

The response is an OpenAI Responses object; the assistant text is in the
`output[]` items (`type: message` → `content[].text`), and tool calls appear as
`type: function_call` items (e.g. `execute_python_code`).

---

## Agent Tools

| Tool | Description |
|---|---|
| `execute_python_code` | Run Python in the sandbox pod; stdout/stderr returned |
| `install_package` | `pip install` a package in the sandbox (persists for the session) |
| `reset_sandbox` | Terminate the sandbox and start a fresh one |
| `write_file` | Write a file to the agent's workspace volume |
| `read_file` | Read a file from the workspace volume |
| `list_files` | List files in the workspace volume |

---

## Running Examples

All clients take the DevUI token via `DEVUI_AUTH_TOKEN` (printed by `deploy.sh`).

```bash
export DEVUI_AUTH_TOKEN=<token>

# Python client (standard library only — no extra deps)
python3 examples/generate_code.py https://coding-agent.api.example.com

# curl-based examples
bash examples/generate_code.sh https://coding-agent.api.example.com

# OpenAI SDK usage (Responses API; needs: pip install "openai>=1.66")
AGENT_URL=https://coding-agent.api.example.com python3 examples/openai_compat.py
```

---

## Memory

Conversation history is stored in Redis using
[RediSearch](https://redis.io/docs/stack/search/) vector indexing. Only **user
messages** are surfaced as injected memories to prevent stale assistant responses
from being re-injected as context.

Memory is scoped per `session_id`. Starting a new session (or omitting
`session_id`) creates a clean context.

---

## Extending

**Switch sandbox template** — deploy a custom `SandboxTemplate` with the runtime
image your use case needs (e.g. Node.js, R), then set `SANDBOX_TEMPLATE` and
`SANDBOX_NAMESPACE` accordingly.

**Add WarmPools** — apply a `SandboxWarmPool` CR referencing
`python-sandbox-template` in the `agent-sandbox-system` namespace for near-instant
sandbox creation.

**Enable Ray** — set `ray.enabled=true` and provide a `ray.address` in
`values.yaml` to enable distributed parallel task execution via KubeRay.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Pod in `CrashLoopBackOff` | Missing `OPENAI_API_KEY` or `MODEL_NAME` | Check `kubectl describe pod -n coding-agent-maf` and verify Helm values |
| `502 Bad Gateway` from ingress | Agent pod not ready yet | Wait 1–2 min; `kubectl logs -n coding-agent-maf -l app.kubernetes.io/name=coding-agent` |
| Sandbox pod not creating | Agent Sandbox controller not running | `kubectl get pods -n agent-sandbox-system`; re-run stack deploy with `deploy_agent_sandbox=on` |
| Redis connection refused | Redis not deployed or wrong URL | `kubectl get pods -n redis`; verify `REDIS_URL` value |
| `Syntax error` from Redis search | Special chars in query | This is handled automatically by the `SanitizedRedisContextProvider` in `agent.py` |
| Model not found in LiteLLM | `MODEL_NAME` not registered | List models: `curl -sk https://api.example.com/v1/models -H "Authorization: Bearer <key>"` |

### View logs

```bash
kubectl logs -n coding-agent-maf -l app.kubernetes.io/name=coding-agent -f
```

### Check pod status

```bash
kubectl describe pod -n coding-agent-maf -l app.kubernetes.io/name=coding-agent
```
