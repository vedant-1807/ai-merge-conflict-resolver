"""Regression verification: parent branches vs candidate merge, inside the sandbox.

For each snapshot (target branch, PR head, candidate merge) a fresh sandbox runs
setup -> build -> typecheck -> tests (+ acceptance commands on the candidate). Parent results
depend only on (repo, sha, config), so they are cached and reused across attempts and jobs;
retries only pay for the candidate run.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from redis.asyncio import Redis

from mergeagent.logging import get_logger
from mergeagent.observability.metrics import VERIFICATIONS
from mergeagent.sandbox.base import ExecResult, Sandbox, SandboxSession
from mergeagent.verification.comparator import Comparison, compare_tests, new_diagnostics
from mergeagent.verification.repo_config import RepoConfig
from mergeagent.verification.reports import (
    Outcome,
    TestResults,
    normalise_diagnostics,
    parse_go_json,
    parse_junit,
)

log = get_logger(__name__)


class Verdict(StrEnum):
    PASS = "pass"  # noqa: S105 - verdict, not a credential
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"  # nothing could be verified (no tests, setup broken everywhere)


@dataclass(frozen=True, slots=True)
class Snapshot:
    name: str  # base | head | candidate
    sha: str
    load_archive: Callable[[], Awaitable[bytes]]


@dataclass(slots=True)
class StepResult:
    exit_code: int
    duration_s: float
    log_tail: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @classmethod
    def of(cls, r: ExecResult, keep: int = 4000) -> StepResult:
        return cls(r.exit_code, round(r.duration_s, 2), r.output[-keep:], r.timed_out)


@dataclass(slots=True)
class SnapshotRun:
    name: str
    sha: str
    setup: StepResult | None = None
    build: StepResult | None = None
    typecheck: StepResult | None = None
    test: StepResult | None = None
    tests: dict[str, str] | None = None
    diagnostics: list[str] = field(default_factory=list)
    acceptance: list[StepResult] = field(default_factory=list)
    cached: bool = False

    def results(self) -> TestResults | None:
        return {k: Outcome(v) for k, v in self.tests.items()} if self.tests is not None else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SnapshotRun:
        def step(x: dict[str, Any] | None) -> StepResult | None:
            return StepResult(**x) if x else None

        return cls(
            name=d["name"], sha=d["sha"], setup=step(d["setup"]), build=step(d["build"]),
            typecheck=step(d["typecheck"]), test=step(d["test"]), tests=d["tests"],
            diagnostics=d["diagnostics"], acceptance=[StepResult(**a) for a in d["acceptance"]],
        )  # fmt: skip


@dataclass(slots=True)
class VerificationReport:
    verdict: Verdict
    gates: dict[str, bool]
    reasons: list[str]
    comparison: Comparison | None
    runs: dict[str, SnapshotRun]

    def feedback(self) -> str:
        """Actionable failure description fed back to the resolver agent on retry."""
        lines = [f"Verification verdict: {self.verdict.value}"] + [f"- {r}" for r in self.reasons]
        cand = self.runs.get("candidate")
        if cand:
            for step_name in ("setup", "build", "typecheck", "test"):
                step = getattr(cand, step_name)
                if step is not None and not step.ok:
                    lines.append(
                        f"\n--- candidate {step_name} output (tail) ---\n{step.log_tail[-2500:]}"
                    )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "gates": self.gates,
            "reasons": self.reasons,
            "comparison": asdict(self.comparison) if self.comparison else None,
            "runs": {k: v.to_dict() for k, v in self.runs.items()},
        }


class BaselineCache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def set(self, key: str, value: dict[str, Any]) -> None: ...


class MemoryBaselineCache:
    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        return self._d.get(key)

    async def set(self, key: str, value: dict[str, Any]) -> None:
        self._d[key] = value


class RedisBaselineCache:
    def __init__(self, redis: Redis, ttl_s: int = 7 * 24 * 3600) -> None:
        self._r = redis
        self._ttl = ttl_s

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self._r.get(f"mergeagent:baseline:{key}")
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        await self._r.set(f"mergeagent:baseline:{key}", json.dumps(value), ex=self._ttl)


class VerificationPipeline:
    def __init__(
        self, sandbox: Sandbox, cache: BaselineCache, default_image: str | None = None
    ) -> None:
        self._sandbox = sandbox
        self._cache = cache
        self._image = default_image

    async def _results(
        self, sess: SandboxSession, cfg: RepoConfig, test: ExecResult
    ) -> TestResults | None:
        if cfg.test_report == "go-json":
            return parse_go_json(test.output)
        if cfg.test_report:
            raw = await sess.read_file(cfg.test_report)
            if raw:
                try:
                    return parse_junit(raw)
                except Exception as exc:
                    log.warning("verify.junit_parse_failed", error=str(exc))
        return None

    async def run_snapshot(
        self, snap: Snapshot, cfg: RepoConfig, *, acceptance: bool
    ) -> SnapshotRun:
        run = SnapshotRun(snap.name, snap.sha)
        archive = await snap.load_archive()
        t = cfg.timeout_s
        async with self._sandbox.session(archive, image=cfg.image or self._image) as sess:
            if cfg.setup:
                r = await sess.exec(
                    " && ".join(f"({c})" for c in cfg.setup), timeout_s=t, network=True
                )
                run.setup = StepResult.of(r)
                if not r.ok:
                    return run
            if cfg.build:
                run.build = StepResult.of(await sess.exec(cfg.build, timeout_s=t))
            if cfg.typecheck:
                r = await sess.exec(cfg.typecheck, timeout_s=t)
                run.typecheck = StepResult.of(r)
                run.diagnostics = normalise_diagnostics(r.output)
            if cfg.test:
                r = await sess.exec(cfg.test, timeout_s=t)
                run.test = StepResult.of(r)
                res = await self._results(sess, cfg, r)
                run.tests = {k: v.value for k, v in res.items()} if res is not None else None
            if acceptance:
                for cmd in cfg.acceptance:
                    run.acceptance.append(StepResult.of(await sess.exec(cmd, timeout_s=t)))
        return run

    async def _baseline(self, repo: str, snap: Snapshot, cfg: RepoConfig) -> SnapshotRun:
        key = f"{repo}:{snap.sha}:{cfg.fingerprint()}"
        if (hit := await self._cache.get(key)) is not None:
            run = SnapshotRun.from_dict(hit)
            run.name, run.cached = snap.name, True
            return run
        run = await self.run_snapshot(snap, cfg, acceptance=False)
        await self._cache.set(key, run.to_dict())
        return run

    async def verify(
        self, repo: str, cfg: RepoConfig, base: Snapshot, head: Snapshot, candidate: Snapshot
    ) -> VerificationReport:
        parents = [await self._baseline(repo, base, cfg), await self._baseline(repo, head, cfg)]
        cand = await self.run_snapshot(candidate, cfg, acceptance=True)
        report = evaluate(parents, cand, cfg)
        VERIFICATIONS.labels(verdict=report.verdict.value).inc()
        log.info("verify.done", verdict=report.verdict.value, reasons=report.reasons)
        return report


def evaluate(parents: list[SnapshotRun], cand: SnapshotRun, cfg: RepoConfig) -> VerificationReport:
    runs = {p.name: p for p in parents} | {"candidate": cand}
    gates: dict[str, bool] = {}
    reasons: list[str] = []

    def parents_ok(step: str) -> bool:
        return any((s := getattr(p, step)) is not None and s.ok for p in parents)

    if cand.setup is not None and not cand.setup.ok:
        gates["setup"] = False
        if parents_ok("setup"):
            reasons.append("dependency setup fails on the candidate but succeeds on a parent")
            return VerificationReport(Verdict.FAIL, gates, reasons, None, runs)
        reasons.append("dependency setup fails on every snapshot; nothing could be verified")
        return VerificationReport(Verdict.INCONCLUSIVE, gates, reasons, None, runs)

    if cfg.build and cand.build is not None:
        gates["build"] = cand.build.ok or not parents_ok("build")
        if not gates["build"]:
            reasons.append("build fails on the candidate but succeeds on a parent")

    if cfg.typecheck:
        introduced = new_diagnostics([p.diagnostics for p in parents], cand.diagnostics)
        gates["typecheck"] = not introduced
        if introduced:
            reasons.append(
                f"{len(introduced)} new typecheck diagnostic(s): " + "; ".join(introduced[:5])
            )

    comparison: Comparison | None = None
    cand_results = cand.results()
    parent_results = [r for p in parents if (r := p.results()) is not None]
    if cfg.test:
        if cand_results is not None and parent_results:
            comparison = compare_tests(parent_results, cand_results)
            gates["tests"] = comparison.ok
            if comparison.regressions:
                reasons.append(
                    f"{len(comparison.regressions)} regression(s): "
                    + ", ".join(comparison.regressions[:8])
                )
            if comparison.lost:
                reasons.append(
                    f"{len(comparison.lost)} test(s) disappeared: " + ", ".join(comparison.lost[:8])
                )
        elif cand.test is not None:
            # No structured report: fall back to suite-level exit codes.
            gates["tests"] = cand.test.ok or not parents_ok("test")
            if not gates["tests"]:
                reasons.append("test suite fails on the candidate but passes on a parent")

    if cfg.acceptance:
        failed = [c for c, r in zip(cfg.acceptance, cand.acceptance, strict=False) if not r.ok]
        gates["acceptance"] = not failed and len(cand.acceptance) == len(cfg.acceptance)
        if failed:
            reasons.append("acceptance command(s) failed: " + "; ".join(failed))

    if "tests" not in gates:
        reasons.append("no test suite was run; resolution is unverified")
        return VerificationReport(Verdict.INCONCLUSIVE, gates, reasons, comparison, runs)
    verdict = Verdict.PASS if all(gates.values()) else Verdict.FAIL
    if verdict is Verdict.PASS:
        reasons.append("all gates passed: no regressions against either parent")
    return VerificationReport(verdict, gates, reasons, comparison, runs)
