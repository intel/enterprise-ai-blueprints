# Deep Research Agent — Deployment Guide

Step-by-step deployment for both modes, the full Helm values reference, storage
and dashboard notes, and upgrade/rollback procedures.

For what the agent actually does internally, see
[implementation.md](implementation.md).

The target platform is
[Intel® AI for Enterprise Solutions](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2)
(release-1.2) with its agent-toolkit layer — see the
[AI Solutions setup guide](../../../docs/ai-solutions-setup.md).

> **No models are deployed by this use case.** The LLM is reached over HTTP from
> LiteLLM via `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`. Nothing in this chart
> schedules a model server, requests a GPU/XPU, or mounts model weights.

---

## Prerequisites

### Platform

| Component | Namespace | Switch (`env/<env>/config.agent-toolkit.yaml`) |
|---|---|---|
| Kubernetes, Istio, Envoy Gateway (`eg-gateway`) | — / `envoy-gateway-system` | installed by `./es_auto_installer.sh install agent-toolkit` |
| LiteLLM | `litellm` | always installed by the inference layer |
| Tool-calling LLM model | — | `llm_services_autodeploy_models` (default `["qwen2-5-coder-14b"]`) |
| LiteLLM access from this namespace | — | add `deep-research-agent` to `litellm_allowed_namespaces`, then `./es_auto_installer.sh install agent-toolkit` |

### Common

- A **Tavily API key** (required): <https://tavily.com/>. Optionally a **Serper**
  key: <https://serper.dev/>.
- **Direct** outbound network access from the pod to `api.tavily.com`,
  `google.serper.dev` (if used), and the arbitrary web pages the agent fetches.
  The chart sets `NO_PROXY` but has no `http_proxy`/`https_proxy` setting, so a
  proxy-only egress path is not supported by the chart.
- ~6 GB of free disk for the image build (CPU-only torch plus the embedding
  model), and several minutes for the first build.

### Kubernetes-specific

- `kubectl` with the AI Solutions kubeconfig (`env/<env>/kubeconfig.yaml`), and
  `helm` 3.x.
- A default StorageClass, or set `agent.storage.storageClassName` — the chart
  requests a `ReadWriteOnce` PVC (5 Gi by default).
- On containerd clusters, `nerdctl` on the build host so the image lands in the
  `k8s.io` namespace where the kubelet can find it (the chart uses
  `pullPolicy: IfNotPresent`). Otherwise push to a registry and use
  `SKIP_BUILD=true`.

### Verifying the LLM endpoint first

Do this before deploying — a wrong model name is the most common failure:

```bash
MASTER_KEY=$(kubectl get secret litellm-master-key -n litellm \
  -o jsonpath='{.data.master_key}' | base64 -d)
LITELLM=https://litellm.solutions.ai      # https://litellm.<base_domain_name>

# What model names are actually registered?
curl -sk -H "Authorization: Bearer $MASTER_KEY" $LITELLM/v1/models | python3 -m json.tool

# Does it do tool calling?
curl -sk -X POST $LITELLM/v1/chat/completions \
  -H "Authorization: Bearer $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen2-5-coder-14b",
       "messages":[{"role":"user","content":"What is 2+2?"}],
       "tools":[{"type":"function","function":{"name":"calc",
                 "parameters":{"type":"object","properties":{"expr":{"type":"string"}}}}}]}'
```

`LLM_MODEL` must be one of the names returned by `/v1/models`; LiteLLM rejects
unregistered names with a 400. Prefer a virtual key (`POST $LITELLM/key/generate`
with the master key) over the master key for `LLM_API_KEY`.

---

## Docker deployment

### Quick start

```bash
# 1. Configure (values in config/agent-config.env are used when these are unset)
export LITELLM_URL=https://litellm.solutions.ai/v1     # https://litellm.<base_domain_name>/v1
export LITELLM_API_KEY=sk-your-litellm-key
export MODEL_NAME=qwen2-5-coder-14b
export TAVILY_API_KEY=tvly-your-key

# 2. Deploy (from the repo root)
./deploy-usecase.sh deep-research-agent --mode docker
#    or: cd usecases/deep-research-agent && docker compose up --build -d

# 3. Verify
curl -s localhost:8081/health | python3 -m json.tool
#    {"status":"healthy","checks":{"vllm":"ok","tavily":"ok"}}
open http://localhost:8081
```

