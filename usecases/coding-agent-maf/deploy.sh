#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# deploy.sh — One-click deployer for the Coding Agent use case
#
# Deploys the Coding Agent onto Intel® Agent Toolkit (installed by Toolkit
# Manager release-1.2) using Helm.
#
# Prerequisites:
#   • Intel Agent Toolkit is installed with Toolkit Manager (see ../../docs/toolkit-manager-setup.md)
#   • kubectl is configured and pointing at your cluster (env/<env>/kubeconfig.yaml)
#   • helm is installed
#
# Usage:
#   ./deploy.sh
#
# Environment variables (all optional — auto-discovered from the cluster or prompted):
#   CLUSTER_URL         Toolkit Manager base_domain_name, e.g. solutions.ai (from eg-gateway)
#   OPENAI_API_KEY      LiteLLM key (default: litellm-master-key; prefer a virtual key)
#   MODEL_NAME          Model ID in LiteLLM, e.g. qwen2-5-coder-14b
#   OPENAI_BASE_URL     Override LiteLLM URL (default: in-cluster litellm.litellm:4000)
#   REDIS_URL           Override Redis URL (default: redis-stack-server-credentials)
#   SANDBOX_ROUTER_URL  Override sandbox router URL (default: in-cluster router)
#   LANGFUSE_*          Langfuse keys/host (default: langfuse/langfuse-secrets)
#   NAMESPACE           Kubernetes namespace (default: coding-agent-maf)
#   INGRESS_HOST        HTTPRoute hostname (default: coding-agent.<CLUSTER_URL>)
#   PATCH_LITELLM_AUTHZ true = add NAMESPACE to LiteLLM's AuthorizationPolicy directly
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

# shellcheck source=scripts/toolkit-manager-lib.sh
source "${SCRIPT_DIR}/../../scripts/toolkit-manager-lib.sh"

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

tm_discover_base_domain
tm_discover_litellm_key
tm_discover_langfuse

prompt_if_empty CLUSTER_URL \
  "Toolkit Manager base_domain_name (e.g. solutions.ai)"
[[ -n "${CLUSTER_URL}" ]] || die "CLUSTER_URL (Toolkit Manager base_domain_name) is required."

prompt_secret_if_empty OPENAI_API_KEY \
  "LiteLLM API key (OPENAI_API_KEY)"

prompt_if_empty MODEL_NAME \
  "Model name registered in the gateway (must support tool calling)"

# Namespace defaults to the use case name; Helm release/Service stay "coding-agent".
NAMESPACE="${NAMESPACE:-coding-agent-maf}"
INGRESS_HOST="${INGRESS_HOST:-coding-agent.${CLUSTER_URL}}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-${TM_LITELLM_URL}}"
SANDBOX_NAMESPACE="${SANDBOX_NAMESPACE:-agent-sandbox-system}"
SANDBOX_WARMPOOL="${SANDBOX_WARMPOOL:-python-pool}"
SANDBOX_ROUTER_URL="${SANDBOX_ROUTER_URL:-http://sandbox-router-svc.${SANDBOX_NAMESPACE}.svc.cluster.local:8080}"
DEVUI_AUTH_TOKEN="${DEVUI_AUTH_TOKEN:-$(openssl rand -hex 24 2>/dev/null || echo "changeme")}"

# Authenticated Redis URL from the agent-toolkit's credentials secret (the shared Redis
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
echo "  LANGFUSE_HOST      = ${LANGFUSE_HOST:-(tracing disabled)}"
echo ""

# ---------------------------------------------------------------------------
# Build image(s)
# ---------------------------------------------------------------------------
build_images

# ---------------------------------------------------------------------------
# Namespace (PSS restricted + Istio ambient) and LiteLLM access
# ---------------------------------------------------------------------------
# TLS is terminated at the platform gateway — no per-app certificate needed.
tm_prepare_namespace "${NAMESPACE}"
tm_check_litellm_access "${NAMESPACE}"

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
  --set "httpRoute.enabled=true"
  --set "httpRoute.host=${INGRESS_HOST}"
  --wait
  --timeout 5m
)

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
tm_print_gateway_hint "${INGRESS_HOST}"
info "  (or locally: kubectl port-forward -n ${NAMESPACE} svc/coding-agent 8090:8090 → http://localhost:8090)"
info ""
info "  DevUI login token (enter in the browser login prompt):"
info "  ${DEVUI_AUTH_TOKEN}"
echo ""
