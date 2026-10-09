# Observability — Tracing with Langfuse

Every blueprint can export **traces** (each LLM call, tool call, token count,
latency, and agent step) to **Langfuse**, the observability service the Agent
Toolkit deploys. This guide shows how to create a Langfuse project, get its keys,
and point a use case at it — including how the OpenTelemetry (OTEL) export works.

Tracing is **optional**: a blueprint deploys and runs without it. Enable it when
you want to inspect or debug what the agent is doing.

---

## How it works

```
 Use case (agent)                         Langfuse (in the toolkit)
 ┌───────────────────────────┐            ┌──────────────────────────┐
 │ agent_framework spans      │  OTLP/HTTP │  Project (own API keys)  │
 │  → configure_otel_providers│ ─────────► │  api/public/otel         │
 │  → Langfuse SDK (get_client)│  + keys    │  → Traces UI             │
 └───────────────────────────┘            └──────────────────────────┘
```

On startup each use case runs `setup_observability()` (in `src/main.py`):

1. If `LANGFUSE_PUBLIC_KEY` **and** `LANGFUSE_SECRET_KEY` are set, it creates the
   Langfuse client (`get_client()`), which reads `LANGFUSE_HOST` for the endpoint.
2. It runs an auth check; on failure it logs a warning and continues **without**
   tracing (the agent still works).
3. It calls `agent_framework.observability.configure_otel_providers(...)`, which
   registers OpenTelemetry providers so the framework's spans are exported to the
   Langfuse project over OTLP.

You don't configure OTEL endpoints by hand — the Langfuse SDK derives the OTLP
endpoint from `LANGFUSE_HOST` and authenticates with the project keys.

---

## Prerequisites

Langfuse is deployed by the Agent Toolkit (as the `genai-gateway-trace-*`
components). Confirm it's up:

```bash
kubectl get pods -n genai-gateway | grep trace-web
```

In-cluster URL (use as `LANGFUSE_HOST`):
`http://genai-gateway-trace-web.genai-gateway.svc.cluster.local:3000`

See [endpoints-reference.md](endpoints-reference.md#langfuse-llm-observability-traces)
for the UI login and how to retrieve keys.

---

## Step 1 — Create a project (one per use case, recommended)

Traces are grouped by **project**, and each project has its own API keys. Giving
each use case its own project keeps their traces separate.

1. Port-forward and open the Langfuse UI:
   ```bash
   kubectl port-forward -n genai-gateway svc/genai-gateway-trace-web 3000:3000
   # → http://localhost:3000
   ```
2. Log in (see [endpoints-reference.md](endpoints-reference.md#ui-login-initial-admin-user)
   for the initial admin email/password).
3. **New project** (or reuse the default one) — e.g. `banking-assistant`.

> Prefer not to create projects by hand? The toolkit seeds an initial
> organization/project (`LANGFUSE_INIT_*` on the `genai-gateway-trace-web`
> deployment); you can use that project's keys for a quick start.

## Step 2 — Get the project's API keys

In the Langfuse UI: **Project → Settings → API Keys → Create**. Copy:

- **Public key** — `pk-lf-...`
- **Secret key** — `sk-lf-...`

## Step 3 — Enable tracing for the use case

Set these in `config.local.env` (then `set -a && source config.local.env && set +a`):

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-xxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxx
LANGFUSE_HOST=http://genai-gateway-trace-web.genai-gateway.svc.cluster.local:3000
LANGFUSE_ENABLE_SENSITIVE_DATA=true   # include prompt/response content in spans
```

Then deploy as usual — `deploy.sh` passes these to the chart **only when the two
keys are set**:

```bash
./deploy-usecase.sh banking-assistant --mode kubernetes
```

Or enable it on an existing deployment with Helm directly:

```bash
helm upgrade banking-assistant usecases/banking-assistant/helm-chart \
  -n banking-assistant --reuse-values \
  --set agent.langfusePublicKey=pk-lf-xxxx \
  --set agent.langfuseSecretKey=sk-lf-xxxx \
  --set agent.langfuseHost=http://genai-gateway-trace-web.genai-gateway.svc.cluster.local:3000
```

### Variables

| Variable | Helm value | Purpose |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | `agent.langfusePublicKey` | Project public key (`pk-lf-…`) |
| `LANGFUSE_SECRET_KEY` | `agent.langfuseSecretKey` | Project secret key (`sk-lf-…`) |
| `LANGFUSE_HOST` | `agent.langfuseHost` | Langfuse URL (in-cluster or `https://trace-api.<domain>`) |
| `LANGFUSE_ENABLE_SENSITIVE_DATA` | `agent.langfuseEnableSensitiveData` | `true` includes prompt/response text in spans; set `false` to redact |

> **Note:** the env var is `LANGFUSE_HOST` (the Langfuse SDK's own variable) — not
> `LANGFUSE_BASE_URL`. The Helm value `agent.langfuseHost` maps to it.

---

## Verify traces

1. Send a request to the use case (see its README's "Try it" prompts).
2. Check the pod log for: `Langfuse observability enabled`.
   ```bash
   kubectl logs -n banking-assistant deploy/banking-assistant -c banking-assistant | grep -i langfuse
   ```
3. Open the Langfuse UI → your project → **Traces**; the request appears with its
   LLM calls and tool calls.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Log says `Langfuse not configured` | `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` not set on the pod. Set both and redeploy. |
| Log says `Langfuse auth check failed` | Wrong keys, or `LANGFUSE_HOST` unreachable/incorrect. Confirm the keys belong to the project and the host resolves from inside the cluster. |
| No traces though enabled | Keys belong to a **different project** than the one you're viewing, or `LANGFUSE_HOST` points at Langfuse cloud instead of the in-cluster service. |
| Prompts/responses missing from spans | `LANGFUSE_ENABLE_SENSITIVE_DATA=false` — set `true` to include them. |