`docker-compose.yml` maps `LITELLM_URL` / `LITELLM_API_KEY` / `MODEL_NAME` onto
`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` and reads everything else from
`config/agent-config.env` (its `env_file`). The LiteLLM hostname must resolve to
the `eg-gateway` IP from inside the container, and its self-signed CA must be
trusted (or LLM calls fail TLS verification).

### Volumes

| Volume | Mount | Contents |
|---|---|---|
| `deep-research-data` | `/app/data` | SQLite DB (sessions + persisted search cache) and `results/live/*.json` metrics |

Data survives `docker compose restart` and `down`; it is removed by
`docker compose down -v`.

### Verifying

```bash
docker compose ps                                   # healthy after ~90 s
docker compose logs -f deep-research-agent

curl -s localhost:8081/api/system/status | python3 -m json.tool
python3 examples/research_query.py http://localhost:8081

# Persistence check
curl -s localhost:8081/api/sessions | python3 -m json.tool     # non-empty
docker compose restart deep-research-agent && sleep 60
curl -s localhost:8081/api/sessions | python3 -m json.tool     # same sessions
```

### Enabling the eval dashboard

```bash
cd usecases/deep-research-agent
sed -i 's/^EVAL_DASHBOARD_ENABLED=.*/EVAL_DASHBOARD_ENABLED=true/' config/agent-config.env
docker compose up -d
open http://localhost:8082
```

---

## Kubernetes deployment

### With deploy.sh

```bash
# From the repo root
cp config.env config.local.env      # set TAVILY_API_KEY; MODEL_NAME/OPENAI_API_KEY optional
set -a && source config.local.env && set +a
./deploy-usecase.sh deep-research-agent
```

Or directly (replace the dummy values):

```bash
cd usecases/deep-research-agent

LLM_API_KEY="sk-your-litellm-key" \
LLM_MODEL="qwen2-5-coder-14b" \
TAVILY_API_KEY="tvly-your-key" \
./deploy.sh
```

What it does, in order: auto-discover `CLUSTER_URL` (the `base_domain_name`,
from `eg-gateway`) and the LiteLLM key (`litellm/litellm-master-key`, when
`LLM_API_KEY`/`OPENAI_API_KEY` is empty) → prompt for anything missing (model
defaults to `qwen2-5-coder-14b`) → **hard-fail if `TAVILY_API_KEY` is empty** →
build the image (nerdctl into `k8s.io`, or docker) → create the namespace and
label it Istio ambient + Pod Security `restricted` → check that the namespace is
in LiteLLM's `litellm_allowed_namespaces` (`PATCH_LITELLM_AUTHZ=true` patches the
policy as a temporary fix) → `helm upgrade --install deep-research-agent --wait
--timeout 10m` with an HTTPRoute for `deep-research-agent.<base_domain_name>`.

The generous timeout is deliberate: the pod imports torch and loads the embedding
model before it reports Ready.

Useful overrides: `NAMESPACE`, `INGRESS_HOST`, `DASHBOARD_HOST`,
`EVAL_DASHBOARD_ENABLED=true`, `SERPER_API_KEY`, `SECONDARY_LLM_*`, `API_KEYS`,
`SKIP_BUILD=true` + `AGENT_IMAGE`, `CONTAINER_TOOL`.

### Manual Helm

```bash
kubectl create namespace deep-research-agent
kubectl label namespace deep-research-agent istio.io/dataplane-mode=ambient \
  pod-security.kubernetes.io/enforce=restricted

helm upgrade --install deep-research-agent ./helm-chart \
  --namespace deep-research-agent \
  --set agent.image.tag=latest \
  --set agent.llm.baseUrl="http://litellm.litellm.svc.cluster.local:4000/v1" \
  --set-string agent.llm.apiKey="sk-your-litellm-key" \
  --set agent.llm.model="qwen2-5-coder-14b" \
  --set-string agent.search.tavilyApiKey="tvly-your-key" \
  --set httpRoute.enabled=true \
  --set httpRoute.host="deep-research-agent.solutions.ai" \
  --wait --timeout 10m
```

Use `--set-string` for keys — `--set` would mangle values containing commas or
looking numeric.

Render before applying if you want to inspect the manifests:

```bash
helm template deep-research-agent ./helm-chart \
  --set-string agent.llm.apiKey=sk-x --set-string agent.search.tavilyApiKey=tvly-x \
  --set agent.llm.model=qwen2-5-coder-14b \
  --set httpRoute.enabled=true --set httpRoute.host=deep-research-agent.solutions.ai | less
```

