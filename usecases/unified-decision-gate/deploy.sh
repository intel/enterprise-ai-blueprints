#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# deploy.sh — One-click deployer for the Unified Decision Gate use case
#
# Deploys the gate (System-1 front door) + the banking agent + its MCP sidecar,
# as one pod, onto Intel® Agent Toolkit (installed by Toolkit Manager release-1.2)
# using Helm. System-1 is a model on the platform, not part of this chart.
#
# Prerequisites:
#   • Intel Agent Toolkit is installed with Toolkit Manager (see ../../docs/toolkit-manager-setup.md)
#   • System-1 is deployed by Model Manager as qwen3-0-6b-system1 (README.md, Deploy System-1)
#   • kubectl is configured and pointing at your cluster (env/<env>/kubeconfig.yaml)
#   • helm is installed
#   • The gate, agent and MCP container images are built here (nerdctl) or pushed to
#     a registry reachable by the cluster.
#
# Usage:
#   ./deploy.sh
#
# Environment variables (all optional — auto-discovered from the cluster or prompted):
#   CLUSTER_URL         Toolkit Manager base_domain_name, e.g. solutions.ai (from eg-gateway)
#   OPENAI_API_KEY      LiteLLM key (default: litellm-master-key; prefer a virtual key)
#   MODEL_NAME          The agent's LLM in LiteLLM (must support tool calling)
#   OPENAI_BASE_URL     Override LiteLLM URL (default: in-cluster litellm.litellm:4000)
#   SYSTEM1_URL         System-1's base URL without /v1 (default: its route on the AI gateway)
#   SYSTEM1_MODEL       The name System-1 is served under (default: qwen3-0-6b-system1)
#   GATE_SHADOW_MODE    true (default) = log System-1's decisions, every answer from the agent;
#                       false = show System-1's "Yes."/"No." when it is sure
#   SYSTEM1_YES_MIN / SYSTEM1_NO_MAX   display cut-offs on P(yes) (default: the shipped fit)
#   GATE_DEBUG_HEADERS  true = X-Gate-* routing headers on every response (debugging)
#   GATE_ENABLED        false = System-1 is never asked: the gate sends every question to the agent
#                       (same chat API)
#   AGENT_ONLY          true = deploy the agent alone, without the gate container: the Service then
#                       serves DevUI's own API (/v1/responses), with NO /v1/chat/completions
#   REDIS_URL           Override Redis URL (default: redis-stack-server-credentials)
#   LANGFUSE_*          Langfuse keys/host (default: langfuse/langfuse-secrets)
#   NAMESPACE           Kubernetes namespace (default: unified-decision-gate)
#   INGRESS_HOST        HTTPRoute hostname (default: unified-decision-gate.<CLUSTER_URL>)
#   PATCH_LITELLM_AUTHZ true = add NAMESPACE to LiteLLM's AuthorizationPolicy directly
#   DEVUI_AUTH_TOKEN    Client bearer token, enforced by the gate (kept from an existing release,
#                       else generated with openssl)
#   SKIP_BUILD          Set to "true" to skip building images (use pre-built ones)
#   CONTAINER_TOOL      Override image builder: "nerdctl" or "docker" (auto-detected)
#   GATE_IMAGE / AGENT_IMAGE / MCP_IMAGE   image refs (default: unified-decision-gate-{gate,agent,mcp}:latest)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/helm-chart"
SRC_DIR="${SCRIPT_DIR}/src"

