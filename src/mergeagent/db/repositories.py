"""Data-access helpers used by the API and the worker."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mergeagent.db.models import Attempt, ConflictHunkRow, Job, JobStatus, LLMCall, VerificationRun
from mergeagent.db.session import transaction
from mergeagent.llm.gateway import LLMCallRecord


async def upsert_job(session: AsyncSession, **fields: Any) -> tuple[Job, bool]:
    """Insert a job unless one exists for the same (repo, pr, head, base). Returns (job, created)."""
    stmt = (
        insert(Job)
        .values(id=uuid.uuid4(), status=JobStatus.QUEUED, **fields)
        .on_conflict_do_nothing(constraint="uq_job_key")
        .returning(Job.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    key = (
        Job.repo_full_name == fields["repo_full_name"],
        Job.pr_number == fields.get("pr_number"),
        Job.head_sha == fields["head_sha"],
        Job.base_sha == fields["base_sha"],
    )
    job = (await session.execute(select(Job).where(*key))).scalar_one()
    return job, new_id is not None


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> Job | None:
    return await session.get(Job, job_id)


async def list_jobs(session: AsyncSession, *, repo: str | None, limit: int) -> list[Job]:
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if repo:
        q = q.where(Job.repo_full_name == repo)
    return list((await session.execute(q)).scalars())


class DbCallRecorder:
    """CallRecorder that persists every LLM call (used by the worker)."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def record(self, call: LLMCallRecord) -> None:
        async with transaction(self._factory) as s:
            s.add(
                LLMCall(
                    job_id=uuid.UUID(call.job_id), agent=call.agent, task=call.task,
                    model=call.model, route_reason=call.route_reason[:300],
                    input_tokens=call.input_tokens, output_tokens=call.output_tokens,
                    cache_read_tokens=call.cache_read_tokens,
                    cache_write_tokens=call.cache_write_tokens, cost_usd=call.cost_usd,
                    latency_ms=call.latency_ms, redactions=call.redactions, status=call.status,
                    error=call.error,
                )
            )  # fmt: skip


__all__ = [
    "Attempt",
    "ConflictHunkRow",
    "DbCallRecorder",
    "VerificationRun",
    "get_job",
    "list_jobs",
    "upsert_job",
]
