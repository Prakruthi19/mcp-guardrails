FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPSGUARD_CONFIG=/app/opsguard.yaml \
    OPSGUARD_DATABASE=/data/opsguard.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY opsguard ./opsguard
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 opsguard && mkdir /data && chown opsguard /data
COPY opsguard.yaml ./

USER opsguard
VOLUME ["/data"]
EXPOSE 8000
# Hosting platforms (Render, Cloud Run, Railway) pass the port in $PORT.
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz')" || exit 1
CMD ["sh", "-c", "exec opsguard serve --host 0.0.0.0 --port ${PORT:-8000}"]
