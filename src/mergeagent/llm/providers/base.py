"""Provider-neutral request/response types.

Messages use the Anthropic Messages API shape (a list of ``{"role", "content"}`` dicts whose
content is a string or a list of typed blocks). Assistant turns are replayed verbatim from
``LLMResponse.raw_content`` so thinking blocks and tool_use ids round-trip unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from mergeagent.llm.pricing import Usage

Message = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMRequest:
    model: str
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 16_000
    effort: str | None = None  # low | medium | high | xhigh | max


@dataclass(slots=True)
class LLMResponse:
    model: str
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage
    raw_content: list[dict[str, Any]]
    request_id: str | None = None

    def assistant_message(self) -> Message:
        return {"role": "assistant", "content": self.raw_content}


class ProviderError(RuntimeError):
    """Non-retryable provider failure (after the SDK's own retries)."""


class RefusalError(ProviderError):
    def __init__(self, category: str | None) -> None:
        super().__init__(f"model declined the request (category={category})")
        self.category = category


class LLMProvider(Protocol):
    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...
