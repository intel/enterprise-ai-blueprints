# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Centralized configuration for the Customer Support Agent."""
import os

# LLM Endpoint
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2-5-coder-14b")
LLM_VERIFY_SSL = False

# Database
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "techgadgets.db"))

# ─── Memory (RediSearch vector store + LiteLLM embeddings) ───────────────────
# Memory is backed by the agent-toolkit redis-stack (RediSearch) and uses an
# embedding model served by LiteLLM — no local embedding model.
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() in ("1", "true", "yes")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis-stack-server.redis.svc.cluster.local:6379/0")
MEMORY_INDEX = os.getenv("MEMORY_INDEX", "csa_mem_idx")
MEMORY_TOP_K = int(os.getenv("MEMORY_TOP_K", "4"))

# Embeddings served by LiteLLM (Inference Toolkit catalog name for BAAI/bge-base-en-v1.5)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "bge-base-en")
EMBEDDING_DIMS = int(os.getenv("EMBEDDING_DIMS", "768"))

# ─── Parallel tool execution ─────────────────────────────────────────────────
# TOOL_EXECUTOR: "local" (ThreadPoolExecutor, in-process) or "ray" (dispatch to
# the agent-toolkit KubeRay cluster (kuberay_enabled), with automatic fallback to local).
TOOL_EXECUTOR = os.getenv("TOOL_EXECUTOR", "local").lower()
TOOL_MAX_PARALLEL = int(os.getenv("TOOL_MAX_PARALLEL", "8"))
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "ray://ray-cluster-head-svc.ray-system:10001")
# In-cluster URL of this agent's own API — Ray tasks call back here to run tools
# (tools read local SQLite, so they execute against the owning pod, not the worker).
SELF_API_URL = os.getenv("SELF_API_URL", "http://customer-support-agent.customer-support-agent.svc.cluster.local:8502")

# Agent settings
MAX_AGENT_TURNS = 50
MAX_TOKENS_PER_RESPONSE = 1500
TEMPERATURE = 0.3

# API
API_PORT = int(os.getenv("API_PORT", "8502"))
STREAMLIT_PORT = int(os.getenv("STREAMLIT_PORT", "8501"))

# Business rules
RETURN_WINDOW_DAYS = 30
WARRANTY_STANDARD_YEARS = 1
LOYALTY_POINTS_PER_DOLLAR = 10
