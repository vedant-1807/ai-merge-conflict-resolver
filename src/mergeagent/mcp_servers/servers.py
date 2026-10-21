"""MCP servers exposed to the agents: git, tests, issues and rag.

Each factory returns an ``MCPServer`` bound to one job. They can be served over stdio or
streamable HTTP like any MCP server; the worker connects to them in-process (``mcp.Client``),
which avoids a network hop while keeping the exact MCP contract (schemas, errors, results).
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mergeagent.conflicts.applier import ResolutionError, apply_resolutions
from mergeagent.conflicts.models import FileConflictKind, HunkResolution
from mergeagent.conflicts.parser import has_conflict_markers
from mergeagent.conflicts.validators import syntax_error
from mergeagent.mcp_servers.context import JobContext

MAX_OUT = 12_000


def _clip(text: str, limit: int = MAX_OUT) -> str:
    return (
        text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    )


def _side_ref(ctx: JobContext, side: str) -> str:
    match side:
        case "ours" | "head":
            return ctx.outcome.ours_sha
        case "theirs" | "target":
            return ctx.outcome.theirs_sha
        case "base" | "ancestor":
            if not ctx.outcome.merge_base:
                raise ToolError("no merge base available")
            return ctx.outcome.merge_base
    raise ToolError("side must be one of: ours, theirs, base")


def _hunk(ctx: JobContext, hunk_id: str) -> Any:
    try:
        return ctx.conflicts.hunk(hunk_id)
    except (KeyError, IndexError, ValueError) as exc:
        raise ToolError(f"unknown hunk id {hunk_id!r}; call list_conflicts") from exc


# --------------------------------------------------------------------------- git
def build_git_server(ctx: JobContext) -> MCPServer:
    srv = MCPServer("git", instructions="Read-only git history plus conflict resolution writes.")

    @srv.tool()
    async def list_conflicts() -> str:
        """List every conflicted file and hunk id, with resolution status."""
        rows = []
        for f in ctx.conflicts.files:
            for h in f.hunks:
                status = "resolved" if h.id in ctx.resolutions else "open"
                rows.append(
                    {
                        "hunk_id": h.id,
                        "kind": f.kind.value,
                        "lines": f"{h.start_line}-{h.end_line}",
                        "status": status,
                    }
                )
        return json.dumps(rows, indent=1)

    @srv.tool()
    async def show_hunk(hunk_id: str) -> str:
        """Show one conflict hunk: PR side (ours), common ancestor (base), target side (theirs), with context."""
        return _clip(_hunk(ctx, hunk_id).render())

    @srv.tool()
    async def read_file(
        path: str, side: str = "ours", start_line: int = 1, end_line: int = 400
    ) -> str:
        """Read lines of a file as it exists on a side: 'ours' (PR head), 'theirs' (target branch) or 'base'."""
        try:
            text = await ctx.ws.read_file(_side_ref(ctx, side), path)
        except FileNotFoundError as exc:
            raise ToolError(str(exc)) from exc
        lines = text.splitlines()
        start, end = max(start_line, 1), min(end_line, len(lines))
        return _clip("\n".join(f"{i:>5} {lines[i - 1]}" for i in range(start, end + 1)))

    @srv.tool()
    async def history_for_hunk(hunk_id: str) -> str:
        """Commits on each side that last touched the lines of this hunk (git log -L), with messages."""
        h = _hunk(ctx, hunk_id)
        out = []
        for side in ("ours", "theirs"):
            text = await ctx.ws.read_file(_side_ref(ctx, side), h.path) if h.path else ""
            needle = (h.ours if side == "ours" else h.theirs).strip().splitlines()
            start = 1
            if needle:
                for i, line in enumerate(text.splitlines(), start=1):
                    if line.strip() == needle[0].strip():
                        start = i
                        break
            end = start + max(len(needle) - 1, 0)
            log = await ctx.ws.log_for_lines(_side_ref(ctx, side), h.path, start, end)
            out.append(f"== {side} ==\n{log or '(no history found)'}")
        return _clip("\n".join(out))

    @srv.tool()
    async def diff_sides(path: str) -> str:
        """Unified diff of a file from the merge base to each side, to see what each branch intended."""
        base = ctx.outcome.merge_base
        if not base:
            raise ToolError("no merge base available")
        ours = await ctx.ws.diff_refs(base, ctx.outcome.ours_sha, path)
        theirs = await ctx.ws.diff_refs(base, ctx.outcome.theirs_sha, path)
        return _clip(f"== base -> ours (PR) ==\n{ours}\n== base -> theirs (target) ==\n{theirs}")

    @srv.tool()
    async def write_resolution(
        hunk_id: str, content: str, rationale: str, delete_file: bool = False
    ) -> str:
        """Record the resolved text for one hunk (replaces the whole conflict region, markers excluded).
        For modify/delete conflicts set delete_file=true to delete the file instead."""
        h = _hunk(ctx, hunk_id)
        cf = ctx.conflicts.file(h.path)
        if delete_file and cf.kind is not FileConflictKind.DELETE_MODIFY:
            raise ToolError("delete_file is only allowed for modify/delete conflicts")
        if has_conflict_markers(content):
            raise ToolError("content still contains conflict markers")
        ctx.resolutions[hunk_id] = HunkResolution(
            hunk_id=hunk_id,
            text=content,
            strategy="llm",
            rationale=rationale,
            delete_file=delete_file,
        )
        # If the whole file is now resolved, validate it immediately so errors surface early.
        if all(x.id in ctx.resolutions for x in cf.hunks):
            try:
                merged = apply_resolutions(cf, {x.index: ctx.resolutions[x.id] for x in cf.hunks})
            except ResolutionError as exc:
                raise ToolError(str(exc)) from exc
            if merged is not None and (err := syntax_error(cf.path, merged)):
                del ctx.resolutions[hunk_id]
                raise ToolError(f"resolution rejected, file no longer parses: {err}")
            return f"recorded {hunk_id}; {cf.path} fully resolved and parses"
        return f"recorded {hunk_id}"

    @srv.tool()
    async def resolution_diff() -> str:
        """Diff of the current candidate resolution against the PR head (what reviewers will see)."""
        paths = sorted({h.path for h in ctx.conflicts.hunks})
        return _clip(await ctx.ws.resolution_diff(paths) or "(candidate not materialised yet)")

    return srv


# --------------------------------------------------------------------------- tests
def build_tests_server(ctx: JobContext) -> MCPServer:
    srv = MCPServer("tests", instructions="Regression verification inside the isolated sandbox.")

    @srv.tool()
    async def run_verification() -> str:
        """Run setup, build, typecheck and tests for both parents and the candidate merge in fresh sandboxes
        and compare them. Returns the verdict, per-gate results and any regressions."""
        if ctx.run_verification is None:
            raise ToolError("verification is not available in this context")
        ctx.last_verification = await ctx.run_verification()
        rep = ctx.last_verification
        brief = {k: rep[k] for k in ("verdict", "gates", "reasons")}
        if rep.get("comparison"):
            c = rep["comparison"]
            brief["tests"] = {
                k: c[k]
                for k in ("regressions", "lost", "fixed", "candidate_passed", "candidate_failed")
            }
        return json.dumps(brief, indent=1)

    @srv.tool()
    async def get_step_log(snapshot: str, step: str) -> str:
        """Tail of a step's output. snapshot: base | head | candidate; step: setup | build | typecheck | test."""
        rep = ctx.last_verification
        if rep is None:
            raise ToolError("run_verification first")
        run = rep["runs"].get(snapshot)
        if (
            run is None
            or step not in {"setup", "build", "typecheck", "test"}
            or run.get(step) is None
        ):
            raise ToolError(f"no {step} log for {snapshot}")
        return _clip(run[step]["log_tail"])

    return srv


