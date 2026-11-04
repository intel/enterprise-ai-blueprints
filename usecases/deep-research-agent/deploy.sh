#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# deploy.sh — One-click deployer for the Deep Research Agent use case
#
# Deploys the Deep Research Agent onto Intel® AI for Enterprise Solutions
# (release-1.2, agent-toolkit layer) using Helm. This use case deploys NO
# models: it consumes LiteLLM (inference layer) via URL + API key.
#
# Prerequisites:
#   • AI Solutions agent-toolkit is installed (see ../../docs/ai-solutions-setup.md)
#   • kubectl is configured and pointing at your cluster (env/<env>/kubeconfig.yaml)
#   • helm is installed
#   • A Tavily search API key (https://tavily.com/)
#
# Usage:
#   ./deploy.sh
#
# Environment variables (all optional — auto-discovered from the cluster or prompted):
#   CLUSTER_URL         AI Solutions base_domain_name, e.g. solutions.ai (from eg-gateway)
#   LLM_API_KEY         LiteLLM key (alias OPENAI_API_KEY; default: litellm-master-key)
#   LLM_MODEL           Model ID in LiteLLM (alias MODEL_NAME) — MUST support tool calling
#   LLM_BASE_URL        Override LiteLLM URL (alias OPENAI_BASE_URL; default: in-cluster)
#   TAVILY_API_KEY      Tavily search key (REQUIRED)
#   SERPER_API_KEY      Optional second search provider (https://serper.dev/)
#   SECONDARY_LLM_*     Optional secondary model slot for lightweight modules
#   API_KEYS            Optional X-API-Key list for /api/* (breaks the bundled web UI)
#   EVAL_DASHBOARD_ENABLED  "true" to also serve the metrics dashboard on its own host
#   NAMESPACE           Kubernetes namespace (default: deep-research-agent)
#   INGRESS_HOST        HTTPRoute hostname (default: deep-research-agent.<CLUSTER_URL>)
#   DASHBOARD_HOST      Dashboard hostname (default: deep-research-agent-dashboard.<CLUSTER_URL>)
#   PATCH_LITELLM_AUTHZ true = add NAMESPACE to LiteLLM's AuthorizationPolicy directly
#   SKIP_BUILD          Set to "true" to skip building the image (use a pre-built one)
#   CONTAINER_TOOL      Override image builder: "nerdctl" or "docker" (auto-detected)
#   AGENT_IMAGE         Image ref (default: deep-research-agent:latest)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/helm-chart"
SRC_DIR="${SCRIPT_DIR}/src"

AGENT_IMAGE="${AGENT_IMAGE:-deep-research-agent:latest}"

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
echo "  Deep Research Agent — Deployment"
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
  "LLM model name (MUST support tool calling; registered in LiteLLM)" "qwen2-5-coder-14b"

prompt_secret_if_empty TAVILY_API_KEY \
  "Tavily search API key (required — https://tavily.com/)"

# Search is not optional for this agent: with no provider it can only answer
# from parametric knowledge and every research query fails to produce a report.
[[ -n "${TAVILY_API_KEY:-}" ]] || die "TAVILY_API_KEY is required (https://tavily.com/)."

NAMESPACE="${NAMESPACE:-deep-research-agent}"
INGRESS_HOST="${INGRESS_HOST:-deep-research-agent.${CLUSTER_URL}}"
DASHBOARD_HOST="${DASHBOARD_HOST:-deep-research-agent-dashboard.${CLUSTER_URL}}"
LLM_BASE_URL="${LLM_BASE_URL:-${AISOL_LITELLM_URL}}"
EVAL_DASHBOARD_ENABLED="${EVAL_DASHBOARD_ENABLED:-false}"

echo ""
info "Deployment configuration:"
echo "  NAMESPACE          = ${NAMESPACE}"
echo "  INGRESS_HOST       = ${INGRESS_HOST}"
[[ "${EVAL_DASHBOARD_ENABLED}" == "true" ]] && echo "  DASHBOARD_HOST     = ${DASHBOARD_HOST}"
echo "  LLM_BASE_URL       = ${LLM_BASE_URL}"
echo "  LLM_MODEL          = ${LLM_MODEL}"
echo "  SECONDARY_LLM      = ${SECONDARY_LLM_MODEL:-(none)}"
echo "  SEARCH             = tavily$([[ -n "${SERPER_API_KEY:-}" ]] && echo ' + serper')"
echo "  API_KEYS           = $([[ -n "${API_KEYS:-}" ]] && echo '****(set — web UI will get 401)' || echo '(open)')"
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
  upgrade --install deep-research-agent "${CHART_DIR}"
  --namespace "${NAMESPACE}"
  --set "agent.image.repository=${AGENT_IMAGE_REPO}"
  --set "agent.image.tag=${AGENT_IMAGE_TAG}"
  --set "agent.llm.baseUrl=${LLM_BASE_URL}"
  --set-string "agent.llm.apiKey=${LLM_API_KEY}"
  --set "agent.llm.model=${LLM_MODEL}"
  --set "agent.secondaryLlm.baseUrl=${SECONDARY_LLM_BASE_URL:-}"
  --set-string "agent.secondaryLlm.apiKey=${SECONDARY_LLM_API_KEY:-}"
  --set "agent.secondaryLlm.model=${SECONDARY_LLM_MODEL:-}"
  --set-string "agent.secondaryLlm.modules=${SECONDARY_LLM_MODULES:-}"
  --set-string "agent.search.tavilyApiKey=${TAVILY_API_KEY}"
  --set-string "agent.search.serperApiKey=${SERPER_API_KEY:-}"
  --set-string "agent.apiKeys=${API_KEYS:-}"
  --set "agent.evalDashboard.enabled=${EVAL_DASHBOARD_ENABLED}"
  --set "httpRoute.enabled=true"
  --set "httpRoute.host=${INGRESS_HOST}"
  --set "httpRoute.dashboardHost=${DASHBOARD_HOST}"
  # Longer than other use cases: the image is large and the pod loads an
  # embedding model before it reports ready.
  --wait
  --timeout 10m
)

info "Deploying Deep Research Agent via Helm..."
helm "${HELM_ARGS[@]}"

# ---------------------------------------------------------------------------
# Post-deploy summary
# ---------------------------------------------------------------------------
success "Deep Research Agent deployed — namespace: ${NAMESPACE}"
echo ""
info "Access the Deep Research Agent:"
info "  https://${INGRESS_HOST}   (health: /health)"
[[ "${EVAL_DASHBOARD_ENABLED}" == "true" ]] && info "  https://${DASHBOARD_HOST}   (eval metrics dashboard)"
aisol_print_gateway_hint "${INGRESS_HOST}"
info "  (or locally: kubectl port-forward -n ${NAMESPACE} svc/deep-research-agent 8081:8081 → http://localhost:8081)"
echo ""
