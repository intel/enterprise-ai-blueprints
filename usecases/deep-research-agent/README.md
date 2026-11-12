# Deep Research Agent

An autonomous web-research agent. Give it a question; it plans searches, runs
them against live web search, scores and filters the sources, extracts and
cross-references claims, and synthesises a **cited markdown report** —
streaming its progress to the browser as it works.

The agent is a **LangGraph ReAct state machine** with five tools and a circuit
breaker. It ships with its own web UI, a REST + SSE API, and an optional live
metrics dashboard.

This use case runs on top of
[Intel® Agent Toolkit](https://github.com/intel/enterprise-agent-toolkit/tree/release-2.0.0),
installed and operated by [Toolkit Manager](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2).

> **This use case deploys no models.** The LLM is consumed over HTTP from
> LiteLLM (`LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`). There are no model
> servers, GPU node selectors, or model volumes in this chart.

---

## 1. Overview

```
  https://deep-research-agent.<base_domain_name>            (UI + API, 3600s route timeout)
  https://deep-research-agent-dashboard.<base_domain_name>  (optional dashboard)
        │
        ▼
  Envoy Gateway eg-gateway (envoy-gateway-system) ──HTTPRoute──┐
                    ┌──────────── Pod (1 replica, ns deep-research-agent) ─────┐
                    │                                                          │
  user / API client │   ┌──────────────────────────────────────────────────┐   │
  ───────────────►  │   │            deep-research-agent                   │   │
   Web UI + REST    │   │  FastAPI :8081  ·  static frontend at /          │   │
   + SSE  @ :8081   │   │  LangGraph ReAct loop (5 tools)                  │   │
                    │   │  extractive summarizer (MiniLM, CPU, in-image)   │   │
  eval dashboard    │   ├──────────────────────────────────────────────────┤   │
  ───────────────►  │   │  eval/dashboard.py :8082  (optional, off by      │   │
        @ :8082     │   │  default — EVAL_DASHBOARD_ENABLED=true)          │   │
                    │   └───┬───────────────┬────────────────────┬─────────┘   │
                    │       │               │                    │             │
                    │       │               │            ┌───────▼─────────┐   │
                    └───────┼───────────────┼────────────┤ PVC /app/data   ├───┘
                            │               │            │ SQLite DB +     │
                     model calls      web search         │ results/live/   │
                            │               │            └─────────────────┘
                            ▼               ▼
                 LiteLLM                   Tavily  (required)
                 litellm.litellm.svc:4000  Serper  (optional)
                 (Inference Toolkit)       — needs direct internet egress
```

- **Agent** (`src/agent/`) — LangGraph graph: a `classify` node routes each query
  to a direct answer, a search-and-answer shortcut, or the full ReAct research
  loop. A deterministic `pipeline_recovery_node` guarantees a report even when a
  smaller model exits the loop early.
- **API** (`src/api.py`) — REST endpoints plus an SSE progress stream; the static
  web UI (`src/frontend/`) is mounted at `/`.
- **Search** (`src/search_providers/`) — pluggable providers, queried in
  parallel; results cached in SQLite with a 24 h TTL and warmed on startup.
- **Summarization** (`src/extractive_summarizer.py`) — each fetched page is
  compressed with sentence embeddings (`all-MiniLM-L6-v2`, CPU) before it reaches
  the LLM, which cuts token cost substantially. The model and the NLTK tokenizer
  are **baked into the image**, so the pod never calls out to HuggingFace.
- **Persistence** — one PVC at `/app/data` holds the SQLite database (session
  history + search cache) and the per-query metrics the dashboard reads.

### Agent tools

| Tool | LLM? | Purpose |
|---|---|---|
| `web_search` | no | Parallel web search via Tavily and/or Serper |
| `evaluate_sources` | yes | Score sources on relevance, density, authority |
| `analyze_gaps` | yes | Identify missing info, suggest follow-up queries |
| `extract_claims` | yes | Cross-reference findings, assign confidence scores |
| `write_report` | yes (×3) | Map-reduce synthesis into a cited markdown report |

See [docs/implementation.md](docs/implementation.md) for the full graph and the
per-node LLM calls.

---

## 2. Prerequisites

The following Intel® Agent Toolkit components must be running **before** deploying this
use case. See the [Toolkit Manager setup guide](../../docs/toolkit-manager-setup.md).

| Component | Namespace | Switch (`env/<env>/config.agent-toolkit.yaml`) |
|---|---|---|
| Kubernetes, Istio, Envoy Gateway | — / `envoy-gateway-system` | installed by `./es_auto_installer.sh install agent-toolkit` |
| LiteLLM | `litellm` | always installed by Intel® Inference Toolkit |
| LLM model (**tool-calling capable**) | — | `llm_services_autodeploy_models` (default `["qwen2-5-coder-14b"]`) |
| LiteLLM access from this namespace | — | add `deep-research-agent` to `litellm_allowed_namespaces` |

Redis, Agent Sandbox, KubeRay and Langfuse are **not** used by this blueprint.

In addition you need:

- A **Tavily API key** — required. The free tier is enough for testing:
  <https://tavily.com/>. A **Serper** key (<https://serper.dev/>) is an optional
  second provider.
- **Direct internet egress** from the pod to `api.tavily.com` (and
  `google.serper.dev` if used) plus outbound HTTP(S) to fetch source pages. The
  chart sets `NO_PROXY` but does **not** configure `http_proxy`/`https_proxy` on
  the pod, so a proxy-only egress path is not supported by the chart (see
  [Troubleshooting](#8-troubleshooting)).
- `kubectl` + `helm` for the cluster deploy, or Docker + Docker Compose v2 for
  local dev.
- Build note: the image is ~2.5 GB (CPU-only torch + the embedding model) and the
  first build takes several minutes.

Verify the prerequisites:

```bash
./es_auto_installer.sh validate agent-toolkit     # from the enterprise-ai-solutions checkout
kubectl get pods -n litellm
kubectl get pods,gateway -n envoy-gateway-system
```

---

## 3. Quick Start

### Option A — Kubernetes (Cluster)

```bash
# From the repo root
cp config.env config.local.env          # set TAVILY_API_KEY (+ optional MODEL_NAME, SERPER_API_KEY)
set -a && source config.local.env && set +a
./deploy-usecase.sh deep-research-agent
```

Or using the use case's own deploy script:

```bash
cd usecases/deep-research-agent
TAVILY_API_KEY="tvly-your-key" ./deploy.sh
```

Both prompt for anything they cannot discover (the model, defaulting to
`qwen2-5-coder-14b`, and the Tavily key — the deploy aborts without it).

**What the deploy does:**
1. Builds the agent image (into the containerd `k8s.io` namespace via `nerdctl`)
2. Creates the `deep-research-agent` namespace and labels it
   `istio.io/dataplane-mode=ambient` + Pod Security `restricted`; warns if it is
   not in `litellm_allowed_namespaces` (opt-in `PATCH_LITELLM_AUTHZ=true` patches
   the `litellm-ingress` policy directly as a stop-gap)
3. Auto-discovers `CLUSTER_URL` and the LiteLLM key (`litellm/litellm-master-key`;
   a virtual key is recommended — set `LLM_API_KEY`/`OPENAI_API_KEY`)
4. Exposes the UI/API via an HTTPRoute on `eg-gateway` with a 3600 s request
   timeout for the SSE progress stream (TLS is terminated at the gateway — no TLS
   secret needed); optionally a second HTTPRoute for the eval dashboard
5. Runs `helm upgrade --install deep-research-agent` (release, Service and
   namespace all `deep-research-agent`) and waits up to 10 min for readiness —
   the pod loads an embedding model before it reports ready
6. Prints the access URL(s)

**Expected output:**
```
[OK]    Deep Research Agent deployed — namespace: deep-research-agent

  Access the Deep Research Agent:
    https://deep-research-agent.solutions.ai   (health: /health)
```

To use the external URL, add `<eg-gateway-ip> deep-research-agent.<base_domain_name>`
to `/etc/hosts` and trust `env/local/logs/ai-solutions-ca.crt` (or use `curl -k`) —
see [toolkit-manager-setup.md](../../docs/toolkit-manager-setup.md#3-reach-the-gateway-hostnames--ca).
The route ships an empty SecurityPolicy (`httpRoute.bypassGatewayAuth: true`) so
the gateway does not demand a LiteLLM key from the browser.

Or locally:

```bash
kubectl port-forward -n deep-research-agent svc/deep-research-agent 8081:8081
#   → http://localhost:8081
```

> **The UI/API is open by default.** Anyone who can reach the hostname can start
> research runs, which spend LLM and Tavily quota. `API_KEYS` adds an `X-API-Key`
> check on `/api/*`, but the bundled web UI does not send that header — with
> `API_KEYS` set the UI gets `401` and the deployment becomes API-only. To keep
> the UI and still restrict access, set `httpRoute.bypassGatewayAuth=false` so
> the gateway requires a LiteLLM key on every request.

#### Try it — example prompts

```text
What are the latest advances in solid-state battery technology?
Compare the main approaches to small modular nuclear reactors and their deployment status.
How does CRISPR base editing differ from prime editing?
```

**Healthy run:** the UI streams progress (classify → web_search → evaluate_sources
→ extract_claims → write_report) for a few minutes and ends with a markdown
report with numbered citations.

#### Useful flags

```bash
# Reuse an image already in a registry instead of building
SKIP_BUILD=true AGENT_IMAGE=myregistry/deep-research-agent:1.0 ./deploy.sh

# Force the builder
CONTAINER_TOOL=docker ./deploy.sh

# Also serve the live metrics dashboard on its own host
#   https://deep-research-agent-dashboard.<base_domain_name>  (override with DASHBOARD_HOST)
EVAL_DASHBOARD_ENABLED=true ./deploy.sh
```

The dashboard needs its own hostname (it serves assets from absolute paths), so
add `deep-research-agent-dashboard.<base_domain_name>` to `/etc/hosts` as well.

### Option B — Docker Compose (Local Dev)

```bash
# Point at LiteLLM (a LiteLLM virtual key; trust the gateway CA or expect TLS
# errors with the default self-signed certificate)
export LITELLM_URL=https://litellm.solutions.ai/v1
export LITELLM_API_KEY=sk-your-litellm-key
export MODEL_NAME=qwen2-5-coder-14b
export TAVILY_API_KEY=tvly-your-key

./deploy-usecase.sh deep-research-agent --mode docker     # from the repo root
# or: cd usecases/deep-research-agent && docker compose up --build -d

curl -s localhost:8081/health | python3 -m json.tool
#   → {"status": "healthy", "checks": {"vllm": "ok", "tavily": "ok"}}
```

Values in `config/agent-config.env` are used when those variables are unset.

---

## 4. Configuration

Every setting is an environment variable (pydantic-settings maps each uppercase
name onto a field in `src/config.py`). In Docker mode they come from
`config/agent-config.env` (plus `LITELLM_URL`/`LITELLM_API_KEY`/`MODEL_NAME`
from the shell); in Kubernetes from the chart's `configmap.yaml` (non-secrets)
and `secret.yaml` (keys), wired by `deploy.sh` with `--set`.

`deploy.sh` reads `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` and also accepts
the repo-wide `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL_NAME` from
`config.env` as aliases.

### LLM — primary

| Variable | Helm value | Default | Description |
|---|---|---|---|
| `LLM_BASE_URL` (alias `OPENAI_BASE_URL`) | `agent.llm.baseUrl` | `http://litellm.litellm.svc.cluster.local:4000/v1` | LiteLLM OpenAI-compatible `/v1` endpoint |
| `LLM_API_KEY` (alias `OPENAI_API_KEY`) | `agent.llm.apiKey` | auto-discovered **(required)** | LiteLLM key (falls back to `litellm-master-key`; a virtual key is recommended) |
| `LLM_MODEL` (alias `MODEL_NAME`) | `agent.llm.model` | prompted, default `qwen2-5-coder-14b` **(required)** | Model id; **must support tool calling** and be registered in LiteLLM |
| `LLM_TEMPERATURE` | `agent.llm.temperature` | `0.3` | Sampling temperature |
| `LLM_MAX_TOKENS` | `agent.llm.maxTokens` | `4096` | Max tokens per response |
| `LLM_HARDWARE` | `agent.llm.hardware` | `GPU` | UI telemetry label (cosmetic) |

### LLM — secondary (optional)

Offload lightweight structured modules to a smaller model. Leave
`SECONDARY_LLM_MODULES` empty to route everything to the primary.

| Variable | Helm value | Default | Description |
|---|---|---|---|
| `SECONDARY_LLM_BASE_URL` | `agent.secondaryLlm.baseUrl` | *(empty)* | Second endpoint (usually the same LiteLLM URL) |
| `SECONDARY_LLM_API_KEY` | `agent.secondaryLlm.apiKey` | *(empty)* | Its bearer token |
| `SECONDARY_LLM_MODEL` | `agent.secondaryLlm.model` | *(empty)* | Its model id |
| `SECONDARY_LLM_MODULES` | `agent.secondaryLlm.modules` | *(empty)* | e.g. `classify,analyze_gaps,extract_claims` |
| `SECONDARY_LLM_HARDWARE` | `agent.secondaryLlm.hardware` | `Xeon` | UI telemetry label |

Valid module names: `classify`, `cat1_answer`, `cat2_answer`, `agent`,
`evaluate_sources`, `analyze_gaps`, `extract_claims`, `write_report`,
`decompose_query`, `synthesize_report`, `fallback_answer`.

### Web search

| Variable | Helm value | Default | Description |
|---|---|---|---|
| `TAVILY_API_KEY` | `agent.search.tavilyApiKey` | — **(required)** | Tavily key |
| `TAVILY_SEARCH_DEPTH` | `agent.search.searchDepth` | `advanced` | `basic` or `advanced` |
| `TAVILY_MAX_RESULTS` | `agent.search.maxResults` | `5` | Results per query |
| `SERPER_API_KEY` | `agent.search.serperApiKey` | *(empty)* | Optional second provider |

### Research pipeline

| Variable | Helm value | Default | Description |
|---|---|---|---|
| `MAX_RESEARCH_CYCLES` | `agent.pipeline.maxResearchCycles` | `1` | search→evaluate→gap cycles; `1` = single fast pass |
| `MIN_SOURCES_FOR_REPORT` | `agent.pipeline.minSourcesForReport` | `3` | Sources needed before synthesis |
| `RELEVANCE_THRESHOLD` | `agent.pipeline.relevanceThreshold` | `0.4` | Drop sources scoring below this |
| `REPORT_MAX_TOKENS` | `agent.pipeline.reportMaxTokens` | `8192` | Budget for the final report |
| `AGENT_MAX_ITERATIONS` | `agent.pipeline.agentMaxIterations` | `15` | ReAct loop circuit breaker |
| `USE_EXTRACTIVE_SUMMARIZER` | `agent.pipeline.useExtractiveSummarizer` | `true` | Compress pages before the LLM sees them |
| `EXTRACT_EMBEDDING_DEVICE` | `agent.pipeline.extractEmbeddingDevice` | `cpu` | Embedding device (this image is CPU-only) |

### Server, auth, storage, proxy

| Variable | Helm value | Default | Description |
|---|---|---|---|
| `PORT` | `agent.port` | `8081` | API + web UI port |
| `HOST` | — | `0.0.0.0` | Bind address (set by the chart) |
| `EVAL_DASHBOARD_ENABLED` | `agent.evalDashboard.enabled` | `false` | Start the metrics dashboard too |
| `EVAL_DASHBOARD_PORT` | `agent.evalDashboard.port` | `8082` | Dashboard port |
| `API_KEYS` | `agent.apiKeys` | *(empty)* | Comma-separated keys checked on `X-API-Key` for `/api/*`; empty = open. `/health` is always public |
| `DATABASE_URL` | derived from `agent.storage.mountPath` | `sqlite+aiosqlite:////app/data/deep_research.db` | SQLite location (on the volume) |
| `RESULTS_DIR` | derived from `agent.storage.mountPath` | `/app/data/results/live` | Per-query metrics dir (on the volume) |
| `NO_PROXY` / `no_proxy` | `agent.noProxy` | `localhost,127.0.0.1,.svc,.svc.cluster.local,10.0.0.0/8,…` | Hosts that bypass a proxy. The chart sets no `http_proxy`/`https_proxy` |
| `CLUSTER_URL` | — | auto-discovered | Toolkit Manager `base_domain_name`; route host is `deep-research-agent.<CLUSTER_URL>` (`deploy.sh` only) |
| `INGRESS_HOST` / `DASHBOARD_HOST` | `httpRoute.host` / `httpRoute.dashboardHost` | `deep-research-agent.<base>` / `deep-research-agent-dashboard.<base>` | HTTPRoute hostnames (`deploy.sh` only) |

Chart-only values: `agent.image.*`, `agent.replicaCount` (**keep at 1** — the
SQLite DB and the in-memory SSE queues are per-pod), `agent.resources`,
`agent.nodeSelector`, `agent.storage.{enabled,size,storageClassName,mountPath}`
(5Gi PVC at `/app/data` by default, owned via `fsGroup: 10001`),
`serviceAccount.*`, `service.{type,nodePort,dashboardNodePort}`.

### Helm values (key overrides)

```yaml
agent:
  llm:
    baseUrl: "http://litellm.litellm.svc.cluster.local:4000/v1"
    apiKey: "sk-your-litellm-key"          # required
    model: "qwen2-5-coder-14b"             # required — must support tool calling
  search:
    tavilyApiKey: "tvly-your-key"          # required
    serperApiKey: ""                       # optional
  apiKeys: ""                              # X-API-Key on /api/* — breaks the bundled UI
  evalDashboard:
    enabled: false
  storage:
    enabled: true
    size: 5Gi
  replicaCount: 1                          # keep at 1

httpRoute:
  enabled: true
  host: "deep-research-agent.solutions.ai"
  dashboardHost: "deep-research-agent-dashboard.solutions.ai"
  timeout: "3600s"                         # long-running SSE research streams
  gateway:
    name: eg-gateway
    namespace: envoy-gateway-system
    sectionName: https
  bypassGatewayAuth: true                  # empty route-level SecurityPolicy
```

Full values reference: [`helm-chart/values.yaml`](helm-chart/values.yaml)

---

## 5. API Reference

`/health` is public. All `/api/*` routes require `X-API-Key: <key>` **only** when
`API_KEYS` is set. Through the gateway use
`AGENT=https://deep-research-agent.solutions.ai` and add `-k` (or
`--cacert env/local/logs/ai-solutions-ca.crt`) to the `curl` calls.

```bash
AGENT=http://localhost:8081      # port-forward

# Health — checks the LLM endpoint and Tavily reachability
curl -s $AGENT/health | python3 -m json.tool
#   {"status":"healthy","checks":{"vllm":"ok","tavily":"ok"}}

# Model backend status (primary + secondary slots, via LiteLLM)
curl -s $AGENT/api/system/status | python3 -m json.tool

# Start a research run → returns a research_id immediately
curl -s -X POST $AGENT/api/research \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the latest advances in solid-state batteries?"}'
#   {"research_id":"a1b2c3…","status":"pending"}

# Follow progress (Server-Sent Events; ends when the run finishes)
curl -sN $AGENT/api/research/a1b2c3…/stream

# Fetch the finished session: report_markdown, citations, claims, sources
curl -s $AGENT/api/research/a1b2c3… | python3 -m json.tool

# Session history (optionally scoped to a browser client)
curl -s "$AGENT/api/sessions?limit=10"
curl -s -X DELETE $AGENT/api/sessions

# Search cache
curl -s $AGENT/api/system/cache-stats | python3 -m json.tool
curl -s -X DELETE $AGENT/api/cache
```

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness/readiness; checks LLM + Tavily (public) |
| `GET` | `/api/system/status` | Primary/secondary model backend health |
| `POST` | `/api/research` | Start a run — `{"query": "...", "summarize": true\|false\|null}` |
| `GET` | `/api/research/{id}/stream` | SSE progress events |
| `GET` | `/api/research/{id}` | Full session incl. `report_markdown` |
| `GET` | `/api/sessions?limit=&client_id=` | Session history |
| `DELETE` | `/api/sessions?client_id=` | Clear history |
| `GET` | `/api/system/cache-stats` | In-memory + DB search cache stats |
| `DELETE` | `/api/cache` | Clear the search cache |
| `GET` | `/` | Static web UI |

A research run takes **minutes**, not seconds — set generous client timeouts. The
chart's HTTPRoute raises the gateway request timeout to 3600 s
(`httpRoute.timeout`) so the SSE stream is not cut at the 600 s gateway default.

---

## 6. Running Examples

The examples default to `http://localhost:8081` (port-forward). To target the
gateway host instead, pass `https://deep-research-agent.<base_domain_name>` and
trust the CA via `CURL_CA_BUNDLE=env/local/logs/ai-solutions-ca.crt` (curl) or
`SSL_CERT_FILE=env/local/logs/ai-solutions-ca.crt` (httpx).

```bash
kubectl port-forward -n deep-research-agent svc/deep-research-agent 8081:8081 &

# Python (httpx): POST a query, stream progress, print the cited report
pip install httpx
python3 examples/research_query.py http://localhost:8081 \
  --query "What are the latest advances in solid-state batteries?"

# curl-based equivalents for every endpoint
./examples/research_query.sh http://localhost:8081 "How does CRISPR base editing work?"

# With app-level auth enabled
python3 examples/research_query.py http://localhost:8081 --api-key mykey
API_KEY=mykey ./examples/research_query.sh http://localhost:8081
```

---

## 7. Local Development

Run the whole thing in Compose (no models are started — point it at LiteLLM):

```bash
cd usecases/deep-research-agent
export LITELLM_URL=https://litellm.solutions.ai/v1 LITELLM_API_KEY=sk-your-litellm-key
export MODEL_NAME=qwen2-5-coder-14b TAVILY_API_KEY=tvly-your-key
docker compose up --build
#   deep-research-agent → :8081  (API + web UI)
#                       → :8082  (eval dashboard, if EVAL_DASHBOARD_ENABLED=true)
#   volume deep-research-data → /app/data  (SQLite + metrics)
```

Run it on the host instead:

```bash
cd src
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export LLM_BASE_URL=https://litellm.solutions.ai/v1 LLM_API_KEY=sk-… LLM_MODEL=qwen2-5-coder-14b TAVILY_API_KEY=tvly-…
python main.py                                    # → http://localhost:8081
python eval/dashboard.py --serve-only             # → http://localhost:8082
```

Tests (they also ship inside the image):

```bash
cd src && python -m pytest tests/ -q
docker run --rm deep-research-agent:latest python -m pytest tests/ -q
```

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `/health` shows `"vllm": "error: …"` (the LLM endpoint check) | `LLM_BASE_URL` unreachable or the key is rejected. Check `kubectl get pods -n litellm` and the key (`curl -sk https://litellm.solutions.ai/v1/models -H "Authorization: Bearer <key>"`). |
| LLM calls fail with `RBAC: access denied` / connection reset | Namespace not allowed to reach LiteLLM. Add `deep-research-agent` to `litellm_allowed_namespaces` and re-run `./es_auto_installer.sh install agent-toolkit` (or redeploy with `PATCH_LITELLM_AUTHZ=true`); check `kubectl get ns deep-research-agent --show-labels` has `istio.io/dataplane-mode=ambient`. |
| LiteLLM returns **400** on every call | `LLM_MODEL` isn't a *registered* `model_name` in LiteLLM. Use a name from `GET /v1/models` (e.g. `qwen2-5-coder-14b`); raw provider ids are rejected. |
| `deploy.sh` exits with `TAVILY_API_KEY is required` | Set `TAVILY_API_KEY` (in `config.local.env` or the shell) — search is mandatory for this agent. |
| `/health` shows `"tavily": "not configured"` | `TAVILY_API_KEY` is empty in the Secret — the agent can then only answer from parametric knowledge. Redeploy with the key set. |
| `"tavily": "error: …"` but the key is set | No egress from the pod to `api.tavily.com`. The pod needs **direct** internet egress: the chart sets only `NO_PROXY` and has **no** `http_proxy`/`https_proxy` setting, so environments that only reach the internet via a proxy are not supported by the chart as shipped. (A manual `kubectl set env deploy/deep-research-agent -n deep-research-agent HTTPS_PROXY=…` works until the next `helm upgrade`.) |
| `401` from the gateway on `https://deep-research-agent.<base>` | The gateway-wide `gateway-auth` policy applies — keep `httpRoute.bypassGatewayAuth=true` (route-level SecurityPolicy present: `kubectl get securitypolicy -n deep-research-agent`). |
| `401 Invalid or missing API key` in the web UI | `API_KEYS` is set — the bundled UI does not send `X-API-Key`. Redeploy with `API_KEYS` empty, or use the API only. |
| Report never appears; loop spins to 15 iterations | The model isn't calling tools reliably. Use a tool-calling model; the deterministic `pipeline_recovery_node` should still produce a report from whatever sources exist. |
| SSE stream cuts off after 600 s | The route timeout was overridden. Keep `httpRoute.timeout: "3600s"`, or use `kubectl port-forward`. |
| Pod restarts before Ready / `helm --wait` slow | The embedding model load takes ~30–60 s on first start. Probes allow 45 s + 6 failures; `deploy.sh` waits up to 10 min. Check `kubectl logs -n deep-research-agent deploy/deep-research-agent`. |
| `ImagePullBackOff` | Image built with docker on a containerd cluster, or built on a different node. Build with nerdctl (`--namespace k8s.io`), pin `agent.nodeSelector`, or push to a registry and use `SKIP_BUILD=true AGENT_IMAGE=…`. |
| Pod rejected with `violates PodSecurity "restricted"` | Custom values re-added privileged settings. Keep the chart's `securityContext` (UID/GID 10001, `fsGroup` 10001, drop ALL, seccomp RuntimeDefault). |
| PVC stays `Pending` | No default StorageClass. Set `agent.storage.storageClassName`, or `agent.storage.enabled=false` (emptyDir — data lost on restart). |
| Logs show HuggingFace download attempts | `HF_HUB_OFFLINE=1` and the baked assets should prevent this. Verify with `docker run --rm deep-research-agent:latest python -c "from sentence_transformers import SentenceTransformer as S; S('all-MiniLM-L6-v2'); print('ok')"`. |
| Session history empty after restart | `agent.storage.enabled=false` falls back to an `emptyDir`. Enable the PVC. |
| Second replica sees no in-flight session | By design — SQLite and the SSE queues are per-pod. Keep `replicaCount: 1`. |
| Dashboard host returns 404 / not found | `EVAL_DASHBOARD_ENABLED=true` was not set at deploy time (the second HTTPRoute is only created then), or `deep-research-agent-dashboard.<base>` is missing from `/etc/hosts`. |
| Eval dashboard shows nothing | Metrics are written to `RESULTS_DIR` after a completed run. Confirm `EVAL_DASHBOARD_ENABLED=true` and that both processes see the same volume path. |

### View logs

```bash
kubectl logs -n deep-research-agent -l app.kubernetes.io/name=deep-research-agent -f
```

---

## Further reading

- [docs/implementation.md](docs/implementation.md) — graph, nodes, per-node LLM
  calls, dual-LLM routing, caching, session isolation, pipeline recovery.
- [docs/deployment.md](docs/deployment.md) — step-by-step deploy for both modes,
  full values reference, storage, dashboard, upgrade/rollback.

## Security & limitations

- **Secrets** come from the environment / a Kubernetes Secret only — no keys are
  committed. Don't commit real keys into `config/agent-config.env` or
  `config.local.env`.
- **App-level auth is off by default** (`API_KEYS` empty) and the route bypasses
  the gateway's LiteLLM-key check, so any caller that can reach
  `deep-research-agent.<base>` can start research runs, which spend LLM and
  Tavily quota. Set `API_KEYS` (API-only — the bundled UI then gets `401`) or
  `httpRoute.bypassGatewayAuth=false` (gateway requires a LiteLLM key) on shared
  clusters.
- The container runs as the non-root `researcher` user (UID/GID 10001, Pod
  Security `restricted`); the service account has
  `automountServiceAccountToken: false` (the agent needs no Kubernetes API access).
- **The agent fetches arbitrary web pages** chosen by the model. Reports can
  contain inaccurate or adversarial content from those sources — citations are
  provided so claims can be checked, but output is not verified.
- Single replica by design; not built for high concurrency.
