# Coding Agent (LangGraph) -- Deployment Guide

This guide covers deploying the LangGraph Coding Agent on
[Intel® Agent Toolkit](https://github.com/intel/enterprise-agent-toolkit/tree/release-2.0.0) (installed by
[Toolkit Manager](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2) release-1.2) with Helm, and running it locally with Docker
Compose.

| Item | Value |
|---|---|
| Namespace | `coding-agent` |
| Helm release / Service | `coding-agent-langgraph` |
| External host | `https://coding-agent-langgraph.<base_domain_name>` |
| In-cluster URL | `http://coding-agent-langgraph.coding-agent.svc.cluster.local:8090` |
| Port | `8090` (API only — no web UI) |
| Auth | `Authorization: Bearer <DEVUI_AUTH_TOKEN>` on `/v1/*`; `/health` is public |

## Prerequisites

### Platform

Install Intel® Agent Toolkit with Toolkit Manager first — see the
[Toolkit Manager setup guide](../../../docs/toolkit-manager-setup.md).

| Component | Namespace | Switch (`env/<env>/config.agent-toolkit.yaml`) |
|---|---|---|
| Kubernetes, Istio, Envoy Gateway (`eg-gateway`) | — / `envoy-gateway-system` | installed by `./es_auto_installer.sh install agent-toolkit` |
| LiteLLM | `litellm` | always installed by Intel® Inference Toolkit |
| Agent Sandbox controller + router | `agent-sandbox-system` | `agent_sandbox_enabled` |
| Tool-calling LLM model | — | `llm_services_autodeploy_models` (default `["qwen2-5-coder-14b"]`) |
| LiteLLM access | — | add `coding-agent` to `litellm_allowed_namespaces` |
| Langfuse (optional) | `langfuse` | Intel® Inference Toolkit |
| KubeRay (optional) | `ray-system` | `kuberay_enabled` |

Example `config.agent-toolkit.yaml` excerpt, then
`./es_auto_installer.sh install agent-toolkit`:

```yaml
agent_sandbox_enabled: true
litellm_allowed_namespaces:
  - envoy-gateway-system
  - litellm
  - agent-sandbox-system
  - ray-system
  - coding-agent
```

Check the sandbox resources the agent uses:

```bash
kubectl get pods -n agent-sandbox-system
kubectl get sandboxtemplate,sandboxwarmpool -n agent-sandbox-system
#   python-sandbox-template, python-pool
```

### Local tooling

| Mode | Requires |
|---|---|
| Kubernetes | `kubectl` (Toolkit Manager kubeconfig `env/<env>/kubeconfig.yaml`), `helm` 3, `nerdctl` (or `docker`) to build the image |
| Docker | Docker Engine 24+ with Compose v2, a reachable LiteLLM endpoint + key, and a port-forwarded sandbox router |

## Kubernetes Deployment

### Quick start with deploy.sh

```bash
# From the repo root
cp config.env config.local.env
$EDITOR config.local.env           # MODEL_NAME (e.g. qwen2-5-coder-14b); optionally OPENAI_API_KEY (virtual key)
set -a && source config.local.env && set +a
./deploy-usecase.sh coding-agent
# or: cd usecases/coding-agent && ./deploy.sh
```

The script:

1. Auto-discovers `CLUSTER_URL` (from the `eg-gateway` listener), the LiteLLM
   key (`litellm/litellm-master-key`, when `OPENAI_API_KEY` is empty), the
   Langfuse keys (`langfuse/langfuse-secrets`), the authenticated Redis URL
   (`redis/redis-stack-server-credentials`) and the sandbox-router token
   (`agent-sandbox-system/sandbox-router-auth`); prompts for anything still
   empty (e.g. `MODEL_NAME`).
