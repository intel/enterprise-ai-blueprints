# Adding a New Use Case

This guide explains how to add a new AI use case to this repository so that it
follows the same structure, deploy flow, and documentation standards as the
existing `coding-agent-maf`.

---

## Directory Structure

Every use case lives under `usecases/<usecase-name>/` and must have:

```
usecases/<usecase-name>/
├── README.md                  # Full documentation (required)
├── deploy.sh                  # Per-use-case one-click deploy script (required)
├── docker-compose.yml         # Local development without Kubernetes (recommended)
├── examples/                  # Working client examples (required)
│   ├── example_client.py
│   └── example_client.sh
├── helm-chart/                # Kubernetes Helm chart (required for K8s deploy)
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       ├── service.yaml
│       ├── ingress.yaml
│       ├── configmap.yaml
│       ├── secret.yaml
│       └── serviceaccount.yaml
└── src/                       # Agent/application source code (required)
    ├── Dockerfile
    ├── requirements.txt
    ├── agent.py               # (or main application file)
    └── main.py
```

---

## Step 1 — Create the source application

Your use case application should:

1. **Connect to the GenAI Gateway** via OpenAI-compatible API
   ```python
   import os
   from openai import OpenAI
   client = OpenAI(
       base_url=os.getenv("OPENAI_BASE_URL", "http://genai-gateway-service.genai-gateway.svc.cluster.local:4000/v1"),
       api_key=os.getenv("OPENAI_API_KEY"),
   )
   ```

2. **Use Redis for memory** (optional but recommended)
   ```python
   # NOTE: the shared toolkit Redis requires auth — REDIS_URL must include
   # credentials, e.g. redis://default:<password>@redis-stack-server.redis.svc.cluster.local:6379
   # Get it from: kubectl get secret -n redis redis-stack-server-credentials \
   #   -o jsonpath='{.data.REDIS_URL}' | base64 -d
   REDIS_URL = os.getenv("REDIS_URL", "redis://redis-stack-server.redis.svc.cluster.local:6379")
   ```

3. **Expose an HTTP API** on a well-known port (e.g. 8080 or 8090)

4. **Include a `/health` endpoint** for liveness probes

5. **Read all configuration from environment variables** — no hardcoded values

---

## Step 2 — Write the Dockerfile

Base your Dockerfile on `usecases/coding-agent-maf/src/Dockerfile`.
Key rules:

- Use a non-root user
- Accept proxy build args (`http_proxy`, `https_proxy`, `no_proxy`, etc.)
- Set `PYTHONUNBUFFERED=1`
- Add a `HEALTHCHECK` instruction

```dockerfile
FROM python:3.12-slim

ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG no_proxy
ARG NO_PROXY

RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R appuser:appuser /app

ENV PYTHONUNBUFFERED=1
USER appuser
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import socket; s=socket.socket(); s.connect(('localhost', 8090)); s.close()"

CMD ["python", "main.py"]
```

---

## Step 3 — Create the Helm chart

Bootstrap from the coding-agent-maf chart:

```bash
cp -r usecases/coding-agent-maf/helm-chart usecases/<your-usecase>/helm-chart
```

Edit `Chart.yaml`:
```yaml
apiVersion: v2
name: <your-usecase>
description: <Short description>
version: 0.1.0
appVersion: "1.0.0"
```

Minimum required templates:
- `deployment.yaml` — Deployment with env vars from values + secret
- `service.yaml` — ClusterIP service
- `ingress.yaml` — Ingress (enabled by values flag)
- `secret.yaml` — Kubernetes Secret for API keys
- `serviceaccount.yaml` — Dedicated ServiceAccount

Standard environment variables to wire in `values.yaml`:

```yaml
agent:
  openaiBaseUrl: "http://genai-gateway-service.genai-gateway.svc.cluster.local:4000/v1"
  openaiApiKey: ""      # injected via secret
  modelName: ""         # required — set at deploy time
  redisUrl: "redis://redis-stack-server.redis.svc.cluster.local:6379"

ingress:
  enabled: false
  host: ""
  tls:
    enabled: false
    secretName: ""
```

