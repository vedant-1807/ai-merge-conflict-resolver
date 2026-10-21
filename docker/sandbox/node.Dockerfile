# Sandbox image for JavaScript/TypeScript repositories.
FROM node:22-bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git coreutils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 runner && useradd --uid 10001 --gid runner --create-home runner
ENV npm_config_cache=/tmp/.npm CI=true
WORKDIR /workspace
