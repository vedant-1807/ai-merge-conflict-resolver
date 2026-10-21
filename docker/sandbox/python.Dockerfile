# Sandbox image for Python repositories. Untrusted code runs here as an unprivileged user,
# with no network during build/test (see mergeagent.sandbox.docker_sandbox).
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential coreutils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest pytest-xdist mypy \
    && groupadd --gid 10001 runner && useradd --uid 10001 --gid runner --create-home runner
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_USER=1 PYTHONUSERBASE=/tmp/.local PATH="/tmp/.local/bin:$PATH"
WORKDIR /workspace
