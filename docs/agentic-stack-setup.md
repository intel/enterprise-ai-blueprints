# Agent Toolkit — Setup & Verification

Every blueprint runs on top of the **Agent Toolkit**, which must be deployed
**before** any use case. This page is the pre-flight checklist: deploy the
toolkit, then verify each service a blueprint depends on is up.

## 1. Deploy the Agent Toolkit

Follow the toolkit's own documentation:

> **➡️ [github.com/intel/enterprise-agent-toolkit](https://github.com/intel/enterprise-agent-toolkit)**

## 2. Verify the required services

Run these before deploying a blueprint — each should show `Running`/`Ready` pods.

| Component | Required by | Verify |
|---|---|---|
| **GenAI Gateway (LiteLLM)** | all blueprints | `kubectl get pods -n genai-gateway` |
| **An LLM model** (tool-calling capable) | all blueprints | `curl -sk "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY"` |
| **Redis** | conversational memory | `kubectl get pods -n redis` |
| **Agent Sandbox** (controller + router) | code-execution blueprints (coding-agent) | `kubectl get pods -n agent-sandbox-system` |
| **Langfuse** (optional) | tracing | `kubectl get pods -n genai-gateway \| grep trace-web` |

Quick model check (the value you'll use for `MODEL_NAME` must appear here):

```bash
kubectl port-forward -n genai-gateway svc/genai-gateway-service 4000:4000 &
curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer <litellm-key>" | python3 -m json.tool
```

See **[endpoints-reference.md](endpoints-reference.md)** for the exact in-cluster
URLs and how to retrieve each credential (LLM key, Redis URL, sandbox-router token,
Langfuse keys).

## 3. Deploy a blueprint

```bash
cp config.env config.local.env      # fill OPENAI_API_KEY and MODEL_NAME
set -a && source config.local.env && set +a
./deploy-usecase.sh coding-agent-maf --mode kubernetes
```

For tracing, see **[observability-langfuse.md](observability-langfuse.md)**.
