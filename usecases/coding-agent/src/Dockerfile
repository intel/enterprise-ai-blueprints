FROM python:3.12-slim
# Numeric UID/GID 10001 so runAsNonRoot can be verified (PSS restricted).
RUN groupadd -r -g 10001 agentuser && useradd -r -u 10001 -g agentuser -d /app agentuser

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
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY agent.py .
COPY tools.py .
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

USER 10001

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/health')"

CMD ["python", "main.py"]
