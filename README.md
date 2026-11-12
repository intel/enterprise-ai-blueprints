# Agentic AI Blueprints

A catalog of **deployable AI reference use cases** ("blueprints") that
run on top of **Intel® Agent Toolkit**, installed and operated by
**[Toolkit Manager](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2)**.
Together with Intel® Inference Toolkit, it provides the shared services every
blueprint consumes: an OpenAI-compatible LLM gateway (LiteLLM), vector/key-value
stores, an isolated code-execution sandbox, distributed compute (KubeRay), an
Envoy Gateway, and observability.

Each blueprint is **self-contained** — its own source, container images, Helm
chart, and one-click deploy script — and plugs into Intel® Agent Toolkit without
modifying the platform. Clone the repo, point a blueprint at your
cluster (most endpoints are auto-discovered), and deploy.

> **Important: these are demonstration samples, not products.** Every use case
> in this repository is reference code intended to show how an agent can be built
> on and deployed to Intel® Agent Toolkit. The samples use synthetic data, carry
> minimal authentication, and have not been hardened, load-tested, or validated
> for production use. Do not deploy them to production environments or connect
> them to real customer data or systems without your own security review,
> hardening, and testing. See [Known limitations](#known-limitations) and
> [SECURITY.md](SECURITY.md).

> **New here?** Read [Prerequisites](#prerequisites) first — a blueprint cannot
> run without Intel® Agent Toolkit installed (via Toolkit Manager).

> [!NOTE]
> **Coming soon:** the blueprints will be installable directly through
> Toolkit Manager as their own layer (`./es_auto_installer.sh install blueprints`).
> See [ROADMAP.md](ROADMAP.md).

---

## Table of Contents

- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Use Case Catalog](#use-case-catalog)
- [Getting Started](#getting-started)
- [Deploy a Use Case](#deploy-a-use-case)
- [Repository Structure](#repository-structure)
- [Documentation](#documentation)
- [Known limitations](#known-limitations)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

---

## How it works

```
        https://<usecase>.<base_domain_name>
                     │
┌────────────────────▼─────────────────────────────────────────────────┐
│  Envoy Gateway  eg-gateway  (envoy-gateway-system, TLS *.<base>)     │
│                     │ HTTPRoutes (one per blueprint)                 │
│                     │                                                │
│  Toolkit Manager — installs the stack (deploy first)                 │
│  Inference Toolkit : LiteLLM │ LLM models (vLLM) │ Langfuse          │
│  Agent Toolkit     : Redis │ Agent Sandbox │ KubeRay (optional)      │
│  platform          : Istio ambient (STRICT mTLS) │ Grafana / OTel    │
└────────────────────┼─────────────────────────────────────────────────┘
                     │  routes traffic in; blueprints call LiteLLM,
                     │  Redis, sandbox router, Langfuse by URL + key
      ┌──────────────▼───────────────┐
      │   Agentic AI Blueprints      │  ← this repo
      │   (ambient-mesh namespaces)  │
      │   • coding-agent-maf         │
      │   • banking-assistant        │
      │   • coding-agent (LangGraph) │
      │   • customer-support-agent   │
      │   • deep-research-agent      │
      │   • (your use case here)     │
      └──────────────────────────────┘
```

Toolkit Manager installs and operates the platform (inference, gateway, mesh, memory, sandboxing,
compute, telemetry). Blueprints are thin applications that **consume** those
services by URL + credentials and publish themselves through an HTTPRoute on
`eg-gateway`. This separation means a blueprint never re-deploys infrastructure —
it just connects to what the platform already runs.

---

## Prerequisites

### 1. Install Intel® Agent Toolkit with Toolkit Manager (required, first)

Every blueprint depends on services **Intel® Agent Toolkit** and **Intel® Inference Toolkit**
provide. Install them with Toolkit Manager **before** any blueprint:

> **➡️ [github.com/intel/enterprise-ai-solutions (release-1.2)](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2)** —
> Intel® Agent Toolkit: [intel/enterprise-agent-toolkit release-2.0.0](https://github.com/intel/enterprise-agent-toolkit/tree/release-2.0.0);
> Intel® Inference Toolkit: [intel/enterprise-inference release-1.1](https://github.com/intel/enterprise-inference/tree/release-1.1).

```bash
git clone https://github.com/intel/enterprise-ai-solutions.git -b release-1.2 && cd enterprise-ai-solutions
./es_auto_installer.sh configure
./es_auto_installer.sh init agent-toolkit      # seeds env/local/*.yaml
#   edit env/local/global_config.yaml (base_domain_name, proxy) and add the blueprint
#   namespaces to litellm_allowed_namespaces in env/local/config.agent-toolkit.yaml
./es_auto_installer.sh install agent-toolkit
./es_auto_installer.sh validate agent-toolkit
```

The platform provides the endpoints blueprints consume:

| Service | Consumed as | Used by |
|---|---|---|
| LiteLLM (Inference Toolkit) | `OPENAI_BASE_URL` + `OPENAI_API_KEY` | all blueprints |
| Redis (`redis_stack_enabled`) | `REDIS_URL` | conversational memory |
| Agent Sandbox (`agent_sandbox_enabled`) | `SANDBOX_ROUTER_URL` | code-execution blueprints |
| KubeRay (`kuberay_enabled`, optional) | `RAY_ADDRESS` | distributed parallel execution |
| Langfuse (Inference Toolkit) | `LANGFUSE_*` | tracing (optional) |
| Envoy Gateway (`eg-gateway`) | `CLUSTER_URL` (= `base_domain_name`) | external access via HTTPRoute |

See [docs/toolkit-manager-setup.md](docs/toolkit-manager-setup.md) for the setup
checklist (namespace allow-list, verification, `/etc/hosts` + CA) and
[docs/endpoints-reference.md](docs/endpoints-reference.md) for the exact
in-cluster service URLs.

### 2. Local tooling

| Deployment mode | Requires |
|---|---|
| **Kubernetes** (cluster) | `kubectl`, `helm`, a kubeconfig with access to the Intel® Agent Toolkit cluster (`env/local/kubeconfig.yaml` in your Toolkit Manager checkout), and `nerdctl` (present on containerd clusters — the default) **or** `docker`. That's it: each `deploy.sh` builds the use case's image into the cluster's containerd `k8s.io` namespace (so the kubelet uses it with `pullPolicy: IfNotPresent`). On containerd, `nerdctl build` needs `buildkitd` — **the deploy installs and starts it for you** (see below); you don't set it up by hand. |
| **Docker** (local dev) | Docker Engine 24+ with Docker Compose v2, and a reachable LLM endpoint (e.g. `https://litellm.<base_domain_name>/v1` + a LiteLLM key) |

> **Building images (automatic).** Blueprints ship with local (`:latest`) image
> references, so the k8s `deploy.sh` builds them on first deploy. Before building it
> runs [`scripts/ensure-image-builder.sh`](scripts/ensure-image-builder.sh), which
> installs and starts `buildkitd` if it isn't already running and pre-stages base
> images (so builds also work behind a corporate proxy).

---

## Use Case Catalog

| Use Case | Category | Description | Ports | Docs |
|---|---|---|---|---|
| [coding-agent-maf](usecases/coding-agent-maf/) | Intel® Agent Toolkit | AI coding assistant (Microsoft Agent Framework) that writes, executes, and debugs Python in sandboxed Kubernetes pods, with persistent Redis memory | 8090 (DevUI) | [README](usecases/coding-agent-maf/README.md) |
| [banking-assistant](usecases/banking-assistant/) | Intel® Agent Toolkit | Conversational banking assistant that answers account/loan queries and moves funds via a FastMCP banking-tools sidecar (MCP client/server demo) | 8090 (DevUI) | [README](usecases/banking-assistant/README.md) |
| [coding-agent](usecases/coding-agent/) | Intel® Agent Toolkit | AI coding assistant (LangGraph) that writes and runs Python in Agent Sandbox pods; API-only (OpenAI-compatible `/v1/chat/completions` + `/v1/responses`, bearer token), host `coding-agent-langgraph.<base>` | 8090 (API) | [README](usecases/coding-agent/README.md) |
| [customer-support-agent](usecases/customer-support-agent/) | Intel® Agent Toolkit | E-commerce support agent with 40 tools over a SQLite demo DB, RediSearch long-term memory (`bge-base-en` embeddings) and parallel tool execution; **no user auth**, host `customer-support-agent.<base>` | 8501 (Streamlit UI), 8502 (API) | [README](usecases/customer-support-agent/README.md) |
| [deep-research-agent](usecases/deep-research-agent/) | Intel® Agent Toolkit | Autonomous web-research agent (LangGraph) that searches the web (Tavily key required), scores sources and writes a cited report, streamed over SSE; host `deep-research-agent.<base>` (+ optional `-dashboard`) | 8081 (UI + API), 8082 (dashboard) | [README](usecases/deep-research-agent/README.md) |

> **Categories** group blueprints by the primary capability they demonstrate
> (e.g. `Intel® Agent Toolkit`, `Intel® Inference Toolkit`, `RAG`). Adding a use case? Pick or propose
> a category — see [Contributing](#contributing).

---

## Getting Started

```bash
# 1. Clone
git clone <THIS_REPO_URL>
cd blueprints

# 2. Configure (copy the template; in Kubernetes mode most values are auto-discovered)
cp config.env config.local.env
$EDITOR config.local.env          # set MODEL_NAME (e.g. qwen2-5-coder-14b); optionally
                                  # CLUSTER_URL, OPENAI_API_KEY (LiteLLM virtual key), ...

# 3. Load config into the environment
set -a && source config.local.env && set +a
```

`config.local.env` holds secrets — it is git-ignored; never commit it.

`CLUSTER_URL` is the Toolkit Manager `base_domain_name` (e.g. `solutions.ai`);
blueprints are published at `https://<usecase>.<CLUSTER_URL>`. To reach them from
your workstation, map those hostnames to the `eg-gateway` IP in `/etc/hosts` and
trust `env/local/logs/ai-solutions-ca.crt` (see
[docs/toolkit-manager-setup.md](docs/toolkit-manager-setup.md#3-reach-the-gateway-hostnames--ca)).

---

## Deploy a Use Case

Every blueprint supports two modes: **Kubernetes** (cluster) and **Docker**
(local development). Use the top-level dispatcher or a blueprint's own `deploy.sh`.

### Kubernetes (recommended)

```bash
# Dispatcher — auto-discovers LiteLLM, Redis, sandbox token, Langfuse keys and the base domain
./deploy-usecase.sh coding-agent-maf  --mode kubernetes
./deploy-usecase.sh banking-assistant --mode kubernetes

# Deploy every use case at once
./deploy-usecase.sh --all --mode kubernetes
```

### Docker (local development)

```bash
./deploy-usecase.sh coding-agent-maf  --mode docker
./deploy-usecase.sh banking-assistant --mode docker
```

### Per-use-case script

Each blueprint is also deployable on its own:

```bash
cd usecases/coding-agent-maf
./deploy.sh
```

Configuration is read from the environment (see [config.env](config.env)); the
scripts prompt for anything still unset. In Kubernetes mode the deploy
auto-discovers cluster services so you rarely need to set URLs by hand. It also
labels the namespace for the Istio ambient mesh and Pod Security `restricted`,
and warns if the namespace is not yet in `litellm_allowed_namespaces`.

---

## Repository Structure

```
.
├── README.md                  # This file
├── CONTRIBUTING.md            # How to add a use case / contribute
├── SECURITY.md                # Reporting + accepted scan exceptions
├── ROADMAP.md                 # Planned work (install through Toolkit Manager)
├── LICENSE
├── config.env                 # Unified configuration template
├── deploy-usecase.sh          # One-click dispatcher (delegates to each use case)
│
├── scripts/
│   ├── toolkit-manager-lib.sh     # Shared platform discovery + namespace helpers (sourced by deploy.sh)
│   └── ensure-image-builder.sh # Installs/starts buildkitd + pre-stages base images (auto-run by deploy.sh)
│
├── docs/
│   ├── toolkit-manager-setup.md    # Toolkit Manager / Intel® Agent Toolkit setup checklist (deploy this first)
│   ├── endpoints-reference.md   # In-cluster service URLs + credentials the platform exposes
│   ├── observability-langfuse.md # Enable Langfuse tracing for a use case
│   └── adding-new-usecases.md   # Step-by-step guide to author a new use case
│
└── usecases/
    ├── coding-agent-maf/      # One self-contained blueprint per directory
    │   ├── README.md
    │   ├── deploy.sh
    │   ├── docker-compose.yml
    │   ├── examples/
    │   ├── helm-chart/
    │   └── src/               # source + Dockerfile
    ├── banking-assistant/
    │   └── ...
    ├── coding-agent/          # LangGraph coding agent (host coding-agent-langgraph.<base>)
    │   └── ...
    ├── customer-support-agent/
    │   └── ...
    └── deep-research-agent/
        └── ...
```

Use cases live **flat** under `usecases/<name>/`; the dispatcher and the CI
security scans auto-discover any directory that contains a `deploy.sh`.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/toolkit-manager-setup.md](docs/toolkit-manager-setup.md) | Installing Intel® Agent Toolkit with Toolkit Manager — the prerequisite for every blueprint |
| [docs/endpoints-reference.md](docs/endpoints-reference.md) | The in-cluster service URLs (and credentials) blueprints connect to |
| [docs/observability-langfuse.md](docs/observability-langfuse.md) | Enabling Langfuse tracing for a use case (projects, keys, OTEL) |
| [docs/adding-new-usecases.md](docs/adding-new-usecases.md) | Authoring a new blueprint that matches repo standards |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution workflow, DCO sign-off, linting, PRs |
| [ROADMAP.md](ROADMAP.md) | Planned work, incl. a `blueprints` layer installable through Toolkit Manager |

Each use case also has its own README with architecture, configuration, API
reference, and troubleshooting.

---

## Known limitations

These limitations are intentional for sample code and are documented in each
use case README.

- **Demo-level authentication only.** Where a blueprint has auth, it is a single
  shared token: a bearer token generated at deploy time for coding-agent-maf,
  banking-assistant and coding-agent, and optional `API_KEYS` for
  deep-research-agent. **customer-support-agent has no authentication at all.**
  There is no user management, SSO, role-based access, rate limiting, or audit logging.
- **Banking assistant has no per-user authorization.** Any caller can query any
  synthetic `user_id` and move synthetic funds. The MCP server is reachable only
  inside the pod. Never connect it to a real banking system.
- **Single shared memory user.** Conversation memory in Redis is scoped per
  session but uses one demo user identity; there is no multi-tenant isolation.
- **Prompt injection is not mitigated.** As with any LLM agent, model output can
  trigger tool calls. Code execution is isolated in sandbox pods, but outputs
  should be treated as untrusted.
- **Self-signed TLS by default.** TLS is terminated at the platform's Envoy
  Gateway with a self-signed wildcard certificate unless you configure your own.
  The blueprint routes opt out of the gateway's LiteLLM-key check so browsers can
  open them, and rely on each app's own auth (see above).
- **Ray parallel execution is optional and off by default** (`RAY_ENABLED=false`).
  When Ray is not connected, the parallel tool falls back to sequential execution
  inside the agent pod.
- **Container images are built locally** at deploy time from `python:3.12-slim`;
  no signed or published images are provided.
- **Not performance- or scale-tested.** Single replica per use case; resource
  limits are illustrative.

---

## Contributing

Contributions are welcome — new blueprints, fixes, and documentation. A blueprint
can target Intel® Agent Toolkit or any other purpose (inference, RAG, tooling demos).

Start with **[CONTRIBUTING.md](CONTRIBUTING.md)** and the authoring guide in
**[docs/adding-new-usecases.md](docs/adding-new-usecases.md)**. In short: fork,
add a self-contained `usecases/<name>/`, pick a category, run the linters and
security scans locally, sign your commits (DCO), and open a pull request.

---

## Security

Report vulnerabilities via the process in [SECURITY.md](SECURITY.md), which also
records the project's reviewed-and-accepted scan exceptions.

---

## License

Licensed under the terms in [LICENSE](LICENSE) (Apache-2.0). By contributing you
agree your contribution is released under the same license.
