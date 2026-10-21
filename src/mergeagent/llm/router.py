"""Cost-aware model routing.

Picks the cheapest model tier that is likely to succeed for a given task, escalates on retries,
and downgrades when the remaining job budget cannot afford the preferred tier. A ``fixed_model``
disables routing entirely: that mode is the baseline the eval harness compares against.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from mergeagent.llm.pricing import estimate_max_cost


class Task(StrEnum):
    TRIAGE = "triage"
    ANALYZE = "analyze"
    RESOLVE = "resolve"
    REVIEW = "review"
    SUMMARIZE = "summarize"


class Tier(IntEnum):
    SMALL = 0
    MEDIUM = 1
    LARGE = 2


EFFORT = {Tier.SMALL: "low", Tier.MEDIUM: "medium", Tier.LARGE: "high"}
# Typical prompt size (chars) per task, used to check affordability before choosing a tier.
TYPICAL_PROMPT_CHARS = {
    Task.TRIAGE: 6_000,
    Task.ANALYZE: 30_000,
    Task.RESOLVE: 40_000,
    Task.REVIEW: 30_000,
    Task.SUMMARIZE: 8_000,
}


@dataclass(frozen=True, slots=True)
class RouteContext:
    task: Task
    complexity: float = 0.5
    attempt: int = 1
    budget_remaining_usd: float | None = None


@dataclass(frozen=True, slots=True)
class ModelChoice:
    model: str
    tier: Tier | None
    effort: str | None
    max_tokens: int
    reason: str


class ModelRouter:
    def __init__(
        self,
        small: str,
        medium: str,
        large: str,
        *,
        fixed_model: str | None = None,
        max_output_tokens: int = 16_000,
    ) -> None:
        self.models = {Tier.SMALL: small, Tier.MEDIUM: medium, Tier.LARGE: large}
        self.fixed_model = fixed_model
        self.max_output_tokens = max_output_tokens

    def _preferred_tier(self, ctx: RouteContext) -> tuple[Tier, str]:
        c = ctx.complexity
        match ctx.task:
            case Task.TRIAGE | Task.SUMMARIZE:
                tier, why = Tier.SMALL, "classification/summary task"
            case Task.ANALYZE:
                tier, why = (
                    (Tier.SMALL, "low complexity") if c < 0.35 else (Tier.MEDIUM, "intent analysis")
                )
            case Task.RESOLVE:
                if c < 0.25:
                    tier, why = Tier.SMALL, f"easy hunk (complexity={c:.2f})"
                elif c < 0.6:
                    tier, why = Tier.MEDIUM, f"moderate hunk (complexity={c:.2f})"
                else:
                    tier, why = Tier.LARGE, f"hard hunk (complexity={c:.2f})"
            case Task.REVIEW:
                tier, why = (
                    (Tier.LARGE, "high-risk review") if c >= 0.7 else (Tier.MEDIUM, "review")
                )
        if ctx.attempt > 1 and ctx.task in (Task.RESOLVE, Task.REVIEW):
            escalated = Tier(min(tier + ctx.attempt - 1, Tier.LARGE))
            if escalated != tier:
                why += f"; escalated to {escalated.name.lower()} on attempt {ctx.attempt}"
            tier = escalated
        return tier, why

    def choose(self, ctx: RouteContext) -> ModelChoice:
        max_tokens = (
            self.max_output_tokens
            if ctx.task is Task.RESOLVE
            else min(self.max_output_tokens, 8_000)
        )
        if self.fixed_model:
            return ModelChoice(
                self.fixed_model, None, None, max_tokens, "fixed model (routing disabled)"
            )

        tier, why = self._preferred_tier(ctx)
        if ctx.budget_remaining_usd is not None:
            chars = TYPICAL_PROMPT_CHARS[ctx.task]
            while tier > Tier.SMALL and (
                estimate_max_cost(self.models[tier], chars, max_tokens) > ctx.budget_remaining_usd
            ):
                tier = Tier(tier - 1)
                why += f"; downgraded to {tier.name.lower()} to fit remaining budget"
        return ModelChoice(self.models[tier], tier, EFFORT[tier], max_tokens, why)
