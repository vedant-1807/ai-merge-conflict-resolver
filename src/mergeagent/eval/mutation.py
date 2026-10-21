"""Regression-injection study: does the verification pipeline catch behavioural bugs?

Start from the *human* merge resolution (known good), apply one mutation operator to one line
of a conflicted production file, and run the parent-vs-candidate pipeline. A mutant is
**detected** when the verdict is FAIL, i.e. no draft PR would have been opened.

Mutant validity follows common mutation-testing practice (mutmut, Stryker):

* only shipped package code is mutated: not tests, scripts, docs, examples or setup files;
* operators never fire inside string literals or comments, and docstrings are skipped;
* mutants that no longer parse are discarded (a syntax check catches those trivially);
* each mutant is tagged ``covered`` when the project's own test suite executes the mutated line
  on the merge commit. No test-based check can catch a bug in code no test runs, so the
  headline rate is reported over covered mutants, with the uncovered rate reported separately.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import random
import re
import tempfile
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from mergeagent.config import Settings
from mergeagent.eval.scenarios import Scenario, append_jsonl, mirror, read_jsonl
from mergeagent.sandbox.base import Sandbox
from mergeagent.service import build_sandbox, load_repo_config
from mergeagent.verification.pipeline import (
    MemoryBaselineCache,
    Snapshot,
    Verdict,
    VerificationPipeline,
)
from mergeagent.verification.repo_config import RepoConfig
from mergeagent.workspace.git_workspace import GitWorkspace

OPERATORS: list[tuple[str, re.Pattern[str], str]] = [
    ("negate_eq", re.compile(r"(?<![=!<>])==(?!=)"), "!="),
    ("negate_neq", re.compile(r"!="), "=="),
    ("boundary_lt", re.compile(r"(?<![<\-])<(?![<=])"), "<="),
    ("boundary_gt", re.compile(r"(?<![>\-])>(?![>=])"), ">="),
    ("and_to_or", re.compile(r"\band\b"), "or"),
    ("or_to_and", re.compile(r"\bor\b"), "and"),
    ("true_to_false", re.compile(r"\bTrue\b"), "False"),
    ("false_to_true", re.compile(r"\bFalse\b"), "True"),
    ("off_by_one", re.compile(r"(?<![\w.])1(?![\w.])"), "2"),
    ("drop_not", re.compile(r"\bnot\s+"), ""),
    ("return_none", re.compile(r"^(\s*)return\s+(?!None\b)\S.*$"), r"\1return None"),
]
NON_PACKAGE_DIRS = {"scripts", "docs", "doc", "examples", "example", "benchmarks", "bench",
                    "tools", "extra", "artwork", "ci", ".github"}  # fmt: skip
NON_PACKAGE_FILES = {"setup.py", "noxfile.py", "conftest.py", "fabfile.py", "tasks.py"}
COV_DATA = "/tmp/.mergeagent-cov"  # noqa: S108 - path inside the sandbox
COV_JSON = "/tmp/mergeagent-cov.json"  # noqa: S108 - path inside the sandbox


@dataclass(frozen=True, slots=True)
class Mutant:
    path: str
    line: int
    operator: str
    original: str
    mutated: str
    text: str  # full mutated file


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        any(p in {"tests", "test", "testing"} for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def is_production_path(path: str) -> bool:
    parts = path.split("/")
    return (
        path.endswith(".py")
        and not is_test_path(path)
        and parts[0] not in NON_PACKAGE_DIRS
        and parts[-1] not in NON_PACKAGE_FILES
    )


def _string_lines(tree: ast.AST) -> set[int]:
    """Lines inside docstrings or other multi-line string literals."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            end = node.end_lineno or node.lineno
            if end > node.lineno:
                lines.update(range(node.lineno, end + 1))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


_PROTECTED = {tokenize.STRING, tokenize.COMMENT}
_PROTECTED |= {getattr(tokenize, n) for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
               if hasattr(tokenize, n)}  # fmt: skip


def _protected_spans(text: str) -> dict[int, list[tuple[int, int]]]:
    """Column spans of single-line string literals and comments, per (1-based) line."""
    spans: dict[int, list[tuple[int, int]]] = {}
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in _PROTECTED and tok.start[0] == tok.end[0]:
            spans.setdefault(tok.start[0], []).append((tok.start[1], tok.end[1]))
    return spans


def mutants_for(path: str, text: str) -> Iterator[Mutant]:
    """Behavioural mutants of shipped package code (never tests, docs, strings or comments)."""
    if not is_production_path(path):
        return
    try:
        skip = _string_lines(ast.parse(text))
        protected = _protected_spans(text)
    except (SyntaxError, tokenize.TokenError):
        return
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        lineno = i + 1
        stripped = line.strip()
        if lineno in skip or not stripped or stripped.startswith(("#", "import ", "from ", "@")):
            continue
        spans = protected.get(lineno, [])
        for name, pattern, repl in OPERATORS:
            match = next(
                (m for m in pattern.finditer(line)
                 if not any(s <= m.start() < e for s, e in spans)),
                None,
            )  # fmt: skip
            if match is None:
                continue
            new_line = line[: match.start()] + match.expand(repl) + line[match.end() :]
            if new_line.rstrip("\n") == line.rstrip("\n"):
                continue
            if not new_line.endswith("\n") and line.endswith("\n"):
                new_line += "\n"
            new_text = "".join([*lines[:i], new_line, *lines[i + 1 :]])
            try:
                ast.parse(new_text)
            except SyntaxError:
                continue
            yield Mutant(path, lineno, name, line.rstrip(), new_line.rstrip(), new_text)


