import io
import tarfile
from pathlib import Path

from mergeagent.sandbox.local_sandbox import LocalSandbox
from mergeagent.verification.comparator import compare_tests, new_diagnostics
from mergeagent.verification.pipeline import (
    MemoryBaselineCache,
    Snapshot,
    Verdict,
    VerificationPipeline,
)
from mergeagent.verification.repo_config import RepoConfig, detect, load
from mergeagent.verification.reports import (
    Outcome,
    normalise_diagnostics,
    parse_go_json,
    parse_junit,
)
from tests.fixtures.repo_factory import BASE_CALC, BASE_TESTS, GOOD_RESOLUTION, MAIN_TESTS

P, F, S = Outcome.PASSED, Outcome.FAILED, Outcome.SKIPPED


def test_compare_tests_rules() -> None:
    base = {"a": P, "b": P, "c": F, "d": P}
    head = {"a": P, "b": P, "c": F, "e": P}
    cand = {"a": P, "b": F, "c": F, "e": P}  # b regressed, d lost, c pre-existing
    cmp = compare_tests([base, head], cand)
    assert cmp.regressions == ["b"]
    assert cmp.lost == ["d"]
    assert cmp.pre_existing == ["c"]
    assert not cmp.ok
    assert compare_tests([{"x": F}], {"x": P}).fixed == ["x"]
    assert compare_tests([base], base).ok


def test_new_diagnostics_ignores_shifted_lines() -> None:
    base = normalise_diagnostics("a.py:10: error: Incompatible types\n")
    cand = normalise_diagnostics(
        "a.py:12: error: Incompatible types\nb.py:3: error: Name 'x' is not defined\n"
    )
    assert new_diagnostics([base], cand) == ["b.py: error: Name 'x' is not defined"]


def test_report_parsers() -> None:
    xml = b"""<testsuites><testsuite>
      <testcase classname="t" name="ok"/>
      <testcase classname="t" name="bad"><failure message="x"/></testcase>
      <testcase classname="t" name="skip"><skipped/></testcase>
    </testsuite></testsuites>"""
    assert parse_junit(xml) == {"t::ok": P, "t::bad": F, "t::skip": S}
    go = '{"Action":"pass","Package":"p","Test":"TestA"}\nnoise\n{"Action":"fail","Package":"p","Test":"TestB"}\n'
    assert parse_go_json(go) == {"p::TestA": P, "p::TestB": F}


def test_repo_config_detection_and_yaml() -> None:
    assert detect(["pyproject.toml", "src/a.py"]).detected == "python"
    assert detect(["package.json", "tsconfig.json"]).typecheck is not None
    assert detect(["go.mod"]).test_report == "go-json"
    assert detect(["README"]).detected == "none"
    cfg = load("test: make test\nacceptance: [./smoke.sh]\n", [])
    assert cfg.test == "make test"
    assert cfg.acceptance == ["./smoke.sh"]
    assert cfg.fingerprint() == load("test: make test\n", []).fingerprint()  # acceptance excluded


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _snap(name: str, files: dict[str, str]) -> Snapshot:
    async def load() -> bytes:
        return _tar(files)

    return Snapshot(name, f"sha-{name}-{hash(frozenset(files.items()))}", load)


CFG = RepoConfig(
    build="python -m compileall -q .",
    test="python -m pytest -q -p no:cacheprovider --junitxml=junit.xml",
    test_report="junit.xml",
    acceptance=["python -c 'import calc'"],
)
TAX_CALC = BASE_CALC.replace(
    "def total(prices):\n    return sum(prices)\n",
    "def total(prices, tax=0.0):\n    return round(sum(prices) * (1 + tax), 2)\n",
)


async def test_pipeline_pass_fail_and_cache(tmp_path: Path) -> None:
    cache = MemoryBaselineCache()
    pipe = VerificationPipeline(LocalSandbox(), cache)
    base = _snap("base", {"calc.py": TAX_CALC, "test_calc.py": MAIN_TESTS})
    head = _snap("head", {"calc.py": BASE_CALC, "test_calc.py": BASE_TESTS})

    good = BASE_CALC.replace("def total(prices):\n    return sum(prices)\n", GOOD_RESOLUTION)
    report = await pipe.verify(
        "o/r", CFG, base, head, _snap("candidate", {"calc.py": good, "test_calc.py": MAIN_TESTS})
    )
    assert report.verdict is Verdict.PASS, report.reasons
    assert report.gates == {"build": True, "tests": True, "acceptance": True}

    # Injected behavioural regression: tax ignored -> test_total_with_tax regresses.
    buggy = good.replace("* (1 + tax)", "")
    report = await pipe.verify(
        "o/r", CFG, base, head, _snap("candidate", {"calc.py": buggy, "test_calc.py": MAIN_TESTS})
    )
    assert report.verdict is Verdict.FAIL
    assert report.comparison is not None
    assert report.comparison.regressions == ["test_calc::test_total_with_tax"]
    assert report.runs["base"].cached  # parents served from the baseline cache on retry
    assert "regression" in report.feedback()

    # Syntax error: build gate fails, tests disappear.
    broken = _snap("candidate", {"calc.py": "def total(:\n", "test_calc.py": MAIN_TESTS})
    report = await pipe.verify("o/r", CFG, base, head, broken)
    assert report.verdict is Verdict.FAIL
    assert report.gates["build"] is False


async def test_pipeline_inconclusive_without_tests() -> None:
    pipe = VerificationPipeline(LocalSandbox(), MemoryBaselineCache())
    s = _snap("x", {"a.py": "x = 1\n"})
    report = await pipe.verify("o/r", RepoConfig(build="true"), s, s, s)
    assert report.verdict is Verdict.INCONCLUSIVE