# --------------------------------------------------------------------------- issues
_ISSUE_REF = re.compile(r"(?i)(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)?\s*#(\d+)")


def build_issues_server(ctx: JobContext) -> MCPServer:
    srv = MCPServer("issues", instructions="Pull request and linked issue context.")

    @srv.tool()
    async def get_pull_request() -> str:
        """Title, description, author and labels of the pull request being merged."""
        return _clip(json.dumps(await ctx.issues.pull_request(), indent=1, default=str))

    @srv.tool()
    async def get_linked_issues() -> str:
        """Issues referenced by the PR (e.g. 'closes #12'), with title and body: the requirement each side implements."""
        return _clip(json.dumps(await ctx.issues.linked_issues(), indent=1, default=str))

    @srv.tool()
    async def get_commit_messages(side: str = "ours") -> str:
        """Commit messages unique to one side since the merge base: 'ours' (PR) or 'theirs' (target)."""
        base = ctx.outcome.merge_base
        if not base:
            raise ToolError("no merge base available")
        return _clip(await ctx.ws.commits_between(base, _side_ref(ctx, side)))

    return srv


# --------------------------------------------------------------------------- rag
def build_rag_server(ctx: JobContext) -> MCPServer:
    srv = MCPServer("rag", instructions="Semantic + keyword search over the repository's code.")

    @srv.tool()
    async def search_code(query: str, k: int = 6) -> str:
        """Hybrid semantic/keyword search over the repository (target branch). Use to find callers,
        related implementations and conventions."""
        hits = await ctx.code_store.search(ctx.repo, query, k=min(max(k, 1), 12))
        return _clip("\n\n".join(
            f"## {h.path}:{h.start_line}-{h.end_line} {h.symbol or ''} (score {h.score:.3f})\n{h.content}"
            for h in hits
        ) or "no results")  # fmt: skip

    @srv.tool()
    async def find_symbol_usages(symbol: str) -> str:
        """Find code that references a function/class/variable name (callers that a resolution must keep working)."""
        hits = await ctx.code_store.search(ctx.repo, symbol, k=12)
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        rows = []
        for h in hits:
            for i, line in enumerate(h.content.splitlines(), start=h.start_line):
                if pattern.search(line):
                    rows.append(f"{h.path}:{i}: {line.strip()}")
        return _clip("\n".join(dict.fromkeys(rows)) or "no usages found")

    return srv
