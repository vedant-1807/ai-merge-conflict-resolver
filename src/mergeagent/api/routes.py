"""Operational API: health, metrics, and job inspection/trigger (API-key protected)."""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import select, text

from mergeagent.db.models import Job, LLMCall
from mergeagent.db.repositories import list_jobs

health = APIRouter()
api = APIRouter(prefix="/api")


def require_api_key(request: Request, x_api_key: str = Header("")) -> None:
    expected = request.app.state.settings.api_key.get_secret_value()
    if not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")


@health.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@health.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}
    try:
        async with request.app.state.db() as s:
            await s.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc.__class__.__name__}"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc.__class__.__name__}"
    ok = all(v == "ok" for v in checks.values())
    response.status_code = 200 if ok else 503
    return {"status": "ok" if ok else "degraded", "checks": checks}


@health.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class JobOut(BaseModel):
    id: uuid.UUID
    repo_full_name: str
    pr_number: int | None
    status: str
    trigger: str
    attempts_used: int
    spent_usd: float
    budget_usd: float
    result_url: str | None
    created_at: datetime
    summary: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class TriggerIn(BaseModel):
    installation_id: int
    repo: str
    pr_number: int


@api.get("/jobs", dependencies=[Depends(require_api_key)])
async def jobs(request: Request, repo: str | None = None, limit: int = 50) -> list[JobOut]:
    async with request.app.state.db() as s:
        rows = await list_jobs(s, repo=repo, limit=min(limit, 200))
    return [JobOut.model_validate(r) for r in rows]


@api.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def job_detail(request: Request, job_id: uuid.UUID) -> dict[str, Any]:
    async with request.app.state.db() as s:
        job = await s.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        calls = (
            await s.execute(select(LLMCall).where(LLMCall.job_id == job_id).order_by(LLMCall.id))
        ).scalars()
        return {
            "job": JobOut.model_validate(job).model_dump(mode="json"),
            "llm_calls": [
                {"agent": c.agent, "model": c.model, "reason": c.route_reason, "cost_usd": c.cost_usd,
                 "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
                 "cache_read_tokens": c.cache_read_tokens, "latency_ms": c.latency_ms,
                 "redactions": c.redactions, "status": c.status}
                for c in calls
            ],
        }  # fmt: skip


@api.post("/jobs", status_code=202, dependencies=[Depends(require_api_key)])
async def trigger(request: Request, body: TriggerIn) -> dict[str, Any]:
    await request.app.state.arq.enqueue_job(
        "check_pr",
        installation_id=body.installation_id,
        repo=body.repo,
        number=body.pr_number,
        trigger="comment",
    )
    return {"queued": True}
