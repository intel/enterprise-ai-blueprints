#!/usr/bin/env bash
# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# toolkit-manager-lib.sh — shared helpers for deploying blueprints onto
# Intel® Agent Toolkit, installed by Toolkit Manager (release-1.2).
#
# Sourced by usecases/*/deploy.sh (expects info/success/warn helpers defined).
# Every lookup is best-effort: it only fills variables that are still empty and
# silently no-ops when the resource (or the cluster) is not reachable.
#
# Platform contract (see docs/endpoints-reference.md):
#   LiteLLM   litellm.litellm.svc:4000         Secret litellm/litellm-master-key   (master_key)
#   Langfuse  langfuse-web.langfuse.svc:3000   Secret langfuse/langfuse-secrets    (public_key, secret_key)
#   Gateway   eg-gateway (envoy-gateway-system), listener https = *.<base_domain_name>
#   Mesh      Istio ambient + STRICT mTLS; LiteLLM AuthorizationPolicy litellm/litellm-ingress
# =============================================================================

# shellcheck disable=SC2034  # used by the sourcing deploy.sh scripts
TM_LITELLM_URL="http://litellm.litellm.svc.cluster.local:4000/v1"
TM_LANGFUSE_URL="http://langfuse-web.langfuse.svc.cluster.local:3000"
TM_GATEWAY_NAME="eg-gateway"
TM_GATEWAY_NAMESPACE="envoy-gateway-system"

# Read and base64-decode one key of a secret: <namespace> <secret> <key>
tm_secret_value() {
  kubectl get secret -n "$1" "$2" -o jsonpath="{.data.$3}" 2>/dev/null | base64 -d 2>/dev/null || true
}

# CLUSTER_URL <- base_domain_name, read from the eg-gateway listeners.
tm_discover_base_domain() {
  [[ -z "${CLUSTER_URL:-}" ]] || return 0
  local host
  host="$(kubectl get gateway "${TM_GATEWAY_NAME}" -n "${TM_GATEWAY_NAMESPACE}" \
    -o jsonpath='{.spec.listeners[?(@.name=="https-apex")].hostname}' 2>/dev/null || true)"
  if [[ -z "${host}" ]]; then
    host="$(kubectl get gateway "${TM_GATEWAY_NAME}" -n "${TM_GATEWAY_NAMESPACE}" \
      -o jsonpath='{.spec.listeners[?(@.name=="https")].hostname}' 2>/dev/null || true)"
    host="${host#\*.}"
  fi
  if [[ -n "${host}" ]]; then
    CLUSTER_URL="${host}"
    success "Auto-discovered base domain from ${TM_GATEWAY_NAME}: ${CLUSTER_URL}"
  fi
}

# OPENAI_API_KEY <- LiteLLM master key (a virtual key is preferable).
tm_discover_litellm_key() {
  [[ -z "${OPENAI_API_KEY:-}" ]] || return 0
  OPENAI_API_KEY="$(tm_secret_value litellm litellm-master-key master_key)"
  if [[ -n "${OPENAI_API_KEY}" ]]; then
    success "Auto-discovered LiteLLM master key (litellm/litellm-master-key)."
    warn "Using the LiteLLM master key. For production, create a virtual key"
    warn "(POST https://litellm.<base_domain>/key/generate) and set OPENAI_API_KEY."
  fi
}

# LANGFUSE_* <- the project keys the Inference Toolkit seeds in langfuse/langfuse-secrets.
tm_discover_langfuse() {
  if [[ -z "${LANGFUSE_PUBLIC_KEY:-}" && -z "${LANGFUSE_SECRET_KEY:-}" ]]; then
    LANGFUSE_PUBLIC_KEY="$(tm_secret_value langfuse langfuse-secrets public_key)"
    LANGFUSE_SECRET_KEY="$(tm_secret_value langfuse langfuse-secrets secret_key)"
    [[ -n "${LANGFUSE_PUBLIC_KEY}" && -n "${LANGFUSE_SECRET_KEY}" ]] \
      && success "Auto-discovered Langfuse project keys (langfuse/langfuse-secrets)."
  fi
  if [[ -n "${LANGFUSE_PUBLIC_KEY:-}" && -n "${LANGFUSE_SECRET_KEY:-}" ]]; then
    LANGFUSE_HOST="${LANGFUSE_HOST:-${TM_LANGFUSE_URL}}"
  fi
  return 0
}