### Accessing it

Map the hostname to the gateway and trust the CA (see
[ai-solutions-setup.md](../../../docs/ai-solutions-setup.md#3-reach-the-gateway-hostnames--ca)):

```bash
kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=eg-gateway
# /etc/hosts:
# <gateway-ip> deep-research-agent.solutions.ai deep-research-agent-dashboard.solutions.ai

curl --cacert env/local/logs/ai-solutions-ca.crt https://deep-research-agent.solutions.ai/health
```

### Verifying

```bash
kubectl -n deep-research-agent get pods,svc,pvc,httproute,securitypolicy
kubectl -n deep-research-agent logs deploy/deep-research-agent -f

kubectl -n deep-research-agent port-forward svc/deep-research-agent 8081:8081
curl -s localhost:8081/health | python3 -m json.tool
python3 examples/research_query.py http://localhost:8081
```

The logs should show **no** HuggingFace download attempts — the model and
tokenizer are baked into the image and `HF_HUB_OFFLINE=1` is set.

---

## Helm values reference

### Image and scale

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.image.repository` | string | `deep-research-agent` | Image repository |
| `agent.image.tag` | string | `""` (chart `appVersion`) | Image tag; deploy.sh sets the tag it built (`latest` by default) |
| `agent.image.pullPolicy` | string | `IfNotPresent` | Pull policy — keep for locally built images |
| `agent.replicaCount` | int | `1` | **Keep at 1.** SQLite and the in-memory SSE queues are per-pod |

### LLM — primary

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.llm.baseUrl` | string | `http://litellm.litellm.svc.cluster.local:4000/v1` | LiteLLM OpenAI-compatible `/v1` endpoint |
| `agent.llm.apiKey` | string | `""` | **(Required)** bearer token → Secret |
| `agent.llm.model` | string | `""` | **(Required)** registered, tool-calling model id |
| `agent.llm.temperature` | float | `0.3` | Sampling temperature |
| `agent.llm.maxTokens` | int | `4096` | Max tokens per response |
| `agent.llm.hardware` | string | `GPU` | UI telemetry label (cosmetic) |

### LLM — secondary (optional)

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.secondaryLlm.baseUrl` | string | `""` | Second endpoint (may be the same LiteLLM URL) |
| `agent.secondaryLlm.apiKey` | string | `""` | Its bearer token → Secret |
| `agent.secondaryLlm.model` | string | `""` | Its model id |
| `agent.secondaryLlm.modules` | string | `""` | Modules to route there, e.g. `classify,extract_claims` |
| `agent.secondaryLlm.hardware` | string | `Xeon` | UI telemetry label |

### Search

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.search.tavilyApiKey` | string | `""` | **(Required)** Tavily key → Secret |
| `agent.search.serperApiKey` | string | `""` | Optional Serper key → Secret |
| `agent.search.searchDepth` | string | `advanced` | `basic` or `advanced` |
| `agent.search.maxResults` | int | `5` | Results per query |

### Pipeline

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.pipeline.maxResearchCycles` | int | `1` | search→evaluate→gap cycles |
| `agent.pipeline.minSourcesForReport` | int | `3` | Sources required before synthesis |
| `agent.pipeline.relevanceThreshold` | float | `0.4` | Minimum source score |
| `agent.pipeline.reportMaxTokens` | int | `8192` | Final report token budget |
| `agent.pipeline.agentMaxIterations` | int | `15` | ReAct loop circuit breaker |
| `agent.pipeline.useExtractiveSummarizer` | bool | `true` | Compress pages before the LLM |
| `agent.pipeline.extractEmbeddingDevice` | string | `cpu` | Embedding device (image is CPU-only) |

### Server, auth, storage, scheduling

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.port` | int | `8081` | API + web UI port |
| `agent.apiKeys` | string | `""` | Comma-separated `X-API-Key` values → Secret; empty = open. The bundled UI does not send the header — set only for API-only use |
| `agent.evalDashboard.enabled` | bool | `false` | Also run the metrics dashboard |
| `agent.evalDashboard.port` | int | `8082` | Dashboard port |
| `agent.storage.enabled` | bool | `true` | PVC for DB + metrics; `false` = `emptyDir` |
| `agent.storage.size` | string | `5Gi` | PVC size |
| `agent.storage.storageClassName` | string | `""` | StorageClass (empty = cluster default) |
| `agent.storage.mountPath` | string | `/app/data` | Mount path; `DATABASE_URL` and `RESULTS_DIR` derive from it |
| `agent.noProxy` | string | cluster CIDRs + `.svc` | Proxy bypass list (`NO_PROXY`). There is no `http_proxy`/`https_proxy` value |
| `agent.resources.requests.cpu` | string | `1` | CPU request |
| `agent.resources.requests.memory` | string | `2Gi` | Memory request |
| `agent.resources.limits.cpu` | string | `4` | CPU limit |
| `agent.resources.limits.memory` | string | `6Gi` | Memory limit |
| `agent.nodeSelector` | map | `{}` | Pin the pod (e.g. to the node holding a locally built image) |
| `serviceAccount.create` | bool | `true` | Create a ServiceAccount (`automountServiceAccountToken: false`) |
| `serviceAccount.name` | string | `""` | Override the name |
| `serviceAccount.annotations` | map | `{}` | Extra annotations |

Requests and limits are higher than other use cases because the extractive
summarizer keeps a sentence-transformers model resident and embeds on the CPU.

### Service and HTTPRoute

| Key | Type | Default | Description |
|---|---|---|---|
| `service.type` | string | `ClusterIP` | `ClusterIP`, `NodePort`, or `LoadBalancer` |
| `service.nodePort` | string | `""` | Pin the API NodePort (30000–32767) |
| `service.dashboardNodePort` | string | `""` | Pin the dashboard NodePort |
| `httpRoute.enabled` | bool | `false` | Create an HTTPRoute on `eg-gateway` (`deploy.sh` sets `true`) |
| `httpRoute.host` | string | `""` | Hostname for the API + UI, e.g. `deep-research-agent.solutions.ai` |
| `httpRoute.dashboardHost` | string | `""` | Separate hostname for the dashboard (see below); only used when `agent.evalDashboard.enabled` |
| `httpRoute.timeout` | string | `3600s` | Per-request timeout — research runs stream SSE for several minutes |
| `httpRoute.gateway.name` / `.namespace` / `.sectionName` | string | `eg-gateway` / `envoy-gateway-system` / `https` | Parent gateway listener |
| `httpRoute.bypassGatewayAuth` | bool | `true` | Ship an empty route-level SecurityPolicy so the gateway does not require a LiteLLM key; set `false` to have the gateway enforce one |

TLS is terminated at `eg-gateway` with its wildcard `*.<base_domain_name>`
certificate, so the chart needs no TLS secret.

### Custom values file

```yaml
# my-values.yaml
agent:
  llm:
    baseUrl: "http://litellm.litellm.svc.cluster.local:4000/v1"
    apiKey: "sk-your-litellm-key"
    model: "qwen2-5-coder-14b"
  secondaryLlm:
    baseUrl: "http://litellm.litellm.svc.cluster.local:4000/v1"
    apiKey: "sk-your-litellm-key"
    model: "<smaller model registered in LiteLLM>"
    modules: "classify,analyze_gaps,extract_claims"
  search:
    tavilyApiKey: "tvly-your-key"
  pipeline:
    maxResearchCycles: 2          # deeper research, more tokens
  evalDashboard:
    enabled: true
  storage:
    size: 20Gi

httpRoute:
  enabled: true
  host: deep-research-agent.solutions.ai
  dashboardHost: deep-research-agent-dashboard.solutions.ai
```

```bash
helm upgrade --install deep-research-agent ./helm-chart \
  --namespace deep-research-agent -f my-values.yaml --wait --timeout 10m
```

---

## Storage

One `ReadWriteOnce` PVC mounted at `agent.storage.mountPath` (`/app/data`) holds
everything durable:

| Path | Env var | Contents |
|---|---|---|
| `<mountPath>/deep_research.db` | `DATABASE_URL` | Sessions, reports, persisted search cache |
| `<mountPath>/results/live/*.json` | `RESULTS_DIR` | One metrics file per completed run |

Both env vars are derived from `mountPath` in `configmap.yaml`, so changing the
mount path moves both. The pod runs as UID/GID 10001 with `fsGroup: 10001`, so
the volume is writable under Pod Security `restricted`. With `agent.storage.enabled=false` the mount becomes an
`emptyDir`: everything works but session history, the warm search cache, and
metrics are lost on restart.

Because the volume is RWO and SQLite is single-writer, this deployment is
**single-pod by design**. Scaling out would need PostgreSQL for state and Redis
for the progress channel.

Inspecting and backing up:

```bash
POD=$(kubectl -n deep-research-agent get pod -l app.kubernetes.io/name=deep-research-agent -o name | head -1)
kubectl -n deep-research-agent exec $POD -- ls -la /app/data /app/data/results/live
kubectl -n deep-research-agent cp "${POD#pod/}:/app/data/deep_research.db" ./deep_research.db
```

---

## Eval dashboard

Off by default. It runs as a second process in the same container, started by
`startup.sh` when `EVAL_DASHBOARD_ENABLED=true`, and reads the same `RESULTS_DIR`
the agent writes to.

```bash
helm upgrade --install deep-research-agent ./helm-chart \
  --namespace deep-research-agent --reuse-values \
  --set agent.evalDashboard.enabled=true

kubectl -n deep-research-agent port-forward svc/deep-research-agent 8082:8082
open http://localhost:8082
```

To expose it through the gateway, it gets **its own hostname** — the dashboard
serves assets from absolute paths (`/vendor/chart.js`, `/report/<id>`), so a
path-prefix route would 404 on every asset. `deploy.sh` does this with
`EVAL_DASHBOARD_ENABLED=true` (host `deep-research-agent-dashboard.<base_domain_name>`,
override with `DASHBOARD_HOST`); with Helm directly:

```bash
--set agent.evalDashboard.enabled=true \
--set httpRoute.dashboardHost=deep-research-agent-dashboard.solutions.ai
```

The dashboard has no authentication of its own, shows full report text, and its
route also bypasses the gateway's LiteLLM-key check. Only expose it on a trusted
network, or keep it on port-forward.

---

## Health checks

`/health` is public (in the auth middleware's public-path set) and checks two
things: a `GET {LLM_BASE_URL}/models` and a minimal Tavily search.

```json
{"status": "healthy", "checks": {"vllm": "ok", "tavily": "ok"}}
```

`status` is `degraded` if either check fails; `tavily: "not configured"` when the
key is empty is not counted as a failure. **Note that the probes only require an
HTTP 200**, which `/health` returns even when degraded — so a pod can be Ready
while the LLM is unreachable. Always check the body, not just pod status.

| Probe | Path | Delay | Period | Timeout | Failures |
|---|---|---|---|---|---|
| Liveness | `/health` | 45 s | 30 s | 15 s | 6 |
| Readiness | `/health` | 45 s | 15 s | 15 s | 6 |

The 45 s delay covers importing torch and loading `all-MiniLM-L6-v2`. Raise it if
the pod restart-loops on a slow or CPU-throttled node. The Docker healthcheck uses
the same endpoint with `start_period: 90s`.

---

## Upgrade and rollback

```bash
# Rebuild the image and roll the deployment
cd usecases/deep-research-agent
./deploy.sh                            # helm upgrade --install is idempotent

# Change one value without re-supplying everything
helm upgrade deep-research-agent ./helm-chart -n deep-research-agent --reuse-values \
  --set agent.pipeline.maxResearchCycles=2

# History and rollback
helm history deep-research-agent -n deep-research-agent
helm rollback deep-research-agent <REVISION> -n deep-research-agent
```

A ConfigMap or Secret change rolls the pod automatically — `deployment.yaml`
carries `checksum/config` and `checksum/secret` pod annotations.

With `:latest` and `pullPolicy: IfNotPresent`, rebuilding the image does **not**
by itself restart the pod. Either use a real tag, or force it:

```bash
kubectl -n deep-research-agent rollout restart deploy/deep-research-agent
```

Uninstall (the PVC is intentionally left behind):

```bash
helm uninstall deep-research-agent -n deep-research-agent
kubectl -n deep-research-agent delete pvc deep-research-agent-data   # deletes the data
```

---

## Troubleshooting

### Pod won't start

```bash
kubectl -n deep-research-agent describe pod -l app.kubernetes.io/name=deep-research-agent
kubectl -n deep-research-agent logs deploy/deep-research-agent --previous
```

| Cause | Fix |
|---|---|
| `ImagePullBackOff` | Image not in the kubelet's store. Build with `nerdctl --namespace k8s.io` on the node, or push to a registry and use `SKIP_BUILD=true AGENT_IMAGE=…`. Pin with `agent.nodeSelector` when only one node has it. |
| `Pending` on the PVC | No default StorageClass — set `agent.storage.storageClassName`, or `agent.storage.enabled=false` to test without persistence. |
| `CrashLoopBackOff`, `OOMKilled` | Raise `agent.resources.limits.memory`; torch plus the embedding model needs ~2 GB before serving traffic. |
| Restarts right before Ready | Probe delay too short for this node — raise `initialDelaySeconds`. |

### Cannot reach the LLM

```bash
POD=$(kubectl -n deep-research-agent get pod -l app.kubernetes.io/name=deep-research-agent -o name | head -1)
kubectl -n deep-research-agent exec $POD -- python3 -c \
  "import os,urllib.request;print(urllib.request.urlopen(os.environ['LLM_BASE_URL']+'/models').status)"
kubectl -n deep-research-agent exec $POD -- env | grep -iE 'proxy|llm_base'
```

- **400 on every call** — `LLM_MODEL` isn't registered in LiteLLM. Compare
  against `GET /v1/models`.
- **`RBAC: access denied` / connection reset** — the namespace is not allowed to
  call LiteLLM. Add `deep-research-agent` to `litellm_allowed_namespaces` in
  `env/<env>/config.agent-toolkit.yaml` and re-run
  `./es_auto_installer.sh install agent-toolkit` (or redeploy with
  `PATCH_LITELLM_AUTHZ=true`). Confirm the namespace has
  `istio.io/dataplane-mode=ambient`.
- **401/403** — wrong `LLM_API_KEY`. It lives in the Secret:
  `kubectl -n deep-research-agent get secret deep-research-agent-secret -o jsonpath='{.data.LLM_API_KEY}' | base64 -d`.

### Search failures

```bash
curl -s localhost:8081/health | python3 -m json.tool     # look at checks.tavily
```

`"not configured"` means the key is empty. `"error: …"` with a key set means no
egress to `api.tavily.com`. The pod needs **direct** internet egress: the chart
sets `NO_PROXY` only and has **no** `http_proxy`/`https_proxy` value, so a
proxy-only environment is not supported by the chart as shipped. As a manual
workaround, `kubectl -n deep-research-agent set env deploy/deep-research-agent
HTTPS_PROXY=http://proxy:port HTTP_PROXY=http://proxy:port` works until the next
`helm upgrade` resets it. HTTP 429 is a Tavily rate limit: lower
`TAVILY_MAX_RESULTS`, or set `TAVILY_SEARCH_DEPTH=basic`.

### Research completes but produces no report

Usually the model isn't calling tools reliably.

```bash
kubectl -n deep-research-agent logs deploy/deep-research-agent | grep -E 'Routing:|Circuit breaker|pipeline_recovery'
```

- `Routing: -> fallback` immediately ⇒ no tool calls at all; use a tool-calling
  model.
- `pipeline_recovery` in the logs ⇒ the recovery path did its job; the report is
  real, just produced deterministically.
- `Circuit breaker` ⇒ the loop hit `AGENT_MAX_ITERATIONS`; raise it, or reduce
  `MAX_RESEARCH_CYCLES` so the pipeline is shorter.

### SSE stream drops

The chart's HTTPRoute sets a 3600 s request timeout (`httpRoute.timeout`); the
gateway default is 600 s, so a stream that stops at ~10 min means the value was
overridden. Bypass the gateway with `kubectl port-forward` to rule it out. Any
intermediate proxy that buffers responses will also break progress streaming.

A `401` on `https://deep-research-agent.<base>` from the gateway itself means the
route-level SecurityPolicy is missing — keep `httpRoute.bypassGatewayAuth=true`
(`kubectl -n deep-research-agent get securitypolicy`).

### First start is slow / HuggingFace access in logs

~30–60 s of startup is torch import plus the embedding model load. Downloads
should never happen — verify the assets are in the image:

```bash
docker run --rm deep-research-agent:latest python -c \
  "import nltk; nltk.data.find('tokenizers/punkt_tab'); \
   from sentence_transformers import SentenceTransformer as S; S('all-MiniLM-L6-v2'); \
   print('offline assets ok')"
```

This runs with `HF_HUB_OFFLINE=1` already set in the image, so a download attempt
fails rather than silently succeeding.

### Logs

```bash
# Docker
docker compose logs -f deep-research-agent

# Kubernetes
kubectl -n deep-research-agent logs -f deploy/deep-research-agent
kubectl -n deep-research-agent logs deploy/deep-research-agent | grep -iE 'error|traceback'
```
