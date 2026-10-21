"""Generic agent loop: LLM gateway <-> scoped MCP toolbox."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from mergeagent.llm.gateway import LLMGateway
from mergeagent.llm.providers.base import Message
from mergeagent.llm.router import ModelChoice, Task
from mergeagent.mcp_servers.registry import ScopedToolbox, ToolScopeError


@dataclass
class AgentRun:
    final_text: str
    steps: int
    tool_calls: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    hit_step_limit: bool = False


async def run_agent(
    *,
    gateway: LLMGateway,
    toolbox: ScopedToolbox,
    job_id: str,
    task: Task,
    system: str,
    prompt: str,
    complexity: float,
    attempt: int,
    max_steps: int,
) -> AgentRun:
    messages: list[Message] = [{"role": "user", "content": prompt}]
    run = AgentRun(final_text="", steps=0)
    choice: ModelChoice | None = None  # pin the model for the whole loop (keeps the cache warm)
    for step in range(1, max_steps + 1):
        run.steps = step
        response, choice = await gateway.complete(
            job_id=job_id, agent=toolbox.agent, task=task, system=system, messages=messages,
            tools=toolbox.tools, complexity=complexity, attempt=attempt, choice=choice,
        )  # fmt: skip
        run.models.append(choice.model)
        messages.append(response.assistant_message())
        if not response.tool_calls:
            run.final_text = response.text
            return run
        results: list[dict[str, Any]] = []
        for call in response.tool_calls:  # all results go back in ONE user message
            run.tool_calls.append(call.name)
            try:
                res = await toolbox.call(call.name, call.arguments)
                text, is_error = res.text, res.is_error
            except ToolScopeError as exc:
                text, is_error = str(exc), True
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": text,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})
    run.hit_step_limit = True
    return run


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Parse the JSON object an agent was asked to finish with (tolerates fences/prose)."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for cand in candidates:
        start, end = cand.find("{"), cand.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cand[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in agent output")
