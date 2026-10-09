#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# deploy.sh — One-click deployer for the Coding Agent use case
#
# Deploys the Coding Agent into an existing Agentic AI Stack cluster using Helm.
#
# Prerequisites:
#   • Agentic AI Stack is running (see ../../docs/agentic-stack-setup.md)
#   • kubectl is configured and pointing at your cluster
#   • helm is installed
#
# Usage:
#   ./deploy.sh
#
# Environment variables (all optional — script prompts if not set):
#   CLUSTER_URL         Base domain of the stack, e.g. api.example.com
#   OPENAI_API_KEY      LiteLLM / GenAI Gateway API key
#   MODEL_NAME          Model ID in LiteLLM, e.g. Qwen/Qwen3-Coder-30B-A3B-Instruct
#   OPENAI_BASE_URL     Override LiteLLM URL (default: https://<CLUSTER_URL>/v1)
#   REDIS_URL           Override Redis URL (default: in-cluster redis)
#   SANDBOX_ROUTER_URL  Override sandbox router URL (default: in-cluster router)
#   NAMESPACE           Kubernetes namespace (default: coding-agent)
#   INGRESS_HOST        Ingress hostname (default: coding-agent.<CLUSTER_URL>)
#   CERT_FILE           Path to TLS certificate PEM (optional)
#   KEY_FILE            Path to TLS private key PEM (optional)
#   DEVUI_AUTH_TOKEN    Auth token for DevUI web interface (auto-generated if unset)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/helm-chart"
SRC_DIR="${SCRIPT_DIR}/src"

AGENT_IMAGE="${AGENT_IMAGE:-coding-agent:latest}"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
for cmd in kubectl helm; do
  command -v "$cmd" &>/dev/null || die "Required tool '${cmd}' not found. Install it and re-run."
done

# ---------------------------------------------------------------------------
# Build the image into containerd's k8s.io namespace (nerdctl) so the kubelet
# uses it with pullPolicy: IfNotPresent. Skip with SKIP_BUILD=true + AGENT_IMAGE.
# ---------------------------------------------------------------------------
build_images() {
  if [[ "${SKIP_BUILD:-false}" == "true" ]]; then
    info "SKIP_BUILD=true — skipping image build."
    return
  fi

  local tool="${CONTAINER_TOOL:-}"
  if [[ -z "${tool}" ]]; then
    if command -v nerdctl &>/dev/null; then
      tool="nerdctl"
    elif command -v docker &>/dev/null; then
      tool="docker"
      warn "nerdctl not found; using docker. On a containerd-backed cluster the"
      warn "kubelet may not see docker-built images — push to a registry and set"
      warn "AGENT_IMAGE + SKIP_BUILD=true if the pod stays in ImagePullBackOff."
    else
      die "No container builder found. Install nerdctl (or docker), or set SKIP_BUILD=true."
    fi
  fi

  local build_cmd=("${tool}")
  local ns_args=()
  if [[ "${tool}" == "nerdctl" ]]; then
    ns_args=(--namespace k8s.io)
    [[ "$(id -u)" -ne 0 ]] && build_cmd=(sudo nerdctl)
    # Install/start buildkitd + pre-stage base images if needed.
    CONTAINER_TOOL="${tool}" "${SCRIPT_DIR}/../../scripts/ensure-image-builder.sh" "${SRC_DIR}/Dockerfile"
  fi

  local proxy_args=(
    --build-arg "http_proxy=${http_proxy:-}"
    --build-arg "https_proxy=${https_proxy:-}"
    --build-arg "no_proxy=${no_proxy:-}"
    --build-arg "HTTP_PROXY=${HTTP_PROXY:-${http_proxy:-}}"
    --build-arg "HTTPS_PROXY=${HTTPS_PROXY:-${https_proxy:-}}"
    --build-arg "NO_PROXY=${NO_PROXY:-${no_proxy:-}}"
  )

  info "Building agent image '${AGENT_IMAGE}' with ${build_cmd[*]}..."
  "${build_cmd[@]}" "${ns_args[@]}" build "${proxy_args[@]}" \
    -t "${AGENT_IMAGE}" -f "${SRC_DIR}/Dockerfile" "${SRC_DIR}"
  success "Built image: ${AGENT_IMAGE}"
}

# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------
prompt_if_empty() {
  local var_name="$1" prompt_text="$2" default="${3:-}"
  if [[ -z "${!var_name:-}" ]]; then
    if [[ -n "$default" ]]; then
      read -rp "  ${prompt_text} [${default}]: " input
      input="${input:-$default}"
    else
      read -rp "  ${prompt_text}: " input
    fi
    printf -v "$var_name" '%s' "$input"
  fi
}

