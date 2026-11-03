# Coding Agent

An AI-powered coding assistant that writes, executes, and debugs Python code.

Code execution runs in isolated **Kubernetes sandbox pods** managed by the
[Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox) controller —
each agent session gets its own sandbox pod, keeping installed packages and
defined variables alive across tool calls.

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
        │  https://coding-agent.<base_domain_name>
        ▼
Envoy Gateway eg-gateway ──HTTPRoute──┐
  (envoy-gateway-system ns)           │
        ┌─────────────────────────────┘
        ▼
Coding Agent Pod  ──(cluster DNS)──▶  sandbox-router-svc  ──▶  Sandbox Pod
  (coding-agent-maf ns)             (agent-sandbox-system ns)      (ephemeral,
        │                                                       public egress only)
        ▼
LiteLLM                           Redis (memory)
  (litellm ns)                     (redis ns)
```

- The coding agent talks to **LiteLLM** (AI Solutions inference layer) for all LLM
  inference. The `coding-agent-maf` namespace is in the Istio ambient mesh and
  must be listed in `litellm_allowed_namespaces`.
- The coding agent calls the **sandbox-router** over in-cluster DNS — no
  port-forward or extra RBAC needed on the coding-agent pod.
- The sandbox-router owns all Kubernetes API interactions (create/delete SandboxClaims).
- Sandbox pods (NetworkPolicy `eat-sandbox-pool-egress`) accept traffic only from
  the router and can reach public IPs only — code run there cannot call LiteLLM or
  Redis, and `install_package` needs direct internet access from the sandbox.
- Each session reuses one sandbox pod — state (installed packages, defined variables,
  `/tmp` files) persists across `execute_python_code` and `install_package` calls.
  Call `reset_sandbox()` to start fresh.

---

## Prerequisites

The following AI Solutions components must be running **before** deploying this
use case. See the [AI Solutions setup guide](../../docs/ai-solutions-setup.md).

| Component | Namespace | Switch (`env/<env>/config.agent-toolkit.yaml`) |
|---|---|---|
| Kubernetes, Istio, Envoy Gateway | — / `envoy-gateway-system` | installed by `./es_auto_installer.sh install agent-toolkit` |
| LiteLLM | `litellm` | always installed by the inference layer |
| Redis Stack | `redis` | `redis_stack_enabled` |
| Agent Sandbox controller + router | `agent-sandbox-system` | `agent_sandbox_enabled` |
| LLM model | — | `llm_services_autodeploy_models` (default `["qwen2-5-coder-14b"]`) |
| LiteLLM access from this namespace | — | add `coding-agent-maf` to `litellm_allowed_namespaces` |

Verify all prerequisites are up:

```bash
./es_auto_installer.sh validate agent-toolkit     # from the enterprise-ai-solutions checkout
kubectl get pods -n litellm
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
auto-discover the base domain, LiteLLM URL and key, the authenticated Redis URL,
the sandbox-router auth token and the Langfuse keys from the platform's secrets.

**What the deploy does:**
1. Builds the agent image (into the containerd `k8s.io` namespace via `nerdctl`)
2. Creates the `coding-agent-maf` namespace and labels it
   `istio.io/dataplane-mode=ambient` + Pod Security `restricted`; warns if it is
   not in `litellm_allowed_namespaces` (opt-in `PATCH_LITELLM_AUTHZ=true` patches
   the `litellm-ingress` policy directly as a stop-gap)
3. Auto-discovers `CLUSTER_URL`, LiteLLM URL/key, authenticated Redis URL,
   sandbox-router token and Langfuse keys (prompts for `MODEL_NAME`)
4. Exposes the DevUI via an HTTPRoute on `eg-gateway` (TLS is terminated at the
   gateway — no TLS secret needed)
5. Runs `helm upgrade --install` with your configuration and waits for readiness
6. Prints the access URL and the DevUI auth token

**Expected output:**
```
[OK]    Coding Agent deployed — namespace: coding-agent-maf

  Access the Coding Agent:
    https://coding-agent.solutions.ai      (or port-forward — see below)
    DevUI login token (enter in the browser login prompt): <token>
```

