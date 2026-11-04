###############################################################################
# Multi-stage Dockerfile for the Deep Research Agent
#
# Builds a Python 3.12-slim image with FastAPI + LangGraph + the extractive
# summarizer. torch is installed from the CPU-only wheel index (no CUDA — this
# blueprint deploys no models; the LLM is consumed over HTTP from the GenAI
# Gateway). NLTK punkt_tab and the sentence-transformers embedding model are
# baked into the image so the running pod needs no HuggingFace access.
###############################################################################

# --- Stage 1: Build dependencies ---------------------------------------------
FROM python:3.12-slim AS builder

# Proxy build args (needed for pip in proxied/air-gapped environments)
ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG no_proxy
ARG NO_PROXY

WORKDIR /build

COPY requirements.txt .

# --extra-index-url pulls the CPU-only torch wheel (sentence-transformers dep),
# which is ~1GB smaller than the default CUDA build.
RUN pip install --no-cache-dir --prefix=/install \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r requirements.txt

# --- Stage 2: Runtime image --------------------------------------------------
FROM python:3.12-slim AS runtime

ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG no_proxy
ARG NO_PROXY

# Numeric UID/GID 10001 so runAsNonRoot can be verified (PSS restricted).
RUN groupadd -r -g 10001 researcher && useradd -r -u 10001 -g researcher -d /app researcher

WORKDIR /app

# Copy installed Python packages from the builder stage
COPY --from=builder /install /usr/local

# Model/tokenizer asset caches live inside the image
ENV NLTK_DATA=/app/nltk_data \
    HF_HOME=/app/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/app/hf_cache

# Pre-download the NLTK sentence tokenizer and the embedding model used by
# extractive_summarizer.py. Done at build time so the pod never reaches out to
# HuggingFace at runtime (see HF_HUB_OFFLINE below).
# NLTK_ALLOW_PROXIED_URLOPEN: recent NLTK refuses downloads through an HTTP(S)
# proxy unless explicitly allowed (build-time only; the pod stays offline).
RUN NLTK_ALLOW_PROXIED_URLOPEN=1 python -c "import nltk; assert nltk.download('punkt_tab', download_dir='/app/nltk_data')" \
 && python -c "from sentence_transformers import SentenceTransformer as S; S('all-MiniLM-L6-v2')"

# Application source
COPY agent/ ./agent/
COPY db/ ./db/
COPY search_providers/ ./search_providers/
COPY middleware/ ./middleware/
COPY frontend/ ./frontend/
COPY eval/ ./eval/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY api.py cache.py chunk_summarizer.py clean_text.py config.py \
     extractive_summarizer.py llm_client.py llm_summarizer.py main.py \
     models.py researcher.py search.py token_tracker.py ./
COPY startup.sh .

RUN chmod +x startup.sh

# /app/data is the volume mount point (PVC in Kubernetes, named volume in
# Compose). Holds the SQLite DB and the per-query metrics the eval dashboard
# reads. Created here so the container still starts when nothing is mounted.
RUN mkdir -p /app/data/results/live && chown -R researcher:researcher /app

ENV PYTHONUNBUFFERED=1 \
    HOME=/app \
    HF_HUB_OFFLINE=1 \
    RESULTS_DIR=/app/data/results/live \
    DATABASE_URL=sqlite+aiosqlite:////app/data/deep_research.db \
    EXTRACT_EMBEDDING_DEVICE=cpu \
    PORT=8081 \
    EVAL_DASHBOARD_PORT=8082

USER 10001

# 8081 — research API + web UI, 8082 — eval metrics dashboard (opt-in)
EXPOSE 8081 8082

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.getenv('PORT','8081'))"

CMD ["./startup.sh"]
