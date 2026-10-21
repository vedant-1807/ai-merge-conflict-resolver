"""Anthropic Claude provider (official ``anthropic`` SDK, async client).

* Prompt caching: the stable prefix (tool definitions + system prompt) is marked with
  ``cache_control`` so repeated calls inside one tool loop read it from cache at ~10% cost.
* Tool choice is ``auto``: current Opus/Sonnet models reject forced tool choice; the prompts
  instruct the model which tool to call instead.
* Retries for 408/409/429/5xx and connection errors are delegated to the SDK.
"""

from __future__ import annotations

from typing import Any

import anthropic

from mergeagent.llm.pricing import Usage
from mergeagent.llm.providers.base import (
    LLMRequest,
    LLMResponse,
    ProviderError,
    RefusalError,
    ToolCall,
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self, api_key: str | None = None, *, timeout_s: float = 300.0, max_retries: int = 3
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )

    @staticmethod
    def build_params(request: LLMRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": request.messages,
        }
        if request.tools:
            tools: list[dict[str, Any]] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
            tools[-1]["cache_control"] = {"type": "ephemeral"}
            params["tools"] = tools
            params["tool_choice"] = {"type": "auto"}
        if request.effort:
            params["output_config"] = {"effort": request.effort}
        return params

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            msg = await self._client.messages.create(**self.build_params(request))
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"bad request: {exc.message}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderError(f"authentication failed: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"connection error: {exc}") from exc

        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            raise RefusalError(getattr(details, "category", None))

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in msg.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))

        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        return LLMResponse(
            model=msg.model,
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=msg.stop_reason or "end_turn",
            usage=usage,
            raw_content=[b.to_dict() for b in msg.content],
            request_id=msg._request_id,
        )
