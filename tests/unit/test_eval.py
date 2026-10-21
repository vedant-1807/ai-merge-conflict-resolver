from pathlib import Path

from mergeagent.config import Settings
from mergeagent.demo import git as _git
from mergeagent.eval.mining import mine_repo
from mergeagent.eval.mutation import mutants_for, run_mutations
from mergeagent.eval.report import write_report
from mergeagent.eval.scenarios import append_jsonl, asdict, read_jsonl
from tests.fixtures.repo_factory import BASE_CALC, GOOD_RESOLUTION, MAIN_TESTS, make_conflict_repo

CONFIG = """build: python -m compileall -q .
test: python -m pytest -q -p no:cacheprovider --junitxml=junit.xml
test_report: junit.xml
"""


def repo_with_human_merge(tmp: Path) -> Path:
    repo = make_conflict_repo(tmp)
    (repo / ".mergeagent.yml").write_text(CONFIG)
    _git(repo, "add", ".mergeagent.yml")
    _git(repo, "commit", "--quiet", "-m", "ci config")
    import subprocess

    from mergeagent.demo import ENV

    subprocess.run(
        ["git", "merge", "feature", "-m", "Merge feature"],
        cwd=repo,
        env=ENV,
        capture_output=True,
        check=False,
    )
    (repo / "calc.py").write_text(
        BASE_CALC.replace("def total(prices):\n    return sum(prices)\n", GOOD_RESOLUTION)
    )
    tests = (
        MAIN_TESTS
        + "\n\ndef test_total_with_discount():\n    assert total([10, 10], discount=0.25) == 15.0\n"
    )
    (repo / "test_calc.py").write_text(tests)
    (repo / "app.py").write_text(
        "import os\nimport json\nimport sys\n\n\ndef main():\n    return os.getcwd()\n"
    )
    _git(repo, "rm", "--quiet", "-f", "legacy.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "--no-edit")
    return repo


async def test_mine_finds_conflicting_merge(tmp_path: Path) -> None:
    repo = repo_with_human_merge(tmp_path)
    found = await mine_repo(
        "demo/calc",
        str(repo),
        "main",
        tmp_path / "cache",
        target=5,
        max_hunks=10,
        max_candidates=50,
    )
    assert len(found) == 1
    s = found[0]
    assert {"calc.py", "legacy.py"} <= set(s.files)
    assert s.hunks >= 3


def test_mutants_skip_strings_comments_and_non_package_code() -> None:
    src = 'def f(a):\n    log("x == y and z")  # a == b\n    return a == 1\n'
    muts = list(mutants_for("pkg/mod.py", src))
    assert all(m.line == 3 for m in muts)
    assert any(m.mutated.strip() == "return a != 1" for m in muts)
    assert list(mutants_for("scripts/gen.py", "x = 1 == 1\n")) == []
    assert list(mutants_for("setup.py", "x = 1 == 1\n")) == []


def test_mutants_skip_tests_and_docstrings() -> None:
    src = 'def f(a):\n    """Return True when a == 1."""\n    msg = """x\n    a == b\n    """\n    return a == 1\n'
    muts = list(mutants_for("pkg/mod.py", src))
    assert {m.line for m in muts} == {6}
    assert list(mutants_for("tests/test_mod.py", "def test():\n    assert 1 == 1\n")) == []
    assert list(mutants_for("pkg/conftest.py", "X = 1 == 1\n")) == []


def test_mutants_are_behavioural_and_parse() -> None:
    src = "def f(a, b):\n    if a == b and not a:\n        return a + 1\n    return True\n"
    ops = {m.operator for m in mutants_for("f.py", src)}
    assert {
        "negate_eq",
        "and_to_or",
        "drop_not",
        "off_by_one",
        "true_to_false",
        "return_none",
    } <= ops
    assert all("import" not in m.original for m in mutants_for("f.py", "import os\n"))


async def test_mutation_study_detects_regressions(tmp_path: Path) -> None:
    repo = repo_with_human_merge(tmp_path)
    [s] = await mine_repo(
        "demo/calc",
        str(repo),
        "main",
        tmp_path / "cache",
        target=5,
        max_hunks=10,
        max_candidates=50,
    )
    scen = tmp_path / "scenarios.jsonl"
    append_jsonl(scen, asdict(s))
    settings = Settings()
    settings.sandbox.backend = "local"
    out = tmp_path / "mut.jsonl"
    n = await run_mutations(settings, scen, out, tmp_path / "cache", per_scenario=6)
    rows = read_jsonl(out)
    assert n == len(rows) > 0
    assert any(r["detected"] for r in rows)
    assert all(r["covered"] is not None for r in rows)  # coverage pass ran
    assert any(r["covered"] and r["detected"] for r in rows)
    assert all(r["verdict"] in {"pass", "fail", "inconclusive"} for r in rows)


def test_report(tmp_path: Path) -> None:
    def row(verified: bool, cost: float, mode: str) -> dict[str, object]:
        return {"scenario": "x", "repo": "a/b", "verified": verified, "attempts": 1, "cost_usd": cost,
                "similarity": 0.9, "exact_match": False, "mode": mode}  # fmt: skip

    for i in range(4):
        append_jsonl(tmp_path / "routed.jsonl", row(i < 3, 0.075, "routed"))
        append_jsonl(tmp_path / "fixed.jsonl", row(i < 3, 0.1, "fixed:claude-opus-5-5"))
    for covered, detected in [(True, True), (True, False), (False, False)]:
        append_jsonl(
            tmp_path / "mutations.jsonl",
            {
                "operator": "negate_eq",
                "repo": "a/b",
                "covered": covered,
                "detected": detected,
                "verdict": "fail" if detected else "pass",
            },
        )
    text = write_report(tmp_path, tmp_path / "out.md")
    assert "3 (75.0%)" in text
    assert "Cost per attempt reduction: 25.0%" in text
    assert "Detected 1/2 (50.0%) of regressions injected into code" in text
    assert "| All valid mutants | 1/3 (33.3%) |" in text
    assert "No results yet" in write_report(tmp_path / "empty", tmp_path / "e.md")
