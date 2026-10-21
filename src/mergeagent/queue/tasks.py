"""arq background tasks.

``check_pr``    webhook -> wait for GitHub's mergeability -> create Job row if conflicted.
``resolve_job`` clone, merge, run the agent graph, publish, persist. Idempotent per job id and
                serialised per PR with a Redis lock.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mergeagent.config import Settings
from mergeagent.db.models import Attempt, ConflictHunkRow, Job, JobStatus
from mergeagent.db.repositories import DbCallRecorder, upsert_job
from mergeagent.db.session import transaction
from mergeagent.github.auth import InstallationTokenProvider
from mergeagent.github.client import GitHubClient, GitHubIssueContext
from mergeagent.github.publisher import GitHubPublisher
from mergeagent.llm.budget import RedisBudget
from mergeagent.llm.providers.base import LLMProvider
from mergeagent.logging import get_logger
from mergeagent.observability.metrics import JOBS
from mergeagent.rag.store import CodeStore
from mergeagent.sandbox.base import Sandbox
from mergeagent.service import ResolutionRequest, build_router, resolve_workspace
from mergeagent.verification.pipeline import RedisBaselineCache
from mergeagent.workspace.git_workspace import GitWorkspace
from mergeagent.workspace.safety import safe_repo_name

log = get_logger(__name__)


@dataclass
class Runtime:
    settings: Settings
    redis: Redis
    db: async_sessionmaker[AsyncSession]
    tokens: InstallationTokenProvider
    provider: LLMProvider
    store: CodeStore
    sandbox: Sandbox


def _rt(ctx: dict[str, Any]) -> Runtime:
    return ctx["runtime"]  # type: ignore[no-any-return]


async def check_pr(
    ctx: dict[str, Any], installation_id: int, repo: str, number: int, trigger: str
) -> str:
    rt = _rt(ctx)
    safe_repo_name(repo)
    gh = GitHubClient(await rt.tokens.token(installation_id), rt.settings.github.api_url)
    try:
        pr = await gh.wait_for_mergeability(repo, number)
    finally:
        await gh.aclose()
    if pr.get("state") != "open":
        return "closed"
    if pr["head"]["ref"].startswith(rt.settings.github.bot_branch_prefix):
        return "own-branch"  # never resolve our own resolution PRs
    if pr.get("mergeable") is not False and trigger != "comment":
        return f"mergeable={pr.get('mergeable')} state={pr.get('mergeable_state')}"
    async with transaction(rt.db) as s:
        job, created = await upsert_job(
            s, repo_full_name=repo, installation_id=installation_id, pr_number=number,
            head_ref=pr["head"]["ref"], base_ref=pr["base"]["ref"], head_sha=pr["head"]["sha"],
            base_sha=pr["base"]["sha"], trigger=trigger, budget_usd=rt.settings.llm.max_job_budget_usd,
        )  # fmt: skip
    if created or trigger == "comment":
        await ctx["redis"].enqueue_job("resolve_job", str(job.id), _job_id=f"resolve:{job.id}")
    return f"job {job.id} ({'new' if created else 'existing'})"


async def resolve_job(ctx: dict[str, Any], job_id: str) -> str:
    rt = _rt(ctx)
    async with rt.db() as s:
        job = await s.get(Job, uuid.UUID(job_id))
        if job is None:
            return "missing"
        if job.status not in (JobStatus.QUEUED, JobStatus.ERROR):
            return f"skipped ({job.status})"
        repo, number, installation_id = job.repo_full_name, job.pr_number, job.installation_id

    lock = rt.redis.lock(f"mergeagent:lock:{repo}#{number}", timeout=3600, blocking_timeout=5)
    if not await lock.acquire():
        raise RuntimeError("another job holds the PR lock; arq will retry")
    workdir = rt.settings.workspace_root / job_id
    ws: GitWorkspace | None = None
    gh: GitHubClient | None = None
    try:
        await _set_status(rt, job_id, JobStatus.RUNNING)
        token = await rt.tokens.token(int(installation_id or 0))
        gh = GitHubClient(token, rt.settings.github.api_url)
        pr = await gh.get_pull(repo, int(number or 0))
        ws = await GitWorkspace.clone(
            f"https://github.com/{repo}.git", Path(workdir),
            refs=[pr["base"]["ref"], f"refs/pull/{number}/head"], token=token,
        )  # fmt: skip
        outcome = await ws.start_merge(pr["head"]["sha"], pr["base"]["sha"])
        budget = RedisBudget(rt.redis)

        async def spent() -> float:
            return (await budget.snapshot(job_id)).spent_usd

        final = await resolve_workspace(
            ResolutionRequest(
                job_id, repo, ws, outcome, GitHubIssueContext(gh, repo, pr),
                GitHubPublisher(gh, pr, rt.settings.github.bot_branch_prefix, cost=spent),
            ),
            settings=rt.settings, provider=rt.provider, router=build_router(rt.settings),
            budget=budget, recorder=DbCallRecorder(rt.db), store=rt.store, sandbox=rt.sandbox,
            baseline_cache=RedisBaselineCache(rt.redis),
        )  # fmt: skip
        await _persist(rt, job_id, final, await spent())
        JOBS.labels(status=final.get("status", "error")).inc()
        return str(final.get("status"))
    except Exception as exc:
        log.exception("job.error", job_id=job_id)
        await _set_status(rt, job_id, JobStatus.ERROR, error=str(exc)[:4000])
        JOBS.labels(status="error").inc()
        raise
    finally:
        if gh:
            await gh.aclose()
        if ws:
            ws.cleanup()
        await lock.release()


async def _set_status(rt: Runtime, job_id: str, status: str, error: str | None = None) -> None:
    async with transaction(rt.db) as s:
        job = await s.get(Job, uuid.UUID(job_id))
        if job:
            job.status = status
            job.error = error


async def _persist(rt: Runtime, job_id: str, final: Mapping[str, Any], spent: float) -> None:
    async with transaction(rt.db) as s:
        job = await s.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        job.status = final.get("status", JobStatus.ERROR)
        job.result_url = final.get("result_url")
        job.attempts_used = int(final.get("attempt", 0))
        job.spent_usd = spent
        verification = final.get("verification") or {}
        job.summary = {
            "reason": final.get("reason"),
            "events": final.get("events", []),
            "verification": {k: v for k, v in verification.items() if k != "runs"},
            "review": final.get("review"),
        }
        s.add(Attempt(
            job_id=job.id, number=job.attempts_used or 1, verdict=str(verification.get("verdict", "n/a")),
            patch=final.get("patch", "")[:500_000], feedback=final.get("feedback", "")[:20_000],
            verification=verification,
        ))  # fmt: skip
        for h in final.get("hunks", []):
            path, _, idx = h["hunk_id"].rpartition("#")
            s.add(ConflictHunkRow(job_id=job.id, path=path, hunk_index=int(idx), category=h["category"],
                                  complexity=h["complexity"], strategy=h["strategy"]))  # fmt: skip
