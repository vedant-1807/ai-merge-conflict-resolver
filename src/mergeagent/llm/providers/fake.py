"""Deterministic provider for tests, CI and offline demos.

Behaviour is supplied by a ``responder`` callable that receives the request and returns either
a final text answer or a list of tool calls. Token usage is estimated from character counts so
budget and cost accounting are exercised exactly as with a real provider.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mergeagent.llm.pricing import Usage
from mergeagent.llm.providers.base import LLMRequest, LLMResponse, ToolCall


@dataclass(frozen=True, slots=True)
class FakeTurn:
    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


Responder = Callable[[LLMRequest], FakeTurn]


def _chars(obj: Any) -> int:
    return len(obj) if isinstance(obj, str) else len(json.dumps(obj, default=str))


class FakeProvider:
    name = "fake"

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        turn = self._responder(request)
        calls = [ToolCall(f"toolu_{uuid.uuid4().hex[:12]}", n, a) for n, a in turn.tool_calls]
        raw: list[dict[str, Any]] = []
        if turn.text:
            raw.append({"type": "text", "text": turn.text})
        raw += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in calls
        ]
        prompt_chars = _chars(request.system) + _chars(request.messages)
        out_chars = _chars(turn.text) + _chars([c.arguments for c in calls])
        return LLMResponse(
            model=request.model,
            text=turn.text,
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            usage=Usage(input_tokens=prompt_chars // 4 + 1, output_tokens=out_chars // 4 + 1),
            raw_content=raw,
        )
