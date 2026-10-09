FROM python:3.12-slim
RUN groupadd -r agentuser && useradd -r -g agentuser -d /app agentuser

WORKDIR /app

# Proxy build args (passed via --build-arg; needed for pip in air-gapped/proxied envs)
ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG no_proxy
ARG NO_PROXY

# Install deps first (layer cache)
COPY requirements.txt .
# gcc and libffi-dev are required to compile native extensions pulled in by
# ray[default] (e.g. grpcio) when no pre-built wheel is available for the
# python:3.12-slim base image.
# apt-get upgrade patches base-image OS packages flagged by the image scan
# (perl-base, gzip, libpcre2-8, libsqlite3, ...) to their fixed Debian versions.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

# Copy application
COPY agent.py .
COPY main.py .
COPY __init__.py .

# Workspace dir (mounted as PVC in Kubernetes) + user-writable pip install dir
RUN mkdir -p /app/workspace /app/.local && chown -R agentuser:agentuser /app

# PIP_USER=1: pip install <pkg> at runtime writes to /app/.local (HOME/.local)
# so the non-root agentuser can use the install_package tool without root access.
ENV HOME=/app \
    PIP_USER=1 \
    WORKSPACE_DIR=/app/workspace \
    PYTHONUNBUFFERED=1 \
    AUTO_OPEN_BROWSER=false

USER agentuser

EXPOSE 8090

# TCP socket check — compatible with any web framework (DevUI doesn't expose /health)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import socket; s=socket.socket(); s.connect(('localhost', 8090)); s.close()"

CMD ["python", "main.py"]
