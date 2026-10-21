"""Graph nodes. Each takes (state, deps) and returns a partial state update."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from mergeagent.agents import prompts
from mergeagent.agents.state import Deps, GraphState, HunkInfo
from mergeagent.agents.tool_loop import extract_json, run_agent
from mergeagent.conflicts.applier import ResolutionError, apply_resolutions
from mergeagent.conflicts.triage import triage_file
from mergeagent.conflicts.validators import static_problems
from mergeagent.llm.router import Task
from mergeagent.logging import get_logger
from mergeagent.mcp_servers.registry import ScopedToolbox
from mergeagent.observability.metrics import HUNKS
from mergeagent.verification.pipeline import Snapshot, Verdict

log = get_logger(__name__)


def _complexity(state: GraphState, ids: list[str]) -> float:
    by_id = {h["hunk_id"]: h["complexity"] for h in state.get("hunks", [])}
    return max((by_id.get(i, 0.5) for i in ids), default=0.5)


# --------------------------------------------------------------------------- triage
async def triage(state: GraphState, deps: Deps) -> dict[str, Any]:
    ctx = deps.ctx
    if ctx.outcome.clean:
        return {
            "status": "no_conflict",
            "reason": "branches merge cleanly",
            "events": ["triage: clean merge"],
        }
    if ctx.outcome.unsupported:
        return {
            "status": "needs_human",
            "reason": "unsupported conflicts (binary/rename): "
            + ", ".join(ctx.outcome.unsupported),
            "events": ["triage: unsupported conflict types"],
        }
    if len(ctx.conflicts.hunks) > deps.settings.max_hunks_per_job:
        return {"status": "needs_human", "reason": f"{len(ctx.conflicts.hunks)} hunks exceeds limit",
                "events": ["triage: too many hunks"]}  # fmt: skip

    hunks: list[HunkInfo] = []
    pending: list[str] = []
    for cf in ctx.conflicts.files:
        for r in triage_file(cf):
            HUNKS.labels(category=r.category.value).inc()
            if r.resolution is not None:
                ctx.resolutions[r.hunk.id] = r.resolution
            else:
                pending.append(r.hunk.id)
            hunks.append(HunkInfo(
                hunk_id=r.hunk.id, category=r.category.value, complexity=r.complexity,
                strategy=r.resolution.strategy if r.resolution else None,
            ))  # fmt: skip
    trivial = len(hunks) - len(pending)
    return {
        "hunks": hunks,
        "pending": pending,
        "attempt": 1,
        "events": [
            f"triage: {len(hunks)} hunks, {trivial} resolved deterministically, {len(pending)} need reasoning"
        ],
    }


# --------------------------------------------------------------------------- analyzer
async def analyze(state: GraphState, deps: Deps) -> dict[str, Any]:
    pending = state["pending"]
    ctx = deps.ctx
    prompt = (
        "Conflicted hunks needing analysis:\n"
        + "\n".join(f"- {h}" for h in pending)
        + "\n\nInvestigate each and return the JSON intents object."
    )
    async with ScopedToolbox("analyzer", ctx) as tb:
        run = await run_agent(
            gateway=deps.gateway, toolbox=tb, job_id=ctx.job_id, task=Task.ANALYZE,
            system=prompts.ANALYZER, prompt=prompt, complexity=_complexity(state, pending),
            attempt=1, max_steps=deps.settings.max_tool_steps,
        )  # fmt: skip
    try:
        intents = extract_json(run.final_text)
    except ValueError:
        intents = {"_raw": run.final_text[:4000]}
    return {
        "intents": intents,
        "events": [f"analyzer: {run.steps} steps, tools={len(run.tool_calls)}"],
    }


# --------------------------------------------------------------------------- resolver
async def resolve(state: GraphState, deps: Deps) -> dict[str, Any]:
    ctx = deps.ctx
    attempt = state.get("attempt", 1)
    by_file: dict[str, list[str]] = defaultdict(list)
    for hid in state["pending"]:
        by_file[hid.rpartition("#")[0]].append(hid)

    events = []
    for path, ids in by_file.items():
        intents = {i: state.get("intents", {}).get(i) for i in ids}
        prompt = (
            f"File: {path}\nHunks to resolve: {', '.join(ids)}\n\n"
            f"Analyzer findings:\n{json.dumps(intents, indent=1, default=str)}\n"
        )
        if state.get("intents", {}).get("_raw"):
            prompt += f"\nAnalyzer notes:\n{state['intents']['_raw']}\n"
        if attempt > 1 and state.get("feedback"):
            prompt += (
                f"\nThis is attempt {attempt}. The previous resolution FAILED verification:\n"
                f"{state['feedback']}\nFix the cause; rewrite every hunk listed above.\n"
            )
        prompt += "\nUse git__show_hunk for each hunk, then git__write_resolution for each."
        async with ScopedToolbox("resolver", ctx) as tb:
            run = await run_agent(
                gateway=deps.gateway, toolbox=tb, job_id=ctx.job_id, task=Task.RESOLVE,
                system=prompts.RESOLVER, prompt=prompt, complexity=_complexity(state, ids),
                attempt=attempt, max_steps=deps.settings.max_tool_steps,
            )  # fmt: skip
        events.append(
            f"resolver[{path}] attempt {attempt}: {run.steps} steps via {sorted(set(run.models))}"
        )

    missing = [h for h in state["pending"] if h not in ctx.resolutions]
    if missing:
        return {
            "feedback": f"These hunks were never resolved: {', '.join(missing)}",
            "events": events,
        }
    return {"events": events}


# --------------------------------------------------------------------------- materialise + verify
async def materialise(deps: Deps) -> tuple[str | None, list[str]]:
    """Write all resolutions into the worktree. Returns (tree sha, problems)."""
    ctx = deps.ctx
    files: dict[str, str | None] = {}
    problems: list[str] = []
    for cf in ctx.conflicts.files:
        try:
            files[cf.path] = apply_resolutions(
                cf, {h.index: ctx.resolutions[h.id] for h in cf.hunks}
            )
        except (ResolutionError, KeyError) as exc:
            problems.append(f"{cf.path}: {exc}")
    problems += static_problems(files)
    if problems:
        return None, problems
    for path, content in files.items():
        await ctx.ws.write_resolved(path, content)
    return await ctx.ws.write_tree(), []


async def verify(state: GraphState, deps: Deps) -> dict[str, Any]:
    ctx = deps.ctx
    attempt = state.get("attempt", 1)
    tree, problems = await materialise(deps)
    if tree is None:
        fb = "Static checks failed:\n" + "\n".join(problems)
        return {"verification": {"verdict": "fail", "reasons": problems}, "feedback": fb,
                "events": [f"verify attempt {attempt}: static checks failed"]}  # fmt: skip

    ws, out = ctx.ws, ctx.outcome
    base = Snapshot("base", out.theirs_sha, lambda: ws.archive(out.theirs_sha))
    head = Snapshot("head", out.ours_sha, lambda: ws.archive(out.ours_sha))
    cand = Snapshot("candidate", tree, lambda: ws.archive(tree))

    async def run_pipeline() -> dict[str, Any]:
        rep = await deps.pipeline.verify(ctx.repo, deps.repo_config, base, head, cand)
        return rep.to_dict() | {"feedback": rep.feedback()}

    ctx.run_verification = run_pipeline
    report = await run_pipeline()
    ctx.last_verification = report
    patch = await ws.resolution_diff(sorted({h.path for h in ctx.conflicts.hunks}))
    update: dict[str, Any] = {
        "verification": report, "patch": patch,
        "events": [f"verify attempt {attempt}: {report['verdict']} ({'; '.join(report['reasons'])[:300]})"],
    }  # fmt: skip
    if report["verdict"] != Verdict.PASS:
        update["feedback"] = report["feedback"]
        return update

    # Independent LLM review, only once the tests are green (cheap insurance, not a gate on cost).
    async with ScopedToolbox("verifier", ctx) as tb:
        run = await run_agent(
            gateway=deps.gateway, toolbox=tb, job_id=ctx.job_id, task=Task.REVIEW,
            system=prompts.VERIFIER, prompt="Review the candidate resolution.",
            complexity=_complexity(state, state.get("pending", [])), attempt=attempt,
            max_steps=max(deps.settings.max_tool_steps // 2, 4),
        )  # fmt: skip
    try:
        review = extract_json(run.final_text)
    except ValueError:
        review = {"approve": True, "concerns": ["review output unparseable; relying on tests"]}
    update["review"] = review
    update["events"] = update["events"] + [f"review: approve={review.get('approve')}"]
    if not review.get("approve", True):
        update["feedback"] = "Independent review rejected the resolution:\n- " + "\n- ".join(
            map(str, review.get("concerns", []))
        )
    return update


def route_after_verify(state: GraphState, deps: Deps) -> str:
    v = (state.get("verification") or {}).get("verdict")
    approved = (state.get("review") or {}).get("approve", True)
    if v == Verdict.PASS and approved:
        return "publish"
    if v == Verdict.INCONCLUSIVE:
        return "give_up"
    if state.get("attempt", 1) < deps.settings.max_attempts:
        return "retry"
    return "give_up"


async def next_attempt(state: GraphState, deps: Deps) -> dict[str, Any]:
    # On retry every LLM-resolved hunk is redone with the failure feedback; deterministic ones stay.
    for hid in state.get("pending", []):
        deps.ctx.resolutions.pop(hid, None)
    if not state.get("pending"):
        # Every hunk was "trivial" yet verification failed: escalate them all to the LLM.
        semantic = [h["hunk_id"] for h in state.get("hunks", [])]
        for hid in semantic:
            deps.ctx.resolutions.pop(hid, None)
        return {"attempt": state.get("attempt", 1) + 1, "pending": semantic, "review": None,
                "events": ["retry: escalating deterministic resolutions to the resolver"]}  # fmt: skip
    return {
        "attempt": state.get("attempt", 1) + 1,
        "review": None,
        "events": ["retry with feedback"],
    }


def summary(state: GraphState, deps: Deps) -> dict[str, Any]:
    cats: defaultdict[str, int] = defaultdict(int)
    for h in state.get("hunks", []):
        cats[h["category"]] += 1
    return {
        "attempts": state.get("attempt", 1),
        "hunks": state.get("hunks", []),
        "categories": dict(cats),
        "verification": {k: v for k, v in (state.get("verification") or {}).items() if k != "runs"},
        "review": state.get("review"),
        "intents": state.get("intents"),
    }


async def publish(state: GraphState, deps: Deps) -> dict[str, Any]:
    res = await deps.publisher.publish(deps.ctx, summary(state, deps), state.get("patch", ""))
    return {
        "status": "resolved",
        "result_url": res.url,
        "reason": res.detail,
        "events": [f"publish: {res.detail}"],
    }


async def give_up(state: GraphState, deps: Deps) -> dict[str, Any]:
    v = (state.get("verification") or {}).get("verdict")
    status = "failed_verification" if v == Verdict.FAIL else "needs_human"
    res = await deps.publisher.report_failure(
        deps.ctx, summary(state, deps), state.get("patch", "")
    )
    reason = state.get("feedback", "") or "; ".join(
        (state.get("verification") or {}).get("reasons", [])
    )
    return {
        "status": status,
        "result_url": res.url,
        "reason": reason[:2000],
        "events": [f"give_up: {res.detail}"],
    }
