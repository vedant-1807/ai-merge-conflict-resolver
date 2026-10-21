"""Subprocess "sandbox" for unit tests and the offline CLI demo.

WARNING: provides **no isolation**: code runs as the current user with network access. Never
use it for untrusted repositories; production always uses ``DockerSandbox``.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tarfile
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mergeagent.sandbox.base import ExecResult, tail


class LocalSandboxSession:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def exec(self, command: str, *, timeout_s: int, network: bool = False) -> ExecResult:
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", command, cwd=self.root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "CI": "true"},
        )  # fmt: skip
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
            timed_out = False
        except TimeoutError:
            proc.kill()
            out, _ = await proc.communicate()
            timed_out = True
        return ExecResult(
            124 if timed_out else proc.returncode or 0,
            tail((out or b"").decode(errors="replace")),
            time.perf_counter() - started,
            timed_out,
        )

    async def read_file(self, path: str) -> bytes | None:
        p = Path(path) if path.startswith("/") else self.root / path
        return p.read_bytes() if p.is_file() else None


class LocalSandbox:
    @asynccontextmanager
    async def session(
        self, archive: bytes, *, image: str | None = None
    ) -> AsyncIterator[LocalSandboxSession]:
        root = Path(tempfile.mkdtemp(prefix="mergeagent-local-"))
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(root, filter="data")
            yield LocalSandboxSession(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
