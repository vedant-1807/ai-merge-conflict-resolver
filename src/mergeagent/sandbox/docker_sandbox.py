"""Hardened Docker sandbox.

Each session is a fresh container, seeded via ``put_archive`` (no host bind mounts, so one job
can never see another job's files or the worker's filesystem). Hardening:

* non-root user, all Linux capabilities dropped, ``no-new-privileges``;
* memory / CPU / PID limits, wall-clock timeout on every command (``timeout -k``);
* **no network** by default. Dependency installation (``network=True``) runs on a dedicated
  egress network; the container is disconnected again before build/test commands run;
* optional gVisor (``runtime=runsc``) for kernel-level isolation.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import docker
from docker.errors import NotFound

from mergeagent.config import SandboxSettings
from mergeagent.logging import get_logger
from mergeagent.observability.metrics import SANDBOX_RUNS
from mergeagent.sandbox.base import ExecResult, tail

log = get_logger(__name__)
WORKDIR = "/workspace"
TIMEOUT_EXIT = 124


class DockerSandboxSession:
    def __init__(self, container: Any, network: Any, settings: SandboxSettings) -> None:
        self._c = container
        self._net = network
        self._s = settings
        self._connected = False

    async def _set_network(self, on: bool) -> None:
        if on == self._connected:
            return
        if on:
            await asyncio.to_thread(self._net.connect, self._c)
        else:
            await asyncio.to_thread(self._net.disconnect, self._c, force=True)
        self._connected = on

    async def exec(self, command: str, *, timeout_s: int, network: bool = False) -> ExecResult:
        await self._set_network(network)
        wrapped = ["timeout", "-k", "10", str(timeout_s), "sh", "-c", command]
        started = time.perf_counter()
        env = {"HOME": "/tmp", "CI": "true", "PYTHONDONTWRITEBYTECODE": "1"}  # noqa: S108 - in-container path
        code, out = await asyncio.to_thread(
            self._c.exec_run,
            wrapped,
            workdir=WORKDIR,
            user=self._s.user,
            demux=False,
            environment=env,
        )
        duration = time.perf_counter() - started
        SANDBOX_RUNS.labels(phase="network" if network else "offline").observe(duration)
        text = (out or b"").decode(errors="replace")
        return ExecResult(code, tail(text), duration, timed_out=code == TIMEOUT_EXIT)

    async def read_file(self, path: str) -> bytes | None:
        target = path if path.startswith("/") else f"{WORKDIR}/{path}"
        try:
            stream, _ = await asyncio.to_thread(self._c.get_archive, target)
        except NotFound:
            return None
        buf = io.BytesIO(b"".join(stream))
        with tarfile.open(fileobj=buf) as tar:
            member = tar.next()
            f = tar.extractfile(member) if member else None
            return f.read() if f else None


class DockerSandbox:
    def __init__(self, settings: SandboxSettings) -> None:
        self._s = settings
        self._client = docker.from_env()

    def _network(self) -> Any:
        try:
            return self._client.networks.get(self._s.setup_network)
        except NotFound:
            return self._client.networks.create(self._s.setup_network, driver="bridge")

    @asynccontextmanager
    async def session(
        self, archive: bytes, *, image: str | None = None
    ) -> AsyncIterator[DockerSandboxSession]:
        name = f"mergeagent-sbx-{uuid.uuid4().hex[:12]}"
        net = await asyncio.to_thread(self._network)
        container = await asyncio.to_thread(
            self._client.containers.create,
            image or self._s.image,
            command=["sleep", "infinity"],
            name=name,
            working_dir=WORKDIR,
            user="0:0",  # only for the initial chown below; commands run as settings.user
            network=self._s.setup_network,
            mem_limit=self._s.memory,
            memswap_limit=self._s.memory,
            nano_cpus=int(self._s.cpus * 1e9),
            pids_limit=self._s.pids_limit,
            cap_drop=["ALL"],
            cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"],
            security_opt=["no-new-privileges:true"],
            runtime=self._s.runtime,
            labels={"mergeagent.sandbox": "1"},
            auto_remove=False,
        )
        try:
            await asyncio.to_thread(container.start)
            # Cut egress before any repository content lands in the container; it is
            # re-attached only for explicit network=True (dependency setup) commands.
            await asyncio.to_thread(net.disconnect, container, force=True)
            await asyncio.to_thread(container.exec_run, ["mkdir", "-p", WORKDIR])
            await asyncio.to_thread(container.put_archive, WORKDIR, archive)
            await asyncio.to_thread(container.exec_run, ["chown", "-R", self._s.user, WORKDIR])
            yield DockerSandboxSession(container, net, self._s)
        finally:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception as exc:  # pragma: no cover - best effort cleanup
                log.warning("sandbox.cleanup_failed", container=name, error=str(exc))
