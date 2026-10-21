"""Per-model token prices (USD per 1M tokens) and cost calculation.

Prices are Anthropic first-party list prices. Cache writes (5-minute TTL) bill at 1.25x input.
Keep this table in sync with https://www.anthropic.com/pricing when models change.
"""

from __future__ import annotations

from dataclasses import dataclass

PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:
        return self.input * 1.25


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01),
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_read=0.20),
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_read=0.20),
    "claude-fable-5-1": ModelPrice(input=10.00, output=50.00, cache_read=0.25),
    # Deterministic test double.
    "fake-small": ModelPrice(input=0.10, output=0.50, cache_read=0.01),
    "fake-medium": ModelPrice(input=2.00, output=10.00, cache_read=0.20),
    "fake-large": ModelPrice(input=4.00, output=20.00, cache_read=0.20),
}


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def price_for(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError as exc:
        raise KeyError(f"no price configured for model {model!r}; add it to PRICES") from exc


def cost_usd(model: str, usage: Usage) -> float:
    p = price_for(model)
    return (
        usage.input_tokens * p.input
        + usage.output_tokens * p.output
        + usage.cache_read_tokens * p.cache_read
        + usage.cache_write_tokens * p.cache_write
    ) / PER_MILLION


def estimate_max_cost(model: str, prompt_chars: int, max_output_tokens: int) -> float:
    """Upper-bound cost used to reserve budget before a call (~3.5 chars per token)."""
    p = price_for(model)
    est_input = int(prompt_chars / 3.5) + 1
    return (est_input * p.input + max_output_tokens * p.output) / PER_MILLION
