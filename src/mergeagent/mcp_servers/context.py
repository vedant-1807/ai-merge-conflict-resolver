"""Per-job state shared by the MCP servers. Servers are built per job and bound to it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from mergeagent.conflicts.models import HunkResolution, MergeConflictSet
from mergeagent.rag.store import CodeStore
from mergeagent.workspace.git_workspace import GitWorkspace, MergeOutcome


class IssueContext(Protocol):
    async def pull_request(self) -> dict[str, Any]: ...
    async def linked_issues(self) -> list[dict[str, Any]]: ...


@dataclass
class StaticIssueContext:
    """Issue context supplied up-front (CLI, eval harness)."""

    pr: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    async def pull_request(self) -> dict[str, Any]:
        return self.pr

    async def linked_issues(self) -> list[dict[str, Any]]:
        return self.issues


@dataclass
class JobContext:
    job_id: str
    repo: str
    ws: GitWorkspace
    outcome: MergeOutcome
    issues: IssueContext
    code_store: CodeStore
    resolutions: dict[str, HunkResolution] = field(default_factory=dict)
    # Set by the verifier node: runs the regression pipeline on the current candidate.
    run_verification: Callable[[], Awaitable[dict[str, Any]]] | None = None
    last_verification: dict[str, Any] | None = None

    @property
    def conflicts(self) -> MergeConflictSet:
        return self.outcome.conflicts
