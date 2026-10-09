# Agentic AI Blueprints

A catalog of **deployable AI reference use cases** ("blueprints") that
run on top of the **Agent Toolkit** — the Kubernetes-based infrastructure that
provides the shared services every blueprint consumes: an OpenAI-compatible LLM
gateway, vector/key-value stores, an isolated code-execution sandbox, distributed
compute (KubeRay), and observability.

Each blueprint is **self-contained** — its own source, container images, Helm
chart, and one-click deploy script — and plugs into the Agent Toolkit without
modifying the infrastructure. Clone the repo, point a blueprint at your toolkit
endpoints, and deploy.

> **Important: these are demonstration samples, not products.** Every use case
> in this repository is reference code intended to show how an agent can be built
> on and deployed to the Agent Toolkit. The samples use synthetic data, carry
> minimal authentication, and have not been hardened, load-tested, or validated
> for production use. Do not deploy them to production environments or connect
> them to real customer data or systems without your own security review,
> hardening, and testing. See [Known limitations](#known-limitations) and
> [SECURITY.md](SECURITY.md).

> **New here?** Read [Prerequisites](#prerequisites) first — a blueprint cannot
> run without the Agent Toolkit deployed.

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
┌──────────────────────────────────────────────────────────────────┐
│                     Agent Toolkit  (deploy first)                  │
│                                                                    │
│  GenAI Gateway (LiteLLM) │ Redis │ Agent Sandbox │ KubeRay         │
│  LLM Models (vLLM) │ Keycloak │ Observability (Langfuse / OTEL)    │
└──────────────────────────────────┬─────────────────────────────────┘
                                   │  provides endpoints (LLM, memory,
                                   │  sandbox, compute, telemetry)
                    ┌──────────────▼──────────────┐
                    │   Agentic AI Blueprints      │  ← this repo
                    │                              │
                    │   • coding-agent-maf         │
                    │   • banking-assistant        │
                    │   • (your use case here)     │
                    └──────────────────────────────┘
```

The Agent Toolkit owns the platform (inference, auth, memory, sandboxing,
compute, telemetry). Blueprints are thin applications that **consume** those
services by URL + credentials. This separation means a blueprint never re-deploys
infrastructure — it just connects to what the toolkit already runs.

---

## Prerequisites

### 1. Deploy the Agent Toolkit (required, first)

Every blueprint depends on services the **Agent Toolkit** provides. Deploy it
**before** any blueprint and follow its own documentation:

> **➡️ Agent Toolkit: [github.com/intel/enterprise-agent-toolkit](https://github.com/intel/enterprise-agent-toolkit)** — follow its README to deploy the infrastructure.

The toolkit provides the endpoints blueprints consume:

| Service | Consumed as | Used by |
|---|---|---|
| GenAI Gateway (LiteLLM) | `OPENAI_BASE_URL` + `OPENAI_API_KEY` | all blueprints |
| Redis | `REDIS_URL` | conversational memory |
| Agent Sandbox | `SANDBOX_ROUTER_URL` | code-execution blueprints |
| KubeRay (optional) | `RAY_ADDRESS` | distributed parallel execution |
| Observability (Langfuse) | `LANGFUSE_*` | tracing (optional) |

See [docs/agentic-stack-setup.md](docs/agentic-stack-setup.md) for the setup
checklist and [docs/endpoints-reference.md](docs/endpoints-reference.md) for the
exact in-cluster service URLs.

### 2. Local tooling

| Deployment mode | Requires |
|---|---|
| **Kubernetes** (cluster) | `kubectl`, `helm`, a kubeconfig with access to the Agent Toolkit cluster, and `nerdctl` (present on containerd clusters — the default) **or** `docker`. That's it: each `deploy.sh` builds the use case's image into the cluster's containerd `k8s.io` namespace (so the kubelet uses it with `pullPolicy: IfNotPresent`). On containerd, `nerdctl build` needs `buildkitd` — **the deploy installs and starts it for you** (see below); you don't set it up by hand. |
| **Docker** (local dev) | Docker Engine 24+ with Docker Compose v2, and a reachable LLM endpoint |

> **Building images (automatic).** Blueprints ship with local (`:latest`) image
> references, so the k8s `deploy.sh` builds them on first deploy. Before building it
> runs [`scripts/ensure-image-builder.sh`](scripts/ensure-image-builder.sh), which
> installs and starts `buildkitd` if it isn't already running and pre-stages base
> images (so builds also work behind a corporate proxy).

---

## Use Case Catalog

| Use Case | Category | Description | Ports | Docs |
|---|---|---|---|---|
| [coding-agent-maf](usecases/coding-agent-maf/) | Agent Toolkit | AI coding assistant (Microsoft Agent Framework) that writes, executes, and debugs Python in sandboxed Kubernetes pods, with persistent Redis memory | 8090 (DevUI) | [README](usecases/coding-agent-maf/README.md) |
| [banking-assistant](usecases/banking-assistant/) | Agent Toolkit | Conversational banking assistant that answers account/loan queries and moves funds via a FastMCP banking-tools sidecar (MCP client/server demo) | 8090 (DevUI) | [README](usecases/banking-assistant/README.md) |

> **Categories** group blueprints by the primary capability they demonstrate
> (e.g. `Agent Toolkit`, `Inference`, `RAG`). Adding a use case? Pick or propose
> a category — see [Contributing](#contributing).

---

## Getting Started

```bash
# 1. Clone
git clone <THIS_REPO_URL>
cd blueprints

# 2. Configure (copy the template and fill in your Agent Toolkit endpoints)
cp config.env config.local.env
$EDITOR config.local.env          # set OPENAI_BASE_URL, OPENAI_API_KEY, MODEL_NAME, ...

# 3. Load config into the environment
set -a && source config.local.env && set +a
```

`config.local.env` holds secrets — it is git-ignored; never commit it.

---

## Deploy a Use Case

Every blueprint supports two modes: **Kubernetes** (cluster) and **Docker**
(local development). Use the top-level dispatcher or a blueprint's own `deploy.sh`.

### Kubernetes (recommended)

```bash
# Dispatcher — auto-discovers GenAI Gateway + Redis in the cluster where possible
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
scripts prompt for anything still unset. In Kubernetes mode the dispatcher
auto-discovers cluster services so you rarely need to set URLs by hand.

---

## Repository Structure

```
.
├── README.md                  # This file
├── CONTRIBUTING.md            # How to add a use case / contribute
├── SECURITY.md                # Reporting + accepted scan exceptions
├── LICENSE
├── config.env                 # Unified configuration template
├── deploy-usecase.sh          # One-click dispatcher (delegates to each use case)
│
├── scripts/
│   └── ensure-image-builder.sh # Installs/starts buildkitd + pre-stages base images (auto-run by deploy.sh)
│
├── docs/
│   ├── agentic-stack-setup.md   # Agent Toolkit setup checklist (deploy this first)
│   ├── endpoints-reference.md   # In-cluster service URLs + credentials the toolkit exposes
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
    └── banking-assistant/
        └── ...
```

Use cases live **flat** under `usecases/<name>/`; the dispatcher and the CI
security scans auto-discover any directory that contains a `deploy.sh`.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/agentic-stack-setup.md](docs/agentic-stack-setup.md) | Deploying the Agent Toolkit — the prerequisite for every blueprint |
| [docs/endpoints-reference.md](docs/endpoints-reference.md) | The in-cluster service URLs (and credentials) blueprints connect to |
| [docs/observability-langfuse.md](docs/observability-langfuse.md) | Enabling Langfuse tracing for a use case (projects, keys, OTEL) |
| [docs/adding-new-usecases.md](docs/adding-new-usecases.md) | Authoring a new blueprint that matches repo standards |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution workflow, DCO sign-off, linting, PRs |

Each use case also has its own README with architecture, configuration, API
reference, and troubleshooting.

---

## Known limitations

These limitations are intentional for sample code and are documented in each
use case README.

- **Demo-level authentication only.** The DevUI/API is protected by a single
  bearer token generated at deploy time. There is no user management, SSO,
  role-based access, rate limiting, or audit logging.
- **Banking assistant has no per-user authorization.** Any caller can query any
  synthetic `user_id` and move synthetic funds. The MCP server is reachable only
  inside the pod. Never connect it to a real banking system.
- **Single shared memory user.** Conversation memory in Redis is scoped per
  session but uses one demo user identity; there is no multi-tenant isolation.
- **Prompt injection is not mitigated.** As with any LLM agent, model output can
  trigger tool calls. Code execution is isolated in sandbox pods, but outputs
  should be treated as untrusted.
- **TLS is optional.** Ingress TLS is enabled only when you supply a certificate
  and key at deploy time.
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
can target the Agent Toolkit or any other purpose (inference, RAG, tooling demos).

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
