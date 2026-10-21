"""Regression-injection study: does the verification pipeline catch behavioural bugs?

Start from the *human* merge resolution (known good), apply one mutation operator to a line in
a conflicted file, and run the parent-vs-candidate pipeline. A mutant is **detected** when the
verdict is FAIL. Mutants that no longer parse are discarded (a syntax check would catch them
trivially; we only score behavioural regressions).
"""

from __future__ import annotations

import ast
import random
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from mergeagent.config import Settings
from mergeagent.eval.scenarios import Scenario, append_jsonl, mirror, read_jsonl
from mergeagent.service import build_sandbox, load_repo_config
from mergeagent.verification.pipeline import (
    MemoryBaselineCache,
    Snapshot,
    Verdict,
    VerificationPipeline,
)
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


@dataclass(frozen=True, slots=True)
class Mutant:
    path: str
    line: int
    operator: str
    original: str
    mutated: str
    text: str  # full mutated file


def mutants_for(path: str, text: str) -> Iterator[Mutant]:
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", '"""', "'''", "import ", "from ")):
            continue
        for name, pattern, repl in OPERATORS:
            if pattern.search(line):
                new_line = pattern.sub(repl, line, count=1)
                if new_line == line:
                    continue
                new_text = "".join([*lines[:i], new_line, *lines[i + 1 :]])
                try:
                    ast.parse(new_text)
                except SyntaxError:
                    continue
                yield Mutant(path, i + 1, name, line.rstrip(), new_line.rstrip(), new_text)


async def run_mutations(
    settings: Settings, scenarios_path: Path, out: Path, cache_dir: Path, per_scenario: int
) -> int:
    rng = random.Random(1234)  # noqa: S311 - reproducible sampling, not crypto
    count = 0
    pipeline = VerificationPipeline(
        build_sandbox(settings), MemoryBaselineCache(), settings.sandbox.image
    )
    for row in read_jsonl(scenarios_path):
        s = Scenario(**row)
        py_files = [f for f in s.files if f.endswith(".py")]
        if not py_files:
            continue
        ws = await GitWorkspace.clone(str(mirror(s.url, cache_dir)), Path(tempfile.mkdtemp()) / "ws",
                                      refs=[s.ours, s.theirs, s.merge_sha])  # fmt: skip
        try:
            cfg = await load_repo_config(ws, s.theirs)
            pool = []
            for f in py_files:
                try:
                    pool += list(mutants_for(f, await ws.read_file(s.merge_sha, f)))
                except FileNotFoundError:
                    continue
            await ws.run("checkout", "--quiet", "--force", "--detach", s.merge_sha)
            for m in rng.sample(pool, min(per_scenario, len(pool))):
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
                    "original": m.original, "mutated": m.mutated, "verdict": rep.verdict.value,
                    "detected": rep.verdict is Verdict.FAIL, "reasons": rep.reasons,
                })  # fmt: skip
                count += 1
        finally:
            ws.cleanup()
    return count
