#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# deploy.sh — One-click deployer for the Customer Support Agent use case
#
# Deploys the Customer Support Agent (Streamlit UI + FastAPI) onto Intel® AI for
# Enterprise Solutions (release-1.2, agent-toolkit layer) using Helm.
#
# Prerequisites:
#   • AI Solutions agent-toolkit is installed (see ../../docs/ai-solutions-setup.md)
#   • kubectl is configured and pointing at your cluster (env/<env>/kubeconfig.yaml)
#   • helm is installed
#
# Usage:
#   ./deploy.sh
#
# Environment variables (all optional — auto-discovered from the cluster or prompted):
#   CLUSTER_URL         AI Solutions base_domain_name, e.g. solutions.ai (from eg-gateway)
#   LLM_API_KEY         LiteLLM key (alias OPENAI_API_KEY; default: litellm-master-key)
#   LLM_MODEL           Chat model ID in LiteLLM (alias MODEL_NAME), e.g. qwen2-5-coder-14b
#   LLM_BASE_URL        Override LiteLLM URL (alias OPENAI_BASE_URL; default: in-cluster)
#   EMBEDDING_MODEL     Embedding model for memory (default: bge-base-en)
#   MEMORY_ENABLED      true|false (default: true; fail-open without embeddings)
#   REDIS_URL           Override Redis URL (default: redis-stack-server-credentials)
#   NAMESPACE           Kubernetes namespace (default: customer-support-agent)
#   INGRESS_HOST        HTTPRoute hostname (default: customer-support-agent.<CLUSTER_URL>)
#   PATCH_LITELLM_AUTHZ true = add NAMESPACE to LiteLLM's AuthorizationPolicy directly
#   SKIP_BUILD          Set to "true" to skip building the image (use a pre-built one)
#   CONTAINER_TOOL      Override image builder: "nerdctl" or "docker" (auto-detected)
#   AGENT_IMAGE         Image ref (default: customer-support-agent:latest)
#
# NOTE: this blueprint has no user authentication — its route is reachable by
# anyone who can resolve the hostname. It ships synthetic demo data only.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/helm-chart"
SRC_DIR="${SCRIPT_DIR}/src"

AGENT_IMAGE="${AGENT_IMAGE:-customer-support-agent:latest}"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# shellcheck source=scripts/ai-solutions-lib.sh
source "${SCRIPT_DIR}/../../scripts/ai-solutions-lib.sh"

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
echo "  Customer Support Agent — Deployment"
echo "============================================================"
echo ""

# Accept the dispatcher's OPENAI_* / MODEL_NAME names as aliases.
LLM_BASE_URL="${LLM_BASE_URL:-${OPENAI_BASE_URL:-}}"
LLM_API_KEY="${LLM_API_KEY:-${OPENAI_API_KEY:-}}"
LLM_MODEL="${LLM_MODEL:-${MODEL_NAME:-}}"

aisol_discover_base_domain
OPENAI_API_KEY="${LLM_API_KEY}"
aisol_discover_litellm_key
LLM_API_KEY="${OPENAI_API_KEY}"

prompt_if_empty CLUSTER_URL \
  "AI Solutions base_domain_name (e.g. solutions.ai)"
[[ -n "${CLUSTER_URL}" ]] || die "CLUSTER_URL (AI Solutions base_domain_name) is required."

prompt_secret_if_empty LLM_API_KEY \
  "LiteLLM API key (LLM_API_KEY)"

prompt_if_empty LLM_MODEL \
  "Chat model name registered in LiteLLM" "qwen2-5-coder-14b"

NAMESPACE="${NAMESPACE:-customer-support-agent}"
INGRESS_HOST="${INGRESS_HOST:-customer-support-agent.${CLUSTER_URL}}"
LLM_BASE_URL="${LLM_BASE_URL:-${AISOL_LITELLM_URL}}"
EMBEDDING_MODEL="${EMBEDDING_MODEL:-bge-base-en}"
MEMORY_ENABLED="${MEMORY_ENABLED:-true}"

