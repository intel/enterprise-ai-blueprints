#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# deploy-usecase.sh — One-click deployer for Agentic AI Stack use cases
#
# A thin dispatcher: it collects shared configuration once, then delegates to
# each use case's own deploy.sh (Kubernetes) or docker-compose.yml (Docker).
# Any directory under usecases/ with a deploy.sh is deployable — no per-use-case
# code lives here, so adding a use case needs no edit to this script.
#
# Usage:
#   ./deploy-usecase.sh <usecase-name> [--mode docker|kubernetes]
#   ./deploy-usecase.sh --all [--mode docker|kubernetes]
#
# Examples:
#   ./deploy-usecase.sh coding-agent-maf --mode kubernetes
#   ./deploy-usecase.sh banking-assistant --mode docker
#   ./deploy-usecase.sh --all --mode kubernetes
#
# Environment variables (all optional — the use case's deploy.sh prompts if unset):
#   CLUSTER_URL       Base domain of your Agentic AI Stack, e.g. api.example.com
#   OPENAI_API_KEY    API key accepted by LiteLLM / GenAI Gateway
#   OPENAI_BASE_URL   LLM endpoint URL (auto-discovered in K8s mode if unset)
#   MODEL_NAME        Model ID registered in LiteLLM
#   REDIS_URL         Redis endpoint (auto-discovered in K8s mode)
#   NAMESPACE         Kubernetes namespace to deploy into (default: <usecase-name>)
#   INGRESS_HOST      Ingress hostname (default: <usecase-name>.<CLUSTER_URL>)
#   CERT_FILE/KEY_FILE  TLS cert + key PEM paths for ingress (optional)
#   KUBECONFIG        Path to kubeconfig (default: ~/.kube/config)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USECASES_DIR="${SCRIPT_DIR}/usecases"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MODE="kubernetes"
DEPLOY_ALL=false
USECASE=""

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF

Usage: $0 <usecase-name|--all> [OPTIONS]

Deploy one or all use cases to Docker or Kubernetes.

Options:
  --mode <docker|kubernetes>   Deployment mode (default: kubernetes)
  --all                        Deploy ALL use cases at once
  --namespace <ns>             Kubernetes namespace (default: <usecase-name>)
  --help, -h                   Show this help

