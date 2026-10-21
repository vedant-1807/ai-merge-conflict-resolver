"""LangGraph state (JSON-serialisable) and the non-serialisable dependencies of a run."""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from mergeagent.agents.publisher import Publisher
from mergeagent.config import AgentSettings
from mergeagent.llm.gateway import LLMGateway
from mergeagent.mcp_servers.context import JobContext
from mergeagent.verification.pipeline import VerificationPipeline
from mergeagent.verification.repo_config import RepoConfig


class HunkInfo(TypedDict):
    hunk_id: str
    category: str
    complexity: float
    strategy: str | None


class GraphState(TypedDict, total=False):
    attempt: int
    hunks: list[HunkInfo]
    pending: list[str]  # hunk ids that still need an LLM resolution
    intents: dict[str, Any]
    feedback: str
    verification: dict[str, Any] | None
    review: dict[str, Any] | None
    patch: str
    status: str  # resolved | failed_verification | needs_human | no_conflict
    reason: str
    result_url: str | None
    events: Annotated[list[str], operator.add]


@dataclass
class Deps:
    ctx: JobContext
    gateway: LLMGateway
    pipeline: VerificationPipeline
    publisher: Publisher
    repo_config: RepoConfig
    settings: AgentSettings