GATE_IMAGE="${GATE_IMAGE:-unified-decision-gate-gate:latest}"
AGENT_IMAGE="${AGENT_IMAGE:-unified-decision-gate-agent:latest}"
MCP_IMAGE="${MCP_IMAGE:-unified-decision-gate-mcp:latest}"
GATE_ENABLED="${GATE_ENABLED:-true}"
AGENT_ONLY="${AGENT_ONLY:-false}"
# With the gate container gone, System-1 is not used at all.
[[ "${AGENT_ONLY}" == "true" ]] && GATE_ENABLED=false

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
# Build the images (gate + agent + MCP sidecar) into containerd's k8s.io
# namespace (nerdctl) so the kubelet uses them with pullPolicy: IfNotPresent.
# Skip with SKIP_BUILD=true + GATE_IMAGE/AGENT_IMAGE/MCP_IMAGE.
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
      warn "GATE_IMAGE/AGENT_IMAGE/MCP_IMAGE + SKIP_BUILD=true if pods stay in ImagePullBackOff."
    else
      die "No container builder found. Install nerdctl (or docker), or set SKIP_BUILD=true."
    fi
  fi

  local build_cmd=("${tool}")
  local ns_args=()
  if [[ "${tool}" == "nerdctl" ]]; then
    ns_args=(--namespace k8s.io)
    if [[ "$(id -u)" -ne 0 ]]; then
      build_cmd=(sudo nerdctl)
    fi
    # Install/start buildkitd + pre-stage base images if needed.
    CONTAINER_TOOL="${tool}" "${SCRIPT_DIR}/../../scripts/ensure-image-builder.sh" \
      "${SRC_DIR}/Dockerfile" "${SRC_DIR}/Dockerfile.mcp" "${SRC_DIR}/Dockerfile.gate"
  fi

  # Proxy build args (inherited from the environment; empty when unset).
  local proxy_args=(
    --build-arg "http_proxy=${http_proxy:-}"
    --build-arg "https_proxy=${https_proxy:-}"
    --build-arg "no_proxy=${no_proxy:-}"
    --build-arg "HTTP_PROXY=${HTTP_PROXY:-${http_proxy:-}}"
    --build-arg "HTTPS_PROXY=${HTTPS_PROXY:-${https_proxy:-}}"
    --build-arg "NO_PROXY=${NO_PROXY:-${no_proxy:-}}"
  )

  info "Building MCP server image '${MCP_IMAGE}' with ${build_cmd[*]}..."
  "${build_cmd[@]}" "${ns_args[@]}" build "${proxy_args[@]}" \
    -t "${MCP_IMAGE}" -f "${SRC_DIR}/Dockerfile.mcp" "${SRC_DIR}"

  info "Building agent image '${AGENT_IMAGE}' with ${build_cmd[*]}..."
  "${build_cmd[@]}" "${ns_args[@]}" build "${proxy_args[@]}" \
    -t "${AGENT_IMAGE}" -f "${SRC_DIR}/Dockerfile" "${SRC_DIR}"

  if [[ "${AGENT_ONLY}" != "true" ]]; then
    info "Building gate image '${GATE_IMAGE}' with ${build_cmd[*]}..."
    "${build_cmd[@]}" "${ns_args[@]}" build "${proxy_args[@]}" \
      -t "${GATE_IMAGE}" -f "${SRC_DIR}/Dockerfile.gate" "${SRC_DIR}"
  fi

  success "Built images: ${AGENT_IMAGE}, ${MCP_IMAGE}$([[ "${AGENT_ONLY}" != "true" ]] && echo ", ${GATE_IMAGE}")"
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

# Split "repo:tag" into repository + tag (default tag: latest).
image_repo() { if [[ "$1" == *:* ]]; then echo "${1%:*}"; else echo "$1"; fi; }
image_tag()  { if [[ "$1" == *:* ]]; then echo "${1##*:}"; else echo "latest"; fi; }

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Unified Decision Gate — Deployment"
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
  "The agent's LLM, as registered in LiteLLM (must support tool calling)"

NAMESPACE="${NAMESPACE:-unified-decision-gate}"
INGRESS_HOST="${INGRESS_HOST:-unified-decision-gate.${CLUSTER_URL}}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-${TM_LITELLM_URL}}"
SYSTEM1_MODEL="${SYSTEM1_MODEL:-qwen3-0-6b-system1}"
SYSTEM1_URL="${SYSTEM1_URL:-http://ai-gateway.envoy-gateway-system.svc.cluster.local/llm-inference/${SYSTEM1_MODEL}}"
GATE_SHADOW_MODE="${GATE_SHADOW_MODE:-true}"
GATE_DEBUG_HEADERS="${GATE_DEBUG_HEADERS:-false}"
# The client token is the route's only auth (it opts out of the gateway's LiteLLM-key check), so
# a redeploy keeps the release's token -- clients keep working -- and a new one is never guessable.
if [[ -z "${DEVUI_AUTH_TOKEN:-}" ]]; then
  DEVUI_AUTH_TOKEN="$(tm_secret_value "${NAMESPACE}" unified-decision-gate-secret DEVUI_AUTH_TOKEN)"
  if [[ -n "${DEVUI_AUTH_TOKEN}" ]]; then
    success "Reusing the client token of the existing release (secret unified-decision-gate-secret)."
  else
    command -v openssl &>/dev/null \
      || die "openssl not found: install it, or set DEVUI_AUTH_TOKEN to a long random value."
    DEVUI_AUTH_TOKEN="$(openssl rand -hex 24)"
  fi
fi

# Authenticated Redis URL from the agent-toolkit's credentials secret (the shared Redis
# needs a password). Falls back to a passwordless URL if the secret isn't found.
if [[ -z "${REDIS_URL:-}" ]]; then
  REDIS_URL="$(tm_secret_value redis redis-stack-server-credentials REDIS_URL)"
  if [[ -n "${REDIS_URL}" ]]; then
    success "Auto-discovered authenticated Redis URL."
  else
    REDIS_URL="redis://redis-stack-server.redis.svc.cluster.local:6379"
    warn "redis-stack-server-credentials not found; using passwordless REDIS_URL (set it, or ENABLE_MEMORY=false)."
  fi
fi

[[ "${GATE_ENABLED}" == "true" ]] && tm_check_model "${SYSTEM1_MODEL}"

echo ""
info "Deployment configuration:"
echo "  NAMESPACE          = ${NAMESPACE}"
echo "  INGRESS_HOST       = ${INGRESS_HOST}"
echo "  OPENAI_BASE_URL    = ${OPENAI_BASE_URL}"
echo "  MODEL_NAME         = ${MODEL_NAME}"
echo "  AGENT_ONLY         = ${AGENT_ONLY}"
echo "  GATE_ENABLED       = ${GATE_ENABLED}  (System-1 asked)"
if [[ "${GATE_ENABLED}" == "true" ]]; then
  echo "  SYSTEM1_URL        = ${SYSTEM1_URL}"
  echo "  SYSTEM1_MODEL      = ${SYSTEM1_MODEL}"
  echo "  GATE_SHADOW_MODE   = ${GATE_SHADOW_MODE}"
