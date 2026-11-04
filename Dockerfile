FROM ollama/ollama:latest AS ollama

ENV OLLAMA_HOST=0.0.0.0:11434
RUN set -eu; \
    ollama serve > /tmp/ollama-build.log 2>&1 & server_pid=$!; \
    trap 'kill "$server_pid" 2>/dev/null || true; rm -f /tmp/ollama-build.log' EXIT; \
    ready=0; \
    for attempt in $(seq 1 60); do \
        if ollama list >/dev/null 2>&1; then ready=1; break; fi; \
        sleep 1; \
    done; \
    if [ "$ready" != 1 ]; then cat /tmp/ollama-build.log; exit 1; fi; \
    ollama show qwen3.5:0.8b >/dev/null 2>&1 || ollama pull qwen3.5:0.8b; \
    ollama show qwen3.5:0.8b >/dev/null

FROM python:3.12-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install . \
    && groupadd --gid 10001 service \
    && useradd --uid 10001 --gid service --no-create-home service \
    && mkdir -p /app/data \
    && chown service:service /app/data

USER service
EXPOSE 8000
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8000", "--call", "app.main:create_app"]
