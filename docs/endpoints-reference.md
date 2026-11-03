# Intel® AI for Enterprise Solutions — Endpoints Reference

This document lists every service endpoint exposed by **Intel® AI for Enterprise
Solutions** (agent-toolkit layer, release-1.2) that the blueprints consume, and
shows how to discover them after deployment.

All use cases in this repo consume these endpoints. `<base>` below is the
`base_domain_name` from `env/<env>/global_config.yaml` (default `solutions.ai`) —
the same value the deploy scripts call `CLUSTER_URL`.

---

## Quick Reference

| Service | In-cluster URL | External URL (via `eg-gateway`) |
|---|---|---|
| **LiteLLM** (inference layer) | `http://litellm.litellm.svc.cluster.local:4000/v1` | `https://litellm.<base>/v1` |
| **Langfuse (LLM Traces)** | `http://langfuse-web.langfuse.svc.cluster.local:3000` | `https://langfuse.<base>` |
| **Redis** | `redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379` *(auth required)* | *(in-cluster only)* |
| **Agent Sandbox Router** | `http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080` | *(in-cluster only)* |
| **Grafana** | namespace `monitoring` | `https://grafana.<base>` |
| **OTel Collector** | `opentelemetry-collector.monitoring.svc.cluster.local:4317` (gRPC) / `:4318` (HTTP) | *(in-cluster only)* |
| **Coding Agent** | `http://coding-agent.coding-agent-maf.svc.cluster.local:8090` | `https://coding-agent.<base>` |
| **Banking Assistant** | `http://banking-assistant.banking-assistant.svc.cluster.local:8090` | `https://banking-assistant.<base>` |

Reserved platform hostnames (do not reuse for a blueprint): `inference.<base>`,
`keycloak.<base>`, `grafana.<base>`.

---

## How to Discover Endpoints

### External hostnames (HTTPRoutes)

There is no nginx Ingress. Everything external is an **HTTPRoute** attached to
the Envoy Gateway `eg-gateway` (namespace `envoy-gateway-system`, listener
`https` for `*.<base>`, TLS terminated at the gateway with the wildcard secret
`gateway-tls`):

```bash
kubectl get httproute -A
```

Example output:
```
NAMESPACE           NAME                HOSTNAMES                           AGE
litellm             litellm             ["litellm.solutions.ai"]            2d
langfuse            langfuse            ["langfuse.solutions.ai"]           2d
coding-agent-maf    coding-agent        ["coding-agent.solutions.ai"]       1h
banking-assistant   banking-assistant   ["banking-assistant.solutions.ai"]  1h
```

### Gateway IP, /etc/hosts and CA

```bash
kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=eg-gateway
```

Add the hostnames you need to `/etc/hosts`:

```
<gateway-ip> coding-agent.<base> banking-assistant.<base> litellm.<base> langfuse.<base>
```

The gateway certificate is self-signed by default; its CA is exported by the
installer to `env/local/logs/ai-solutions-ca.crt`. Use
`curl --cacert env/local/logs/ai-solutions-ca.crt …` (or `curl -k`).

> **Gateway auth.** `eg-gateway` carries a gateway-wide SecurityPolicy
> `gateway-auth` (extAuth → LiteLLM `/key/verify`), so every route requires a
> LiteLLM key **unless** the app ships its own (empty) route-level
> SecurityPolicy. The blueprint charts do (`httpRoute.bypassGatewayAuth: true`)
> because DevUI enforces its own `DEVUI_AUTH_TOKEN`.

### All cluster services

```bash
kubectl get svc -A
```

---

## LiteLLM

LiteLLM is the central LLM routing layer, installed by the **inference layer**.
All use cases talk to it using the OpenAI-compatible API. It already sends traces
to Langfuse via callbacks, and the `content_safety` guardrail is on by default.

