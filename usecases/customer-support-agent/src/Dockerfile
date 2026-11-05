###############################################################################
# Multi-stage Dockerfile for the Customer Support Agent
# Builds a Python 3.12-slim image with FastAPI + Streamlit
###############################################################################

# --- Stage 1: Build dependencies ---
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .

# Strip Ray components these images never use (they only connect to a Ray
# cluster as a client): ray_dist.jar is for Java/cross-language workers, and the
# runtime-env agent's vendored libraries (aiohttp, multidict, ...) are only loaded
# on Ray cluster nodes. Both carry unpatched CVEs (jackson-databind, guava,
# httpcore5, aiohttp, multidict).
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && rm -rf /install/lib/python3.12/site-packages/ray/jars \
              /install/lib/python3.12/site-packages/ray/_private/runtime_env/agent/thirdparty_files

# --- Stage 2: Runtime image ---
FROM python:3.12-slim AS runtime

# Proxy build args (passed via --build-arg; needed for apt/pip behind a proxy)
ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG no_proxy
ARG NO_PROXY

# apt-get upgrade patches base-image OS packages flagged by the image scan
# to their fixed Debian versions.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade "pip>=26.2"

# Numeric UID/GID 10001 so runAsNonRoot can be verified (PSS restricted).
RUN groupadd -r -g 10001 agentuser && useradd -r -u 10001 -g agentuser -d /app agentuser

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application source code
COPY agent/ ./agent/
COPY api/ ./api/
COPY backend/ ./backend/
COPY tools/ ./tools/
COPY ui/ ./ui/
COPY config.py .
COPY customer_support_agent.py .
COPY startup.sh .

# Make startup script executable; /app/data holds the SQLite demo DB
# (an emptyDir in Kubernetes) and HOME=/app gives Streamlit a writable config dir.
RUN chmod +x startup.sh && mkdir -p /app/data && chown -R agentuser:agentuser /app

ENV HOME=/app \
    DB_PATH=/app/data/techgadgets.db \
    PYTHONUNBUFFERED=1

USER 10001

# Expose ports: Streamlit UI and FastAPI server
EXPOSE 8501 8502

# FastAPI health endpoint (Streamlit runs alongside on 8501)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8502/api/v1/health')"

# Run both services via startup script
CMD ["./startup.sh"]
