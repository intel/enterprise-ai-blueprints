# Banking Assistant

A conversational banking assistant built on the Agentic AI Stack. It routes
natural-language requests ("what's my balance?", "transfer $200 to savings",
"when is my loan payment due?") to a GenAI-gateway model, and uses a **Model
Context Protocol (MCP)** server for all factual account data and operations.

This is the stack's reference example of an **MCP client/server split**: the
banking tools live in a standalone FastMCP server that runs as a **sidecar
container** in the same pod as the agent. The agent connects to it as a remote
MCP client over loopback.

> ⚠️ **Demo only.** All data is synthetic and in-memory — no real customers, no
> real PII, no real money. See [Security & limitations](#security--limitations).

---

## 1. Overview

```
                    ┌───────────────────────── Pod ──────────────────────────┐
                    │                                                         │
  user (NL query)   │   ┌────────────────────┐         ┌──────────────────┐  │
  ───────────────►  │   │ banking-assistant  │  MCP    │   banking-mcp    │  │
   OpenAI-compatible│   │ (Agent Framework   │ ───────►│  (FastMCP server │  │
   /v1/chat @ :8090 │   │  + DevUI, :8090)   │ over    │  Streamable HTTP │  │
                    │   │                    │ loopback│  :8000 /mcp)     │  │
                    │   └─────────┬──────────┘  :8000  └────────┬─────────┘  │
                    │             │                             │            │
                    └─────────────┼─────────────────────────────┼───────────┘
                                  │ model calls                 │ reads/writes
                                  ▼                             ▼
                       GenAI Gateway (LiteLLM)        synthetic in-memory data
                       genai-gateway.svc:4000         (users, accounts, loans)
                                                       (+ optional Redis memory)
```

- **Agent** (`src/agent.py`, `src/main.py`) — Microsoft Agent Framework agent
  served through DevUI on port `8090`. Exposes the MCP server's tools to the LLM
  and keeps optional text-only conversation memory in Redis.
- **Banking MCP server** (`src/mcp_server/`) — a FastMCP server exposing 10
  banking tools over Streamable HTTP on port `8000`, backed by synthetic
  in-memory data (`src/mcp_server/data.py`).
- **Topology** — the MCP server is a **sidecar** in the agent's pod; the agent
  reaches it at `http://localhost:8000/mcp`. See [Topology](#topology-sidecar)
  for the rationale and alternatives.

### Banking tools (MCP)

| Tool | Purpose |
|---|---|
| `get_user(user_id)` | Customer profile (SSN masked) |
| `find_user(name?, email?)` | Resolve a user id from name/email |
| `get_accounts(user_id)` | List a user's accounts |
| `get_account_balance(account_id)` | Balance for one account |
| `get_balances(user_id)` | Balances across all accounts |
| `get_transactions(account_id, limit?, since?)` | Recent transactions |
| `get_loans(user_id)` | List a user's loans |
| `get_loan_summary(loan_id)` | Payoff-oriented loan summary |
| `transfer_funds(from, to, amount, memo?)` | Move funds (validates funds/ownership) |
| `pay_loan(loan_id, from_account, amount)` | Pay down a loan |

Demo users: `U1001` (Ada Lovelace), `U1002` (Alan Turing), `U1003` (Grace Hopper).

---

## 2. Prerequisites

- A running **Agentic AI Stack** with the **GenAI Gateway (LiteLLM)** reachable
  and a **tool-calling-capable model** registered (e.g.
  `Qwen/Qwen3-Coder-30B-A3B-Instruct`). See the stack setup docs.
- **Redis** (optional, for conversation memory) — the shared instance in the
  `redis` namespace. It **requires authentication**: use the authenticated
  `REDIS_URL` from the `redis-stack-server-credentials` secret (`deploy.sh`
  auto-discovers it), or set `ENABLE_MEMORY=false` to skip memory.
- `kubectl` + `helm` for cluster deploy, or Docker + Docker Compose for local dev.
- The `banking-assistant` and `banking-mcp` container images built and pushed to
  a registry the cluster can pull from.

---

## 3. Quick Start

`deploy.sh` does everything: builds BOTH images (agent + MCP sidecar), creates
the namespace, sets up the TLS secret, and installs the Helm chart. On
containerd clusters it builds with nerdctl into the `k8s.io` namespace so the
kubelet can use the images locally (`pullPolicy: IfNotPresent`).

Run it interactively (it prompts for cluster domain, API key, and model):

```bash
cd usecases/banking-assistant
./deploy.sh
```

Or non-interactively by passing config through the environment (replace the
dummy values with your own):

```bash
cd usecases/banking-assistant

CLUSTER_URL="api.example.com" \
OPENAI_API_KEY="sk-your-litellm-key" \
MODEL_NAME="Qwen/Qwen2.5-Coder-14B-Instruct" \
INGRESS_HOST="banking-assistant.api.example.com" \
CERT_FILE="/path/to/cert.pem" \
KEY_FILE="/path/to/key.pem" \
./deploy.sh
```

You can also deploy from the repo root via the orchestrator, which simply hands
off to this same `deploy.sh`:

```bash
cd ../..   # repo root

CLUSTER_URL="api.example.com" \
OPENAI_API_KEY="sk-your-litellm-key" \
MODEL_NAME="Qwen/Qwen2.5-Coder-14B-Instruct" \
INGRESS_HOST="banking-assistant.api.example.com" \
CERT_FILE="/path/to/cert.pem" \
KEY_FILE="/path/to/key.pem" \
./deploy-usecase.sh banking-assistant
```

Then open the DevUI (or port-forward) and chat:

```bash
# Via ingress (once DNS for INGRESS_HOST points at your ingress controller):
#   https://banking-assistant.api.example.com

# Or locally:
kubectl port-forward -n banking-assistant svc/banking-assistant 8090:8090
#   → http://localhost:8090   (enter the DevUI token printed by deploy.sh)
```

### Try it — example prompts

Demo users: `U1001` (Ada Lovelace), `U1002` (Alan Turing), `U1003` (Grace Hopper).
Type any of these in the DevUI to verify it works:

```text
My user id is U1001. What are the balances across all my accounts?
Show me the 5 most recent transactions on account ACC-1001-CHK.
For user U1001, when is the next mortgage payment due and how much is left on it?
I'm user U1001. Transfer $200 from ACC-1001-CHK to ACC-1001-SAV. I confirm.
```

**Healthy response:** the assistant calls an MCP tool (e.g. `get_balances`) and
answers with concrete account names and dollar amounts from the demo data — not
a generic "I can't access your accounts." If it replies without using tools,
your `MODEL_NAME` likely doesn't support tool calling.

To skip the build (e.g. images already pushed to a registry):

```bash
SKIP_BUILD=true AGENT_IMAGE=myregistry/banking-assistant:1.0 \
  MCP_IMAGE=myregistry/banking-mcp:1.0 ./deploy.sh
```

> **TLS is optional.** Omit `CERT_FILE`/`KEY_FILE` to deploy without TLS
> (`deploy.sh` will prompt; leave the cert path blank to skip). If you already
> have a TLS secret in the namespace, pass `TLS_SECRET_NAME=<secret>` instead.

---

## 4. Configuration

All configuration is read from environment variables (wired via the Helm
`configmap.yaml` / `secret.yaml`).

### Agent container

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | in-cluster gateway | GenAI Gateway (LiteLLM) OpenAI-compatible URL |
| `OPENAI_API_KEY` | — (required) | LiteLLM API key |
| `MODEL_NAME` | — (required) | Model id in LiteLLM; **must support tool calling** |
| `MCP_SERVER_URL` | `http://localhost:8000/mcp` | Banking MCP endpoint (loopback in sidecar mode) |
| `REDIS_URL` | in-cluster redis | Redis URL for conversation memory. **Must include credentials** (`redis://default:<password>@…`) — the shared Redis requires auth; a passwordless URL crashes runs with `NOAUTH Authentication required.` `deploy.sh` auto-discovers it from `redis-stack-server-credentials`. |
| `ENABLE_MEMORY` | `true` | Set `false` to disable Redis memory (skips the Redis dependency entirely) |
| `DEVUI_HOST` | `0.0.0.0` | Host DevUI binds to |
| `DEVUI_AUTH_TOKEN` | auto-generated | DevUI login token |
| `AUTO_OPEN_BROWSER` | `false` | Whether DevUI opens a browser |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | — | Optional observability — see [Observability guide](../../docs/observability-langfuse.md) |

### MCP container

| Variable | Default | Description |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Interface the FastMCP server binds to |
| `MCP_PORT` | `8000` | Port for the Streamable HTTP transport |

---

## 5. API Reference

The agent is served by **Agent Framework DevUI**, which exposes the **OpenAI
Responses API** (`POST /v1/responses`) — not `/v1/chat/completions`. Send natural
language; the LLM decides which MCP tools to call. All routes except `/health`
require `Authorization: Bearer <DEVUI_AUTH_TOKEN>` (the token `deploy.sh` prints).

```bash
AGENT=http://localhost:8090
TOKEN=<DEVUI_AUTH_TOKEN>

# Health (JSON, no auth)
curl -s "$AGENT/health"
# {"status":"healthy","entities_count":1,"framework":"agent_framework"}

# Discover the entity id
EID=$(curl -s "$AGENT/v1/entities" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['entities'][0]['id'])")

# Ask a question — triggers MCP tool calls
curl -s -X POST "$AGENT/v1/responses" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"input\":\"My user id is U1001. What are my account balances?\",\"metadata\":{\"entity_id\":\"$EID\"}}"
```

---

## 6. Running Examples

All clients take the DevUI token via `DEVUI_AUTH_TOKEN` (printed by `deploy.sh`).

```bash
export DEVUI_AUTH_TOKEN=<token>

# Python (standard library only — no extra deps): runs a series of banking queries
AGENT_URL=http://localhost:8090 python3 examples/banking_client.py

# curl-based equivalents
./examples/banking_client.sh http://localhost:8090
```

---

## 7. Local Development

```bash
export LITELLM_URL=http://your-host:4000/v1
export LITELLM_API_KEY=sk-your-key
export MODEL_NAME=Qwen/Qwen3-Coder-30B-A3B-Instruct   # must support tool calling

docker compose up --build
#   banking-mcp        → :8000  (MCP server)
#   banking-assistant  → :8090  (agent + DevUI, MCP_SERVER_URL=http://banking-mcp:8000/mcp)
#   redis-stack        → :6379
```

You can also run the MCP server on its own for tool debugging:

```bash
cd src && MCP_PORT=8000 python -m mcp_server.server
```

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Agent replies but never uses account data | `MODEL_NAME` doesn't support tool/function calling — pick a tool-capable instruct model. |
| Startup log: "Could not resolve an MCP Streamable-HTTP tool class" | The Agent Framework MCP client class name differs in your installed version. Run `python -c "import agent_framework; print([n for n in dir(agent_framework) if 'MCP' in n.upper()])"` and update `_resolve_mcp_tool_class()` / pin the version in `requirements.txt`. |
| Agent can't reach the MCP server | Confirm the `banking-mcp` container is Ready and `MCP_SERVER_URL` is `http://localhost:8000/mcp` (sidecar) or the compose DNS name. Ensure `no_proxy` includes the MCP host. |
| Transfers/payments "forget" after a restart | Expected — data is in-memory and resets on restart (see below). |
| `401 Missing or invalid Authorization header` | DevUI requires `Authorization: Bearer <DEVUI_AUTH_TOKEN>` on all `/v1/*` routes. Pass the token `deploy.sh` printed. |
| Agent run fails instantly with `Authentication required.` | Redis needs auth. Set `REDIS_URL` to the authenticated URL from `redis-stack-server-credentials` (or set `ENABLE_MEMORY=false`). |
| `/health` | Returns JSON `{"status":"healthy",...}` with 200 (no auth). Kubernetes liveness/readiness use a TCP check on 8090. |

---

## Topology (sidecar)

The MCP server runs as a second container in the agent's pod. Because the banking
data is synthetic and in-memory, there is no scaling or availability reason to run
it separately, and co-location removes a Service, cross-pod DNS, and a network hop.

| | Sidecar (this usecase) | Separate deployment |
|---|---|---|
| Networking | loopback, no Service | needs Service + in-cluster DNS |
| Scaling | scales 1:1 with the agent | independent; can front many agents |
| Reuse | this agent only | reusable by other clients |
| Complexity | one chart, one pod | two deployments |

To run the MCP server standalone instead, deploy the `banking-mcp` image via the
core `mcp-server-template` chart and point `MCP_SERVER_URL` at its Service DNS.

---

## Security & limitations

- **Synthetic data only.** Fake users, masked SSN last-4, fake addresses. Data is
  a Python dict rebuilt on process start; **mutations (transfers, loan payments)
  are in-memory and reset on restart** and are not shared across replicas.
- **No authentication / authorization.** Any caller can query any `user_id` and
  move money between accounts. This is intentional for a demo. A real deployment
  would require per-user auth (e.g. the OIDC pattern in the core
  `mcp-server-template`) and would enforce ownership against the authenticated
  principal rather than a free-form `user_id` argument.
- **Never wire this to a real banking system.**
