# Unified Decision Gate

A **gating mechanism** that puts a fast **System-1 decision model** in front of an LLM agent, built on
[Intel® Agent Toolkit](https://github.com/intel/enterprise-agent-toolkit/tree/release-2.0.0), installed by
[Toolkit Manager](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2). For every request,
System-1 decides in one forward pass whether it can answer itself; if it can, the answer is returned with
no LLM call, and everything else goes to the agent and its tools.

System-1 is **Qwen/Qwen3-0.6B**, read through vLLM's
[structured decisions](https://docs.vllm.ai/en/latest/models/structured_decisions_models/) endpoint
(`POST /v1/systemone`: one forward pass, no text generated). "Unified": both models are served the same
way, by vLLM through Intel® Inference Toolkit's Model Manager, as two model deployments: the agent's LLM
behind LiteLLM (`http://litellm.litellm.svc.cluster.local:4000/v1`) and System-1 behind the inference AI
gateway (`http://ai-gateway.envoy-gateway-system.svc.cluster.local/llm-inference/qwen3-0-6b-system1`).

The gate is domain-agnostic. The example domain is **banking**: a banking assistant agent with MCP banking
tools. Here the decision System-1 makes is the yes/no answer to a general banking question.

> ⚠️ **Demo only.** All banking data is synthetic and in-memory — no real customers, no real PII, no real
> money. **Shadow mode is the default**: System-1's decisions are logged and every answer comes from the
> agent until you switch it off (section 3, last paragraph; the setting is in section 4).

---

## 1. Overview

```
                ┌──────────────────────────────── Pod ─────────────────────────────────┐
  user          │  ┌────────────┐  escalate   ┌──────────────────┐  MCP   ┌───────────┐ │
  ────────────► │  │    gate    │ ──────────► │ banking-assistant│ ─────► │banking-mcp│ │
  OpenAI chat   │  │   :8090    │  (loopback) │ (Agent Framework,│        │ (FastMCP, │ │
  completions   │  │ text rules │             │  DevUI :8091)    │        │  :8000)   │ │
  (HTTPRoute)   │  └─────┬──────┘             └────────┬─────────┘        └───────────┘ │
                └────────┼─────────────────────────────┼───────────────────────────────┘
                         │ POST /v1/systemone          │ chat completions (+ tool calls)
                         ▼                             ▼
               inference AI gateway               LiteLLM  litellm.litellm:4000
               llm-inference/qwen3-0-6b-system1       │
                         │                             ▼
                         └──────── vLLM (Model Manager, llm-inference) ───────┘
                                   Qwen3-0.6B (0.32.0)      the agent's LLM
```

- **gate** (`src/decision_gate`): the pod's front door, OpenAI chat completions on :8090. Text rules first
  (explanations, the customer's own records, amounts, urgent actions, two questions in one, "can I
  block/transfer/…" go straight to the agent), then one System-1 call. P(yes) ≥ `SYSTEM1_YES_MIN` →
  "Yes.", ≤ `SYSTEM1_NO_MAX` → "No.", in between → the agent. It **fails open**: if System-1 is down, slower
  than the configured System-1 deadline (`SYSTEM1_TIMEOUT_S`), busy or unsure, the agent answers.
- **banking-assistant** + **banking-mcp**: the example agent (Microsoft Agent Framework, DevUI, Redis memory)
  and its FastMCP banking tools, a sidecar on loopback. The agent moves to :8091 inside the pod.

### How the models are served

| | The agent's LLM | System-1 |
|---|---|---|
| What it does | Writes answers, asks for tools | A score between 0 and 1 for "would a banking expert answer yes". No text |
| Served by | vLLM, deployed by Model Manager (e.g. `qwen3-4b`) | vLLM **0.32.0**, deployed by Model Manager as `qwen3-0-6b-system1` with `--logprobs-mode raw_logprobs` |
| Reached by | the agent, through **LiteLLM** (keys, routing, Langfuse) | the gate, through the **inference AI gateway** (`POST /v1/systemone`; LiteLLM has no such route) |

### Use it for your domain

| Piece | Where | In the banking example |
|---|---|---|
| The question System-1 is asked | `src/decision_gate/questions.py` | "A banking expert is asked the question above. Would the expert answer yes?" |
| The text rules that skip System-1 | `src/decision_gate/text_rules.json` | the customer's own records, amounts, urgent card actions, "can I …" |
| The display cut-offs | `SYSTEM1_YES_MIN` / `SYSTEM1_NO_MAX` | 0.9952 / 0.9906, fitted on 200 labelled questions |
| The System-1 model | `SYSTEM1_URL` / `SYSTEM1_MODEL` | `qwen3-0-6b-system1` on the inference AI gateway |
| The agent behind the gate | Helm values, `docker-compose.yml` | the banking assistant + MCP tools |

Fit the cut-offs on a few hundred labelled requests from your own traffic. The decision log records, per
request, the answer System-1 would have shown and where the request went, not the agent's answer and not the
question text (unless `GATE_LOG_QUESTION_TEXT=true`), so the labels come from your own review. Details:
[docs/implementation.md](docs/implementation.md).

### Measured

On the banking test set, against sending every question to the LLM, the gate cut the mean latency by 39% with
both Qwen3-Coder-30B and Qwen3-4B as the agent's LLM, for a yes/no accuracy 6 points lower with Qwen3-Coder-30B
and 2 points lower with Qwen3-4B.

---

## 2. Prerequisites

- Intel® Agent Toolkit installed with Toolkit Manager (`./es_auto_installer.sh install agent-toolkit`;
  see [../../docs/toolkit-manager-setup.md](../../docs/toolkit-manager-setup.md)), with a **tool-calling**
  model for the agent (e.g. `qwen3-4b`).
- `kubectl` pointing at the cluster (`env/<env>/kubeconfig.yaml`), `helm`, and `nerdctl` to build the images.
- **System-1 deployed by Model Manager** (next section). No GPU.

### Deploy System-1

The catalog's vLLM versions (0.24.0, 0.28.0) have no `/v1/systemone`. Add 0.32.0 and a System-1 entry to
your environment's catalog, `env/<env>/models.yaml` (these steps run **in the enterprise-ai-solutions
checkout**):

```yaml
runtimes:
  vllm:
    versions:
      "0.32.0":                                   # add beside 0.24.0 / 0.28.0
        image: "docker.io/vllm/vllm-openai-cpu:v0.32.0"

models:
  - name: qwen3-0-6b-system1                      # the name the gate asks for (SYSTEM1_MODEL)
    model_id: Qwen/Qwen3-0.6B
    category: llm
    runtime: vllm
    server_version: "0.32.0"
    routing: direct
    cpu: 8
    memory: 16Gi
    args: [ "--logprobs-mode", "raw_logprobs", "--max-model-len", "4096" ]
```

Then deploy it:

```bash
# in the enterprise-ai-solutions checkout
export KUBECONFIG=$PWD/env/<env>/kubeconfig.yaml
./model-manager deploy qwen3-0-6b-system1 --wait
```

Verified on Toolkit Manager 1.2: the model's route on the inference AI gateway forwards every path under
`/llm-inference/<model>` (KServe's generated route), including `/v1/systemone` and `/v1/models`. After a toolkit
upgrade, check it from a pod in the mesh: `GET .../v1/models` and `POST .../v1/systemone` return 200, and the gate's
`/health` shows `"system1_ok": true`.

---

## 3. Quick Start

```bash
# in this repository (enterprise-ai-blueprints), from its root
cp config.env config.local.env && $EDITOR config.local.env
set -a && source config.local.env && set +a
./deploy-usecase.sh unified-decision-gate --mode kubernetes
# or: cd usecases/unified-decision-gate && MODEL_NAME=qwen3-4b ./deploy.sh
```

`deploy.sh` discovers the base domain, the LiteLLM key, Langfuse and Redis from the cluster, checks that
`qwen3-0-6b-system1` is deployed and Ready, builds the three images, labels the namespace (Istio ambient + PSS
restricted), checks LiteLLM access and installs the chart. It prints the client token, and keeps the existing
one on a redeploy. The nerdctl build loads the images on **this node** only: on a multi-node cluster, push them to
a registry and set `GATE_IMAGE` / `AGENT_IMAGE` / `MCP_IMAGE` + `SKIP_BUILD=true`.

The namespace must be in LiteLLM's `litellm_allowed_namespaces` (`env/<env>/config.agent-toolkit.yaml`, then
`./es_auto_installer.sh install agent-toolkit`); `PATCH_LITELLM_AUTHZ=true` patches the policy as a
temporary fix.

```bash
# Via the gateway (add "<eg-gateway-ip> unified-decision-gate.<base_domain_name>" to /etc/hosts and trust
# env/<env>/logs/ai-solutions-ca.crt, or use curl -k):
#   https://unified-decision-gate.<base_domain_name>        (e.g. https://unified-decision-gate.solutions.ai)
# Or locally:
kubectl port-forward -n unified-decision-gate svc/unified-decision-gate 8090:8090   # → http://localhost:8090
```

To show System-1's answers, redeploy with `GATE_SHADOW_MODE=false` (and `GATE_DEBUG_HEADERS=true` to see the
route and scores in the `X-Gate-*` response headers).

---

## 4. Configuration

`deploy.sh` reads these from the environment (everything else is discovered or prompted):

| Variable | Default | Meaning |
|---|---|---|
| `MODEL_NAME` | (prompted) | The agent's LLM in LiteLLM; must support tool calling |
| `SYSTEM1_URL` | `http://ai-gateway.envoy-gateway-system.svc.cluster.local/llm-inference/qwen3-0-6b-system1` | System-1's base URL, without `/v1` |
| `SYSTEM1_MODEL` | `qwen3-0-6b-system1` | The name System-1 is served under |
| `GATE_SHADOW_MODE` | `true` | Log System-1's decisions, answer everything from the agent |
| `SYSTEM1_YES_MIN` / `SYSTEM1_NO_MAX` | `0.9952` / `0.9906` | Display cut-offs on P(yes) |
| `GATE_DEBUG_HEADERS` | `false` | Return the decision in `X-Gate-*` headers |
| `GATE_ENABLED` | `true` | `false`: System-1 is never asked; the gate sends every question to the agent (same API) |
| `AGENT_ONLY` | `false` | `true`: deploy the agent alone, without the gate. The Service then serves DevUI's own API (`/v1/responses`), with no `/v1/chat/completions` |
| `NAMESPACE` / `INGRESS_HOST` | `unified-decision-gate` / `unified-decision-gate.<base>` | Where it is deployed and published |
| `DEVUI_AUTH_TOKEN` | generated | The client bearer token; the gate enforces it |
| `SKIP_BUILD`, `GATE_IMAGE`, `AGENT_IMAGE`, `MCP_IMAGE` | build here | Use pre-built images instead |

More gate settings (`SYSTEM1_TIMEOUT_S`, `SYSTEM1_MAX_P_ACTION`, `GATE_LOG_QUESTION_TEXT`, the decision log,
the text rules) are Helm values under `gate:` in `helm-chart/values.yaml`. Every setting and its default:
`src/decision_gate/config.py`.

The cut-offs are fitted for the banking example on 200 labelled questions: Qwen3-0.6B gives almost every
question P(yes) near 1, so the band is narrow (0.0023 each side), and 10-17% of decisions near it can move
with the serving setup (about 10%) or with load (about 17% at 16 concurrent requests). Refit them for your own traffic (docs/implementation.md, Calibration).

---

## 5. API Reference

All on the gate. Every API request except `/health` and `/live` needs `Authorization: Bearer $DEVUI_AUTH_TOKEN`.

```bash
HOST=unified-decision-gate.<base_domain_name>     # the HTTPRoute host (or use http://localhost:8090 with a port-forward)
export DEVUI_AUTH_TOKEN=<token printed by deploy.sh>

# Liveness: the gate only, never calls System-1
curl -sk https://$HOST/live
# Health: the gate, and whether System-1 answers ("system1_ok"); makes a real System-1 read
curl -sk https://$HOST/health

# A plain yes/no question: System-1 answers when it is sure, no LLM call (shadow mode off)
curl -sk https://$HOST/v1/chat/completions \
  -H "Authorization: Bearer $DEVUI_AUTH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model": "banking-assistant", "messages": [{"role": "user", "content": "Is an overdraft a form of borrowing?"}]}'

# Anything else: the agent answers, calling MCP tools as needed
curl -sk https://$HOST/v1/chat/completions \
  -H "Authorization: Bearer $DEVUI_AUTH_TOKEN" -H 'Content-Type: application/json' -H 'X-Session-Id: demo-1' \
  -d '{"model": "banking-assistant", "messages": [{"role": "user", "content": "My user id is U1001. What are my balances?"}]}'
```

| Endpoint | Description |
|---|---|
| `GET /live` | The gate is up. Never calls System-1; the probes use it |
| `GET /health` | `gate_enabled`, `shadow` and `system1_ok`. Each call makes a real `/v1/systemone` read: don't scrape it every few seconds |
| `POST /v1/chat/completions` | OpenAI chat completions, streaming too (`"stream": true`); the supported subset is below |
| any other path | Proxied to the agent unchanged: the DevUI web UI, `/v1/responses`, `/v1/entities`, … |

With `GATE_DEBUG_HEADERS=true`, responses carry `X-Gate-Route` (`answer` or `escalate`), `X-Gate-Reason`,
`X-Gate-P-Answer`, `X-Gate-System1-Ms` and `X-Gate-Total-Ms`.

**Supported chat subset.** Text chat, streaming, one choice. System and developer messages from the client are
dropped (the agent has its own instructions), and multimodal content is not supported. Other request fields are not
honoured: `response_format`, `n > 1` and the like are not rejected today, and get a plain-text, single-choice answer
(for example "Yes.").

---

## 6. Running Examples

```bash
export DEVUI_AUTH_TOKEN=<token printed by deploy.sh>
AGENT_URL=http://localhost:8090 python3 examples/example_client.py    # pip install openai
./examples/example_client.sh http://localhost:8090                    # curl only
```

Both ask two plain yes/no questions and four that need the agent's tools: balances, recent transactions, the
mortgage, a transfer.

---

## 7. Local Development

Without Kubernetes, against an inference server you run yourself (a LiteLLM gateway for the agent's LLM, and a
vLLM ≥ 0.32.0 serving Qwen3-0.6B with `--logprobs-mode raw_logprobs`):

```bash
cp config/agent-config.env .env && $EDITOR .env     # LITELLM_URL, LITELLM_API_KEY, MODEL_NAME, SYSTEM1_URL, ...
LITELLM_HOST=<host in LITELLM_URL> SYSTEM1_HOST=<host in SYSTEM1_URL> \
  docker compose --env-file .env up -d --build --wait
```

Tests:

```bash
(cd src && python3 -m decision_gate.test_gate && python3 -m decision_gate.test_backends && python3 -m decision_gate.test_config)
python3 tests/helm/check_chart.py --static          # static chart checks incl. PSS restricted (needs helm, PyYAML)
docker compose --env-file .env -f docker-compose.yml -f docker-compose.e2e.yml up -d --build --wait
python3 tests/e2e/smoke.py                                                                 # real images, stub LLM and System-1
```

The gate's unit tests need `pip install -r src/requirements-gate.txt`; `test_config` needs nothing extra. The chart
checker renders the chart and checks it statically: its live mode only runs exec probes, and this chart has HTTP and
TCP probes, so it starts no container. The e2e smoke test needs Docker and runs the real images.

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Every question goes to the agent | Shadow mode is on by default (`GATE_SHADOW_MODE=true`). In Docker mode, `SYSTEM1_URL` must be set in `.env` (the gate logs a warning at start-up when it is empty). Otherwise check `/health`: `system1_ok: false` means System-1 is not answering (rows below) |
| `deploy.sh` warns "Model 'qwen3-0-6b-system1' is not deployed" | Deploy it with Model Manager (section 2) |
| `/health`: "no /v1/systemone on this vLLM" | The model runs on a vLLM older than 0.32.0: pin `server_version: "0.32.0"` in its catalog entry |
| `/health`: http 501, "no read strategy" | The model is served without `--logprobs-mode raw_logprobs` |
| `/health`: "vLLM does not serve SYSTEM1_MODEL" (404) | `SYSTEM1_MODEL` must equal the served name (the catalog `name`) |
| Agent answers fail (502) | The namespace is not in LiteLLM's `litellm_allowed_namespaces` (section 3), or `MODEL_NAME` is not registered in LiteLLM |
| 401 from the gate | The bearer token must equal `DEVUI_AUTH_TOKEN` |
| Pods rejected at admission | The namespace enforces PSS restricted; run `python3 tests/helm/check_chart.py --static` |
| The same question is shown once and sent to the agent another time | Scores move slightly with load and serving setup; the band is narrow (section 4) |

---

## Security & limitations

- **Demo data only**: synthetic users, accounts and loans, in memory.
- **Known demo limitations of the banking MCP tools** (`src/mcp_server/data.py`): a NaN amount passes the
  `amount <= 0` check and corrupts balances or loan principal, and a credit-card account can be the source of a
  transfer with deposit-account arithmetic. Demo only; not for real money.
- The gate enforces `DEVUI_AUTH_TOKEN` on its chat API and on DevUI's APIs (`/v1/entities`, `/v1/responses`, …);
  DevUI's web UI page itself (`GET /`) loads without a token and asks for it. `/v1/responses` goes straight to the
  agent, past System-1 and the decision log (`gate.proxyDevui: false` removes all DevUI routes). The route-level
  SecurityPolicy opts this route out of the gateway-wide LiteLLM-key check (`httpRoute.bypassGatewayAuth: true`),
  like the other blueprints.
- System-1's calls bypass LiteLLM: no virtual keys, usage tracking or Langfuse traces for them.
- The decision log stores question hashes, not text, unless `GATE_LOG_QUESTION_TEXT=true`. With Langfuse configured,
  the agent's prompts and answers are traced (`agent.langfuseEnableSensitiveData: "true"`, as in the other blueprints).
- System-1 reads the surface form of the question: shown answers are a yes/no only, and the cut-offs are fitted for the
  banking example. Keep shadow mode on until System-1's would-be answers have been checked against labelled requests from
  your own traffic.