To use the external URL, add `<eg-gateway-ip> coding-agent.<base_domain_name>` to
`/etc/hosts` and trust `env/local/logs/ai-solutions-ca.crt` (or use `curl -k`) —
see [ai-solutions-setup.md](../../docs/ai-solutions-setup.md#3-reach-the-gateway-hostnames--ca).
The route ships an empty SecurityPolicy so the gateway does not demand a LiteLLM
key; the DevUI token protects it instead.

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

# Set your LiteLLM URL and API key (a LiteLLM virtual key; trust the gateway CA or
# expect TLS errors with the default self-signed certificate)
export LITELLM_URL=https://litellm.solutions.ai/v1
export LITELLM_API_KEY=sk-your-litellm-key
export MODEL_NAME=qwen2-5-coder-14b
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
| `OPENAI_BASE_URL` | `http://litellm.litellm.svc.cluster.local:4000/v1` | LLM endpoint (LiteLLM / OpenAI-compatible); auto-discovered |
| `OPENAI_API_KEY` | — | LiteLLM key (**required**; falls back to `litellm-master-key` — a virtual key is recommended) |
| `MODEL_NAME` | — | Model ID registered in LiteLLM, e.g. `qwen2-5-coder-14b` (**required**) |
| `REDIS_URL` | `redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379` | Redis for persistent memory. **Must include credentials** — the shared Redis requires auth (see [config.env](../../config.env)) |
| `SANDBOX_ROUTER_URL` | `http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080` | Sandbox router in-cluster URL |
| `SANDBOX_NAMESPACE` | `agent-sandbox-system` | Namespace where sandbox pods are created |
| `SANDBOX_WARMPOOL` | `python-pool` | Warm pool to adopt pre-warmed sandboxes from |
| `SANDBOX_ROUTER_TOKEN` | — | Bearer token for the router when `ALLOW_UNAUTHENTICATED_ROUTER=false` |
| `SANDBOX_TEMPLATE` | `python-sandbox-template` | SandboxTemplate name to use |
| `WORKSPACE_DIR` | `/app/workspace` | Workspace volume mount path |
| `CLUSTER_URL` | auto-discovered | AI Solutions `base_domain_name`; route host is `coding-agent.<CLUSTER_URL>` |
| `LANGFUSE_PUBLIC_KEY` | auto-discovered | Langfuse public key (optional — enables tracing; from `langfuse-secrets`) |
| `LANGFUSE_SECRET_KEY` | — | Langfuse secret key (optional) |
| `LANGFUSE_HOST` | `http://langfuse-web.langfuse.svc.cluster.local:3000` | Langfuse URL (optional) — see [Observability guide](../../docs/observability-langfuse.md) |
| `DEVUI_AUTH_TOKEN` | — | Auth token for the DevUI web interface |
| `AUTO_OPEN_BROWSER` | `true` | Set to `false` in containers |

### Helm Values (key overrides)

```yaml
agent:
  modelName: "qwen2-5-coder-14b"                    # required — must match /v1/models
  openaiApiKey: "your-litellm-key"                    # required
  openaiBaseUrl: "http://litellm.litellm.svc.cluster.local:4000/v1"
  devuiAuthToken: "your-token"                        # required for web UI
  sandboxNamespace: "agent-sandbox-system"
  sandboxTemplate: "python-sandbox-template"
  sandboxWarmPool: "python-pool"                      # warm pool to adopt from
  sandboxRouterUrl: "http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080"
  sandboxRouterToken: "your-router-token"             # when router auth is enabled

# Authenticated Redis URL (from redis-stack-server-credentials secret)
redisUrl: "redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379"

httpRoute:
  enabled: true
  host: "coding-agent.solutions.ai"                   # coding-agent.<base_domain_name>
  gateway:
    name: eg-gateway
    namespace: envoy-gateway-system
    sectionName: https
  bypassGatewayAuth: true     # empty route-level SecurityPolicy; DevUI token protects the app

affinity: {}                  # no node affinity required
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
curl -k https://coding-agent.solutions.ai/health
# {"status":"healthy","entities_count":1,"framework":"agent_framework"}
```

### `GET /v1/entities` — list agent entities

```bash
curl -s https://coding-agent.solutions.ai/v1/entities \
  -H "Authorization: Bearer $DEVUI_AUTH_TOKEN"
```

Grab the `entities[0].id` — you pass it as `metadata.entity_id` below.

### `POST /v1/responses` — run the agent (generate + execute code)

```bash
AGENT=https://coding-agent.solutions.ai
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
python3 examples/generate_code.py https://coding-agent.solutions.ai

# curl-based examples
bash examples/generate_code.sh https://coding-agent.solutions.ai

# OpenAI SDK usage (Responses API; needs: pip install "openai>=1.66")
AGENT_URL=https://coding-agent.solutions.ai python3 examples/openai_compat.py
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
`values.yaml` to enable distributed parallel task execution via KubeRay
(requires `kuberay_enabled` in `config.agent-toolkit.yaml`; KubeRay runs in `ray-system`).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Pod in `CrashLoopBackOff` | Missing `OPENAI_API_KEY` or `MODEL_NAME` | Check `kubectl describe pod -n coding-agent-maf` and verify Helm values |
| `502`/`503` from the gateway | Agent pod not ready yet | Wait 1–2 min; `kubectl logs -n coding-agent-maf -l app.kubernetes.io/name=coding-agent` |
| Sandbox pod not creating | Agent Sandbox controller not running | `kubectl get pods -n agent-sandbox-system`; set `agent_sandbox_enabled` in `config.agent-toolkit.yaml` and re-run `./es_auto_installer.sh install agent-toolkit` |
| Redis connection refused | Redis not deployed or wrong URL | `kubectl get pods -n redis`; verify `REDIS_URL` value |
| `Syntax error` from Redis search | Special chars in query | This is handled automatically by the `SanitizedRedisContextProvider` in `agent.py` |
| Model not found in LiteLLM | `MODEL_NAME` not registered | List models: `curl -sk https://litellm.solutions.ai/v1/models -H "Authorization: Bearer <key>"` |
| LLM calls fail with `RBAC: access denied` / connection reset | Namespace not allowed to reach LiteLLM | Add `coding-agent-maf` to `litellm_allowed_namespaces` and re-run `./es_auto_installer.sh install agent-toolkit` (or redeploy with `PATCH_LITELLM_AUTHZ=true`); check `kubectl get ns coding-agent-maf --show-labels` has `istio.io/dataplane-mode=ambient` |
| `401` from the gateway on `https://coding-agent.<base>` | Gateway-wide `gateway-auth` applied to the route | Ensure `httpRoute.bypassGatewayAuth=true` (route-level SecurityPolicy present: `kubectl get securitypolicy -n coding-agent-maf`) |
| `install_package` times out in the sandbox | Sandbox egress is public-IPs-only, no proxy | The sandbox needs direct internet access for `pip install` |
| Pod rejected with `violates PodSecurity "restricted"` | Custom values re-added privileged settings | Keep the chart's `securityContext` (UID/GID 10001, drop ALL, seccomp RuntimeDefault) |

### View logs

```bash
kubectl logs -n coding-agent-maf -l app.kubernetes.io/name=coding-agent -f
```

### Check pod status

```bash
kubectl describe pod -n coding-agent-maf -l app.kubernetes.io/name=coding-agent
```
