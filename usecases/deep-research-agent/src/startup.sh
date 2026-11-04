#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

###############################################################################
# Container entry point.
#
# Starts the eval metrics dashboard in the background when
# EVAL_DASHBOARD_ENABLED=true, then runs the research API in the foreground.
###############################################################################

if [[ "${EVAL_DASHBOARD_ENABLED:-false}" == "true" ]]; then
    echo "Starting eval dashboard on port ${EVAL_DASHBOARD_PORT:-8082}..."
    python eval/dashboard.py --port "${EVAL_DASHBOARD_PORT:-8082}" --serve-only &
fi

echo "Starting Deep Research API on port ${PORT:-8081}..."
exec python main.py
