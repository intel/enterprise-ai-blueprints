# CLAUDE.md

Guidance for Claude Code (and other contributors) working in this repository.

## What this repo is

Reference **AI blueprints**: self-contained agent use cases. Each one has its own source,
Dockerfile(s), Helm chart and `deploy.sh`. They run on top of
**Intel® AI for Enterprise Solutions** [release-1.2](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2),
in its `agent-toolkit` layer ([enterprise-agent-toolkit release-2.0.0](https://github.com/intel/enterprise-agent-toolkit/tree/release-2.0.0)),
which pulls in the inference layer (enterprise-inference release-1.1).

The blueprints only *consume* the platform. They never install or modify platform components.

**Roadmap:** blueprints will be packaged as a native AI Solutions layer
(`configs/repos/repos.blueprints.yaml` in ai-solutions, plus `components.yaml`, `config.yaml` and
Ansible roles here). Until that lands, deployment is the standalone Helm + `deploy.sh` flow below.
Don't add Ansible/layer files unless asked.

## Layout

```
deploy-usecase.sh            # thin dispatcher: shared config + auto-discovery, delegates to usecases/<name>/deploy.sh
config.env                   # config template (copy to config.local.env, never commit that)
scripts/ai-solutions-lib.sh  # shared platform helpers sourced by every usecases/*/deploy.sh
scripts/ensure-image-builder.sh  # buildkit/nerdctl bootstrap for local image builds
usecases/<name>/
  deploy.sh                  # build image(s) → prepare namespace → helm upgrade --install
  docker-compose.yml         # local/dev mode
  helm-chart/                # Deployment, Service, ConfigMap, Secret, HTTPRoute, SecurityPolicy (+RBAC/PVC)
  src/                       # agent code (Microsoft Agent Framework), Dockerfile(s), requirements
  examples/                  # client scripts
docs/                        # ai-solutions-setup, endpoints-reference, observability-langfuse, adding-new-usecases
```

Current use cases (directory = namespace; host in parentheses):
- `coding-agent-maf` (`coding-agent.<base>`): Microsoft Agent Framework, Agent Sandbox code execution, Redis memory, optional KubeRay.
- `banking-assistant` (`banking-assistant.<base>`): FastMCP sidecar, Redis memory.
- `coding-agent` (`coding-agent-langgraph.<base>`, release `coding-agent-langgraph`): LangGraph, Agent Sandbox code execution, API-only with a Bearer `DEVUI_AUTH_TOKEN`.
- `customer-support-agent` (`customer-support-agent.<base>`): Streamlit UI at `/`, FastAPI at `/api`+`/v1`, SQLite demo data, RediSearch memory with the `bge-base-en` embedding model. It has **no user auth**.
- `deep-research-agent` (`deep-research-agent.<base>`, optional `-dashboard` host): LangGraph web research; needs `TAVILY_API_KEY`; SQLite on a PVC; 3600s route timeout for SSE.

`customer-support-agent` and `deep-research-agent` read `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`. Their
deploy.sh maps the dispatcher's `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`MODEL_NAME` onto those.

## Platform contract (AI Solutions 1.2 / agent-toolkit 2.0.0)

| Service | In-cluster endpoint | Credentials |
|---|---|---|
| LiteLLM (OpenAI API) | `http://litellm.litellm.svc.cluster.local:4000/v1` (external `https://litellm.<base>/v1`) | Secret `litellm/litellm-master-key` key `master_key`; prefer virtual keys (`/key/generate`) |
| Langfuse | `http://langfuse-web.langfuse.svc.cluster.local:3000` (UI `https://langfuse.<base>`) | Secret `langfuse/langfuse-secrets` keys `public_key`, `secret_key`, `admin_email`, `admin_password` |
| Redis Stack | `redis-stack-server.redis.svc.cluster.local:6379` (auth required) | Secret `redis/redis-stack-server-credentials` key `REDIS_URL` |
| Agent Sandbox router | `http://sandbox-router-svc.agent-sandbox-system.svc.cluster.local:8080` | Secret `agent-sandbox-system/sandbox-router-auth` key `ROUTER_AUTH_TOKEN` (Bearer) |
| Sandbox resources | SandboxTemplate `python-sandbox-template`, SandboxWarmPool `python-pool` | CRDs `extensions.agents.x-k8s.io/v1beta1`, `agents.x-k8s.io` |
| Gateway | Gateway `eg-gateway`, ns `envoy-gateway-system`, listener `https` = `*.<base_domain_name>` | wildcard TLS terminated at the gateway |

Rules every blueprint must follow:
- **Exposure:** use an `HTTPRoute` (`parentRefs` → `eg-gateway`/`envoy-gateway-system`/`https`, host `<usecase>.<base>`). TLS is terminated at the gateway, so there is no per-app TLS secret.
  - The gateway-wide `SecurityPolicy gateway-auth` requires a LiteLLM key on every route. Charts ship an empty route-level SecurityPolicy (`httpRoute.bypassGatewayAuth: true`) because DevUI has its own `DEVUI_AUTH_TOKEN`.
  - Don't use the reserved hosts `inference.`, `keycloak.` or `grafana.`.
- **Mesh:** Istio ambient with STRICT mTLS.
  - The app namespace must carry `istio.io/dataplane-mode=ambient`; `aisol_prepare_namespace` sets it.
  - To call LiteLLM, the namespace must also be in `litellm_allowed_namespaces` (`env/<env>/config.agent-toolkit.yaml`, then `./es_auto_installer.sh install agent-toolkit`). `aisol_check_litellm_access` warns, and `PATCH_LITELLM_AUTHZ=true` patches the policy as a temporary fix.
- **Pod Security:** namespaces are labelled `restricted`.
  - Pods: `runAsNonRoot`, UID/GID 10001 (numeric `USER` in the Dockerfile), `fsGroup`, `seccompProfile: RuntimeDefault`.
  - Containers: `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`.
- **Scheduling:** no hard node affinity. AI Solutions labels nodes `workload-class=platform|inference`, on multi-node clusters only.
- **Sandboxes:** sandbox pods have no in-cluster egress (NetworkPolicy `eat-sandbox-pool-egress`). Code running in a sandbox cannot call LiteLLM or Redis.
- **Auth mode:** the agent-toolkit default is `auth_provider: litellm`, so Keycloak is not deployed. Don't add Keycloak dependencies.

## Common commands

```bash
# Deploy (Kubernetes)
cp config.env config.local.env && $EDITOR config.local.env
set -a && source config.local.env && set +a
./deploy-usecase.sh coding-agent-maf --mode kubernetes
./deploy-usecase.sh --all

# Local checks (CI runs shellcheck, bandit, checkov, trivy)
bash -n deploy-usecase.sh usecases/*/deploy.sh scripts/*.sh
shellcheck -x deploy-usecase.sh usecases/*/deploy.sh scripts/*.sh
helm lint usecases/<name>/helm-chart --set agent.openaiApiKey=x --set agent.devuiAuthToken=x --set agent.modelName=x
helm template t usecases/<name>/helm-chart --set agent.openaiApiKey=x --set agent.devuiAuthToken=x \
  --set agent.modelName=x --set httpRoute.enabled=true --set httpRoute.host=t.solutions.ai
```

Platform kubeconfig: `env/<env>/kubeconfig.yaml` in the enterprise-ai-solutions checkout.
Validate the platform with `./es_auto_installer.sh validate agent-toolkit`.

## Conventions

- `deploy-usecase.sh` holds **no per-use-case code**. Any `usecases/<name>/` with an executable `deploy.sh` is deployable.
- Each `deploy.sh` does the following, in order:
  1. sources `scripts/ai-solutions-lib.sh`;
  2. runs discovery (`aisol_discover_*`), then prompts only for values that are still empty;
  3. builds images into containerd `k8s.io` via nerdctl (`SKIP_BUILD=true` to skip);
  4. runs `aisol_prepare_namespace` and `aisol_check_litellm_access`;
  5. calls `helm upgrade --install`, wiring everything with `--set`.
- Put new platform lookups in `scripts/ai-solutions-lib.sh`, not in individual `deploy.sh` files. Lookups stay best-effort: fill only empty vars, and no-op when the resource is missing.
- Chart values: secrets go into `<fullname>-secret` and plain config into `<fullname>-config`, both consumed with `envFrom`. Keep hardening `securityContext` blocks as inline literals so static scanners (checkov) can verify them.
- Shell: `set -euo pipefail`, the `info`/`success`/`warn`/`die` helpers, and shellcheck-clean code.
- Platform services are referenced by in-cluster DNS. Don't hard-code `base_domain_name`; discover it from `eg-gateway`, or read it from `CLUSTER_URL`.
- When adding a use case, follow `docs/adding-new-usecases.md` and add it to the catalog in `README.md`.