# Create the namespace with the labels the platform expects on app namespaces:
# Pod Security 'restricted' and (when Istio is installed) ambient mesh enrolment,
# which STRICT mTLS requires to reach LiteLLM / Redis.
tm_prepare_namespace() {
  local ns="$1"
  info "Ensuring namespace '${ns}' exists..."
  kubectl create namespace "${ns}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace "${ns}" --overwrite \
    pod-security.kubernetes.io/enforce=restricted \
    pod-security.kubernetes.io/audit=restricted \
    pod-security.kubernetes.io/warn=restricted >/dev/null
  if kubectl get crd authorizationpolicies.security.istio.io &>/dev/null; then
    kubectl label namespace "${ns}" --overwrite istio.io/dataplane-mode=ambient >/dev/null
    success "Namespace '${ns}' labelled for Istio ambient + PSS restricted."
  else
    info "Istio not detected — skipping ambient mesh label."
  fi
}

# LiteLLM only admits mesh callers from litellm_allowed_namespaces
# (AuthorizationPolicy litellm/litellm-ingress). Warn when <ns> is missing, or
# patch it in when PATCH_LITELLM_AUTHZ=true (stop-gap: the next
# `es_auto_installer.sh install` re-renders the policy from config).
tm_check_litellm_access() {
  local ns="$1" allowed
  kubectl get authorizationpolicy litellm-ingress -n litellm &>/dev/null || return 0
  allowed="$(kubectl get authorizationpolicy litellm-ingress -n litellm \
    -o jsonpath='{.spec.rules[*].from[*].source.namespaces[*]}' 2>/dev/null || true)"
  if [[ " ${allowed} " == *" ${ns} "* ]]; then
    success "Namespace '${ns}' is allowed to call LiteLLM."
    return 0
  fi
  if [[ "${PATCH_LITELLM_AUTHZ:-false}" == "true" ]]; then
    # The policy Toolkit Manager renders has a single rule/source with a namespaces list.
    if kubectl patch authorizationpolicy litellm-ingress -n litellm --type=json \
      -p "[{\"op\":\"add\",\"path\":\"/spec/rules/0/from/0/source/namespaces/-\",\"value\":\"${ns}\"}]" >/dev/null; then
      success "Added '${ns}' to AuthorizationPolicy litellm/litellm-ingress (PATCH_LITELLM_AUTHZ=true)."
      warn "This is overwritten by the next Toolkit Manager install — also add it to litellm_allowed_namespaces."
      return 0
    fi
    warn "Could not patch litellm/litellm-ingress (unexpected policy shape) — use the fix below."
  fi
  warn "Namespace '${ns}' is NOT in LiteLLM's allowed namespaces — LLM calls will be rejected."
  warn "Fix (in your enterprise-ai-solutions checkout):"
  warn "  1. add '- ${ns}' to litellm_allowed_namespaces in env/<env>/config.agent-toolkit.yaml"
  warn "  2. ./es_auto_installer.sh install agent-toolkit"
  warn "Or re-run with PATCH_LITELLM_AUTHZ=true to patch the policy directly (temporary)."
}

# Print how to reach a host published on the platform gateway.
tm_print_gateway_hint() {
  local host="$1" ip
  ip="$(kubectl get svc -n "${TM_GATEWAY_NAMESPACE}" \
    -l "gateway.envoyproxy.io/owning-gateway-name=${TM_GATEWAY_NAME}" \
    -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  info "  Gateway IP: ${ip:-<kubectl get svc -n ${TM_GATEWAY_NAMESPACE}>} — add '${ip:-<gateway-ip>} ${host}' to /etc/hosts"
  info "  TLS: self-signed by default — trust env/<env>/logs/ai-solutions-ca.crt (or use curl -k)"
}