Available use cases:
EOF
  for d in "${USECASES_DIR}"/*/; do
    [[ -d "$d" ]] || continue
    [[ -f "$d/deploy.sh" ]] && echo "  - $(basename "$d")"
  done
  cat <<EOF

Examples:
  $0 coding-agent-maf --mode kubernetes
  $0 banking-assistant --mode docker
  $0 --all --mode kubernetes

EOF
  exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
POSITIONAL_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)      MODE="$2"; shift 2 ;;
    --all)       DEPLOY_ALL=true; shift ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --help|-h)   usage ;;
    -*)          die "Unknown option: $1. Use --help for usage." ;;
    *)           POSITIONAL_ARGS+=("$1"); shift ;;
  esac
done

if [[ "$DEPLOY_ALL" == false ]]; then
  [[ ${#POSITIONAL_ARGS[@]} -ge 1 ]] || { error "No use case specified."; usage; }
  USECASE="${POSITIONAL_ARGS[0]}"
fi

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
check_deps() {
  local missing=()
  case "$MODE" in
    docker)
      command -v docker &>/dev/null || missing+=(docker)
      [[ ${#missing[@]} -eq 0 ]] || die "Missing required tools for Docker mode: ${missing[*]}."
      docker compose version &>/dev/null || die "Docker Compose (v2) is required. Install docker-compose-plugin."
      ;;
    kubernetes|k8s)
      for cmd in kubectl helm; do command -v "$cmd" &>/dev/null || missing+=("$cmd"); done
      [[ ${#missing[@]} -eq 0 ]] || die "Missing required tools for Kubernetes mode: ${missing[*]}."
      ;;
    *) die "Invalid mode '${MODE}'. Use 'docker' or 'kubernetes'." ;;
  esac
}

# ---------------------------------------------------------------------------
# Auto-discover cluster services (Kubernetes mode). Best-effort: fills
# OPENAI_BASE_URL / REDIS_URL from the cluster so the use case scripts don't
# have to prompt for them. Silently no-ops when kubectl can't reach a cluster.
# ---------------------------------------------------------------------------
auto_discover_services() {
  command -v kubectl &>/dev/null || return 0
  kubectl cluster-info &>/dev/null 2>&1 || { warn "Cannot reach cluster — skipping auto-discovery."; return 0; }
  info "Auto-discovering cluster services..."

  discover_svc() { # <grep-pattern> -> "http://name.ns.svc.cluster.local:port"
    local match
    match=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.namespace}{" "}{.spec.ports[0].port}{"\n"}{end}' 2>/dev/null \
      | grep -iE "$1" | head -1) || true
    [[ -n "$match" ]] || return 1
    awk '{printf "%s.%s.svc.cluster.local:%s", $1, $2, $3}' <<<"$match"
  }

  if [[ -z "${OPENAI_BASE_URL:-}" ]]; then
    # The LiteLLM / GenAI Gateway serves the OpenAI API on port 4000. Match the
    # gateway service by that port — matching the name alone would wrongly pick a
    # co-located service (e.g. genai-gateway-postgresql:5432 sorts first).
    local match
    match=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.namespace}{" "}{range .spec.ports[*]}{.port}{" "}{end}{"|"}{"\n"}{end}' 2>/dev/null \
      | grep -iE 'litellm|genai-gateway' | grep -E ' 4000( |\|)' | head -1) || true
    if [[ -n "$match" ]]; then
      OPENAI_BASE_URL="http://$(awk '{printf "%s.%s.svc.cluster.local", $1, $2}' <<<"$match"):4000/v1"
      success "Auto-discovered LLM: $OPENAI_BASE_URL"
    fi
  fi
  if [[ -z "${REDIS_URL:-}" ]]; then
    # Prefer the ready-made authenticated URL from the credentials secret — the
    # shared Redis requires a password, so a bare service URL would fail at runtime.
    REDIS_URL="$(kubectl get secret -n redis redis-stack-server-credentials \
      -o jsonpath='{.data.REDIS_URL}' 2>/dev/null | base64 -d 2>/dev/null || true)"
    if [[ -n "${REDIS_URL}" ]]; then
      success "Auto-discovered authenticated Redis URL."
    else
      local svc; svc=$(discover_svc "^redis") && {
        REDIS_URL="redis://${svc}"
        warn "Discovered Redis without credentials: $REDIS_URL — set REDIS_URL if it needs a password."; }
    fi
  fi
}

# ---------------------------------------------------------------------------
# Deploy a single use case by delegating to its own scripts.
#   kubernetes -> usecases/<name>/deploy.sh   (falls back to generic helm install)
#   docker     -> docker compose up in usecases/<name>/
# Shared config is exported so the delegated script skips its own prompts.
# ---------------------------------------------------------------------------
deploy_single() {
  local usecase="$1"
  local usecase_dir="${USECASES_DIR}/${usecase}"
  [[ -d "${usecase_dir}" ]] || die "Use case '${usecase}' not found in ${USECASES_DIR}."

  export CLUSTER_URL OPENAI_API_KEY OPENAI_BASE_URL MODEL_NAME REDIS_URL \
         NAMESPACE INGRESS_HOST CERT_FILE KEY_FILE TLS_SECRET_NAME 2>/dev/null || true

  case "$MODE" in
    docker)
      [[ -f "${usecase_dir}/docker-compose.yml" ]] || die "No docker-compose.yml for '${usecase}'."
      info "Deploying ${usecase} with Docker Compose..."
      # The compose files read LITELLM_URL/LITELLM_API_KEY (with sensible defaults),
      # so bridge the shared OPENAI_* config to them.
      LITELLM_URL="${OPENAI_BASE_URL:-}" LITELLM_API_KEY="${OPENAI_API_KEY:-}" MODEL_NAME="${MODEL_NAME:-}" \
        docker compose -f "${usecase_dir}/docker-compose.yml" --project-directory "${usecase_dir}" up --build -d
      success "${usecase} deployed with Docker Compose."
      ;;
    kubernetes|k8s)
      if [[ -x "${usecase_dir}/deploy.sh" ]]; then
        info "Delegating to ${usecase}/deploy.sh..."
        "${usecase_dir}/deploy.sh"
      elif [[ -d "${usecase_dir}/helm-chart" ]]; then
        local ns="${NAMESPACE:-${usecase}}"
        kubectl create namespace "${ns}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
        helm upgrade --install "${usecase}" "${usecase_dir}/helm-chart" \
          --namespace "${ns}" --create-namespace --wait --timeout 5m
        success "${usecase} deployed to namespace: ${ns}"
      else
        die "No deploy.sh or helm-chart/ found for '${usecase}'."
      fi
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Deploy all use cases
# ---------------------------------------------------------------------------
deploy_all() {
  info "Deploying ALL use cases (mode: ${MODE})..."
  local usecases=()
  for d in "${USECASES_DIR}"/*/; do
    [[ -d "$d" && -f "$d/deploy.sh" ]] && usecases+=("$(basename "$d")")
  done
  [[ ${#usecases[@]} -gt 0 ]] || die "No deployable use cases found in ${USECASES_DIR}."

  info "Use cases to deploy: ${usecases[*]}"
  local failed=()
  for uc in "${usecases[@]}"; do
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    info "Deploying: ${uc}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    NAMESPACE="${uc}"
    if deploy_single "${uc}"; then success "${uc} ✓"; else error "${uc} failed!"; failed+=("${uc}"); fi
  done

  echo ""
  echo "============================================================"
  if [[ ${#failed[@]} -eq 0 ]]; then
    success "All ${#usecases[@]} use cases deployed successfully!"
  else
    warn "Deployed ${#usecases[@]} use cases. Failures: ${failed[*]}"
  fi
  echo "============================================================"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  echo ""
  echo "============================================================"
  echo "  Agentic AI Stack — Use Case Deployer"
  echo "  Mode: ${MODE}"
  [[ "$DEPLOY_ALL" == true ]] && echo "  Target: ALL use cases" || echo "  Target: ${USECASE}"
  echo "============================================================"

  check_deps
  [[ "$MODE" == "kubernetes" || "$MODE" == "k8s" ]] && auto_discover_services

  if [[ "$DEPLOY_ALL" == true ]]; then
    deploy_all
  else
    NAMESPACE="${NAMESPACE:-${USECASE}}"
    deploy_single "$USECASE"
  fi
}

main "$@"
