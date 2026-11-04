# Toolkit Manager & Intel® Agent Toolkit — Setup & Verification

Every blueprint runs on top of **Intel® Agent Toolkit**, installed and operated
by **Toolkit Manager**. The Agent Toolkit must be installed **before** any use
case. This page is the pre-flight checklist: install Intel® Agent Toolkit with
Toolkit Manager, allow the blueprint namespaces to reach LiteLLM, verify each
service a blueprint depends on, then deploy a blueprint.

> **➡️ [github.com/intel/enterprise-ai-solutions (release-1.2)](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2)**
> — Toolkit Manager's `agent-toolkit` layer installs Intel® Agent Toolkit
> [intel/enterprise-agent-toolkit release-2.0.0](https://github.com/intel/enterprise-agent-toolkit/tree/release-2.0.0)
> and pulls in Intel® Inference Toolkit
> [intel/enterprise-inference release-1.1](https://github.com/intel/enterprise-inference/tree/release-1.1).

## 1. Install Intel® Agent Toolkit with Toolkit Manager

```bash
git clone https://github.com/intel/enterprise-ai-solutions.git -b release-1.2
cd enterprise-ai-solutions

./es_auto_installer.sh configure
./es_auto_installer.sh init agent-toolkit
```

`init agent-toolkit` creates `env/local/` and seeds `global_config.yaml`,
`config.inference.yaml`, `config.agent-toolkit.yaml`, and `models.yaml`
(with `auth_provider: litellm`, so Keycloak is not deployed).

Edit `env/local/global_config.yaml` — at minimum `base_domain_name` (default
`solutions.ai`; every hostname below is `<name>.<base_domain_name>`) and your
proxy settings. Kata Containers are controlled here too (`kata_containers_enabled`
/ `kata_runtime`).

### Allow the blueprint namespaces to reach LiteLLM

LiteLLM is protected by an Istio `AuthorizationPolicy` (`litellm-ingress` in
namespace `litellm`); only namespaces in `litellm_allowed_namespaces` can call it.
Add the blueprint namespaces in `env/local/config.agent-toolkit.yaml` **before**
installing (or re-run `install` afterwards):

```yaml
litellm_allowed_namespaces:
  - envoy-gateway-system
  - litellm
  - agent-sandbox-system
  - ray-system
  - coding-agent-maf        # blueprint namespaces
  - banking-assistant
```

Other switches in `config.agent-toolkit.yaml` that blueprints rely on:

| Switch | Needed by |
|---|---|
| `redis_stack_enabled` | conversational memory (both blueprints) |
| `agent_sandbox_enabled` | code execution (coding-agent-maf) |
| `kuberay_enabled` | optional Ray parallel execution |
| `llm_services_autodeploy_models` | the model(s) to serve — default `["qwen2-5-coder-14b"]` |
| `pgvector_enabled`, `openshell_enabled`, `guardrails_enabled`, `hybrid_router_enabled` | not required by the shipped blueprints |

Then install:

```bash
./es_auto_installer.sh install agent-toolkit   # infrastructure → platform → inference → agent-toolkit
export KUBECONFIG=$PWD/env/local/kubeconfig.yaml
```

> Added the namespaces after installing? Re-run `./es_auto_installer.sh install agent-toolkit`.
> As a stop-gap, the blueprint `deploy.sh` can patch the policy directly with
> `PATCH_LITELLM_AUTHZ=true` (overwritten on the next platform install).

## 2. Verify the required services

Run these before deploying a blueprint — each should show `Running`/`Ready` pods.

| Component | Required by | Verify |
|---|---|---|
| **Whole layer** | all blueprints | `./es_auto_installer.sh validate agent-toolkit` |
| **LiteLLM** (Inference Toolkit) | all blueprints | `kubectl get pods -n litellm` |
| **An LLM model** (tool-calling capable) | all blueprints | models `curl` below |
| **Envoy Gateway** (`eg-gateway`) | external access | `kubectl get pods,gateway -n envoy-gateway-system` |
| **Redis** | conversational memory | `kubectl get pods -n redis` |
| **Agent Sandbox** (controller + router) | code-execution blueprints (coding-agent-maf) | `kubectl get pods -n agent-sandbox-system` |
| **Langfuse** (optional) | tracing | `kubectl get pods -n langfuse` |

Quick model check (the value you'll use for `MODEL_NAME` must appear here):

```bash
MASTER_KEY=$(kubectl get secret litellm-master-key -n litellm \
  -o jsonpath='{.data.master_key}' | base64 -d)
kubectl port-forward -n litellm svc/litellm 4000:4000 &
curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer $MASTER_KEY" | python3 -m json.tool
# expect e.g. "qwen2-5-coder-14b" (and "hybrid-router" if enabled)
```

For a blueprint, prefer a **virtual key** over the master key:

```bash
curl -k https://litellm.<base_domain_name>/key/generate \
  -H "Authorization: Bearer $MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"models":["qwen2-5-coder-14b"]}'
```

See **[endpoints-reference.md](endpoints-reference.md)** for the exact in-cluster
URLs and how to retrieve each credential (LLM key, Redis URL, sandbox-router token,
Langfuse keys).

## 3. Reach the gateway (hostnames + CA)

All external access goes through the Envoy Gateway `eg-gateway`, which terminates
TLS with a self-signed wildcard certificate by default. Map the hostnames to the
gateway IP and trust the exported CA:

```bash
kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=eg-gateway
# add to /etc/hosts (replace solutions.ai with your base_domain_name):
# <gateway-ip> coding-agent.solutions.ai banking-assistant.solutions.ai litellm.solutions.ai langfuse.solutions.ai

curl --cacert env/local/logs/ai-solutions-ca.crt https://litellm.solutions.ai/v1/models \
  -H "Authorization: Bearer $MASTER_KEY"          # or curl -k
```

## 4. Deploy a blueprint

```bash
cp config.env config.local.env      # MODEL_NAME (e.g. qwen2-5-coder-14b); the rest is auto-discovered
set -a && source config.local.env && set +a
./deploy-usecase.sh coding-agent-maf --mode kubernetes
```

`deploy.sh` labels the namespace for Istio ambient and Pod Security `restricted`,
auto-discovers `CLUSTER_URL` (the `base_domain_name`), the LiteLLM URL/key, Redis
URL, sandbox-router token and Langfuse keys, and exposes the app at
`https://coding-agent.<base_domain_name>` via an HTTPRoute.

For tracing, see **[observability-langfuse.md](observability-langfuse.md)**.
