"""Composition root: builds providers/sandboxes/pipelines from settings and runs one resolution.

``resolve_workspace`` is the single entry point shared by the GitHub worker, the CLI and the
evaluation harness, so all three exercise exactly the same pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mergeagent.agents.graph import run_resolution
from mergeagent.agents.publisher import Publisher
from mergeagent.agents.state import Deps, GraphState
from mergeagent.config import Settings
from mergeagent.llm.budget import Budget
from mergeagent.llm.gateway import CallRecorder, LLMGateway
from mergeagent.llm.providers.anthropic import AnthropicProvider
from mergeagent.llm.providers.base import LLMProvider
from mergeagent.llm.router import ModelRouter
from mergeagent.mcp_servers.context import IssueContext, JobContext
from mergeagent.rag.embeddings import Embedder, make_embedder
from mergeagent.rag.store import CodeStore
from mergeagent.sandbox.base import Sandbox
from mergeagent.verification.pipeline import BaselineCache, VerificationPipeline
from mergeagent.verification.repo_config import CONFIG_FILE, RepoConfig, load
from mergeagent.workspace.git_workspace import GitWorkspace, MergeOutcome


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm.provider == "anthropic":
        key = settings.llm.anthropic_api_key
        return AnthropicProvider(
            key.get_secret_value() if key else None,
            timeout_s=settings.llm.request_timeout_s,
            max_retries=settings.llm.max_retries,
        )
    raise ValueError(f"unsupported LLM provider {settings.llm.provider!r} (fake is test-only)")


def build_router(settings: Settings, fixed_model: str | None = None) -> ModelRouter:
    s = settings.llm
    return ModelRouter(
        s.model_small, s.model_medium, s.model_large,
        fixed_model=fixed_model or s.fixed_model, max_output_tokens=s.max_output_tokens,
    )  # fmt: skip


def build_embedder(settings: Settings) -> Embedder:
    e = settings.embeddings
    key = e.voyage_api_key.get_secret_value() if e.voyage_api_key else None
    return make_embedder(e.provider, api_key=key, model=e.model, dimensions=e.dimensions)


def build_sandbox(settings: Settings) -> Sandbox:
    if settings.sandbox.backend == "local":
        from mergeagent.sandbox.local_sandbox import LocalSandbox

        return LocalSandbox()
    from mergeagent.sandbox.docker_sandbox import DockerSandbox

    return DockerSandbox(settings.sandbox)


@dataclass
class ResolutionRequest:
    job_id: str
    repo: str
    ws: GitWorkspace
    outcome: MergeOutcome
    issues: IssueContext
    publisher: Publisher


async def load_repo_config(ws: GitWorkspace, target_sha: str) -> RepoConfig:
    """Verification commands come from the *target* branch, never from the PR under test, so a
    PR cannot weaken the checks that judge it."""
    try:
        text: str | None = await ws.read_file(target_sha, CONFIG_FILE)
    except FileNotFoundError:
        text = None
    return load(text, await ws.list_files(target_sha))


async def index_target(
    store: CodeStore, ws: GitWorkspace, repo: str, sha: str, max_files: int = 4000
) -> int:
    files: dict[str, str] = {}
    for path in (await ws.list_files(sha))[:max_files]:
        try:
            files[path] = await ws.read_file(sha, path, max_bytes=200_000)
        except FileNotFoundError:
            continue
    return await store.index_files(repo, files)


async def resolve_workspace(
    req: ResolutionRequest,
    *,
    settings: Settings,
    provider: LLMProvider,
    router: ModelRouter,
    budget: Budget,
    recorder: CallRecorder,
    store: CodeStore,
    sandbox: Sandbox,
    baseline_cache: BaselineCache,
    budget_usd: float | None = None,
    on_context: Callable[[JobContext], None] | None = None,
) -> GraphState:
    await budget.open(
        req.job_id, budget_usd if budget_usd is not None else settings.llm.max_job_budget_usd
    )
    if not req.outcome.clean:
        await index_target(store, req.ws, req.repo, req.outcome.theirs_sha)
    repo_config = await load_repo_config(req.ws, req.outcome.theirs_sha)
    ctx = JobContext(req.job_id, req.repo, req.ws, req.outcome, req.issues, store)
    if on_context is not None:
        on_context(ctx)
    deps = Deps(
        ctx=ctx,
        gateway=LLMGateway(provider, router, budget, recorder),
        pipeline=VerificationPipeline(sandbox, baseline_cache, settings.sandbox.image),
        publisher=req.publisher,
        repo_config=repo_config,
        settings=settings.agent,
    )
    return await run_resolution(deps)