---

## Step 4 — Create `deploy.sh`

Each use case must have its own `deploy.sh`. Model it after the coding-agent-maf's:

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Collect config
CLUSTER_URL="${CLUSTER_URL:-}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
MODEL_NAME="${MODEL_NAME:-}"
NAMESPACE="${NAMESPACE:-<your-usecase>}"
INGRESS_HOST="${INGRESS_HOST:-<your-usecase>.${CLUSTER_URL}}"

# Deploy
helm upgrade --install <your-usecase> "${SCRIPT_DIR}/helm-chart" \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  --set "agent.openaiApiKey=${OPENAI_API_KEY}" \
  --set "agent.modelName=${MODEL_NAME}" \
  --set "ingress.host=${INGRESS_HOST}" \
  --wait --timeout 5m

echo "Deployed! Endpoint: https://${INGRESS_HOST}"
```

Make it executable: `chmod +x deploy.sh`

---

## Step 5 — Create `docker-compose.yml`

Provide a local development stack (no Kubernetes needed). At minimum include
your service and Redis:

```yaml
services:
  <your-usecase>:
    build:
      context: ./src
      dockerfile: Dockerfile
    ports:
      - "8090:8090"
    environment:
      OPENAI_BASE_URL: "${LITELLM_URL:-http://host.docker.internal:4000/v1}"
      OPENAI_API_KEY: "${LITELLM_API_KEY:-sk-placeholder}"
      MODEL_NAME: "${MODEL_NAME:-}"
      REDIS_URL: "redis://redis:6379"

  redis:
    image: redis/redis-stack-server:7.4.0-v8
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      retries: 5
```

---

## Step 6 — Write `README.md`

The use case README should include these sections:

1. **Overview** — What the use case does, architecture diagram
2. **Prerequisites** — Required stack components
3. **Quick Start** — Three commands to get it running
4. **Configuration** — All environment variables in a table
5. **API Reference** — All endpoints with `curl` examples
6. **Running Examples** — How to run the scripts in `examples/`
7. **Local Development** — Docker Compose instructions
8. **Troubleshooting** — Common issues and fixes

---

## Step 7 — Add examples

Add at least two example clients:

- **`examples/example_client.py`** — Python client using `httpx` or `openai`
- **`examples/example_client.sh`** — `curl`-based examples

Each example must be runnable immediately after `./deploy-usecase.sh`:

```bash
python3 usecases/<your-usecase>/examples/example_client.py https://<host>
```

---

## Step 8 — Nothing to register

The root `deploy-usecase.sh` is a thin dispatcher: it auto-discovers any
`usecases/<name>/` that contains a `deploy.sh` and delegates to it. Once your
use case has a `deploy.sh` (Step 4) and a `docker-compose.yml` (Step 5), it is
immediately deployable — no edit to the master script is needed:

```bash
./deploy-usecase.sh <your-usecase> --mode kubernetes
./deploy-usecase.sh <your-usecase> --mode docker
```

---

## Checklist

- [ ] `usecases/<usecase-name>/README.md` written
- [ ] `usecases/<usecase-name>/deploy.sh` created and `chmod +x`
- [ ] `usecases/<usecase-name>/docker-compose.yml` works locally
- [ ] `usecases/<usecase-name>/src/Dockerfile` builds cleanly
- [ ] `usecases/<usecase-name>/helm-chart/` deploys with `helm install --dry-run`
- [ ] `usecases/<usecase-name>/examples/` contains at least one `.py` and one `.sh`
- [ ] Root `README.md` use case table updated
- [ ] All env vars read from environment (no hardcoded secrets or URLs)
- [ ] Non-root user in Dockerfile
- [ ] `/health` endpoint responds with `200 OK`
