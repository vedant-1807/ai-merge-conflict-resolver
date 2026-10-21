import json
from pathlib import Path

import pytest

from mergeagent.mcp_servers.context import JobContext, StaticIssueContext
from mergeagent.mcp_servers.registry import ScopedToolbox, ToolScopeError
from mergeagent.rag.embeddings import HashingEmbedder
from mergeagent.rag.store import MemoryVectorStore
from mergeagent.workspace.git_workspace import GitWorkspace


@pytest.fixture
async def ctx(conflict_repo: Path, tmp_path: Path) -> JobContext:
    ws = await GitWorkspace.clone(str(conflict_repo), tmp_path / "ws", refs=["main", "feature"])
    outcome = await ws.start_merge(await ws.resolve("feature"), await ws.resolve("main"))
    store = MemoryVectorStore(HashingEmbedder(256))
    files = {
        p: await ws.read_file(outcome.theirs_sha, p)
        for p in await ws.list_files(outcome.theirs_sha)
    }
    await store.index_files("o/r", files)
    issues = StaticIssueContext(
        pr={"number": 7, "title": "Add discounts"},
        issues=[{"number": 7, "title": "Support discounts"}],
    )
    return JobContext("job-1", "o/r", ws, outcome, issues, store)


async def test_scoped_tool_listing(ctx: JobContext) -> None:
    async with ScopedToolbox("analyzer", ctx) as tb:
        names = {t.name for t in tb.tools}
    assert "git__show_hunk" in names
    assert "issues__get_linked_issues" in names
    assert "rag__search_code" in names
    assert "git__write_resolution" not in names  # analyzer is read-only
    assert not any(n.startswith("tests__") for n in names)

    async with ScopedToolbox("resolver", ctx) as tb:
        names = {t.name for t in tb.tools}
    assert "git__write_resolution" in names
    assert not any(n.startswith(("issues__", "tests__")) for n in names)


async def test_scope_enforced_on_call(ctx: JobContext) -> None:
    async with ScopedToolbox("analyzer", ctx) as tb:
        with pytest.raises(ToolScopeError):
            await tb.call(
                "git__write_resolution", {"hunk_id": "calc.py#0", "content": "x", "rationale": "r"}
            )
        with pytest.raises(ToolScopeError):
            await tb.call("tests__run_verification", {})
    with pytest.raises(ValueError, match="unknown agent"):
        ScopedToolbox("root", ctx)


async def test_git_and_issue_tools(ctx: JobContext) -> None:
    async with ScopedToolbox("analyzer", ctx) as tb:
        listing = json.loads((await tb.call("git__list_conflicts", {})).text)
        assert {r["hunk_id"] for r in listing} >= {"calc.py#0", "legacy.py#0"}
        hunk = await tb.call("git__show_hunk", {"hunk_id": "calc.py#0"})
        assert "OURS" in hunk.text
        assert "discount" in hunk.text
        missing = await tb.call("git__show_hunk", {"hunk_id": "nope.py#0"})
        assert missing.is_error
        f = await tb.call("git__read_file", {"path": "calc.py", "side": "theirs"})
        assert "tax" in f.text
        assert "closes #7" in (await tb.call("issues__get_commit_messages", {"side": "ours"})).text
        assert "Support discounts" in (await tb.call("issues__get_linked_issues", {})).text
        hits = await tb.call("rag__search_code", {"query": "count prices"})
        assert "calc.py" in hits.text
        assert "calc.py" in (await tb.call("rag__find_symbol_usages", {"symbol": "total"})).text


async def test_write_resolution_validates(ctx: JobContext) -> None:
    async with ScopedToolbox("resolver", ctx) as tb:
        bad = await tb.call(
            "git__write_resolution",
            {"hunk_id": "calc.py#0", "content": "def total(:\n", "rationale": "x"},
        )
        assert bad.is_error
        assert "parses" in bad.text
        assert "calc.py#0" not in ctx.resolutions
        markers = await tb.call(
            "git__write_resolution",
            {"hunk_id": "calc.py#0", "content": "<<<<<<< HEAD\n", "rationale": "x"},
        )
        assert markers.is_error
        ok = await tb.call(
            "git__write_resolution",
            {
                "hunk_id": "calc.py#0",
                "content": "def total(prices):\n    return 1\n",
                "rationale": "x",
            },
        )
        assert not ok.is_error
        assert "fully resolved" in ok.text
        not_dm = await tb.call(
            "git__write_resolution",
            {"hunk_id": "calc.py#0", "content": "", "rationale": "x", "delete_file": True},
        )
        assert not_dm.is_error
