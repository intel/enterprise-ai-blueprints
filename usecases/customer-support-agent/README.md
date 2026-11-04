# TechGadgets.com AI Customer Support Agent

An AI-powered customer support agent for TechGadgets.com, a fictional premium
online electronics store. The agent handles order management, returns, product
search, warranty claims, promotions, shipping inquiries, and escalation — all
backed by **40 callable tools**, a SQLite database with seeded demo data,
**long-term semantic memory** (RediSearch vector store + LiteLLM embeddings), and
**parallel tool execution** (in-process thread pool or the agent-toolkit's
KubeRay cluster).

This use case runs on top of
[Intel® AI for Enterprise Solutions](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2)
with its [agent-toolkit layer](https://github.com/intel/enterprise-agent-toolkit/tree/release-2.0.0).

> ⚠️ **No user authentication — demo data only.** The UI and API have no login
> and the chart's HTTPRoute bypasses the gateway's LiteLLM-key check, so anyone
> who can resolve `customer-support-agent.<base_domain_name>` can chat with the
> agent, call every tool directly (`POST /api/v1/tools/{name}`), and spend LLM
> quota. All customers, orders and payments are synthetic. Set
> `httpRoute.bypassGatewayAuth=false` to make the gateway require a LiteLLM key on
> every request (this also blocks plain browser access), or keep the release on
> `kubectl port-forward` only. Never connect it to real customer data.

---

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
  - [Option A — Kubernetes (Cluster)](#option-a--kubernetes-cluster)
  - [Option B — Docker Compose (Local Dev)](#option-b--docker-compose-local-dev)
- [Configuration](#configuration)
- [API Endpoints](#api-endpoints)
- [Validation](#validation)
- [Troubleshooting](#troubleshooting)
- [Tool Categories](#tool-categories-40-tools)

---

## Architecture

```
User / Browser
        │  https://customer-support-agent.<base_domain_name>
        ▼
Envoy Gateway eg-gateway ──HTTPRoute──┬── /api, /v1 ──▶ FastAPI      :8502
  (envoy-gateway-system ns)           └── /  (rest)  ──▶ Streamlit UI :8501
                                                │
                       customer-support-agent Pod (customer-support-agent ns)
                       ┌────────────────────────┴───────────────────────────┐
                       │ Agent loop ── Tool registry (40 tools) ── SQLite   │
                       │     │            parallel executor      /app/data  │
                       └─────┼──────────────────┼────────────────────────────┘
                             │                  │ (optional, TOOL_EXECUTOR=ray)
             ┌───────────────┼──────────┐       ▼
             ▼               ▼          │   KubeRay (ray-system)
          LiteLLM          Redis Stack  │
         (litellm ns)      (redis ns)   │
     chat model +        RediSearch     │
     bge-base-en         memory index ◀─┘
     embeddings
```

**Agent Loop:** User query → (recall memory) → LLM call → parse response for one
or more `<tool_call>` blocks → execute tools **in parallel** → inject results →
repeat until final text response (max 50 turns) → (store memory).

Tool calling is **prompt-parsed**: the tool catalogue and the `<tool_call>{json}</tool_call>`
format are described in the system prompt and parsed from the model's text, so
the model does not need native function-calling support — any chat model
registered in LiteLLM works (default `qwen2-5-coder-14b`).

### Long-term memory

Each exchange is summarized, embedded via LiteLLM (model `bge-base-en` — the AI
Solutions catalog name for `BAAI/bge-base-en-v1.5`, 768 dims), and stored
per-customer as a vector in the shared `redis-stack-server` (RediSearch,
HNSW/cosine, index `csa_mem_idx`, keys `csa:mem:*`). On each new message, the
most relevant past interactions for that customer are retrieved and injected into
the system prompt — so the agent remembers preferences across separate
conversations.

The embedding model is **not deployed by default**: add `"bge-base-en"` to
`llm_services_autodeploy_models` in `env/<env>/config.agent-toolkit.yaml` and
re-run `./es_auto_installer.sh install agent-toolkit`. Memory is **fail-open**:
without the embedding model, or if Redis is unreachable, the agent runs normally
without memory (`memory_enabled: false` in `/api/v1/health`, or embed errors in
the logs).

### Parallel tool execution

The model may emit **multiple independent `<tool_call>` blocks in a single turn**
(e.g. "compare these three products"); they are executed concurrently instead of
one-per-turn. Two backends, selected by `TOOL_EXECUTOR`:

- **`local`** (default) — `ThreadPoolExecutor`, in-process. Best latency for these
  tools (local SQLite reads are sub-millisecond).
- **`ray`** — dispatches each tool call as a task to the agent-toolkit's
  **KubeRay** cluster (`kuberay_enabled`). Because the tools read a SQLite DB that
  lives only in the agent pod, each Ray task calls back to the agent's own
  `/api/v1/tools/{name}` endpoint over in-cluster DNS. This genuinely exercises
  KubeRay (tasks appear in the Ray dashboard) but is a **toolkit showcase, not a
  speedup** — it adds a network hop. Auto-falls back to `local` if the Ray cluster
  is unreachable.

### Demo data

The SQLite DB lives at `/app/data/techgadgets.db` on an `emptyDir` volume. It is
created and seeded at startup with a fixed random seed, so every restart produces
the **same** deterministic dataset (100 customers `CUST-0001`…`CUST-0100`, 33
products, 500 orders) — and any changes made through the tools (returns, address
updates, tickets) are lost on restart.

---

## Prerequisites

The following AI Solutions components must be running **before** deploying this
use case. See the [AI Solutions setup guide](../../docs/ai-solutions-setup.md).

| Component | Namespace | Switch (`env/<env>/config.agent-toolkit.yaml`) |
|---|---|---|
| Kubernetes, Istio, Envoy Gateway | — / `envoy-gateway-system` | installed by `./es_auto_installer.sh install agent-toolkit` |
| LiteLLM | `litellm` | always installed by the inference layer |
| Chat model | — | `llm_services_autodeploy_models` (default `["qwen2-5-coder-14b"]`) |
| Embedding model `bge-base-en` (memory) | — | add `"bge-base-en"` to `llm_services_autodeploy_models` (optional — memory fails open) |
| Redis Stack (memory) | `redis` | `redis_stack_enabled` |
| KubeRay (optional) | `ray-system` | `kuberay_enabled` (only for `TOOL_EXECUTOR=ray`) |
| LiteLLM access from this namespace | — | add `customer-support-agent` to `litellm_allowed_namespaces` |

Verify the prerequisites:

```bash
./es_auto_installer.sh validate agent-toolkit     # from the enterprise-ai-solutions checkout
kubectl get pods -n litellm
kubectl get pods -n redis
# the chat and embedding model names must both appear:
curl -sk https://litellm.solutions.ai/v1/models -H "Authorization: Bearer <key>" | python3 -m json.tool
```

---

## Quick Start

### Option A — Kubernetes (Cluster)

```bash
# From the repo root
cp config.env config.local.env          # optionally MODEL_NAME / OPENAI_API_KEY (LiteLLM virtual key)
set -a && source config.local.env && set +a
./deploy-usecase.sh customer-support-agent
```

Or using the use case's own deploy script:

```bash
cd usecases/customer-support-agent
./deploy.sh
```

**What the deploy does:**
1. Builds the image (into the containerd `k8s.io` namespace via `nerdctl`;
   `SKIP_BUILD=true AGENT_IMAGE=<ref>` to use a pre-built image)
2. Creates the `customer-support-agent` namespace and labels it
   `istio.io/dataplane-mode=ambient` + Pod Security `restricted`; warns if it is
   not in `litellm_allowed_namespaces` (opt-in `PATCH_LITELLM_AUTHZ=true` patches
   the `litellm-ingress` policy directly as a stop-gap)
3. Auto-discovers `CLUSTER_URL`, the LiteLLM key (`litellm-master-key`; a virtual
   key is recommended) and the authenticated Redis URL
   (`redis-stack-server-credentials`); prompts for the chat model (default
   `qwen2-5-coder-14b`)
4. Exposes the app via an HTTPRoute on `eg-gateway` (TLS is terminated at the
   gateway — no TLS secret needed): `/api` and `/v1` → FastAPI (8502), everything
   else → Streamlit UI (8501)
5. Runs `helm upgrade --install customer-support-agent` and waits for readiness
6. Prints the access URLs and a no-authentication warning

**Expected output:**
```
[OK]    Customer Support Agent deployed — namespace: customer-support-agent

  Access the Customer Support Agent:
    Streamlit UI: https://customer-support-agent.solutions.ai/
    Chat UI/API:  https://customer-support-agent.solutions.ai/api/v1/health
[WARN]  No user authentication: anyone who can reach customer-support-agent.solutions.ai can use the agent (demo data only).
```

To use the external URL, add `<eg-gateway-ip> customer-support-agent.<base_domain_name>`
to `/etc/hosts` and trust `env/local/logs/ai-solutions-ca.crt` (or use `curl -k`) —
see [ai-solutions-setup.md](../../docs/ai-solutions-setup.md#3-reach-the-gateway-hostnames--ca).

Or locally:

```bash
kubectl port-forward -n customer-support-agent svc/customer-support-agent 8501:8501 8502:8502
#   Streamlit UI:        http://localhost:8501
#   HTML chat UI + API:  http://localhost:8502   (the HTML UI at / is reachable via port-forward only —
#                                                 on the gateway host, / is the Streamlit UI)
```

#### Try it — example prompts

```text
Hi, I'm customer CUST-0001. Can you list my recent orders?
Compare PROD-0006, PROD-0008 and PROD-0017 by price and rating.
Is order ORD-2024-000013 still eligible for a return?
What is your warranty policy for laptops?
```

**Healthy response:** the agent calls tools (e.g. `list_customer_orders`,
`compare_products`) and answers with concrete order IDs, products and prices from
the demo data.

### Option B — Docker Compose (Local Dev)

Runs the agent and a local Redis Stack.

```bash
# LiteLLM URL and key (a LiteLLM virtual key; trust the gateway CA or expect TLS
# errors with the default self-signed certificate)
export LITELLM_URL=https://litellm.solutions.ai/v1
export LITELLM_API_KEY=sk-your-litellm-key
export MODEL_NAME=qwen2-5-coder-14b

./deploy-usecase.sh customer-support-agent --mode docker     # from the repo root
# or: cd usecases/customer-support-agent && docker compose up --build -d

#   Streamlit UI: http://localhost:8501
#   FastAPI API:  http://localhost:8502   (health: /api/v1/health)
```

Other settings (memory, executor, embedding model) come from
`config/agent-config.env`.

---

## Configuration

All configuration is read from environment variables (`src/config.py`), wired via
the Helm `configmap.yaml` / `secret.yaml` in Kubernetes and
`config/agent-config.env` + `docker-compose.yml` locally.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://litellm.litellm.svc.cluster.local:4000/v1` | LiteLLM OpenAI-compatible URL (`deploy.sh` also accepts `OPENAI_BASE_URL`) |
| `LLM_API_KEY` | auto-discovered | LiteLLM key (`deploy.sh` also accepts `OPENAI_API_KEY`; falls back to `litellm-master-key` — a virtual key is recommended) |
| `LLM_MODEL` | `qwen2-5-coder-14b` | Chat model registered in LiteLLM (`deploy.sh` also accepts `MODEL_NAME`) |
| `API_PORT` | `8502` | FastAPI backend listening port |
| `STREAMLIT_PORT` | `8501` | Streamlit frontend listening port |
| `DB_PATH` | `/app/data/techgadgets.db` | SQLite database file path (emptyDir in Kubernetes) |
| `CLUSTER_URL` | auto-discovered | AI Solutions `base_domain_name`; route host is `customer-support-agent.<CLUSTER_URL>` (`deploy.sh` only) |
| `INGRESS_HOST` | `customer-support-agent.<CLUSTER_URL>` | HTTPRoute hostname override (`deploy.sh` only) |

### Memory (RediSearch + LiteLLM embeddings)

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_ENABLED` | `true` | Enable/disable long-term memory (fail-open either way) |
| `REDIS_URL` | auto-discovered (`redis-stack-server-credentials`) | Redis (RediSearch) URL. **Must include credentials** — the shared Redis requires auth |
| `MEMORY_INDEX` | `csa_mem_idx` | RediSearch index name |
| `MEMORY_TOP_K` | `4` | Number of past interactions to recall per query |
| `EMBEDDING_MODEL` | `bge-base-en` | Embedding model served by LiteLLM (add it to `llm_services_autodeploy_models`) |
| `EMBEDDING_DIMS` | `768` | Embedding vector dimension (must match the model) |

### Parallel tool execution

| Variable | Default | Description |
|----------|---------|-------------|
| `TOOL_EXECUTOR` | `local` | `local` (thread pool) or `ray` (KubeRay dispatch) |
| `TOOL_MAX_PARALLEL` | `8` | Max concurrent tool calls per batch |
| `RAY_ADDRESS` | `ray://ray-cluster-head-svc.ray-system:10001` | Ray cluster client address (when `TOOL_EXECUTOR=ray`) |
| `SELF_API_URL` | `http://customer-support-agent.customer-support-agent.svc.cluster.local:8502` | In-cluster URL Ray tasks call back to run tools (derived from release/namespace) |

### Helm values (key overrides)

```yaml
agent:
  llmBaseUrl: "http://litellm.litellm.svc.cluster.local:4000/v1"
  llmApiKey: "sk-your-litellm-key"          # required
  llmModel: "qwen2-5-coder-14b"
  memory:
    enabled: true
    redisUrl: "redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379"
    embeddingModel: "bge-base-en"
    embeddingDims: 768
  parallel:
    executor: "local"                         # or "ray" (kuberay_enabled)
  dataDir: /app/data                          # emptyDir, reseeded on restart

httpRoute:
  enabled: true
  host: "customer-support-agent.solutions.ai"
  gateway:
    name: eg-gateway
    namespace: envoy-gateway-system
    sectionName: https
  bypassGatewayAuth: true     # empty route-level SecurityPolicy — the app has NO auth of its own
```

Full values reference: [`helm-chart/values.yaml`](helm-chart/values.yaml)

---

## API Endpoints

Through the gateway, `/api/*` and `/v1/*` reach FastAPI; with port-forward use
`http://localhost:8502`. No endpoint requires authentication.

### Application API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/health` | Health check (status, model, tools count, `memory_enabled`, `executor_backend`) |
| `GET` | `/api/v1/tools` | List all registered tools with parameters |
| `POST` | `/api/v1/tools/{tool_name}` | Execute a single tool directly |
| `POST` | `/api/v1/chat` | Send a message to the agent, get response + tool trace |
| `POST` | `/api/v1/conversation` | Multi-turn conversation (pass history for context) |

### OpenAI-Compatible Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | Chat completions (stream and non-stream) |
| `GET` | `/v1/entities` | Agent discovery |
| `GET` | `/v1/models` | Models list (OpenAI-compatible) |
| `GET` | `/models` | Alternate models endpoint (port 8502 only) |
| `GET` | `/meta` | Framework metadata and capabilities (port 8502 only) |

### UI

| Port / route | Description |
|--------|-------------|
| `/` on the gateway host (Streamlit, 8501) | Streamlit chat UI |
| `/` on port 8502 (port-forward only) | Static HTML chat UI served by FastAPI |

```bash
API=https://customer-support-agent.solutions.ai     # or http://localhost:8502

curl -sk $API/api/v1/health
curl -sk -X POST $API/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"message":"Where is my order ORD-2024-000013?","customer_id":"CUST-0001"}'
curl -sk -X POST $API/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"What gaming laptops do you have?"}]}'
```

## Validation

How to verify, on a live cluster deployment, that (1) memory is stored and
recalled from the cluster, (2) the SQLite tool DB is seeded, and (3) tools
execute in parallel. Set these once:

```bash
API=https://customer-support-agent.solutions.ai      # or http://localhost:8502 via port-forward
CURL="curl -sk"
RPW=$(kubectl get secret -n redis redis-stack-server-credentials -o jsonpath='{.data.REDIS_PASSWORD}' | base64 -d)
AP=$(kubectl get pod -n customer-support-agent -o name | head -1)
RCLI="kubectl exec -n redis statefulset/redis-stack-server -- redis-cli -a $RPW --no-auth-warning"
```

### Health check — features on?

```bash
$CURL $API/api/v1/health
# → {"status":"healthy","model":"qwen2-5-coder-14b","tools_count":40,"memory_enabled":true,"executor_backend":"local"}
```

`memory_enabled: true` confirms the RediSearch index was created at startup
(Redis reachable); `executor_backend` reflects `TOOL_EXECUTOR`. Embedding
availability is only exercised on the first chat — check the logs for
`[memory] embed failed` if recall does not work.

### Scenario 1 — Memory is stored in cluster Redis

```bash
# Index stats (num_docs grows as conversations happen)
$RCLI FT.INFO csa_mem_idx | grep -A1 num_docs

# Live-write proof: count → chat → count
$RCLI DBSIZE
$CURL -XPOST $API/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"message":"I prefer express shipping and gaming laptops.","customer_id":"CUST-0001"}' >/dev/null
$RCLI DBSIZE            # +1

# Inspect the stored record (customer tag, text, 768-dim float32 embedding = 3072 bytes)
KEY=$($RCLI --scan --pattern 'csa:mem:*' | head -1 | tr -d '\r')
$RCLI HGET "$KEY" customer_id
$RCLI HGET "$KEY" text
$RCLI HSTRLEN "$KEY" embedding    # → 3072
```

### Scenario 2 — Memory is recalled across conversations

```bash
# Fresh conversation (no history passed) — the agent should still recall the preference.
$CURL -XPOST $API/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"message":"What shipping and product category do I prefer?","customer_id":"CUST-0001"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['response']);print('memory_used:',d['metadata']['memory_used'])"
# → mentions express shipping + gaming laptops; memory_used: True
```

### Scenario 3 — SQLite tool DB is seeded

```bash
kubectl exec -n customer-support-agent $AP -- python -c "
import sqlite3
c = sqlite3.connect('/app/data/techgadgets.db')
for t in ('products','customers','orders'):
    print(t, c.execute(f'select count(*) from {t}').fetchone()[0])
"
# → products 33 / customers 100 / orders 500
```

### Scenario 4 — Tools execute in parallel (concurrency proof)

The decisive test: register a tool that sleeps 1s and run 4 of them. Sequential ≈ 4s; parallel ≈ 1s.

```bash
kubectl exec -n customer-support-agent $AP -- python -c "
import sys, time; sys.path.insert(0,'/app')
import agent.tool_executor as te; from tools import registry
def slow(**k): time.sleep(1.0); return {'ok': k.get('n')}
registry._tools['slow']={'name':'slow','description':'x','parameters':{},'function':slow}
t=time.time(); res,backend=te.execute_tools([('slow',{'n':i}) for i in range(4)])
print('backend:',backend,'| wall:',round(time.time()-t,2),'s | verdict:', 'PARALLEL' if time.time()-t<2 else 'SEQUENTIAL')
"
# → backend: local | wall: ~1.0s | verdict: PARALLEL
```

Per-request evidence is also returned in the chat response `metadata`:

```bash
$CURL -XPOST $API/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"message":"Compare PROD-0006, PROD-0008, PROD-0017 prices.","customer_id":"CUST-0001"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata'])"
# → {'tools_called': 3, 'parallel_batches': 1, 'max_batch_size': 3, 'executor_backend': 'local', ...}
```

### Scenario 5 — KubeRay backend (optional showcase)

Requires `kuberay_enabled` in `config.agent-toolkit.yaml`.

```bash
# Switch the running deployment to the Ray backend, then re-run Scenario 4's query.
helm upgrade customer-support-agent ./helm-chart -n customer-support-agent \
  --reuse-values --set agent.parallel.executor=ray
# (the checksum/config pod annotation auto-restarts the pod)
$CURL $API/api/v1/health | grep -o '"executor_backend":"[a-z]*"'   # → "ray"

# Confirm tasks actually ran on the Ray cluster:
kubectl exec -n ray-system <ray-head-pod> -c ray-head -- python -c "
import ray; ray.init(address='auto', ignore_reinit_error=True, log_to_driver=False)
from ray.util.state import summarize_tasks; print(summarize_tasks())"
# → shows finished '_remote' tasks (the dispatched tool calls)
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Pod in `CrashLoopBackOff` | Missing `LLM_API_KEY` / bad values | `kubectl describe pod -n customer-support-agent`; `kubectl logs -n customer-support-agent deploy/customer-support-agent` |
| Agent replies with LLM errors / `RBAC: access denied` / connection reset | Namespace not allowed to reach LiteLLM | Add `customer-support-agent` to `litellm_allowed_namespaces` and re-run `./es_auto_installer.sh install agent-toolkit` (or redeploy with `PATCH_LITELLM_AUTHZ=true`); check `kubectl get ns customer-support-agent --show-labels` has `istio.io/dataplane-mode=ambient` |
| Model not found (400) in LiteLLM | `LLM_MODEL` not registered | List models: `curl -sk https://litellm.solutions.ai/v1/models -H "Authorization: Bearer <key>"` |
| `memory_enabled: false` in `/api/v1/health` | Redis unreachable or unauthenticated, or `MEMORY_ENABLED=false` | `kubectl get pods -n redis`; ensure `REDIS_URL` includes credentials (from `redis-stack-server-credentials`) |
| `memory_enabled: true` but nothing is recalled; logs show `[memory] embed failed` | `bge-base-en` not served by LiteLLM | Add `"bge-base-en"` to `llm_services_autodeploy_models` and re-run `./es_auto_installer.sh install agent-toolkit`; or set `EMBEDDING_MODEL` (and `EMBEDDING_DIMS`) to an embedding model you serve. The agent keeps working without memory |
| `index setup failed` / dimension mismatch after changing embedding model | Existing `csa_mem_idx` built with other dims | Drop it: `redis-cli -a <pw> FT.DROPINDEX csa_mem_idx DD`, then restart the pod |
| `401` from the gateway on `https://customer-support-agent.<base>` | Gateway-wide `gateway-auth` applied to the route | Keep `httpRoute.bypassGatewayAuth=true` (`kubectl get securitypolicy -n customer-support-agent`) — or send a LiteLLM key if you deliberately set it to `false` |
| Streamlit UI loads but stays on "Connecting…" | Websocket `/_stcore/stream` blocked | The route sends everything except `/api` and `/v1` to Streamlit; check `kubectl get httproute -n customer-support-agent -o yaml` and any proxy between you and the gateway |
| Orders/returns changes disappear | Expected — the SQLite DB is on an `emptyDir` and reseeded deterministically on restart | — |
| `executor_backend` is `ray` but tasks run locally | Ray cluster unreachable — auto-fallback | Enable `kuberay_enabled`; check `RAY_ADDRESS` and `kubectl get pods -n ray-system` |
| `ImagePullBackOff` | Image built with docker on a containerd cluster, or on another node | Build with nerdctl (`k8s.io` namespace), pin `agent.nodeSelector`, or `SKIP_BUILD=true AGENT_IMAGE=<registry ref>` |
| Pod rejected with `violates PodSecurity "restricted"` | Custom values re-added privileged settings | Keep the chart's `securityContext` (UID/GID 10001, drop ALL, seccomp RuntimeDefault) |

### View logs

```bash
kubectl logs -n customer-support-agent -l app.kubernetes.io/name=customer-support-agent -f
```

---

## Technology Stack

- **Language:** Python 3.12
- **API Framework:** FastAPI + Uvicorn
- **Frontend:** Streamlit
- **LLM Client:** OpenAI Python SDK (pointed at configurable endpoint)
- **Memory:** RediSearch vector store (shared `redis-stack-server`) + LiteLLM embeddings (`bge-base-en`)
- **Parallel execution:** `ThreadPoolExecutor` (local) or Ray client → agent-toolkit KubeRay cluster
- **Database:** SQLite (auto-initialized and seeded)
- **Containerization:** python:3.12-slim image, non-root UID/GID 10001
- **Orchestration:** Helm chart (Deployment, Service, ConfigMap, Secret, HTTPRoute, SecurityPolicy)

## Tool Categories (40 tools)

| Category | Tools | Examples |
|----------|-------|----------|
| Order Management | 4 | `get_order_details`, `track_shipment`, `list_customer_orders` |
| Returns & Refunds | 4 | `check_return_eligibility`, `initiate_return`, `process_refund` |
| Product Search | 4 | `search_products`, `get_product_details`, `compare_products` |
| Account | 5 | `authenticate_customer`, `get_customer_profile`, `get_loyalty_points` |
| Shipping | 3 | `get_shipping_rates`, `estimate_delivery_date`, `update_shipping_address` |
| Warranty | 2 | `check_warranty_status`, `file_warranty_claim` |
| Promotions | 4 | `validate_coupon`, `get_active_promotions`, `apply_price_match` |
| Inventory | 4 | `check_stock`, `get_restock_estimate`, `check_store_availability` |
| Knowledge Base | 4 | `search_knowledge_base`, `get_faq_article`, `get_policy_info` |
| Escalation | 3 | `transfer_to_human_agent`, `create_support_ticket`, `get_ticket_status` |
| Communication | 3 | `send_order_confirmation_email`, `send_return_label`, `schedule_callback` |

## Documentation

- [Implementation Details](docs/implementation.md) — Architecture, agent loop, tool system, memory, database schema
- [Deployment Guide](docs/deployment.md) — Kubernetes on AI Solutions, Docker Compose, Helm values, health checks, troubleshooting

## Project Structure

```
customer-support-agent/
├── README.md                    # This file
├── deploy.sh                    # Kubernetes deploy script (AI Solutions)
├── docker-compose.yml           # Local Docker Compose stack
├── config/
│   └── agent-config.env         # Docker Compose environment configuration
├── examples/
│   └── customer_support_tasks.json  # Sample support tasks / prompts
├── helm-chart/
│   ├── Chart.yaml               # Helm chart metadata
│   ├── values.yaml              # Default Helm values
│   └── templates/               # Deployment, Service, ConfigMap, Secret, HTTPRoute, SecurityPolicy
├── docs/
│   ├── implementation.md        # Detailed implementation documentation
│   └── deployment.md            # Deployment and operations guide
└── src/
    ├── Dockerfile               # Multi-stage Docker build
    ├── requirements.txt         # Python dependencies
    ├── startup.sh               # Entrypoint (starts FastAPI + Streamlit)
    ├── config.py                # Centralized configuration
    ├── customer_support_agent.py # Streamlit UI application
    ├── agent/
    │   ├── agent_loop.py        # Core agent loop (LLM + tool calling)
    │   ├── memory.py            # RediSearch long-term memory (LiteLLM embeddings)
    │   ├── prompt_builder.py    # System prompt construction
    │   ├── tool_executor.py     # Parallel tool execution (threads / KubeRay)
    │   └── tool_parser.py       # Parse tool calls from LLM output
    ├── api/
    │   └── server.py            # FastAPI server + OpenAI-compatible endpoints
    ├── backend/
    │   ├── database.py          # SQLite setup and query helpers
    │   ├── models.py            # Pydantic data models
    │   └── seed_data.py         # Auto-seed sample data
    ├── tools/
    │   ├── registry.py          # Tool decorator and dispatch
    │   ├── order_tools.py       # Order lookup and tracking
    │   ├── refund_tools.py      # Returns and refund processing
    │   ├── product_tools.py     # Product search and comparison
    │   ├── account_tools.py     # Customer account management
    │   ├── shipping_tools.py    # Shipping rates and tracking
    │   ├── warranty_tools.py    # Warranty status and claims
    │   ├── promotion_tools.py   # Coupons and promotions
    │   ├── inventory_tools.py   # Stock and availability
    │   ├── knowledge_base_tools.py  # FAQ and policy articles
    │   ├── escalation_tools.py  # Tickets and human handoff
    │   └── communication_tools.py   # Email and callback scheduling
    └── ui/
        └── index.html           # Static HTML chat UI (served by FastAPI on 8502)
```
