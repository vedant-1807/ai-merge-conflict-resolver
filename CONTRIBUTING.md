# Contributing

```bash
uv sync
make lint typecheck test        # must pass before a PR
make test-integration           # needs `docker compose up -d postgres redis` and Docker
```

- Keep `mypy --strict` clean. Prefer typed dataclasses over loose dicts at module boundaries.
- New agent tools go in `src/mergeagent/mcp_servers/servers.py` and must be added to an agent's
  allowlist in `registry.py`. Don't widen an agent's scope without a reason in the PR.
- Anything that executes repository code must go through `sandbox/`, never the worker's host.
- Benchmark numbers in the README come only from `make eval-report` output.
