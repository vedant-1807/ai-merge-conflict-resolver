"""LLM gateway: the single choke point every agent uses to reach a model.

Responsibilities, in order, for each call:
1. route to a model tier (task, hunk complexity, attempt number, remaining budget);
2. redact secrets from all user-side content;
3. reserve the worst-case cost against the job budget (atomic, fails fast);
4. call the provider and measure latency;
5. settle the actual cost, record the call, emit metrics.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from mergeagent.llm.budget import Budget, BudgetExceededError
from mergeagent.llm.pricing import cost_usd, estimate_max_cost
from mergeagent.llm.providers.base import LLMProvider, LLMRequest, LLMResponse, Message, ToolSpec
from mergeagent.llm.redaction import Redactor
from mergeagent.llm.router import ModelChoice, ModelRouter, RouteContext, Task
from mergeagent.logging import get_logger
from mergeagent.observability import metrics

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    job_id: str
    agent: str
    task: str
    model: str
    route_reason: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    latency_ms: int
    redactions: int
    status: str
    error: str | None = None


class CallRecorder(Protocol):
    async def record(self, call: LLMCallRecord) -> None: ...


@dataclass
class MemoryRecorder:
    calls: list[LLMCallRecord] = field(default_factory=list)

    async def record(self, call: LLMCallRecord) -> None:
        self.calls.append(call)

    @property
    def total_cost(self) -> float:
        return sum(c.cost_usd for c in self.calls)


def _redact_messages(messages: list[Message], redactor: Redactor) -> list[Message]:
    """Redact user-side text and tool results. Assistant turns are replayed untouched so
    thinking-block signatures and tool_use ids stay valid."""

    def block(b: Any) -> Any:
        if not isinstance(b, dict):
            return b
        if b.get("type") == "text":
            return {**b, "text": redactor.redact(b["text"])}
        if b.get("type") == "tool_result":
            content = b.get("content")
            if isinstance(content, str):
                return {**b, "content": redactor.redact(content)}
            if isinstance(content, list):
                return {**b, "content": [block(c) for c in content]}
        return b

    out: list[Message] = []
    for m in messages:
        if m["role"] != "user":
            out.append(m)
        elif isinstance(m["content"], str):
            out.append({**m, "content": redactor.redact(m["content"])})
        else:
            out.append({**m, "content": [block(b) for b in m["content"]]})
    return out


class LLMGateway:
    def __init__(
        self,
        provider: LLMProvider,
        router: ModelRouter,
        budget: Budget,
        recorder: CallRecorder,
    ) -> None:
        self.provider = provider
        self.router = router
        self.budget = budget
        self.recorder = recorder

    async def complete(
        self,
        *,
        job_id: str,
        agent: str,
        task: Task,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        complexity: float = 0.5,
        attempt: int = 1,
        choice: ModelChoice | None = None,
    ) -> tuple[LLMResponse, ModelChoice]:
        snap = await self.budget.snapshot(job_id)
        choice = choice or self.router.choose(
            RouteContext(task, complexity, attempt, snap.remaining_usd)
        )

        redactor = Redactor()
        safe_system = redactor.redact(system)
        safe_messages = _redact_messages(messages, redactor)
        request = LLMRequest(
            model=choice.model,
            system=safe_system,
            messages=safe_messages,
            tools=tools or [],
            max_tokens=choice.max_tokens,
            effort=choice.effort,
        )

        prompt_chars = len(safe_system) + len(json.dumps(safe_messages, default=str))
        prompt_chars += sum(
            len(json.dumps(t.input_schema)) + len(t.description) for t in request.tools
        )
        try:
            reservation = await self.budget.reserve(
                job_id, estimate_max_cost(choice.model, prompt_chars, choice.max_tokens)
            )
        except BudgetExceededError:
            metrics.BUDGET_REJECTIONS.inc()
            raise

        started = time.perf_counter()
        try:
            response = await self.provider.complete(request)
        except Exception as exc:
            await self.budget.settle(reservation, 0.0)
            metrics.LLM_CALLS.labels(choice.model, agent, "error").inc()
            await self.recorder.record(
                LLMCallRecord(
                    job_id, agent, task.value, choice.model, choice.reason, 0, 0, 0, 0, 0.0,
                    int((time.perf_counter() - started) * 1000), redactor.total, "error", str(exc),
                )
            )  # fmt: skip
            raise

        latency = time.perf_counter() - started
        cost = cost_usd(choice.model, response.usage)
        await self.budget.settle(reservation, cost)
        u = response.usage
        metrics.LLM_CALLS.labels(choice.model, agent, "ok").inc()
        metrics.LLM_COST.labels(choice.model, agent).inc(cost)
        metrics.LLM_LATENCY.labels(choice.model).observe(latency)
        for kind, n in (("input", u.input_tokens), ("output", u.output_tokens),
                        ("cache_read", u.cache_read_tokens), ("cache_write", u.cache_write_tokens)):  # fmt: skip
            metrics.LLM_TOKENS.labels(choice.model, kind).inc(n)
        await self.recorder.record(
            LLMCallRecord(
                job_id, agent, task.value, choice.model, choice.reason, u.input_tokens,
                u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, cost,
                int(latency * 1000), redactor.total, "ok",
            )
        )  # fmt: skip
        log.info(
            "llm.call",
            agent=agent,
            model=choice.model,
            cost=round(cost, 5),
            redactions=redactor.total,
        )
        return response, choice
