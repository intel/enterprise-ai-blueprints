#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

# Start FastAPI server in background
echo "Starting FastAPI server on port ${API_PORT:-8502}..."
uvicorn api.server:app --host 0.0.0.0 --port "${API_PORT:-8502}" &

# Start Streamlit UI in foreground
echo "Starting Streamlit UI on port ${STREAMLIT_PORT:-8501}..."
exec streamlit run customer_support_agent.py \
    --server.port "${STREAMLIT_PORT:-8501}" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false
