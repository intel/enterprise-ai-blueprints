# Deployment Guide

This guide covers deploying the TechGadgets Customer Support Agent on
[Intel® AI for Enterprise Solutions](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2)
(release-1.2, agent-toolkit layer) with Helm, and running it locally with Docker
Compose.

> ⚠️ **No user authentication — demo data only.** The Streamlit UI, the FastAPI
> API and every tool endpoint are open, and the chart's HTTPRoute bypasses the
> gateway's LiteLLM-key check. Anyone who can resolve
> `customer-support-agent.<base_domain_name>` can use the agent and spend LLM
> quota. Set `httpRoute.bypassGatewayAuth=false` to have the gateway require a
> LiteLLM key on every request, or use `kubectl port-forward` only.

## Prerequisites

### Platform

Deploy AI Solutions (agent-toolkit) first — see the
[AI Solutions setup guide](../../../docs/ai-solutions-setup.md).

| Component | Namespace | Switch (`env/<env>/config.agent-toolkit.yaml`) |
|---|---|---|
| Kubernetes, Istio, Envoy Gateway (`eg-gateway`) | — / `envoy-gateway-system` | installed by `./es_auto_installer.sh install agent-toolkit` |
| LiteLLM | `litellm` | always installed by the inference layer |
| Chat model | — | `llm_services_autodeploy_models` (default `["qwen2-5-coder-14b"]`) |
| Embedding model `bge-base-en` | — | add `"bge-base-en"` to `llm_services_autodeploy_models` (needed for memory only) |
| Redis Stack | `redis` | `redis_stack_enabled` (needed for memory only) |
| KubeRay | `ray-system` | `kuberay_enabled` (only for `agent.parallel.executor=ray`) |
| LiteLLM access | — | add `customer-support-agent` to `litellm_allowed_namespaces` |

Example `config.agent-toolkit.yaml` excerpt:

```yaml
llm_services_autodeploy_models: ["qwen2-5-coder-14b", "bge-base-en"]
redis_stack_enabled: true
litellm_allowed_namespaces:
  - envoy-gateway-system
  - litellm
  - agent-sandbox-system
  - ray-system
  - customer-support-agent
```

Then `./es_auto_installer.sh install agent-toolkit`.

Because tool calls are parsed from the model's text (`<tool_call>` blocks), the
chat model does **not** need native function-calling support.

### Local tooling

| Mode | Requires |
|---|---|
| Kubernetes | `kubectl` (AI Solutions kubeconfig, `env/<env>/kubeconfig.yaml`), `helm` 3, and `nerdctl` (or `docker`) to build the image |
| Docker | Docker Engine 24+ with Compose v2, and a reachable LiteLLM endpoint + key |

## Kubernetes Deployment

### Step 1: Configure

```bash
# From the repo root
cp config.env config.local.env
$EDITOR config.local.env          # optionally MODEL_NAME, OPENAI_API_KEY (LiteLLM virtual key)
set -a && source config.local.env && set +a
```

`deploy.sh` reads `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` and accepts the
repo-wide `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL_NAME` as aliases.
Everything else is auto-discovered:

| Value | Source |
|---|---|
| `CLUSTER_URL` (base domain) | `eg-gateway` listener hostname |
| `LLM_BASE_URL` | `http://litellm.litellm.svc.cluster.local:4000/v1` |
| `LLM_API_KEY` | `litellm/litellm-master-key` (`master_key`) — prefer a virtual key |
| `REDIS_URL` | `redis/redis-stack-server-credentials` (`REDIS_URL`, authenticated) |
| `LLM_MODEL` | prompted, default `qwen2-5-coder-14b` |
| `EMBEDDING_MODEL` / `MEMORY_ENABLED` | defaults `bge-base-en` / `true` |

### Step 2: Deploy

```bash
./deploy-usecase.sh customer-support-agent
# or: cd usecases/customer-support-agent && ./deploy.sh
```

The script:

1. Builds `customer-support-agent:latest` into the containerd `k8s.io` namespace
   with `nerdctl` (installs/starts `buildkitd` if needed). Use
   `SKIP_BUILD=true AGENT_IMAGE=<registry>/customer-support-agent:<tag>` for a
   pre-built image, `CONTAINER_TOOL=docker` to force docker.
2. Creates the `customer-support-agent` namespace with labels
   `istio.io/dataplane-mode=ambient` and Pod Security `restricted`.
