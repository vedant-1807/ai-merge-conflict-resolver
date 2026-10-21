# MergeAgent: AI Merge Conflict Resolution

A multi-agent **GitHub App** that resolves pull-request merge conflicts and opens a **draft PR only when the fix is proven**. It compares tests on both parent branches against the candidate merge inside isolated Docker sandboxes.

`LangGraph` · `MCP` · `FastAPI` · `PostgreSQL + pgvector` · `Redis` · `Docker` · `Claude (Haiku / Sonnet / Opus)`

```
GitHub ──webhook──► FastAPI ──enqueue──► Redis (arq) ──► Worker
            HMAC verify, dedupe                           │
                                                          ▼
   PostgreSQL ◄── jobs · attempts · llm_calls ──  LangGraph pipeline
   + pgvector (code RAG)                                  │
                         ┌────────────────────────────────┼────────────────────────────┐
                         ▼                                ▼                            ▼
              Agent-scoped MCP servers           LLM gateway                    Docker sandbox
              git · tests · issues · rag   routing · redaction · budget   no network · non-root · cgroups
```

## How it works

1. **Webhook** (`pull_request`, `/resolve` comment, check re-run) → signature verified (constant-time HMAC), redelivery de-duplicated in Redis, and the event is queued. The response takes milliseconds.
2. **Mergeability**: the worker waits for GitHub's async `mergeable` flag, then creates an idempotent `Job` (unique on repo, PR, head SHA and base SHA). A Redis lock serializes work per PR.
3. **Workspace**: a blobless clone with git hooks disabled, system config ignored, an `ext::` transport block, and the token passed through env rather than argv. The worker merges the target branch into the PR with `diff3` and parses every hunk, including modify/delete conflicts.
4. **LangGraph pipeline**

   | Node | What it does | Model |
   |---|---|---|
   | `triage` | Deterministically resolves identical, one-sided, whitespace, import-block, changelog-append and lockfile hunks. Scores the rest for complexity. | none |
   | `analyze` | Analyzer agent: recovers each side's intent from diffs, `git log -L`, the PR, linked issues and RAG over callers. | routed |
   | `resolve` | Resolver agent: writes each hunk through `git__write_resolution`, which rejects leftover markers and unparseable files immediately. | routed, escalates on retry |
   | `verify` | Regression pipeline in the sandbox, then an independent Verifier agent reviews the diff. | routed |
   | `publish` / `give_up` | Draft PR + check run, or an explanatory comment with the best unverified candidate. | none |

   A failed verification loops back to `resolve` with the failure logs and a **larger model tier**, until `max_attempts` is reached or the budget runs out.

5. **Regression verification**: the target branch, the PR head and the candidate each run setup → build → typecheck → tests in a fresh container. A test that passes on a parent but fails or disappears on the candidate is a regression. New typecheck diagnostics (line-number-insensitive) fail the gate, as do failing acceptance commands. Parent results are cached by (repo, SHA, config hash), so retries only pay for the candidate.

## Key design decisions

- **Least-privilege tools (MCP).** Four MCP servers are built per job and connected in-process through the official `mcp` SDK. A per-agent allowlist means the Analyzer can't write, the Resolver can't run the sandbox, and the Verifier can't edit the patch it is judging. Out-of-scope calls are refused before they reach a server.
- **One LLM choke point.** Every call goes through `LLMGateway`:
  - **Routing:** task + hunk complexity + attempt + remaining budget → Haiku, Sonnet or Opus. A `--fixed-model` baseline is available for comparison.
  - **Secret redaction:** known key formats plus entropy-based detection on user-side content. Placeholders are stable within a job.
  - **Per-job budgets:** an atomic Redis Lua reserve/settle in integer micro-dollars. Concurrent agents can't overspend, and a refused call becomes `needs_human`, not a crash.
  - **Prompt caching and accounting:** the stable prompt prefix is cached, and every call is recorded with tokens, cost, latency and route reason.
- **Edits stay inside conflict regions by construction.** Resolutions replace hunks only, and the file is reassembled from the untouched segments.
- **The PR can't weaken its own checks.** Verification commands come from `.mergeagent.yml` on the *target* branch.
- **Sandbox isolation.** No bind mounts; code goes in with `put_archive`. Containers run non-root with all capabilities dropped, `no-new-privileges`, memory/CPU/PID limits and `timeout -k` on every command. Network is attached only for dependency setup. gVisor is supported via `SANDBOX__RUNTIME=runsc`.
- **Hybrid RAG.** pgvector HNSW (cosine) plus Postgres full-text search, fused with reciprocal-rank fusion. Python is chunked per function and class, and indexing is incremental by content hash. Voyage `voyage-code-3` embeddings are used in production; a key-less hashing embedder covers dev and CI.

## Quick start

```bash
uv sync
uv run mergeagent demo          # offline end-to-end run: no API key, real graph + MCP + tests
```

Resolve two local branches with Claude:

```bash
export LLM__ANTHROPIC_API_KEY=sk-ant-...
uv run mergeagent resolve --repo ../some-repo --ours feature --theirs main
```

Run the full stack (API + worker + Postgres/pgvector + Redis):

```bash
cp .env.example .env            # fill in GitHub App + Anthropic settings
make sandbox-images
docker compose up -d --build    # migrations run automatically
curl localhost:8000/readyz
```

Register the GitHub App with [`app.yml`](app.yml). See [docs/github-app-setup.md](docs/github-app-setup.md).

## Evaluation

The harness in [`src/mergeagent/eval`](src/mergeagent/eval) measures the three claims this project is built around:

| Claim | How it's measured | Command |
|---|---|---|
| Test-verified resolution rate | Re-merge the parents of real historical merge commits (5 pytest repos, 20 each: flask, click, werkzeug, jinja, pytest), run the full pipeline, count verified resolutions | `make eval-mine eval-run` |
| LLM cost per attempt vs fixed model | The same scenarios with routing vs `--fixed-model claude-opus-5-5` | `make eval-cost` |
| Regression detection | Inject behavioral mutants (flipped comparisons, off-by-one, dropped `not`, …) into known-good human resolutions; count how many the pipeline blocks | `make eval-mutate` |

`make eval-report` writes the results to `docs/evaluation-results.md`. The benchmark calls the Claude API, so it needs `LLM__ANTHROPIC_API_KEY` and costs real money. Methodology: [docs/evaluation.md](docs/evaluation.md).

## Development

```bash
make lint typecheck test        # ruff, mypy --strict, unit tests
make test-integration           # needs Postgres/Redis (docker compose up postgres redis) + Docker
```

CI runs lint, strict typing, unit tests, integration tests against pgvector, Redis and the real Docker sandbox, the offline demo, and an image build. Tagged releases publish multi-arch images to GHCR.

```
src/mergeagent/
  agents/        LangGraph state, nodes, tool loop, prompts
  conflicts/     diff3 parser, triage, applier, validators
  workspace/     hardened git wrapper, input sanitising
  mcp_servers/   git / tests / issues / rag servers + scoped registry
  llm/           gateway, router, redaction, budget, pricing, providers
  rag/           chunker, embeddings, pgvector + memory stores
  sandbox/       Docker (hardened) and local backends
  verification/  repo config, report parsers, comparator, pipeline
  github/        App auth, REST client, publisher
  api/ queue/    FastAPI app, arq worker tasks
  eval/          mining, benchmark, mutation study, report
```

## License

MIT
