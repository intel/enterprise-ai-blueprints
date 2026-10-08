#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# ensure-image-builder.sh <Dockerfile> [<Dockerfile> ...]
#
# Makes a container image builder available for the use-case deploy.sh scripts
# so the operator doesn't set one up by hand. On containerd (docker absent) it
# installs buildkit if missing, starts buildkitd (containerd worker, k8s.io
# namespace), and pre-stages each Dockerfile's base image via the containerd
# daemon (proxy-aware) so builds also work behind a proxy.
#
# Env: BUILDKIT_VERSION, SKIP_BUILDER_BOOTSTRAP=true, CONTAINER_TOOL,
#      http(s)_proxy / no_proxy.
# =============================================================================
set -euo pipefail

BUILDKIT_VERSION="${BUILDKIT_VERSION:-v0.17.2}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[builder]${NC} $*"; }
success() { echo -e "${GREEN}[builder]${NC} $*"; }
warn()    { echo -e "${YELLOW}[builder]${NC} $*"; }
die()     { echo -e "${RED}[builder]${NC} $*" >&2; exit 1; }

[[ "${SKIP_BUILDER_BOOTSTRAP:-false}" == "true" ]] && { info "SKIP_BUILDER_BOOTSTRAP=true — skipping."; exit 0; }

# docker path — caller builds with docker; buildkit not needed here.
if [[ "${CONTAINER_TOOL:-}" == "docker" ]] || { [[ -z "${CONTAINER_TOOL:-}" ]] && ! command -v nerdctl &>/dev/null && command -v docker &>/dev/null; }; then
  info "docker is available — skipping buildkit bootstrap."; exit 0
fi

command -v nerdctl &>/dev/null || die "Neither nerdctl nor docker found. Install one, or set SKIP_BUILD=true with prebuilt images."

SUDO=""; [[ "$(id -u)" -ne 0 ]] && SUDO="sudo"
HP="${http_proxy:-${HTTP_PROXY:-}}"; HPS="${https_proxy:-${HTTPS_PROXY:-}}"; NP="${no_proxy:-${NO_PROXY:-localhost,127.0.0.1}}"

# Install buildkit if missing.
if ! command -v buildkitd &>/dev/null && [[ ! -x /usr/local/bin/buildkitd ]]; then
  info "Installing buildkit ${BUILDKIT_VERSION}..."
  case "$(uname -m)" in
    x86_64|amd64) bk_arch="amd64" ;;
    aarch64|arm64) bk_arch="arm64" ;;
    *) die "Unsupported architecture '$(uname -m)'. Install buildkit manually." ;;
  esac
  tmp="$(mktemp -d)"
  curl -fsSL --retry 3 -o "${tmp}/bk.tar.gz" \
    "https://github.com/moby/buildkit/releases/download/${BUILDKIT_VERSION}/buildkit-${BUILDKIT_VERSION}.linux-${bk_arch}.tar.gz" \
    || { rm -rf "${tmp}"; die "buildkit download failed (check network/proxy)."; }
  ${SUDO} tar -C /usr/local -xzf "${tmp}/bk.tar.gz"; rm -rf "${tmp}"
  success "Installed buildkit to /usr/local/bin."
fi

# Start buildkitd if not running.
if pgrep -x buildkitd >/dev/null 2>&1; then
  info "buildkitd already running."
else
  info "Starting buildkitd..."
  ${SUDO} env HTTP_PROXY="${HP}" HTTPS_PROXY="${HPS}" NO_PROXY="${NP}" \
              http_proxy="${HP}" https_proxy="${HPS}" no_proxy="${NP}" \
    buildkitd --oci-worker=false --containerd-worker=true --containerd-worker-namespace=k8s.io \
    >/tmp/buildkitd.log 2>&1 &
  for _ in $(seq 1 15); do ${SUDO} test -S /run/buildkit/buildkitd.sock && break; sleep 1; done
  ${SUDO} test -S /run/buildkit/buildkitd.sock || die "buildkitd did not start (see /tmp/buildkitd.log)."
  success "buildkitd is up."
fi

# Pre-stage base images (daemon-side pull honors the proxy; buildkitd's does not).
normalize_ref() {
  local r="$1" first
  if [[ "$r" == *"/"* ]]; then
    first="${r%%/*}"
    [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]] && echo "$r" || echo "docker.io/$r"
  else
    echo "docker.io/library/$r"
  fi
}

declare -A seen=()
for df in "$@"; do
  [[ -f "$df" ]] || { warn "Dockerfile not found: $df"; continue; }
  mapfile -t aliases < <(grep -iE '^[[:space:]]*FROM[[:space:]]' "$df" | sed -nE 's/.*[[:space:]][Aa][Ss][[:space:]]+([A-Za-z0-9_.-]+).*/\1/p')
  while read -r base; do
    [[ -z "$base" || "$base" == "scratch" ]] && continue
    for a in "${aliases[@]:-}"; do [[ "$base" == "$a" ]] && continue 2; done
    ref="$(normalize_ref "$base")"
    [[ -n "${seen[$ref]:-}" ]] && continue
    seen[$ref]=1
    info "Pre-staging base image: ${ref}"
    ${SUDO} env HTTP_PROXY="${HP}" HTTPS_PROXY="${HPS}" NO_PROXY="${NP}" \
                http_proxy="${HP}" https_proxy="${HPS}" no_proxy="${NP}" \
      ctr -n k8s.io content fetch "${ref}" >/dev/null 2>&1 \
      || warn "Could not pre-fetch ${ref} (build will try to pull it directly)."
  done < <(grep -iE '^[[:space:]]*FROM[[:space:]]' "$df" | awk '{print $2}')
done

success "Image builder ready."