2. Generates `DEVUI_AUTH_TOKEN` (`openssl rand -hex 24`) unless you set one.
3. Builds `coding-agent-langgraph:latest` into the containerd `k8s.io` namespace
   with `nerdctl` (installs/starts `buildkitd` if needed, passes proxy build args).
   `SKIP_BUILD=true AGENT_IMAGE=<registry>/<image>:<tag>` uses a pre-built image;
   `CONTAINER_TOOL=docker` forces docker.
4. Creates the `coding-agent` namespace with `istio.io/dataplane-mode=ambient`
   and Pod Security `restricted` labels, and checks LiteLLM access
   (`PATCH_LITELLM_AUTHZ=true` adds the namespace to the `litellm-ingress` policy
   directly — temporary, overwritten by the next platform install).
5. Runs `helm upgrade --install coding-agent-langgraph ./helm-chart --wait --timeout 5m`
   with an HTTPRoute for `coding-agent-langgraph.<CLUSTER_URL>` (override with
   `INGRESS_HOST`; namespace with `NAMESPACE`).
6. Prints the URL, the gateway IP hint and the API bearer token.

### Accessing the API

```bash
kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=eg-gateway
# /etc/hosts:  <gateway-ip> coding-agent-langgraph.solutions.ai

export AUTH_TOKEN=<token printed by deploy.sh>
curl --cacert env/local/logs/ai-solutions-ca.crt https://coding-agent-langgraph.solutions.ai/health
curl -sk https://coding-agent-langgraph.solutions.ai/v1/entities -H "Authorization: Bearer $AUTH_TOKEN"
```

Port-forward alternative:

```bash
kubectl port-forward -n coding-agent svc/coding-agent-langgraph 8090:8090
#   http://localhost:8090
```

Lost the token? It is stored in the release Secret:

```bash
kubectl get secret -n coding-agent coding-agent-langgraph-secret \
  -o jsonpath='{.data.DEVUI_AUTH_TOKEN}' | base64 -d && echo
```

### Manual Helm deployment

```bash
kubectl create namespace coding-agent
kubectl label namespace coding-agent istio.io/dataplane-mode=ambient \
  pod-security.kubernetes.io/enforce=restricted

ROUTER_TOKEN=$(kubectl get secret -n agent-sandbox-system sandbox-router-auth \
  -o jsonpath='{.data.ROUTER_AUTH_TOKEN}' | base64 -d)

helm upgrade --install coding-agent-langgraph ./helm-chart \
  --namespace coding-agent \
  --set agent.image.repository=coding-agent-langgraph --set agent.image.tag=latest \
  --set-string agent.openaiApiKey="sk-your-litellm-key" \
  --set agent.modelName="qwen2-5-coder-14b" \
  --set-string agent.devuiAuthToken="$(openssl rand -hex 24)" \
  --set-string agent.sandboxRouterToken="${ROUTER_TOKEN}" \
  --set httpRoute.enabled=true \
  --set httpRoute.host="coding-agent-langgraph.solutions.ai" \
  --wait --timeout 5m
```

Keep the release name `coding-agent-langgraph`: the chart derives the Service,
Secret, ServiceAccount and HTTPRoute names from it.

### Helm Values Reference (values.yaml)

#### Agent

