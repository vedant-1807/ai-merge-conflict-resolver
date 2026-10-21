"""Benchmark: run the full pipeline on mined scenarios.

Per scenario we record whether a **test-verified** resolution was produced, how many attempts
and LLM calls it took, the cost, and how close the result is to the human merge commit.
"""

from __future__ import annotations

import difflib
import tempfile
import time
from pathlib import Path
from typing import Any

from mergeagent.agents.publisher import PublishResult
from mergeagent.config import Settings
from mergeagent.eval.scenarios import Scenario, append_jsonl, mirror, read_jsonl
from mergeagent.llm.budget import InMemoryBudget
from mergeagent.llm.gateway import MemoryRecorder
from mergeagent.mcp_servers.context import JobContext, StaticIssueContext
from mergeagent.rag.store import MemoryVectorStore
from mergeagent.service import (
    ResolutionRequest,
    build_embedder,
    build_provider,
    build_router,
    build_sandbox,
    resolve_workspace,
)
from mergeagent.verification.pipeline import MemoryBaselineCache
from mergeagent.workspace.git_workspace import GitWorkspace


class EvalPublisher:
    """Captures the final files and compares them with the human resolution."""

    def __init__(self, merge_sha: str) -> None:
        self.merge_sha = merge_sha
        self.similarity: float | None = None
        self.exact: bool | None = None

    async def _compare(self, ctx: JobContext) -> None:
        ratios, exact = [], True
        for cf in ctx.conflicts.files:
            try:
                human = await ctx.ws.read_file(self.merge_sha, cf.path)
            except FileNotFoundError:
                human = ""
            p = ctx.ws.root / cf.path
            ours = p.read_text() if p.exists() else ""
            ratios.append(difflib.SequenceMatcher(a=human, b=ours, autojunk=False).ratio())
            exact &= human == ours
        self.similarity = sum(ratios) / len(ratios) if ratios else None
        self.exact = exact

    async def publish(self, ctx: JobContext, summary: dict[str, Any], patch: str) -> PublishResult:
        await self._compare(ctx)
        return PublishResult(None, "eval: verified")

    async def report_failure(
        self, ctx: JobContext, summary: dict[str, Any], patch: str
    ) -> PublishResult:
        await self._compare(ctx)
        return PublishResult(None, "eval: unverified")


async def run_scenario(
    settings: Settings, s: Scenario, cache_dir: Path, fixed_model: str | None
) -> dict[str, Any]:
    src = mirror(s.url, cache_dir)
    ws = await GitWorkspace.clone(
        str(src), Path(tempfile.mkdtemp()) / "ws", refs=[s.ours, s.theirs, s.merge_sha]
    )
    pub = EvalPublisher(s.merge_sha)
    rec = MemoryRecorder()
    started = time.perf_counter()
    try:
        outcome = await ws.start_merge(s.ours, s.theirs)
        final = await resolve_workspace(
            ResolutionRequest(f"eval-{s.merge_sha[:10]}", s.repo, ws, outcome, StaticIssueContext(), pub),
            settings=settings, provider=build_provider(settings), router=build_router(settings, fixed_model),
            budget=InMemoryBudget(), recorder=rec, store=MemoryVectorStore(build_embedder(settings)),
            sandbox=build_sandbox(settings), baseline_cache=MemoryBaselineCache(),
        )  # fmt: skip
        status, error = final.get("status"), None
        attempts = int(final.get("attempt", 1))
        verdict = (final.get("verification") or {}).get("verdict")
        categories = [h["category"] for h in final.get("hunks", [])]
    except Exception as exc:  # recorded, never aborts the benchmark
        status, error, attempts, verdict, categories = "error", str(exc)[:500], 0, None, []
    finally:
        ws.cleanup()
    return {
        "scenario": s.id, "repo": s.repo, "hunks": s.hunks, "status": status, "verdict": verdict,
        "verified": status == "resolved", "attempts": attempts, "llm_calls": len(rec.calls),
        "cost_usd": round(rec.total_cost, 6), "cost_per_attempt": round(rec.total_cost / attempts, 6) if attempts else None,
        "models": sorted({c.model for c in rec.calls}), "similarity": pub.similarity, "exact_match": pub.exact,
        "categories": categories, "duration_s": round(time.perf_counter() - started, 1),
        "mode": f"fixed:{fixed_model}" if fixed_model else "routed", "error": error,
    }  # fmt: skip


async def run_benchmark(
    settings: Settings,
    scenarios_path: Path,
    out: Path,
    cache_dir: Path,
    *,
    fixed_model: str | None,
    limit: int | None,
) -> int:
    done = {r["scenario"] for r in read_jsonl(out)}  # resumable: skip finished scenarios
    rows = [Scenario(**r) for r in read_jsonl(scenarios_path)][:limit]
    for s in rows:
        if s.id in done:
            continue
        append_jsonl(out, await run_scenario(settings, s, cache_dir, fixed_model))
    return len(rows)
