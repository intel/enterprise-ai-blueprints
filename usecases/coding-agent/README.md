# Coding Agent (LangGraph)

An AI-powered coding assistant that writes, executes, and debugs Python code,
built on **LangGraph**. It is the LangGraph counterpart of
[coding-agent-maf](../coding-agent-maf/) and exposes an **API only** (no web UI):
an OpenAI-compatible `/v1/chat/completions` endpoint and an OpenAI
Responses-style `/v1/responses` endpoint.

Code execution runs in isolated **Kubernetes sandbox pods** managed by the
[Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox) controller —
the agent process holds one sandbox pod, keeping installed packages and defined
variables alive across tool calls.

This use case runs on top of
[Intel® AI for Enterprise Solutions](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2)
with its [agent-toolkit layer](https://github.com/intel/enterprise-agent-toolkit/tree/release-2.0.0).

---

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
  - [Option A — Kubernetes (Cluster)](#option-a--kubernetes-cluster)
  - [Option B — Docker Compose (Local Dev)](#option-b--docker-compose-local-dev)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Agent Tools](#agent-tools)
- [Running Examples](#running-examples)
- [Memory](#memory)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)

---

## Architecture

```
API client (curl / OpenAI SDK)
        │  https://coding-agent-langgraph.<base_domain_name>
        │  Authorization: Bearer <DEVUI_AUTH_TOKEN>
        ▼
Envoy Gateway eg-gateway ──HTTPRoute──┐
  (envoy-gateway-system ns)           │
        ┌─────────────────────────────┘
        ▼
coding-agent-langgraph Pod ──(cluster DNS)──▶ sandbox-router-svc ──▶ Sandbox Pod
  (coding-agent ns, FastAPI :8090)          (agent-sandbox-system ns)  (python-pool,
        │                                                            public egress only)
        ├──▶ LiteLLM (litellm ns)               ──▶ (optional) KubeRay (ray-system)
        └──▶ Langfuse (langfuse ns, optional tracing)
```

- **LangGraph ReAct loop** (`create_react_agent`) drives tool selection and
  multi-step reasoning against **LiteLLM** (AI Solutions inference layer). The
  `coding-agent` namespace is in the Istio ambient mesh and must be listed in
  `litellm_allowed_namespaces`.
- **Agent Sandbox** — the agent talks to the **sandbox-router** over in-cluster
  DNS with the router's bearer token, adopting sandboxes from the `python-pool`
  warm pool (template `python-sandbox-template`). The chart adds a Role/RoleBinding
  in `agent-sandbox-system` so the agent's ServiceAccount can manage
  `sandboxclaims`.
- Sandbox pods (NetworkPolicy `eat-sandbox-pool-egress`) accept traffic only from
  the router and can reach public IPs only — code run there cannot call LiteLLM or
  Redis, and `install_package` needs direct internet access from the sandbox.
- **Langfuse** tracing is enabled through a LangChain `CallbackHandler` when
  Langfuse keys are present (`deploy.sh` auto-discovers them).
- **Ray** (optional) distributes `execute_python_code_parallel` tasks across
  KubeRay workers.

---

## Prerequisites

The following AI Solutions components must be running **before** deploying this
use case. See the [AI Solutions setup guide](../../docs/ai-solutions-setup.md).

| Component | Namespace | Switch (`env/<env>/config.agent-toolkit.yaml`) |
|---|---|---|
| Kubernetes, Istio, Envoy Gateway | — / `envoy-gateway-system` | installed by `./es_auto_installer.sh install agent-toolkit` |
| LiteLLM | `litellm` | always installed by the inference layer |
| Agent Sandbox controller + router | `agent-sandbox-system` | `agent_sandbox_enabled` |
| LLM model (tool-calling capable) | — | `llm_services_autodeploy_models` (default `["qwen2-5-coder-14b"]`) |
| LiteLLM access from this namespace | — | add `coding-agent` to `litellm_allowed_namespaces` |
| Langfuse (optional) | `langfuse` | installed by the inference layer |
| KubeRay (optional) | `ray-system` | `kuberay_enabled`, plus `kuberay_ray_image: rayproject/ray:2.56.0-py312` to match the agent's Ray client (~=2.56.0) |

Verify all prerequisites are up:

```bash
./es_auto_installer.sh validate agent-toolkit     # from the enterprise-ai-solutions checkout
kubectl get pods -n litellm
kubectl get pods -n agent-sandbox-system
kubectl get sandboxtemplate,sandboxwarmpool -n agent-sandbox-system
```

---

## Quick Start

### Option A — Kubernetes (Cluster)

```bash
# From the repo root
cp config.env config.local.env          # MODEL_NAME (e.g. qwen2-5-coder-14b); the rest is auto-discovered
set -a && source config.local.env && set +a
./deploy-usecase.sh coding-agent
```

Or using the use case's own deploy script:

```bash
cd usecases/coding-agent
./deploy.sh
```

**What the deploy does:**
1. Builds the agent image `coding-agent-langgraph:latest` (into the containerd
   `k8s.io` namespace via `nerdctl`; `SKIP_BUILD=true AGENT_IMAGE=<ref>` to use a
   pre-built image)
2. Creates the `coding-agent` namespace and labels it
   `istio.io/dataplane-mode=ambient` + Pod Security `restricted`; warns if it is
   not in `litellm_allowed_namespaces` (opt-in `PATCH_LITELLM_AUTHZ=true` patches
   the `litellm-ingress` policy directly as a stop-gap)
3. Auto-discovers `CLUSTER_URL`, the LiteLLM key, the Redis URL, the
   sandbox-router token and the Langfuse keys (prompts for `MODEL_NAME`)
4. Generates the API bearer token `DEVUI_AUTH_TOKEN` (unless you set one)
5. Runs `helm upgrade --install coding-agent-langgraph` with an HTTPRoute for
   `coding-agent-langgraph.<base_domain_name>` on `eg-gateway` (TLS is terminated
   at the gateway — no TLS secret needed) and waits for readiness
6. Prints the access URL and the API bearer token

> The Helm release, Service and host are `coding-agent-langgraph` —
> `coding-agent.<base_domain_name>` belongs to [coding-agent-maf](../coding-agent-maf/).

**Expected output:**
```
[OK]    Coding Agent (LangGraph) deployed — namespace: coding-agent

[INFO]  Access the Coding Agent:
[INFO]    https://coding-agent-langgraph.solutions.ai
[INFO]    (or locally: kubectl port-forward -n coding-agent svc/coding-agent-langgraph 8090:8090 → http://localhost:8090)
[INFO]    API bearer token (Authorization: Bearer <token> on /v1/*):
[INFO]    <token>
```

To use the external URL, add `<eg-gateway-ip> coding-agent-langgraph.<base_domain_name>`
to `/etc/hosts` and trust `env/local/logs/ai-solutions-ca.crt` (or use `curl -k`) —
see [ai-solutions-setup.md](../../docs/ai-solutions-setup.md#3-reach-the-gateway-hostnames--ca).
The route ships an empty SecurityPolicy so the gateway does not demand a LiteLLM
key; the `DEVUI_AUTH_TOKEN` bearer token protects `/v1/*` instead.

#### Try it

```bash
export AUTH_TOKEN=<token printed by deploy.sh>
AGENT=https://coding-agent-langgraph.solutions.ai

curl -sk "$AGENT/health"
curl -sk -X POST "$AGENT/v1/responses" \
  -H "Authorization: Bearer $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"input":"Compute the sum of all prime numbers below 50, run it in the sandbox, and print the result.","metadata":{"thread_id":"demo"}}'
```

**Healthy response:** the `output[]` contains a `function_call_output` item from
`execute_python_code` and an assistant `message` reporting the computed result
(sum of primes below 50 = 328).

---

### Option B — Docker Compose (Local Dev)

Runs the agent (and a Redis container) locally. Sandbox execution requires the
Agent Sandbox controller to be running in a reachable Kubernetes cluster, with the
sandbox router port-forwarded locally.

```bash
# Port-forward the sandbox router from your cluster
kubectl port-forward -n agent-sandbox-system svc/sandbox-router-svc 8080:8080 &

# LiteLLM URL and key (a LiteLLM virtual key; trust the gateway CA or expect TLS
# errors with the default self-signed certificate)
export LITELLM_URL=https://litellm.solutions.ai/v1
export LITELLM_API_KEY=sk-your-litellm-key
export MODEL_NAME=qwen2-5-coder-14b
export SANDBOX_ROUTER_URL=http://localhost:8080

./deploy-usecase.sh coding-agent --mode docker     # from the repo root
# or: cd usecases/coding-agent && docker compose up --build -d

curl http://localhost:8090/health
```

Set `SANDBOX_ROUTER_TOKEN` (from `agent-sandbox-system/sandbox-router-auth`) and,
optionally, `DEVUI_AUTH_TOKEN` in `config/agent-config.env`. With
`DEVUI_AUTH_TOKEN` empty the API accepts unauthenticated requests — fine for
local dev only.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `http://litellm.litellm.svc.cluster.local:4000/v1` | LiteLLM OpenAI-compatible URL; auto-discovered |
| `OPENAI_API_KEY` | auto-discovered | LiteLLM key (**required**; falls back to `litellm-master-key` — a virtual key is recommended) |
| `MODEL_NAME` | `qwen2-5-coder-14b` (chart) | Model ID registered in LiteLLM; must support tool calling |
| `DEVUI_AUTH_TOKEN` | auto-generated by `deploy.sh` | Bearer token required on all `/v1/*` routes (empty = no auth) |
| `DEVUI_PORT` / `DEVUI_HOST` | `8090` / `0.0.0.0` | API server port / bind address |
| `SANDBOX_ROUTER_URL` | `http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080` | Sandbox router in-cluster URL |
| `SANDBOX_ROUTER_TOKEN` | auto-discovered | Router bearer token (secret `agent-sandbox-system/sandbox-router-auth`, key `ROUTER_AUTH_TOKEN`) |
| `SANDBOX_NAMESPACE` | `agent-sandbox-system` | Namespace where sandbox claims are created |
| `SANDBOX_TEMPLATE` | `python-sandbox-template` | SandboxTemplate name |
| `SANDBOX_WARMPOOL` | `python-pool` | Warm pool to adopt pre-warmed sandboxes from |
| `REDIS_URL` | auto-discovered (`redis-stack-server-credentials`) | Passed to the agent for checkpointing — see [Memory](#memory) (currently unused) |
| `RAY_ENABLED` / `RAY_ADDRESS` | `false` / — | Ray parallel execution, e.g. `ray://ray-cluster-head-svc.ray-system.svc.cluster.local:10001` |
| `WORKSPACE_DIR` | `/app/workspace` | Workspace volume (5Gi PVC) for the file tools |
| `CLUSTER_URL` | auto-discovered | AI Solutions `base_domain_name`; route host is `coding-agent-langgraph.<CLUSTER_URL>` |
| `NAMESPACE` / `INGRESS_HOST` | `coding-agent` / `coding-agent-langgraph.<CLUSTER_URL>` | `deploy.sh` overrides |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | auto-discovered | Langfuse keys from `langfuse/langfuse-secrets` (optional — enables tracing) |
| `LANGFUSE_HOST` | `http://langfuse-web.langfuse.svc.cluster.local:3000` | Langfuse URL — see [Observability guide](../../docs/observability-langfuse.md) |

### Helm Values (key overrides)

```yaml
agent:
  modelName: "qwen2-5-coder-14b"                    # must match /v1/models
  openaiApiKey: "your-litellm-key"                  # required
  openaiBaseUrl: "http://litellm.litellm.svc.cluster.local:4000/v1"
  devuiAuthToken: "your-token"                      # required — bearer token for /v1/*
  sandboxNamespace: "agent-sandbox-system"
  sandboxTemplate: "python-sandbox-template"
  sandboxWarmPool: "python-pool"
  sandboxRouterUrl: "http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080"
  sandboxRouterToken: "your-router-token"
  ray:
    enabled: false
    address: ""          # ray://ray-cluster-head-svc.ray-system.svc.cluster.local:10001 (kuberay_enabled)
  langfuse:
    publicKey: ""
    secretKey: ""
    host: ""             # http://langfuse-web.langfuse.svc.cluster.local:3000

serviceAccount:
  create: true
  sandboxAccess: true    # Role/RoleBinding for sandboxclaims in agent-sandbox-system

httpRoute:
  enabled: true
  host: "coding-agent-langgraph.solutions.ai"
  gateway:
    name: eg-gateway
    namespace: envoy-gateway-system
    sectionName: https
  bypassGatewayAuth: true     # empty route-level SecurityPolicy; the bearer token protects the API
```

Full values reference: [`helm-chart/values.yaml`](helm-chart/values.yaml)

---

## API Reference

FastAPI server on port **8090**. All `/v1/*` routes require
`Authorization: Bearer <DEVUI_AUTH_TOKEN>` (the token `deploy.sh` prints);
`/health` is public. There is no web UI.

### `GET /health`

```bash
curl -sk https://coding-agent-langgraph.solutions.ai/health
# {"status":"healthy","entities_count":1,"framework":"langgraph"}
```

### `GET /v1/entities`

Agent introspection — returns the agent id, framework, model and tool list.

```bash
curl -sk https://coding-agent-langgraph.solutions.ai/v1/entities \
  -H "Authorization: Bearer $AUTH_TOKEN"
```

### `POST /v1/chat/completions`

OpenAI-compatible chat endpoint that runs the full agent (with its sandbox tools).
Supports streaming (`"stream": true`, SSE). The `model` field is informational;
the agent uses `MODEL_NAME`. Pass `session_id` to keep a conversation thread.

```bash
curl -sk -X POST https://coding-agent-langgraph.solutions.ai/v1/chat/completions \
  -H "Authorization: Bearer $AUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Write a fibonacci function, run it, and print the 15th number"}],"session_id":"demo"}'
```

### `POST /v1/responses`

OpenAI Responses-style endpoint. Body: `{"input": "...", "metadata": {"thread_id": "..."}, "stream": false}`.
The conversation thread is `metadata.thread_id` (falls back to
`metadata.entity_id`, then `default`). The response `output[]` contains
`message` items (assistant text) and `function_call_output` items (tool results);
with `"stream": true` it emits SSE events (`response.output_text.delta`,
`response.function_call_output`, `response.completed`, …).

---

## Agent Tools

| Tool | Description |
|---|---|
| `execute_python_code` | Run Python in the sandbox pod; stdout/stderr returned |
| `execute_python_code_parallel` | Distribute N snippets across Ray workers (falls back to sequential local execution in the agent pod) |
| `install_package` | `pip install` a package in the sandbox (persists for the sandbox lifetime) |
| `reset_sandbox` | Terminate the sandbox; a fresh one is created on next use |
| `write_file` | Write a file to the agent's workspace volume |
| `read_file` | Read a file from the workspace volume |
| `list_files` | List files in the workspace volume |

---

## Running Examples

All clients read the bearer token from `AUTH_TOKEN` and default to
`http://localhost:8090` (port-forward). Through the gateway, disable verification
of the self-signed certificate (`CURL_OPTS=-k` / `VERIFY=false`) or point it at
the CA (`CURL_OPTS="--cacert env/local/logs/ai-solutions-ca.crt"` /
`VERIFY=env/local/logs/ai-solutions-ca.crt`).

```bash
export AUTH_TOKEN=<token printed by deploy.sh>

# curl-based examples (/health, /v1/responses, /v1/chat/completions)
CURL_OPTS=-k bash examples/generate_code.sh https://coding-agent-langgraph.solutions.ai

# Python client (pip install httpx)
VERIFY=false python3 examples/generate_code.py https://coding-agent-langgraph.solutions.ai

# OpenAI SDK against /v1/chat/completions (pip install openai) — via port-forward
kubectl port-forward -n coding-agent svc/coding-agent-langgraph 8090:8090 &
AGENT_URL=http://localhost:8090 python3 examples/openai_compat.py
```

---

## Memory

Conversation state is kept per thread (`session_id` / `metadata.thread_id`) by a
LangGraph checkpointer. **Known limitation:** the checkpointer is the in-memory
`InMemorySaver` — the agent tries a Redis checkpointer first, but that needs the
`langgraph-checkpoint-redis` package, which is not installed in the image, so it
falls back to memory. Conversation history therefore lives only in the agent
process: it is lost on pod restart and not shared between replicas. `REDIS_URL`
is wired through for when the Redis checkpointer is added.

The **sandbox** is also per process: one sandbox pod is shared by all sessions of
an agent pod until `reset_sandbox` is called or the pod restarts.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Pod in `CrashLoopBackOff` / Helm error `agent.openaiApiKey is required` | Missing `OPENAI_API_KEY` or `DEVUI_AUTH_TOKEN` | Check `kubectl describe pod -n coding-agent` and the Helm values |
| `401 Missing or invalid Authorization header` | No/wrong bearer token on `/v1/*` | Send `Authorization: Bearer <DEVUI_AUTH_TOKEN>`; recover it with `kubectl get secret -n coding-agent coding-agent-langgraph-secret -o jsonpath='{.data.DEVUI_AUTH_TOKEN}' \| base64 -d` |
| `401` from the gateway (before reaching the app) | Gateway-wide `gateway-auth` applied to the route | Ensure `httpRoute.bypassGatewayAuth=true` (`kubectl get securitypolicy -n coding-agent`) |
| LLM calls fail with `RBAC: access denied` / connection reset | Namespace not allowed to reach LiteLLM | Add `coding-agent` to `litellm_allowed_namespaces` and re-run `./es_auto_installer.sh install agent-toolkit` (or redeploy with `PATCH_LITELLM_AUTHZ=true`); check `kubectl get ns coding-agent --show-labels` has `istio.io/dataplane-mode=ambient` |
| Model not found in LiteLLM | `MODEL_NAME` not registered | `curl -sk https://litellm.solutions.ai/v1/models -H "Authorization: Bearer <key>"` |
| `Error executing code in sandbox` | Agent Sandbox not running, router token wrong, or RBAC missing | `kubectl get pods -n agent-sandbox-system`; check `SANDBOX_ROUTER_TOKEN` and `kubectl get role,rolebinding -n agent-sandbox-system \| grep coding-agent-langgraph`; set `agent_sandbox_enabled` and re-run the platform install |
| `install_package` times out in the sandbox | Sandbox egress is public-IPs-only, no proxy | The sandbox needs direct internet access for `pip install` |
| Conversation context lost after restart | In-memory checkpointer (known limitation) | See [Memory](#memory) |
| Ray tasks run sequentially | `ray.enabled=false` or Ray unreachable | Set `agent.ray.enabled=true` and `agent.ray.address`; requires `kuberay_enabled` |
| No traces in Langfuse | Keys not found at deploy time | `kubectl get secret langfuse-secrets -n langfuse`; set `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` and redeploy |
| Pod rejected with `violates PodSecurity "restricted"` | Custom values re-added privileged settings | Keep the chart's `securityContext` (UID/GID 10001, drop ALL, seccomp RuntimeDefault) |

### View logs

```bash
kubectl logs -n coding-agent -l app.kubernetes.io/instance=coding-agent-langgraph -f
```

---

## Project Structure

```
coding-agent/
├── config/
│   └── agent-config.env       # Docker Compose environment configuration
├── deploy.sh                  # Kubernetes deploy script (AI Solutions)
├── docker-compose.yml         # Local development stack
├── docs/
│   ├── deployment.md          # Detailed deployment guide
│   └── implementation.md      # Architecture and implementation details
├── examples/
│   ├── generate_code.py       # Python client (httpx) — /v1/responses + /v1/chat/completions
│   ├── generate_code.sh       # curl examples
│   └── openai_compat.py       # OpenAI SDK against /v1/chat/completions
├── helm-chart/                # Deployment, Service, ConfigMap, Secret, PVC, RBAC, HTTPRoute, SecurityPolicy
└── src/
    ├── agent.py               # LangGraph ReAct agent definition + checkpointer
    ├── tools.py               # Tools (sandbox, Ray, file ops)
    ├── main.py                # FastAPI server (OpenAI-compatible endpoints, auth, Langfuse)
    ├── Dockerfile             # Container image (UID 10001)
    └── requirements.txt       # Python dependencies
```
