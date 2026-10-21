"""arq worker entrypoint: ``arq mergeagent.queue.worker.WorkerSettings``."""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings
from redis.asyncio import Redis

from mergeagent.config import get_settings
from mergeagent.db.session import make_engine, make_sessionmaker
from mergeagent.github.auth import InstallationTokenProvider
from mergeagent.logging import configure_logging
from mergeagent.queue.tasks import Runtime, check_pr, resolve_job
from mergeagent.rag.store import PgVectorStore
from mergeagent.service import build_embedder, build_provider, build_sandbox

settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(settings.log_level, settings.log_json)
    redis = Redis.from_url(settings.redis_url)
    engine = make_engine(settings.database_url)
    factory = make_sessionmaker(engine)
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    ctx["engine"] = engine
    ctx["runtime"] = Runtime(
        settings=settings,
        redis=redis,
        db=factory,
        tokens=InstallationTokenProvider(
            settings.github.app_id or 0,
            settings.github.load_private_key(),
            redis,
            settings.github.api_url,
        ),
        provider=build_provider(settings),
        store=PgVectorStore(factory, build_embedder(settings)),
        sandbox=build_sandbox(settings),
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["runtime"].redis.aclose()
    await ctx["engine"].dispose()


class WorkerSettings:
    functions = [check_pr, resolve_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 4
    job_timeout = 3600
    max_tries = 3
    keep_result = 24 * 3600
