###############################################################################
# Multi-stage Dockerfile for the Customer Support Agent
# Builds a Python 3.12-slim image with FastAPI + Streamlit
###############################################################################

# --- Stage 1: Build dependencies ---
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Stage 2: Runtime image ---
FROM python:3.12-slim AS runtime

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
