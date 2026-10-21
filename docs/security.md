# Security model

The repository being merged is **untrusted input**: its code, its git metadata and its text, which may contain prompt injection.

| Threat | Mitigation |
|---|---|
| Forged webhooks | HMAC-SHA256 signature check with `hmac.compare_digest`; requests with no secret configured are rejected |
| Replayed webhooks | `X-GitHub-Delivery` stored with `SET NX` for 24 h |
| Budget abuse through `/resolve` | Only `OWNER`, `MEMBER` and `COLLABORATOR` comments trigger runs; hard per-job USD budget enforced atomically in Redis |
| Code execution through git (hooks, fsmonitor, `ext::` transport, filters) | `core.hooksPath=/dev/null`, `core.fsmonitor=false`, `protocol.ext.allow=never`, `GIT_ALLOW_PROTOCOL=https:file`, system and global config ignored, LFS smudge off |
| Argument and path injection | Refs, repo names and paths validated (`workspace/safety.py`); symlink escapes blocked with `resolve()` containment |
| Token leakage | Installation tokens injected through `GIT_CONFIG_*` env (not argv), cached in Redis for under 1 h; secrets redacted from every prompt |
| Untrusted test code | Docker sandbox: no bind mounts, non-root uid 10001, `cap_drop=ALL`, `no-new-privileges`, memory/CPU/PID limits, wall-clock timeouts, network attached only for dependency setup; gVisor supported |
| PR weakening its own verification | `.mergeagent.yml` is read from the target branch, never from the PR |
| Silent deletion of tests or behavior | Comparator flags tests that vanish ("lost"), not just failures; an independent Verifier agent reviews the diff |
| Prompt injection steering tools | Agent-scoped tool allowlists (the analyzer can't write; no agent can run arbitrary commands); writes limited to conflict regions by construction |
| Loops on the bot's own branches | PRs whose head branch starts with `mergeagent/` are ignored |

**Known trade-off:** the worker needs a Docker daemon. `docker-compose.yml` mounts the host socket for local use. In production, run the worker against a rootless Docker daemon, a dedicated sandbox host, or gVisor.
