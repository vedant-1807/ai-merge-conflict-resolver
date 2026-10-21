"""FastAPI application factory: ``uvicorn mergeagent.api.main:app``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from redis.asyncio import Redis

from mergeagent import __version__
from mergeagent.api.routes import api, health
from mergeagent.api.webhooks import router as webhooks
from mergeagent.config import Settings, get_settings
from mergeagent.db.session import make_engine, make_sessionmaker
from mergeagent.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_json)
        engine = make_engine(settings.database_url)
        app.state.settings = settings
        app.state.db = make_sessionmaker(engine)
        app.state.redis = Redis.from_url(settings.redis_url)
        app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        yield
        await app.state.arq.aclose()
        await app.state.redis.aclose()
        await engine.dispose()

    app = FastAPI(title="MergeAgent", version=__version__, lifespan=lifespan)
    app.include_router(health)
    app.include_router(webhooks)
    app.include_router(api)
    return app


app = create_app()