async def covered_lines(
    sandbox: Sandbox, archive: bytes, cfg: RepoConfig, image: str | None
) -> dict[str, set[int]] | None:
    """Run the project's test suite under coverage on the merge commit (pytest projects only)."""
    if not cfg.test or "pytest" not in cfg.test:
        return None
    cov_test = cfg.test.replace(
        "python -m pytest", f"python -m coverage run --data-file={COV_DATA} -m pytest", 1
    )
    async with sandbox.session(archive, image=cfg.image or image) as sess:
        setup = [*cfg.setup, "python -c 'import coverage' 2>/dev/null || pip install -q coverage"]
        r = await sess.exec(
            " && ".join(f"({c})" for c in setup), timeout_s=cfg.timeout_s, network=True
        )
        if not r.ok:
            return None
        await sess.exec(cov_test, timeout_s=cfg.timeout_s * 2)
        await sess.exec(
            f"python -m coverage json --data-file={COV_DATA} -o {COV_JSON} -q", timeout_s=300
        )
        raw = await sess.read_file(COV_JSON)
    if not raw:
        return None
    files = json.loads(raw).get("files", {})
    return {
        name.removeprefix("/workspace/"): set(info.get("executed_lines", []))
        for name, info in files.items()
    }


async def _scenario_mutants(
    s: Scenario,
    pipeline: VerificationPipeline,
    sandbox: Sandbox,
    image: str | None,
    out: Path,
    cache_dir: Path,
    per_scenario: int,
    uncovered_per_scenario: int,
) -> int:
    prod_files = [f for f in s.files if is_production_path(f)]
    if not prod_files:
        return 0
    rng = random.Random(f"{s.merge_sha}:1234")  # noqa: S311 - reproducible sampling, not crypto
    ws = await GitWorkspace.clone(str(mirror(s.url, cache_dir)), Path(tempfile.mkdtemp()) / "ws",
                                  refs=[s.ours, s.theirs, s.merge_sha])  # fmt: skip
    count = 0
    try:
        cfg = await load_repo_config(ws, s.theirs)
        pool: list[Mutant] = []
        for f in prod_files:
            try:
                pool += list(mutants_for(f, await ws.read_file(s.merge_sha, f)))
            except FileNotFoundError:
                continue
        if not pool:
            return 0
        coverage = await covered_lines(sandbox, await ws.archive(s.merge_sha), cfg, image)

        def is_covered(m: Mutant) -> bool | None:
            if coverage is None:
                return None
            return m.line in coverage.get(m.path, set())

        covered = [m for m in pool if is_covered(m)]
        uncovered = [m for m in pool if is_covered(m) is False]
        if coverage is None:
            chosen = rng.sample(pool, min(per_scenario, len(pool)))
        else:
            chosen = rng.sample(covered, min(per_scenario, len(covered))) + rng.sample(
                uncovered, min(uncovered_per_scenario, len(uncovered))
            )

        await ws.run("checkout", "--quiet", "--force", "--detach", s.merge_sha)
        for m in chosen:
            await ws.write_resolved(m.path, m.text)
            tree = await ws.write_tree()
            rep = await pipeline.verify(
                s.repo, cfg,
                Snapshot("base", s.theirs, partial(ws.archive, s.theirs)),
                Snapshot("head", s.ours, partial(ws.archive, s.ours)),
                Snapshot("candidate", tree, partial(ws.archive, tree)),
            )  # fmt: skip
            await ws.run("checkout", "--quiet", "--force", s.merge_sha, "--", m.path)
            append_jsonl(out, {
                "scenario": s.id, "repo": s.repo, "path": m.path, "line": m.line, "operator": m.operator,
                "original": m.original, "mutated": m.mutated, "covered": is_covered(m),
                "pool": len(pool), "pool_covered": len(covered) if coverage is not None else None,
                "verdict": rep.verdict.value, "detected": rep.verdict is Verdict.FAIL, "reasons": rep.reasons,
            })  # fmt: skip
            count += 1
    finally:
        ws.cleanup()
    return count


async def run_mutations(
    settings: Settings,
    scenarios_path: Path,
    out: Path,
    cache_dir: Path,
    per_scenario: int,
    concurrency: int = 1,
    uncovered_per_scenario: int = 2,
) -> int:
    sandbox = build_sandbox(settings)
    pipeline = VerificationPipeline(sandbox, MemoryBaselineCache(), settings.sandbox.image)
    done = {r["scenario"] for r in read_jsonl(out)}  # resumable
    scenarios = [Scenario(**r) for r in read_jsonl(scenarios_path)]
    sem = asyncio.Semaphore(concurrency)

    async def one(s: Scenario) -> int:
        if s.id in done:
            return 0
        async with sem:
            try:
                return await _scenario_mutants(
                    s, pipeline, sandbox, settings.sandbox.image, out, cache_dir,
                    per_scenario, uncovered_per_scenario,
                )  # fmt: skip
            except Exception as exc:  # one broken scenario must not abort the study
                append_jsonl(
                    out.with_suffix(".errors.jsonl"), {"scenario": s.id, "error": str(exc)[:500]}
                )
                return 0

    return sum(await asyncio.gather(*(one(s) for s in scenarios)))
