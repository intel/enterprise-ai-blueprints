# Agentic AI Stack — Endpoints Reference

This document lists every service endpoint exposed by the Agentic AI Stack
and shows how to discover them dynamically after deployment.

All use cases in this repo consume these endpoints.

---

## Quick Reference

| Service | In-cluster URL | External URL (pattern) |
|---|---|---|
| **GenAI Gateway (LiteLLM)** | `http://genai-gateway-service.genai-gateway.svc.cluster.local:4000` | `https://api.<domain>` |
| **Langfuse (LLM Traces)** | `http://genai-gateway-trace-web.genai-gateway.svc.cluster.local:3000` | `https://trace-api.<domain>` |
| **Redis** | `redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379` *(auth required)* | *(in-cluster only)* |
| **Agent Sandbox Router** | `http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080` | *(in-cluster only)* |
| **Grafana** | `http://grafana.observability.svc.cluster.local:3000` | `https://grafana.<domain>` *(if exposed)* |
| **Prometheus** | `http://prometheus.observability.svc.cluster.local:9090` | *(in-cluster only)* |
| **Coding Agent** | `http://coding-agent.coding-agent-maf.svc.cluster.local:8090` | `https://coding-agent.<domain>` |

---

## How to Discover Endpoints

### All ingress hostnames

```bash
kubectl get ingress -A
```

Example output:
```
NAMESPACE        NAME             CLASS   HOSTS                          ADDRESS        PORTS
genai-gateway    litellm-ingress  nginx   api.example.com                <node-ip>      443
genai-gateway    langfuse-ingress nginx   trace-api.example.com          <node-ip>      443
coding-agent     coding-agent-ing nginx   coding-agent.example.com       <node-ip>      443
```

---

### All cluster services

```bash
kubectl get svc -A
```

---

## GenAI Gateway (LiteLLM)

The GenAI Gateway is the central LLM routing layer. All use cases talk to it
using the OpenAI-compatible API.

### Endpoints

| Path | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check |
| `/v1/models` | GET | List all registered models |
| `/v1/chat/completions` | POST | OpenAI-compatible chat |
| `/v1/completions` | POST | Legacy completions |
| `/v1/embeddings` | POST | Text embeddings |

### Retrieve the API key

The master key location depends on how your toolkit rendered the gateway. Try
the deployment env first (works when the key is set inline), then fall back to a
secret:

```bash
# Inline env (common):
kubectl get deploy -n genai-gateway genai-gateway-deployment \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="LITELLM_MASTER_KEY")]}{.value}{end}' && echo

# …or from a secret, if your install uses one (name/key may vary):
kubectl get secret -n genai-gateway -o name | grep -i litellm
```

### List available models

```bash
curl -sk https://api.example.com/v1/models \
  -H "Authorization: Bearer <api-key>" | python3 -m json.tool
```

### Test a chat completion

```bash
API_KEY=$(kubectl get deploy -n genai-gateway genai-gateway-deployment \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="LITELLM_MASTER_KEY")]}{.value}{end}')

curl -sk https://api.example.com/v1/chat/completions \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 64
  }' | python3 -m json.tool
```

### URLs

```
In-cluster (use for OPENAI_BASE_URL): http://genai-gateway-service.genai-gateway.svc.cluster.local:4000/v1
External:                             https://api.<your-cluster-url>/v1
```

---

## Redis

Redis Stack is deployed in the `redis` namespace and used by agents for:
- Session state persistence
- Conversation history (RediSearch vector index)
- Cross-request context continuity

### In-cluster URL (authenticated)

The shared Redis **requires authentication**. Use the ready-made URL from its
credentials secret (it embeds the user and password):

```bash
kubectl get secret -n redis redis-stack-server-credentials \
  -o jsonpath='{.data.REDIS_URL}' | base64 -d && echo
# -> redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379
```

A passwordless `redis://redis-stack-server.redis.svc.cluster.local:6379` will
fail with `NOAUTH Authentication required.` and crash agent runs when memory is
enabled.

### Verify Redis is up

```bash
PW=$(kubectl get secret -n redis redis-stack-server-credentials \
  -o jsonpath='{.data.REDIS_PASSWORD}' | base64 -d)
kubectl exec -n redis statefulset/redis-stack-server -- redis-cli -a "$PW" ping
# Expected: PONG
```

### Port-forward for local access (development)

```bash
kubectl port-forward -n redis svc/redis-stack-server 6379:6379 &
redis-cli ping
```

---

## Agent Sandbox Router

The sandbox router manages isolated Kubernetes pod environments (SandboxClaims)
for safe code execution. It is accessed by agents in-cluster via a stable
ClusterIP service.

### In-cluster URL

The controller/router install into the **`agent-sandbox-system`** namespace by
default (kubernetes-sigs/agent-sandbox). Verify with `kubectl get sandboxtemplate,sandboxwarmpool -A`.

```
http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080
```

### Router authentication

When the router runs with `ALLOW_UNAUTHENTICATED_ROUTER=false` (the secure
default), every request needs `Authorization: Bearer <ROUTER_AUTH_TOKEN>`.
Retrieve the token:

```bash
kubectl get secret -n agent-sandbox-system sandbox-router-auth \
  -o jsonpath='{.data.ROUTER_AUTH_TOKEN}' | base64 -d && echo
```

The coding-agent consumes this as `SANDBOX_ROUTER_TOKEN` (its `deploy.sh`
auto-discovers it).

### Verify the router is up

```bash
kubectl get pods -n agent-sandbox-system
kubectl get svc  -n agent-sandbox-system
```

### Port-forward for local development

```bash
kubectl port-forward -n agent-sandbox-system svc/sandbox-router-svc 8080:8080 &
curl http://localhost:8080/healthz
```