| Key | Default | Description |
|---|---|---|
| `agent.replicaCount` | `1` | Replicas (conversation state and the sandbox are per pod) |
| `agent.image.repository` / `.tag` / `.pullPolicy` | `coding-agent` / `latest` / `IfNotPresent` | Image (`deploy.sh` sets `coding-agent-langgraph:latest`) |
| `agent.openaiApiKey` | `""` | **Required.** LiteLLM key (Secret) |
| `agent.openaiBaseUrl` | `http://litellm.litellm.svc.cluster.local:4000/v1` | LiteLLM URL |
| `agent.modelName` | `qwen2-5-coder-14b` | Model registered in LiteLLM; must support tool calling |
| `agent.devuiAuthToken` | `""` | **Required.** API bearer token (Secret) |
| `agent.noProxy` | cluster CIDRs + `.svc` | `no_proxy`/`NO_PROXY` for in-cluster traffic |
| `agent.workspaceDir` | `/app/workspace` | Workspace mount path |
| `agent.sandboxNamespace` | `agent-sandbox-system` | Sandbox namespace |
| `agent.sandboxTemplate` | `python-sandbox-template` | SandboxTemplate |
| `agent.sandboxWarmPool` | `python-pool` | Warm pool to adopt sandboxes from |
| `agent.sandboxRouterUrl` | `http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080` | Router URL |
| `agent.sandboxRouterToken` | `""` | Router bearer token (Secret) |
| `agent.ray.enabled` / `agent.ray.address` | `false` / `""` | Ray parallel execution (`kuberay_enabled`) |
| `agent.langfuse.publicKey` / `.secretKey` / `.host` | `""` | Langfuse tracing (keys in the Secret) |
| `agent.service.type` / `.port` | `ClusterIP` / `8090` | Service |
| `agent.workspace.size` / `.storageClassName` | `5Gi` / `""` | Workspace PVC |
| `agent.resources` | 250m/512Mi requests, 1/1Gi limits | Container resources |
| `agent.affinity` | `{}` | No node affinity required |

#### Redis, RBAC, HTTPRoute

| Key | Default | Description |
|---|---|---|
| `redisUrl` | `redis://redis-stack-server.redis.svc.cluster.local:6379` | Passed as `REDIS_URL` (`deploy.sh` sets the authenticated URL); not used by the in-memory checkpointer |
| `serviceAccount.create` | `true` | Dedicated ServiceAccount |
| `serviceAccount.sandboxAccess` | `true` | Role/RoleBinding in `agent.sandboxNamespace` for `sandboxclaims` (and read on `sandboxes`) |
| `httpRoute.enabled` | `false` | HTTPRoute on `eg-gateway` (`deploy.sh` sets `true`) |
| `httpRoute.host` | `""` | e.g. `coding-agent-langgraph.solutions.ai` |
| `httpRoute.path` | `/` | Path prefix |
| `httpRoute.timeout` | `""` | Request timeout, e.g. `900s`; empty = gateway default (600s) |
| `httpRoute.gateway.name` / `.namespace` / `.sectionName` | `eg-gateway` / `envoy-gateway-system` / `https` | Parent gateway listener |
| `httpRoute.bypassGatewayAuth` | `true` | Empty route-level SecurityPolicy so the gateway does not require a LiteLLM key (the bearer token protects the API) |

TLS is terminated at `eg-gateway` with its wildcard `*.<base_domain_name>`
certificate — the chart needs no TLS secret.

### Kubernetes resources created

| Resource | Name |
|---|---|
| Deployment, Service, ServiceAccount, HTTPRoute | `coding-agent-langgraph` |
| ConfigMap / Secret | `coding-agent-langgraph-config` / `coding-agent-langgraph-secret` |
| PersistentVolumeClaim | `coding-agent-langgraph-workspace` (5Gi, RWO) |
| SecurityPolicy | `coding-agent-langgraph-no-gateway-auth` |
| Role / RoleBinding (in `agent-sandbox-system`) | `coding-agent-langgraph-sandbox-manager` |

## Docker Deployment

```bash
# Sandbox router from the cluster
kubectl port-forward -n agent-sandbox-system svc/sandbox-router-svc 8080:8080 &

export LITELLM_URL=https://litellm.solutions.ai/v1   # https://litellm.<base_domain_name>/v1
export LITELLM_API_KEY=sk-your-litellm-key          # LiteLLM virtual key
export MODEL_NAME=qwen2-5-coder-14b
export SANDBOX_ROUTER_URL=http://localhost:8080

./deploy-usecase.sh coding-agent --mode docker       # from the repo root
# or: cd usecases/coding-agent && docker compose up --build -d
```