prompt_secret_if_empty() {
  local var_name="$1" prompt_text="$2"
  if [[ -z "${!var_name:-}" ]]; then
    read -rsp "  ${prompt_text}: " input; echo ""
    printf -v "$var_name" '%s' "$input"
  fi
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Coding Agent — Deployment"
echo "============================================================"
echo ""

prompt_if_empty CLUSTER_URL \
  "Agentic Stack base domain (e.g. api.example.com)"

prompt_secret_if_empty OPENAI_API_KEY \
  "LiteLLM / GenAI Gateway API key (OPENAI_API_KEY)"

prompt_if_empty MODEL_NAME \
  "Model name registered in the gateway (must support tool calling)"

# Namespace defaults to the use case name; Helm release/Service stay "coding-agent".
NAMESPACE="${NAMESPACE:-coding-agent-maf}"
INGRESS_HOST="${INGRESS_HOST:-coding-agent.${CLUSTER_URL}}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://genai-gateway-service.genai-gateway.svc.cluster.local:4000/v1}"
SANDBOX_NAMESPACE="${SANDBOX_NAMESPACE:-agent-sandbox-system}"
SANDBOX_WARMPOOL="${SANDBOX_WARMPOOL:-python-pool}"
SANDBOX_ROUTER_URL="${SANDBOX_ROUTER_URL:-http://sandbox-router-svc.${SANDBOX_NAMESPACE}.svc.cluster.local:8080}"
DEVUI_AUTH_TOKEN="${DEVUI_AUTH_TOKEN:-$(openssl rand -hex 24 2>/dev/null || echo "changeme")}"

# Authenticated Redis URL from the toolkit's credentials secret (the shared Redis
# needs a password). Falls back to a passwordless URL if the secret isn't found.
if [[ -z "${REDIS_URL:-}" ]]; then
  REDIS_URL="$(kubectl get secret -n redis redis-stack-server-credentials \
    -o jsonpath='{.data.REDIS_URL}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  if [[ -n "${REDIS_URL}" ]]; then
    success "Auto-discovered authenticated Redis URL."
  else
    REDIS_URL="redis://redis-stack-server.redis.svc.cluster.local:6379"
    warn "redis-stack-server-credentials not found; using passwordless REDIS_URL (set it if auth is required)."
  fi
fi

# Sandbox-router bearer token (needed when the router requires auth).
if [[ -z "${SANDBOX_ROUTER_TOKEN:-}" ]]; then
  SANDBOX_ROUTER_TOKEN="$(kubectl get secret -n "${SANDBOX_NAMESPACE}" sandbox-router-auth \
    -o jsonpath='{.data.ROUTER_AUTH_TOKEN}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  [[ -n "${SANDBOX_ROUTER_TOKEN}" ]] && success "Auto-discovered sandbox-router auth token." || true
fi

echo ""
info "Deployment configuration:"
echo "  NAMESPACE          = ${NAMESPACE}"
echo "  INGRESS_HOST       = ${INGRESS_HOST}"
echo "  OPENAI_BASE_URL    = ${OPENAI_BASE_URL}"
echo "  MODEL_NAME         = ${MODEL_NAME}"
echo "  REDIS_URL          = $(sed -E 's#(://[^:]*:)[^@]*(@)#\1****\2#' <<<"${REDIS_URL}")"
echo "  SANDBOX_NAMESPACE  = ${SANDBOX_NAMESPACE}"
echo "  SANDBOX_WARMPOOL   = ${SANDBOX_WARMPOOL}"
echo "  SANDBOX_ROUTER_URL = ${SANDBOX_ROUTER_URL}"
echo "  SANDBOX_ROUTER_TOKEN = $([[ -n "${SANDBOX_ROUTER_TOKEN}" ]] && echo '****(set)' || echo '(none)')"
echo ""

# ---------------------------------------------------------------------------
# Build image(s)
# ---------------------------------------------------------------------------
build_images

# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------
info "Ensuring namespace '${NAMESPACE}' exists..."
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# ---------------------------------------------------------------------------
# TLS secret
# ---------------------------------------------------------------------------
# Option 1: pass TLS_SECRET_NAME directly to reference an existing cluster secret
# Option 2: pass CERT_FILE + KEY_FILE to create a new secret from files
# Option 3: set neither — ingress is created without TLS
if [[ -z "${TLS_SECRET_NAME:-}" ]]; then
  if [[ -n "${CERT_FILE:-}" && -n "${KEY_FILE:-}" ]]; then
    TLS_SECRET_NAME="coding-agent-tls"
    if kubectl get secret "${TLS_SECRET_NAME}" -n "${NAMESPACE}" &>/dev/null; then
      info "TLS secret '${TLS_SECRET_NAME}' already exists — skipping."
    else
      [[ -f "${CERT_FILE}" ]] || die "CERT_FILE '${CERT_FILE}' not found."
      [[ -f "${KEY_FILE}" ]]  || die "KEY_FILE '${KEY_FILE}' not found."
      kubectl create secret tls "${TLS_SECRET_NAME}" \
        --cert="${CERT_FILE}" --key="${KEY_FILE}" -n "${NAMESPACE}"
      success "Created TLS secret '${TLS_SECRET_NAME}'."
    fi
  else
    info "No TLS_SECRET_NAME or CERT_FILE/KEY_FILE set. Ingress will be created without TLS."
    TLS_SECRET_NAME=""
  fi
else
  if ! kubectl get secret "${TLS_SECRET_NAME}" -n "${NAMESPACE}" &>/dev/null; then
    warn "TLS_SECRET_NAME '${TLS_SECRET_NAME}' not found in namespace '${NAMESPACE}'. TLS will be disabled."
    TLS_SECRET_NAME=""
  else
    info "Using existing TLS secret '${TLS_SECRET_NAME}'."
  fi
fi

# ---------------------------------------------------------------------------
# Helm deploy
# ---------------------------------------------------------------------------
# Split "repo:tag" into repository + tag so the chart image matches what we built.
AGENT_IMAGE_REPO="${AGENT_IMAGE%:*}"; AGENT_IMAGE_TAG="${AGENT_IMAGE##*:}"
[[ "${AGENT_IMAGE}" == *:* ]] || AGENT_IMAGE_TAG="latest"

HELM_ARGS=(
  upgrade --install coding-agent "${CHART_DIR}"
  --namespace "${NAMESPACE}"
  --set "agent.image.repository=${AGENT_IMAGE_REPO}"
  --set "agent.image.tag=${AGENT_IMAGE_TAG}"
  --set "agent.openaiBaseUrl=${OPENAI_BASE_URL}"
  --set "agent.openaiApiKey=${OPENAI_API_KEY}"
  --set "agent.modelName=${MODEL_NAME}"
  --set "agent.devuiAuthToken=${DEVUI_AUTH_TOKEN}"
  --set "agent.sandboxNamespace=${SANDBOX_NAMESPACE}"
  --set "agent.sandboxWarmPool=${SANDBOX_WARMPOOL}"
  --set "agent.sandboxRouterUrl=${SANDBOX_ROUTER_URL}"
  --set "agent.sandboxRouterToken=${SANDBOX_ROUTER_TOKEN}"
  --set "redisUrl=${REDIS_URL}"
  --set "ingress.enabled=true"
  --set "ingress.host=${INGRESS_HOST}"
  --wait
  --timeout 5m
)

if [[ -n "${TLS_SECRET_NAME}" ]]; then
  HELM_ARGS+=(
    --set "ingress.tls.enabled=true"
    --set "ingress.tls.secretName=${TLS_SECRET_NAME}"
  )
fi

# Langfuse tracing (optional) — passed only when keys are provided.
if [[ -n "${LANGFUSE_PUBLIC_KEY:-}" && -n "${LANGFUSE_SECRET_KEY:-}" ]]; then
  HELM_ARGS+=(
    --set "agent.langfusePublicKey=${LANGFUSE_PUBLIC_KEY}"
    --set "agent.langfuseSecretKey=${LANGFUSE_SECRET_KEY}"
    --set "agent.langfuseHost=${LANGFUSE_HOST:-}"
    --set "agent.langfuseEnableSensitiveData=${LANGFUSE_ENABLE_SENSITIVE_DATA:-true}"
  )
  info "Langfuse tracing enabled (host: ${LANGFUSE_HOST:-<default>})."
fi

info "Deploying Coding Agent via Helm..."
helm "${HELM_ARGS[@]}"

# ---------------------------------------------------------------------------
# Post-deploy summary
# ---------------------------------------------------------------------------
success "Coding Agent deployed — namespace: ${NAMESPACE}"
echo ""
info "Access the Coding Agent:"
info "  https://${INGRESS_HOST}"
info "  (or locally: kubectl port-forward -n ${NAMESPACE} svc/coding-agent 8090:8090 → http://localhost:8090)"
info ""
info "  DevUI login token (enter in the browser login prompt):"
info "  ${DEVUI_AUTH_TOKEN}"
echo ""