# Authenticated Redis URL from the agent-toolkit's credentials secret (the shared Redis
# needs a password). Falls back to a passwordless URL if the secret isn't found.
if [[ -z "${REDIS_URL:-}" ]]; then
  REDIS_URL="$(kubectl get secret -n redis redis-stack-server-credentials \
    -o jsonpath='{.data.REDIS_URL}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  if [[ -n "${REDIS_URL}" ]]; then
    success "Auto-discovered authenticated Redis URL."
  else
    REDIS_URL="redis://redis-stack-server.redis.svc.cluster.local:6379"
    warn "redis-stack-server-credentials not found; using passwordless REDIS_URL (set it, or MEMORY_ENABLED=false)."
  fi
fi

echo ""
info "Deployment configuration:"
echo "  NAMESPACE          = ${NAMESPACE}"
echo "  INGRESS_HOST       = ${INGRESS_HOST}"
echo "  LLM_BASE_URL       = ${LLM_BASE_URL}"
echo "  LLM_MODEL          = ${LLM_MODEL}"
echo "  EMBEDDING_MODEL    = ${EMBEDDING_MODEL} (memory: ${MEMORY_ENABLED})"
echo "  REDIS_URL          = $(sed -E 's#(://[^:]*:)[^@]*(@)#\1****\2#' <<<"${REDIS_URL}")"
echo ""

# ---------------------------------------------------------------------------
# Build image
# ---------------------------------------------------------------------------
build_images

# ---------------------------------------------------------------------------
# Namespace (PSS restricted + Istio ambient) and LiteLLM access
# ---------------------------------------------------------------------------
# TLS is terminated at the AI Solutions gateway — no per-app certificate needed.
aisol_prepare_namespace "${NAMESPACE}"
aisol_check_litellm_access "${NAMESPACE}"

# ---------------------------------------------------------------------------
# Helm deploy
# ---------------------------------------------------------------------------
# Split "repo:tag" into repository + tag so the chart image matches what we built.
AGENT_IMAGE_REPO="${AGENT_IMAGE%:*}"; AGENT_IMAGE_TAG="${AGENT_IMAGE##*:}"
[[ "${AGENT_IMAGE}" == *:* ]] || AGENT_IMAGE_TAG="latest"

HELM_ARGS=(
  upgrade --install customer-support-agent "${CHART_DIR}"
  --namespace "${NAMESPACE}"
  --set "agent.image.repository=${AGENT_IMAGE_REPO}"
  --set "agent.image.tag=${AGENT_IMAGE_TAG}"
  --set "agent.llmBaseUrl=${LLM_BASE_URL}"
  --set-string "agent.llmApiKey=${LLM_API_KEY}"
  --set "agent.llmModel=${LLM_MODEL}"
  --set "agent.memory.enabled=${MEMORY_ENABLED}"
  --set "agent.memory.embeddingModel=${EMBEDDING_MODEL}"
  --set "agent.memory.redisUrl=${REDIS_URL}"
  --set "httpRoute.enabled=true"
  --set "httpRoute.host=${INGRESS_HOST}"
  --wait
  --timeout 5m
)

info "Deploying Customer Support Agent via Helm..."
helm "${HELM_ARGS[@]}"

# ---------------------------------------------------------------------------
# Post-deploy summary
# ---------------------------------------------------------------------------
success "Customer Support Agent deployed — namespace: ${NAMESPACE}"
echo ""
info "Access the Customer Support Agent:"
info "  Streamlit UI: https://${INGRESS_HOST}/"
info "  Chat UI/API:  https://${INGRESS_HOST}/api/v1/health"
aisol_print_gateway_hint "${INGRESS_HOST}"
info "  (or locally: kubectl port-forward -n ${NAMESPACE} svc/customer-support-agent 8501:8501 8502:8502)"
warn "No user authentication: anyone who can reach ${INGRESS_HOST} can use the agent (demo data only)."
echo ""