3. Checks that the namespace is allowed to call LiteLLM; with
   `PATCH_LITELLM_AUTHZ=true` it adds it to the `litellm-ingress`
   AuthorizationPolicy directly (temporary — overwritten by the next platform
   install).
4. Runs `helm upgrade --install customer-support-agent ./helm-chart --wait`
   with an HTTPRoute for `customer-support-agent.<base_domain_name>` (override
   with `INGRESS_HOST`; namespace with `NAMESPACE`).

### Step 3: Verify

```bash
kubectl get pods,svc,httproute,securitypolicy -n customer-support-agent
kubectl logs -n customer-support-agent deploy/customer-support-agent | grep -E "memory|Uvicorn|Streamlit"
#   [memory] memory available: True
```

### Step 4: Access

Map the hostname to the gateway IP and trust the platform CA (see
[ai-solutions-setup.md](../../../docs/ai-solutions-setup.md#3-reach-the-gateway-hostnames--ca)):

```bash
kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=eg-gateway
# /etc/hosts:  <gateway-ip> customer-support-agent.solutions.ai

curl --cacert env/local/logs/ai-solutions-ca.crt \
  https://customer-support-agent.solutions.ai/api/v1/health
```

| URL | Backend |
|---|---|
| `https://customer-support-agent.<base>/` | Streamlit UI (Service port 8501) |
| `https://customer-support-agent.<base>/api/...` | FastAPI (Service port 8502) |
| `https://customer-support-agent.<base>/v1/...` | FastAPI OpenAI-compatible endpoints (8502) |

Port-forward alternative:

```bash
kubectl port-forward -n customer-support-agent svc/customer-support-agent 8501:8501 8502:8502
#   http://localhost:8501   Streamlit UI
#   http://localhost:8502   HTML chat UI + API (the HTML UI at / is only reachable this way)
```

### Manual Helm

```bash
kubectl create namespace customer-support-agent
kubectl label namespace customer-support-agent istio.io/dataplane-mode=ambient \
  pod-security.kubernetes.io/enforce=restricted

REDIS_URL=$(kubectl get secret -n redis redis-stack-server-credentials \
  -o jsonpath='{.data.REDIS_URL}' | base64 -d)

helm upgrade --install customer-support-agent ./helm-chart \
  --namespace customer-support-agent \
  --set-string agent.llmApiKey="sk-your-litellm-key" \
  --set agent.llmModel="qwen2-5-coder-14b" \
  --set agent.memory.redisUrl="${REDIS_URL}" \
  --set httpRoute.enabled=true \
  --set httpRoute.host="customer-support-agent.solutions.ai" \
  --wait --timeout 5m
```

## Helm Chart Reference

### Values Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `agent.image.repository` | string | `customer-support-agent` | Container image repository |
| `agent.image.tag` | string | `latest` | Container image tag |
| `agent.image.pullPolicy` | string | `IfNotPresent` | Image pull policy (keep for locally built images) |
| `agent.replicaCount` | int | `1` | Replicas (each pod has its own SQLite copy) |
| `agent.llmBaseUrl` | string | `http://litellm.litellm.svc.cluster.local:4000/v1` | LiteLLM endpoint |
| `agent.llmApiKey` | string | `""` | **Required.** LiteLLM key (stored in the Secret) |
| `agent.llmModel` | string | `qwen2-5-coder-14b` | Chat model registered in LiteLLM |
| `agent.memory.enabled` | bool | `true` | Long-term memory (fail-open) |
| `agent.memory.redisUrl` | string | `redis://redis-stack-server.redis.svc.cluster.local:6379/0` | Redis URL — must include credentials on AI Solutions (`deploy.sh` sets it) |
| `agent.memory.index` | string | `csa_mem_idx` | RediSearch index name |
| `agent.memory.topK` | int | `4` | Memories recalled per query |
| `agent.memory.embeddingModel` | string | `bge-base-en` | Embedding model served by LiteLLM |
| `agent.memory.embeddingDims` | int | `768` | Embedding dimension (must match the model) |
| `agent.parallel.executor` | string | `local` | `local` (thread pool) or `ray` (KubeRay) |
| `agent.parallel.maxParallel` | int | `8` | Max concurrent tool calls per batch |
| `agent.parallel.rayAddress` | string | `ray://ray-cluster-head-svc.ray-system:10001` | Ray client address |
| `agent.parallel.selfApiUrl` | string | `""` | Callback URL for Ray tasks; empty = `http://<release>.<namespace>.svc.cluster.local:8502` |
| `agent.apiPort` | int | `8502` | FastAPI container port |
| `agent.streamlitPort` | int | `8501` | Streamlit container port |
| `agent.dataDir` | string | `/app/data` | SQLite location (`emptyDir`, reseeded deterministically on start) |
| `agent.resources` | map | 500m/512Mi requests, 2/2Gi limits | Container resources |
| `agent.nodeSelector` | map | `{}` | Pin to the node holding a locally built image |
| `service.type` | string | `ClusterIP` | `ClusterIP`, `NodePort` or `LoadBalancer` |
| `service.streamlitNodePort` / `service.apiNodePort` | string | `""` | Pin NodePorts (30000–32767) |
| `httpRoute.enabled` | bool | `false` | Create an HTTPRoute on `eg-gateway` (`deploy.sh` sets `true`) |
| `httpRoute.host` | string | `""` | e.g. `customer-support-agent.solutions.ai` |
| `httpRoute.timeout` | string | `""` | Request timeout, e.g. `900s`; empty = gateway default (600s) |
| `httpRoute.gateway.name` / `.namespace` / `.sectionName` | string | `eg-gateway` / `envoy-gateway-system` / `https` | Parent gateway listener |
| `httpRoute.bypassGatewayAuth` | bool | `true` | Ship an empty route-level SecurityPolicy so browsers can reach the app without a LiteLLM key. `false` = the gateway requires a LiteLLM key on every request |

TLS is terminated at `eg-gateway` with its wildcard `*.<base_domain_name>`
certificate — the chart needs no TLS secret.

### Kubernetes Resources Created

| Resource | Name | Purpose |
|----------|------|---------|
| Deployment | `customer-support-agent` | Runs the agent container (FastAPI + Streamlit) |
| Service | `customer-support-agent` | Exposes ports 8501 and 8502 |
| ConfigMap | `customer-support-agent-config` | Non-sensitive configuration |
| Secret | `customer-support-agent-secret` | `LLM_API_KEY` |
| HTTPRoute | `customer-support-agent` | `/api`, `/v1` → 8502; `/` → 8501 |
| SecurityPolicy | (route-level, empty) | Opts the route out of the gateway's LiteLLM-key check (when `bypassGatewayAuth: true`) |

The pod runs as UID/GID 10001 with `runAsNonRoot`, `fsGroup`, seccomp
`RuntimeDefault`, `allowPrivilegeEscalation: false` and all capabilities
dropped, to satisfy Pod Security `restricted`.

## Docker Deployment

```bash
export LITELLM_URL=https://litellm.solutions.ai/v1   # https://litellm.<base_domain_name>/v1
export LITELLM_API_KEY=sk-your-litellm-key          # LiteLLM virtual key
export MODEL_NAME=qwen2-5-coder-14b

./deploy-usecase.sh customer-support-agent --mode docker     # from the repo root
# or: cd usecases/customer-support-agent && docker compose up --build -d
```

`docker-compose.yml` maps `LITELLM_URL` / `LITELLM_API_KEY` / `MODEL_NAME` onto
`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`, points `REDIS_URL` at its own
`redis-stack` container, and reads the remaining settings from
`config/agent-config.env`. The LiteLLM hostname must resolve to the `eg-gateway`
IP from inside the container.

| Container | Image | Ports | Purpose |
|-----------|-------|-------|---------|
| `customer-support-agent` | built from `./src` | 8501, 8502 | Streamlit UI + FastAPI |
| `customer-support-redis` | `redis/redis-stack-server:7.4.0-v8` | 6379 | RediSearch memory store |

```bash
docker compose ps
curl -s http://localhost:8502/api/v1/health
docker compose down            # add -v to drop the Redis memory volume
```

## Health Checks and Monitoring

`GET /api/v1/health`:

```json
{
  "status": "healthy",
  "model": "qwen2-5-coder-14b",
  "tools_count": 40,
  "memory_enabled": true,
  "executor_backend": "local"
}
```

| Probe | Path | Initial Delay | Period | Timeout |
|-------|------|---------------|--------|---------|
| Liveness | `/api/v1/health` (port 8502) | 30s | 15s | 5s |
| Readiness | `/api/v1/health` (port 8502) | 10s | 10s | 5s |

- `memory_enabled` reflects whether the RediSearch index could be created at
  startup. Embedding failures (e.g. `bge-base-en` not served) show up only as
  `[memory] embed failed` log lines; the agent keeps answering without memory.
- `/api/v1/chat` responses include `metadata.latency_ms`, `tools_called`,
  `parallel_batches` and `memory_used`; `/v1/chat/completions` responses include
  an `agent_trace` with tool-call details.
- LiteLLM traces every model call to Langfuse (`https://langfuse.<base>`) on the
  platform side.

## Troubleshooting

### Container fails to start

```bash
kubectl describe pod -n customer-support-agent -l app.kubernetes.io/name=customer-support-agent
kubectl logs -n customer-support-agent deploy/customer-support-agent --previous
```

| Cause | Fix |
|---|---|
| `ImagePullBackOff` | Image built with docker on a containerd cluster, or on another node. Build with nerdctl into `k8s.io`, pin `agent.nodeSelector`, or push to a registry and use `SKIP_BUILD=true AGENT_IMAGE=…` |
| `violates PodSecurity "restricted"` | Keep the chart's `securityContext` blocks |
| Helm error `agent.llmApiKey is required` | Set `LLM_API_KEY`/`OPENAI_API_KEY`, or make sure `litellm-master-key` is readable for auto-discovery |

### LLM connection errors

```bash
kubectl exec -n customer-support-agent deploy/customer-support-agent -- python -c "
import os, urllib.request
r = urllib.request.Request(os.environ['LLM_BASE_URL'] + '/models',
    headers={'Authorization': 'Bearer ' + os.environ['LLM_API_KEY']})
print(urllib.request.urlopen(r).read()[:300])"
```

- `RBAC: access denied` / connection reset → add `customer-support-agent` to
  `litellm_allowed_namespaces` and re-run `./es_auto_installer.sh install agent-toolkit`
  (or redeploy with `PATCH_LITELLM_AUTHZ=true`). Check the namespace has
  `istio.io/dataplane-mode=ambient`.
- `401` → wrong key; `400` model not found → `LLM_MODEL` is not a registered
  LiteLLM model name.

### Tool calls not working

```bash
curl -sk https://customer-support-agent.solutions.ai/api/v1/tools | python3 -m json.tool | head
curl -sk -X POST https://customer-support-agent.solutions.ai/api/v1/tools/get_order_details \
  -H 'Content-Type: application/json' -d '{"arguments":{"order_id":"ORD-2024-000013"}}'
```

If tools work directly but the agent never calls them, the model is not
following the `<tool_call>` format — try a stronger instruction-following model.

### Memory not working

| Symptom | Fix |
|---|---|
| `memory_enabled: false`, logs `redis connection failed` | `REDIS_URL` missing credentials or Redis down: `kubectl get pods -n redis`; redeploy so `deploy.sh` re-reads `redis-stack-server-credentials` |
| Logs `embed failed` | `bge-base-en` not served by LiteLLM — add it to `llm_services_autodeploy_models`, or set `EMBEDDING_MODEL`/`EMBEDDING_DIMS` to a served embedding model |
| `index setup failed` after changing the embedding model | Drop the old index: `kubectl exec -n redis statefulset/redis-stack-server -- redis-cli -a <pw> FT.DROPINDEX csa_mem_idx DD`, then restart the pod |
| Want to run without memory | `MEMORY_ENABLED=false ./deploy.sh` |

### Gateway / routing

| Symptom | Fix |
|---|---|
| `401` from the gateway | Keep `httpRoute.bypassGatewayAuth=true` (`kubectl get securitypolicy -n customer-support-agent`), or send a LiteLLM key if you disabled it deliberately |
| Hostname does not resolve / TLS error | Add the `/etc/hosts` entry for the `eg-gateway` IP and trust `env/local/logs/ai-solutions-ca.crt` |
| Streamlit stuck on "Connecting…" | The websocket `/_stcore/stream` must reach port 8501 — check `kubectl get httproute -n customer-support-agent -o yaml` |
| Long agent runs cut off | Set `httpRoute.timeout` (e.g. `900s`) |

### Database issues

The SQLite DB is created and seeded at startup in `/app/data` (an `emptyDir`).
To reset it, restart the pod:

```bash
kubectl rollout restart deploy/customer-support-agent -n customer-support-agent
```
