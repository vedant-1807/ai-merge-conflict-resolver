"""Agent-scoped MCP toolboxes.

Least privilege: each agent connects only to the servers it needs and sees only the tools on
its allowlist. The analyzer cannot write resolutions; the resolver cannot run the sandbox; the
verifier cannot change the patch it is judging. A call outside the scope is refused before it
reaches any server, and is counted in metrics.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer

from mergeagent.llm.providers.base import ToolSpec
from mergeagent.mcp_servers.context import JobContext
from mergeagent.mcp_servers.servers import (
    build_git_server,
    build_issues_server,
    build_rag_server,
    build_tests_server,
)
from mergeagent.observability.metrics import TOOL_CALLS

SEP = "__"  # namespaced tool names: "<server>__<tool>" (Anthropic tool names allow [a-zA-Z0-9_-])
ALL = frozenset({"*"})
GIT_READ = frozenset({"list_conflicts", "show_hunk", "read_file", "history_for_hunk", "diff_sides"})

SERVER_FACTORIES: dict[str, Callable[[JobContext], MCPServer]] = {
    "git": build_git_server,
    "tests": build_tests_server,
    "issues": build_issues_server,
    "rag": build_rag_server,
}

AGENT_SCOPES: dict[str, Mapping[str, frozenset[str]]] = {
    "analyzer": {"git": GIT_READ, "issues": ALL, "rag": ALL},
    "resolver": {"git": GIT_READ | {"write_resolution"}, "rag": ALL},
    "verifier": {"tests": ALL, "git": frozenset({"show_hunk", "resolution_diff"})},
}


class ToolScopeError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class ToolResult:
    text: str
    is_error: bool


class ScopedToolbox:
    def __init__(self, agent: str, ctx: JobContext) -> None:
        if agent not in AGENT_SCOPES:
            raise ValueError(f"unknown agent {agent!r}")
        self.agent = agent
        self._ctx = ctx
        self._scope = AGENT_SCOPES[agent]
        self._clients: dict[str, Client] = {}
        self._stack = AsyncExitStack()
        self._specs: list[ToolSpec] = []

    def allowed(self, server: str, tool: str) -> bool:
        names = self._scope.get(server)
        return names is not None and (names == ALL or tool in names)

    async def __aenter__(self) -> ScopedToolbox:
        for server in self._scope:
            client = Client(SERVER_FACTORIES[server](self._ctx))
            self._clients[server] = await self._stack.enter_async_context(client)
            for tool in (await self._clients[server].list_tools()).tools:
                if self.allowed(server, tool.name):
                    self._specs.append(
                        ToolSpec(
                            f"{server}{SEP}{tool.name}",
                            tool.description or "",
                            dict(tool.input_schema),
                        )
                    )
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._stack.aclose()

    @property
    def tools(self) -> list[ToolSpec]:
        return list(self._specs)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        server, _, tool = name.partition(SEP)
        if not self.allowed(server, tool) or server not in self._clients:
            TOOL_CALLS.labels(self.agent, name, "denied").inc()
            raise ToolScopeError(f"agent {self.agent!r} is not allowed to call {name!r}")
        result = await self._clients[server].call_tool(tool, arguments)
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        TOOL_CALLS.labels(self.agent, name, "error" if result.is_error else "ok").inc()
        return ToolResult(text, bool(result.is_error))