`docker-compose.yml` maps `LITELLM_URL` / `LITELLM_API_KEY` / `MODEL_NAME` onto
`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL_NAME` and reads the rest from
`config/agent-config.env` — put `SANDBOX_ROUTER_TOKEN` (router auth) and,
optionally, `DEVUI_AUTH_TOKEN` there. With `DEVUI_AUTH_TOKEN` empty the API is
unauthenticated. The LiteLLM hostname must resolve to the `eg-gateway` IP from
inside the container, and its CA must be trusted.

| Container | Image | Port | Purpose |
|---|---|---|---|
| `coding-agent` | built from `./src` | 8090 | Agent API |
| `coding-agent-redis` | `redis/redis-stack-server:7.4.0-v8` | 6379 | Redis (reserved for checkpointing) |

```bash
curl http://localhost:8090/health
docker compose logs -f coding-agent
docker compose down          # add -v to drop the workspace volume
```

## Health Checks and Monitoring

| Probe | Path | Initial delay | Period | Failure threshold |
|---|---|---|---|---|
| Liveness | `/health` | 30s | 30s | 3 |
| Readiness | `/health` | 15s | 10s | 3 |

The Docker image has an equivalent `HEALTHCHECK`. With Langfuse keys configured,
every request is traced in `https://langfuse.<base_domain_name>`; LiteLLM traces
model calls on the platform side as well.

```bash
kubectl logs -n coding-agent -l app.kubernetes.io/instance=coding-agent-langgraph -f
#   "Langfuse observability enabled" / "[Sandbox] Sandbox-router bearer-token auth enabled."
```

## Troubleshooting

### Agent fails to start

```bash
kubectl describe pod -n coding-agent -l app.kubernetes.io/instance=coding-agent-langgraph
kubectl logs -n coding-agent deploy/coding-agent-langgraph --previous
```

| Cause | Fix |
|---|---|
| Helm `agent.openaiApiKey is required` / `agent.devuiAuthToken is required` | Set `OPENAI_API_KEY` (or make `litellm-master-key` readable) and let `deploy.sh` generate the token |
| `ImagePullBackOff` | Built with docker on a containerd cluster or on another node — build with nerdctl into `k8s.io`, or `SKIP_BUILD=true AGENT_IMAGE=<registry ref>` |
| `violates PodSecurity "restricted"` | Keep the chart's `securityContext` (UID/GID 10001, drop ALL, seccomp RuntimeDefault) |
| PVC `Pending` | No default StorageClass — set `agent.workspace.storageClassName` |

### Cannot reach LiteLLM

- `RBAC: access denied` / connection reset → add `coding-agent` to
  `litellm_allowed_namespaces` and re-run `./es_auto_installer.sh install agent-toolkit`
  (or redeploy with `PATCH_LITELLM_AUTHZ=true`); confirm the namespace label
  `istio.io/dataplane-mode=ambient`.
- `400` model not found → `MODEL_NAME` must be a name from `GET /v1/models`.
- `401` → wrong `OPENAI_API_KEY`.

### API returns 401

- From the app (`Missing or invalid Authorization header.`) → send
  `Authorization: Bearer <DEVUI_AUTH_TOKEN>` on `/v1/*`.
- From the gateway → the route-level SecurityPolicy is missing; keep
  `httpRoute.bypassGatewayAuth=true` (`kubectl get securitypolicy -n coding-agent`).

### Sandbox errors

| Symptom | Fix |
|---|---|
| `Error executing code in sandbox: ...` (401/403) | Wrong or missing `SANDBOX_ROUTER_TOKEN` — redeploy so `deploy.sh` re-reads `sandbox-router-auth` |
| SandboxClaim `forbidden` | RBAC missing — keep `serviceAccount.sandboxAccess=true`; `kubectl get role,rolebinding -n agent-sandbox-system` |
| Slow first execution | Warm pool empty — `kubectl get sandboxwarmpool -n agent-sandbox-system` |
| `install_package` hangs | Sandbox egress is public-IPs-only with no proxy; the sandbox needs direct internet access |

### Conversation context lost

Expected after a pod restart: the checkpointer is in-memory (see
[implementation.md](implementation.md#conversation-state-checkpointer)).
