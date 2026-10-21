"""Where a finished resolution goes: a draft PR (GitHub) or a patch file (local/eval)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mergeagent.mcp_servers.context import JobContext


@dataclass(frozen=True, slots=True)
class PublishResult:
    url: str | None
    detail: str


class Publisher(Protocol):
    async def publish(
        self, ctx: JobContext, summary: dict[str, Any], patch: str
    ) -> PublishResult: ...
    async def report_failure(
        self, ctx: JobContext, summary: dict[str, Any], patch: str
    ) -> PublishResult: ...


class LocalPublisher:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir

    async def _write(self, ctx: JobContext, patch: str, suffix: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{ctx.job_id}{suffix}.patch"
        path.write_text(patch)
        return path

    async def publish(self, ctx: JobContext, summary: dict[str, Any], patch: str) -> PublishResult:
        path = await self._write(ctx, patch, "")
        return PublishResult(str(path), f"verified resolution written to {path}")

    async def report_failure(
        self, ctx: JobContext, summary: dict[str, Any], patch: str
    ) -> PublishResult:
        path = await self._write(ctx, patch, ".unverified")
        return PublishResult(None, f"unverified candidate written to {path}")