fi
echo "  REDIS_URL          = $(sed -E 's#(://[^:]*:)[^@]*(@)#\1****\2#' <<<"${REDIS_URL}")"
echo "  LANGFUSE_HOST      = ${LANGFUSE_HOST:-(tracing disabled)}"
echo ""

# ---------------------------------------------------------------------------
# Build images
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
HELM_ARGS=(
  upgrade --install unified-decision-gate "${CHART_DIR}"
  --namespace "${NAMESPACE}"
  --set "agent.image.repository=$(image_repo "${AGENT_IMAGE}")"
  --set "agent.image.tag=$(image_tag "${AGENT_IMAGE}")"
  --set "mcp.image.repository=$(image_repo "${MCP_IMAGE}")"
  --set "mcp.image.tag=$(image_tag "${MCP_IMAGE}")"
  --set "gate.enabled=$([[ "${AGENT_ONLY}" == "true" ]] && echo false || echo true)"
  --set "gate.system1Enabled=${GATE_ENABLED}"
  --set "gate.image.repository=$(image_repo "${GATE_IMAGE}")"
  --set "gate.image.tag=$(image_tag "${GATE_IMAGE}")"
  --set-string "gate.system1Url=${SYSTEM1_URL}"
  --set "gate.system1Model=${SYSTEM1_MODEL}"
  --set "gate.shadowMode=${GATE_SHADOW_MODE}"
  --set "gate.debugHeaders=${GATE_DEBUG_HEADERS}"
  --set-string "agent.openaiBaseUrl=${OPENAI_BASE_URL}"
  --set-string "agent.openaiApiKey=${OPENAI_API_KEY}"
  --set "agent.modelName=${MODEL_NAME}"
  --set-string "agent.devuiAuthToken=${DEVUI_AUTH_TOKEN}"
  --set-string "redisUrl=${REDIS_URL}"
  --set "httpRoute.enabled=true"
  --set "httpRoute.host=${INGRESS_HOST}"
  --wait
  --timeout 5m
)
[[ -n "${SYSTEM1_YES_MIN:-}" ]] && HELM_ARGS+=(--set-string "gate.yesMin=${SYSTEM1_YES_MIN}")
[[ -n "${SYSTEM1_NO_MAX:-}" ]] && HELM_ARGS+=(--set-string "gate.noMax=${SYSTEM1_NO_MAX}")

# Langfuse tracing (optional) — passed only when keys are provided.
if [[ -n "${LANGFUSE_PUBLIC_KEY:-}" && -n "${LANGFUSE_SECRET_KEY:-}" ]]; then
  HELM_ARGS+=(
    --set-string "agent.langfusePublicKey=${LANGFUSE_PUBLIC_KEY}"
    --set-string "agent.langfuseSecretKey=${LANGFUSE_SECRET_KEY}"
    --set "agent.langfuseHost=${LANGFUSE_HOST:-}"
    --set "agent.langfuseEnableSensitiveData=${LANGFUSE_ENABLE_SENSITIVE_DATA:-true}"
  )
  info "Langfuse tracing enabled (host: ${LANGFUSE_HOST:-<default>})."
fi

info "Deploying the Unified Decision Gate via Helm..."
helm "${HELM_ARGS[@]}"

# Freshly built images keep the same tag (e.g. :latest), so the Deployment spec does not change
# and Kubernetes would keep the old containers: roll the pod so it starts from the new images.
if [[ "${SKIP_BUILD:-false}" != "true" ]]; then
  info "Restarting the pod onto the freshly built images..."
  kubectl rollout restart deployment/unified-decision-gate -n "${NAMESPACE}" >/dev/null
  kubectl rollout status deployment/unified-decision-gate -n "${NAMESPACE}" --timeout=5m
fi

# ---------------------------------------------------------------------------
# Post-deploy summary
# ---------------------------------------------------------------------------
success "Unified Decision Gate deployed — namespace: ${NAMESPACE}"
echo ""
info "Access it (OpenAI chat completions at /v1/chat/completions, DevUI at /):"
info "  https://${INGRESS_HOST}"
tm_print_gateway_hint "${INGRESS_HOST}"
info "  (or locally: kubectl port-forward -n ${NAMESPACE} svc/unified-decision-gate 8090:8090 → http://localhost:8090)"
info ""
info "  Client / DevUI token (Authorization: Bearer <token>):"
info "  ${DEVUI_AUTH_TOKEN}"
if [[ "${GATE_ENABLED}" == "true" && "${GATE_SHADOW_MODE}" == "true" ]]; then
  info ""
  info "  Shadow mode: System-1's decisions are logged, every answer comes from the agent."
  info "  Set GATE_SHADOW_MODE=false to show System-1's answers."
fi
echo ""
