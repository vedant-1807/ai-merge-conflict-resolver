import json
import re
from pathlib import Path

import pytest

from mergeagent.agents.graph import run_resolution
from mergeagent.agents.publisher import LocalPublisher
from mergeagent.agents.state import Deps
from mergeagent.config import AgentSettings
from mergeagent.llm.budget import InMemoryBudget
from mergeagent.llm.gateway import LLMGateway, MemoryRecorder
from mergeagent.llm.providers.base import LLMRequest
from mergeagent.llm.providers.fake import FakeProvider, FakeTurn
from mergeagent.llm.router import ModelRouter
from mergeagent.mcp_servers.context import JobContext, StaticIssueContext
from mergeagent.rag.embeddings import HashingEmbedder
from mergeagent.rag.store import MemoryVectorStore
from mergeagent.sandbox.local_sandbox import LocalSandbox
from mergeagent.verification.pipeline import MemoryBaselineCache, VerificationPipeline
from mergeagent.verification.repo_config import RepoConfig
from mergeagent.workspace.git_workspace import GitWorkspace
from tests.fixtures.repo_factory import GOOD_RESOLUTION

CFG = RepoConfig(
    build="python -m compileall -q .",
    test="python -m pytest -q -p no:cacheprovider --junitxml=junit.xml",
    test_report="junit.xml",
)
BUGGY = GOOD_RESOLUTION.replace("discounted * (1 + tax)", "discounted")


def _last_is_tool_result(req: LLMRequest) -> bool:
    content = req.messages[-1]["content"]
    return isinstance(content, list) and content and content[0].get("type") == "tool_result"


def make_responder(ctx_holder: dict[str, JobContext], buggy_first: bool):
    state = {"resolver_attempts": 0}

    def respond(req: LLMRequest) -> FakeTurn:
        ctx = ctx_holder["ctx"]
        if req.system.startswith("You are the Analyzer"):
            if not _last_is_tool_result(req):
                return FakeTurn(tool_calls=[("git__show_hunk", {"hunk_id": "calc.py#0"})])
            return FakeTurn(
                text=json.dumps({"calc.py#0": {"ours_intent": "discount", "theirs_intent": "tax"}})
            )
        if req.system.startswith("You are the Resolver"):
            if _last_is_tool_result(req):
                return FakeTurn(text="Combined both features.")
            prompt = req.messages[0]["content"]
            ids = re.search(r"Hunks to resolve: (.*)", prompt).group(1).split(", ")  # type: ignore[union-attr]
            if "calc.py#0" in ids:
                state["resolver_attempts"] += 1
            calls = []
            for hid in ids:
                h = ctx.conflicts.hunk(hid)
                if hid == "calc.py#0":
                    content = (
                        BUGGY
                        if (buggy_first and state["resolver_attempts"] == 1)
                        else GOOD_RESOLUTION
                    )
                    calls.append(
                        (
                            "git__write_resolution",
                            {"hunk_id": hid, "content": content, "rationale": "both"},
                        )
                    )
                elif hid.startswith("legacy.py"):
                    calls.append(
                        (
                            "git__write_resolution",
                            {
                                "hunk_id": hid,
                                "content": "",
                                "rationale": "unused",
                                "delete_file": True,
                            },
                        )
                    )
                else:
                    calls.append(
                        (
                            "git__write_resolution",
                            {
                                "hunk_id": hid,
                                "content": h.theirs + h.ours[len(h.base or "") :],
                                "rationale": "keep both tests",
                            },
                        )
                    )
            return FakeTurn(tool_calls=calls)
        if req.system.startswith("You are the Verifier"):
            if not _last_is_tool_result(req):
                return FakeTurn(tool_calls=[("git__resolution_diff", {})])
            return FakeTurn(text='{"approve": true, "concerns": []}')
        raise AssertionError("unexpected agent")

    return respond, state


async def _deps(conflict_repo: Path, tmp_path: Path, buggy_first: bool):
    ws = await GitWorkspace.clone(str(conflict_repo), tmp_path / "ws", refs=["main", "feature"])
    outcome = await ws.start_merge(await ws.resolve("feature"), await ws.resolve("main"))
    store = MemoryVectorStore(HashingEmbedder(256))
    ctx = JobContext("job-e2e", "o/r", ws, outcome, StaticIssueContext(), store)
    holder = {"ctx": ctx}
    responder, rstate = make_responder(holder, buggy_first)
    budget = InMemoryBudget()
    await budget.open("job-e2e", 5.0)
    rec = MemoryRecorder()
    gateway = LLMGateway(
        FakeProvider(responder), ModelRouter("fake-small", "fake-medium", "fake-large"), budget, rec
    )
    deps = Deps(
        ctx,
        gateway,
        VerificationPipeline(LocalSandbox(), MemoryBaselineCache()),
        LocalPublisher(tmp_path / "out"),
        CFG,
        AgentSettings(max_attempts=3),
    )
    return deps, rec, rstate


async def test_resolves_conflict_end_to_end(conflict_repo: Path, tmp_path: Path) -> None:
    deps, rec, _ = await _deps(conflict_repo, tmp_path, buggy_first=False)
    final = await run_resolution(deps)
    assert final["status"] == "resolved", final.get("reason")
    assert final["attempt"] == 1
    cats = {h["hunk_id"]: h["category"] for h in final["hunks"]}
    assert cats["app.py#0"] == "imports"  # resolved deterministically, no LLM
    assert cats["calc.py#0"] == "semantic"
    assert final["verification"]["verdict"] == "pass"
    patch = Path(final["result_url"]).read_text()
    assert "+def total(prices, tax=0.0, discount=0.0):" in patch
    assert {c.agent for c in rec.calls} == {"analyzer", "resolver", "verifier"}
    assert not (deps.ctx.ws.root / "legacy.py").exists()


async def test_regression_triggers_retry_with_escalation(
    conflict_repo: Path, tmp_path: Path
) -> None:
    deps, rec, rstate = await _deps(conflict_repo, tmp_path, buggy_first=True)
    final = await run_resolution(deps)
    assert final["status"] == "resolved", final.get("reason")
    assert final["attempt"] == 2
    assert rstate["resolver_attempts"] == 2
    assert any("verify attempt 1: fail" in e for e in final["events"])
    calc_models = [c.model for c in rec.calls if c.agent == "resolver"]
    assert calc_models[0] != calc_models[-1]  # escalated to a bigger tier on retry


async def test_budget_exhaustion_is_needs_human(conflict_repo: Path, tmp_path: Path) -> None:
    deps, _, _ = await _deps(conflict_repo, tmp_path, buggy_first=False)
    deps.gateway.budget = InMemoryBudget()
    await deps.gateway.budget.open("job-e2e", 0.0000001)
    final = await run_resolution(deps)
    assert final["status"] == "needs_human"
    assert "remains" in final["reason"]


@pytest.mark.parametrize("attempts", [1])
async def test_gives_up_after_max_attempts(
    conflict_repo: Path, tmp_path: Path, attempts: int
) -> None:
    deps, _, _ = await _deps(conflict_repo, tmp_path, buggy_first=True)
    deps.settings = AgentSettings(max_attempts=attempts)
    final = await run_resolution(deps)
    assert final["status"] == "failed_verification"
    assert "regression" in final["reason"]
    assert final["result_url"] is None