Use `SANDBOX_ROUTER_URL=http://localhost:8080` when running the coding agent
locally against a remote cluster.

---

## Langfuse (LLM Observability Traces)

Langfuse captures every LLM call, token count, latency, and agent step. It is
**optional** — the blueprints deploy and run without it.

### URLs

```
In-cluster : http://genai-gateway-trace-web.genai-gateway.svc.cluster.local:3000
External   : https://trace-api.<your-cluster-url>
```

### Retrieve API keys

The keys are set inline on the gateway deployment (there is no `langfuse-secret`):

```bash
# Public key
kubectl get deploy -n genai-gateway genai-gateway-deployment \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="LANGFUSE_PUBLIC_KEY")]}{.value}{end}' && echo

# Secret key
kubectl get deploy -n genai-gateway genai-gateway-deployment \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="LANGFUSE_SECRET_KEY")]}{.value}{end}' && echo
```

### UI login (initial admin user)

Self-signup is disabled; log in with the initial admin user set on the
`genai-gateway-trace-web` deployment:

```bash
# Email (username)
kubectl get deploy -n genai-gateway genai-gateway-trace-web \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="LANGFUSE_INIT_USER_EMAIL")]}{.value}{end}' && echo

# Password
kubectl get deploy -n genai-gateway genai-gateway-trace-web \
  -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="LANGFUSE_INIT_USER_PASSWORD")]}{.value}{end}' && echo
```

Open the UI: `kubectl port-forward -n genai-gateway svc/genai-gateway-trace-web 3000:3000` → `http://localhost:3000`.

### Configure tracing in use cases

Set these env vars (via Helm values or `.env`):

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-xxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxx
LANGFUSE_HOST=https://trace-api.example.com
```

---

## Grafana (Metrics & Dashboards)

Grafana provides real-time dashboards for infrastructure metrics, model
throughput, latency, and token usage.

### Retrieve credentials

```bash
# Default admin password
kubectl get secret -n observability grafana-admin-secret \
  -o jsonpath='{.data.admin-password}' | base64 -d && echo
```

### Port-forward for local access

```bash
kubectl port-forward -n observability svc/grafana 3000:3000
# Open: http://localhost:3000  (admin / <password above>)
```

---

## Coding Agent

The Coding Agent is deployed by this usecases repo into the `coding-agent-maf` namespace
(the Helm release and Service are named `coding-agent`).

### Endpoints (Agent Framework DevUI)

The agent is served by Agent Framework DevUI, which exposes the **OpenAI
Responses API** (not `/v1/chat/completions`). All non-health routes require
`Authorization: Bearer <DEVUI_AUTH_TOKEN>`.

| Path | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check (JSON, no auth) |
| `/v1/entities` | GET | List agent entities (grab the `entity_id`) |
| `/v1/responses` | POST | Run the agent (body: `{"input":"…","metadata":{"entity_id":"…"}}`) |

### External URL (after deploy)

```
https://coding-agent.<your-cluster-url>
```

### Test after deploy

```bash
AGENT=https://coding-agent.api.example.com
TOKEN=<DEVUI_AUTH_TOKEN printed by deploy.sh>

# Health (no auth)
curl -sk "$AGENT/health"

# Discover the entity id
EID=$(curl -sk "$AGENT/v1/entities" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['entities'][0]['id'])")

# Generate and run Python code in the sandbox
curl -sk -X POST "$AGENT/v1/responses" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"input\":\"Find all primes up to 50 with the Sieve of Eratosthenes, run it, and print them.\",\"metadata\":{\"entity_id\":\"$EID\"}}" \
  | python3 -m json.tool
```

### In-cluster URL (for agent-to-agent communication)

```
http://coding-agent.coding-agent-maf.svc.cluster.local:8090
```

---

## Port-Forward Cheatsheet

Useful when testing locally against a remote cluster:

```bash
# GenAI Gateway (LiteLLM)
kubectl port-forward -n genai-gateway svc/genai-gateway-service 4000:4000 &

# Redis
kubectl port-forward -n redis svc/redis-stack-server 6379:6379 &

# Agent Sandbox Router
kubectl port-forward -n agent-sandbox-system svc/sandbox-router-svc 8080:8080 &

# Coding Agent
kubectl port-forward -n coding-agent-maf svc/coding-agent 8090:8090 &

# Grafana
kubectl port-forward -n observability svc/grafana 3000:3000 &

# Langfuse
kubectl port-forward -n genai-gateway svc/genai-gateway-trace-web 3001:3000 &
```

---

## Environment Variable Summary

A ready-to-copy `.env` snippet for use cases connecting to a running stack:

```bash
# GenAI Gateway  (external: https://api.<domain>/v1)
OPENAI_BASE_URL=http://genai-gateway-service.genai-gateway.svc.cluster.local:4000/v1
OPENAI_API_KEY=<litellm-master-key>
MODEL_NAME=Qwen/Qwen2.5-Coder-14B-Instruct   # must match a model from /v1/models

# Redis (authenticated — includes user:password)
REDIS_URL=redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379

# Agent Sandbox
SANDBOX_ROUTER_URL=http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080
SANDBOX_ROUTER_TOKEN=<router-auth-token>   # when ALLOW_UNAUTHENTICATED_ROUTER=false
SANDBOX_NAMESPACE=agent-sandbox-system
SANDBOX_WARMPOOL=python-pool
SANDBOX_TEMPLATE=python-sandbox-template

# Observability (optional)  (external: https://trace-api.<domain>)
LANGFUSE_PUBLIC_KEY=pk-lf-xxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxx
LANGFUSE_HOST=http://genai-gateway-trace-web.genai-gateway.svc.cluster.local:3000
```