> **Mesh access.** LiteLLM is guarded by the Istio AuthorizationPolicy
> `litellm-ingress` (ns `litellm`). A blueprint namespace must be listed in
> `litellm_allowed_namespaces` in `env/<env>/config.agent-toolkit.yaml` (then
> re-run `./es_auto_installer.sh install agent-toolkit`) and be labelled
> `istio.io/dataplane-mode=ambient` (deploy.sh does the label). See
> [ai-solutions-setup.md](ai-solutions-setup.md#allow-the-blueprint-namespaces-to-reach-litellm).

### Endpoints

| Path | Method | Description |
|---|---|---|
| `/health/liveliness` | GET | Liveness check |
| `/v1/models` | GET | List all registered models |
| `/v1/chat/completions` | POST | OpenAI-compatible chat |
| `/v1/completions` | POST | Legacy completions |
| `/v1/embeddings` | POST | Text embeddings |
| `/key/generate` | POST | Create a virtual key (master key required) |

### Retrieve the master key

```bash
kubectl get secret litellm-master-key -n litellm \
  -o jsonpath='{.data.master_key}' | base64 -d && echo
```

The LiteLLM UI (`https://litellm.<base>/ui`) logs in as `admin` with this key.

### Create a virtual key (recommended for blueprints)

```bash
curl -k https://litellm.<base>/key/generate \
  -H "Authorization: Bearer <master_key>" \
  -H 'Content-Type: application/json' \
  -d '{"models":["qwen2-5-coder-14b"]}'
```

### List available models

```bash
curl -sk https://litellm.<base>/v1/models \
  -H "Authorization: Bearer <api-key>" | python3 -m json.tool
```

The default model id is `qwen2-5-coder-14b` (plus `hybrid-router` when
`hybrid_router_enabled`).

### Test a chat completion

```bash
API_KEY=$(kubectl get secret litellm-master-key -n litellm \
  -o jsonpath='{.data.master_key}' | base64 -d)

curl -sk https://litellm.<base>/v1/chat/completions \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2-5-coder-14b",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 64
  }' | python3 -m json.tool
```

### URLs

```
In-cluster (use for OPENAI_BASE_URL): http://litellm.litellm.svc.cluster.local:4000/v1
External:                             https://litellm.<base>/v1
```

---

## Redis

Redis Stack is deployed in the `redis` namespace (`redis_stack_enabled`) and used
by agents for:
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
redis-cli -a "$PW" ping
```

---

## Agent Sandbox Router

The sandbox router manages isolated Kubernetes pod environments (SandboxClaims)
for safe code execution. It is accessed by agents in-cluster via a stable
ClusterIP service.

### In-cluster URL

The controller (v0.5.0, CRDs `extensions.agents.x-k8s.io/v1beta1`) and router
install into the **`agent-sandbox-system`** namespace (`agent_sandbox_enabled`),
with the `python-sandbox-template` SandboxTemplate and the `python-pool` warm pool
(size 5). Verify with `kubectl get sandboxtemplate,sandboxwarmpool -A`.

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

### Sandbox network policy

The NetworkPolicy `eat-sandbox-pool-egress` restricts sandbox pods: they accept
ingress **only from the router**, and egress goes **to public IPs only** — no
in-cluster services and no proxy. Code run in the sandbox therefore cannot call
LiteLLM or Redis, and `pip install` inside the sandbox needs direct internet
access.

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
installed by the **inference layer**, and LiteLLM already sends its own traces to
it. It is **optional** for the blueprints — they deploy and run without it.

### URLs

```
In-cluster : http://langfuse-web.langfuse.svc.cluster.local:3000
External   : https://langfuse.<base>
```

### Retrieve API keys

The seeded project's keys are in the Secret `langfuse-secrets` (ns `langfuse`):

```bash
# Public key (pk-lf-…)
kubectl get secret langfuse-secrets -n langfuse \
  -o jsonpath='{.data.public_key}' | base64 -d && echo

# Secret key (sk-lf-…)
kubectl get secret langfuse-secrets -n langfuse \
  -o jsonpath='{.data.secret_key}' | base64 -d && echo
```

`deploy.sh` auto-discovers these when `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`
are unset. The same Secret also holds `project_id`.

### UI login (initial admin user)

Self-signup is disabled; log in with the initial admin user from the same Secret:

```bash
kubectl get secret langfuse-secrets -n langfuse -o jsonpath='{.data.admin_email}'    | base64 -d && echo
kubectl get secret langfuse-secrets -n langfuse -o jsonpath='{.data.admin_password}' | base64 -d && echo
```

Open the UI at `https://langfuse.<base>`, or
`kubectl port-forward -n langfuse svc/langfuse-web 3000:3000` → `http://localhost:3000`.

### Configure tracing in use cases

Set these env vars (via Helm values or `.env`):

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-xxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxx
LANGFUSE_HOST=http://langfuse-web.langfuse.svc.cluster.local:3000
```

---

## Grafana (Metrics & Dashboards)

Grafana provides real-time dashboards for infrastructure metrics, model
throughput, latency, and token usage. It runs in the `monitoring` namespace and
is exposed at `https://grafana.<base>`. The platform's OpenTelemetry collector is
`opentelemetry-collector.monitoring.svc.cluster.local` (`4317` gRPC, `4318` HTTP).

### Retrieve credentials

```bash
# Admin user / password
kubectl get secret observability-credentials -n monitoring -o jsonpath='{.data.grafanaAdminUser}' | base64 -d; echo
kubectl get secret observability-credentials -n monitoring -o jsonpath='{.data.grafanaAdminPassword}' | base64 -d; echo
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
https://coding-agent.<base>
```

### Test after deploy

```bash
AGENT=https://coding-agent.solutions.ai
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
# LiteLLM
kubectl port-forward -n litellm svc/litellm 4000:4000 &

# Redis
kubectl port-forward -n redis svc/redis-stack-server 6379:6379 &

# Agent Sandbox Router
kubectl port-forward -n agent-sandbox-system svc/sandbox-router-svc 8080:8080 &

# Coding Agent
kubectl port-forward -n coding-agent-maf svc/coding-agent 8090:8090 &

# Banking Assistant
kubectl port-forward -n banking-assistant svc/banking-assistant 8091:8090 &

# Langfuse
kubectl port-forward -n langfuse svc/langfuse-web 3000:3000 &
```

Grafana is easiest via `https://grafana.<base>`.

---

## Environment Variable Summary

A ready-to-copy `.env` snippet for use cases connecting to a running platform.
In Kubernetes mode `deploy.sh` auto-discovers everything except `MODEL_NAME`
when left empty:

```bash
# Base domain (ai-solutions base_domain_name; auto-discovered from eg-gateway)
CLUSTER_URL=solutions.ai

# LiteLLM  (external: https://litellm.<base>/v1)
OPENAI_BASE_URL=http://litellm.litellm.svc.cluster.local:4000/v1
OPENAI_API_KEY=<litellm-virtual-key>   # falls back to litellm-master-key if empty
MODEL_NAME=qwen2-5-coder-14b           # must match a model from /v1/models

# Redis (authenticated — includes user:password)
REDIS_URL=redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379

# Agent Sandbox
SANDBOX_ROUTER_URL=http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080
SANDBOX_ROUTER_TOKEN=<router-auth-token>   # when ALLOW_UNAUTHENTICATED_ROUTER=false
SANDBOX_NAMESPACE=agent-sandbox-system
SANDBOX_WARMPOOL=python-pool
SANDBOX_TEMPLATE=python-sandbox-template

# Observability (optional)  (external: https://langfuse.<base>)
LANGFUSE_PUBLIC_KEY=pk-lf-xxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxx
LANGFUSE_HOST=http://langfuse-web.langfuse.svc.cluster.local:3000
```
