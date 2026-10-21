# syntax=docker/dockerfile:1.7
# Multi-stage build: dependencies are resolved with uv from the lockfile in a builder stage,
# the runtime image is slim, runs as a non-root user and contains no build tooling.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 mergeagent \
    && useradd --system --uid 10001 --gid mergeagent --home-dir /home/mergeagent --create-home mergeagent
WORKDIR /app
COPY --from=builder --chown=mergeagent:mergeagent /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    WORKSPACE_ROOT=/var/lib/mergeagent/workspaces
RUN mkdir -p /var/lib/mergeagent/workspaces && chown -R mergeagent:mergeagent /var/lib/mergeagent
USER mergeagent
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "mergeagent.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
