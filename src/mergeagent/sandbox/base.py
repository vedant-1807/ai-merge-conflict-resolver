"""Sandbox abstraction: run untrusted repository code (setup, build, tests) in isolation."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

LOG_TAIL_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    output: str  # combined stdout+stderr, tail-truncated
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def tail(text: str, limit: int = LOG_TAIL_CHARS) -> str:
    return text if len(text) <= limit else "...[truncated]...\n" + text[-limit:]


class SandboxSession(Protocol):
    async def exec(self, command: str, *, timeout_s: int, network: bool = False) -> ExecResult: ...
    async def read_file(self, path: str) -> bytes | None: ...


class Sandbox(Protocol):
    def session(
        self, archive: bytes, *, image: str | None = None
    ) -> AbstractAsyncContextManager[SandboxSession]: ...
