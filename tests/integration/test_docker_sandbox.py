import io
import tarfile

import pytest

from mergeagent.config import SandboxSettings
from mergeagent.sandbox.docker_sandbox import DockerSandbox
from mergeagent.verification.pipeline import (
    MemoryBaselineCache,
    Snapshot,
    Verdict,
    VerificationPipeline,
)
from mergeagent.verification.repo_config import RepoConfig
from tests.fixtures.repo_factory import BASE_CALC, GOOD_RESOLUTION, MAIN_TESTS

pytestmark = pytest.mark.integration
IMAGE = "mergeagent-sandbox-python:latest"


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def sandbox() -> DockerSandbox:
    return DockerSandbox(SandboxSettings(image=IMAGE, memory="512m", cpus=1.0))


async def test_hardening_and_network_isolation(sandbox: DockerSandbox) -> None:
    async with sandbox.session(_tar({"a.txt": "hi"})) as s:
        who = await s.exec("id -u && cat a.txt", timeout_s=30)
        assert who.ok
        assert who.output.split() == ["10001", "hi"]  # non-root, archive seeded
        caps = await s.exec("grep CapEff /proc/self/status", timeout_s=30)
        assert caps.output.split()[-1] == "0000000000000000"  # no capabilities
        offline = await s.exec(
            "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\"",
            timeout_s=30,
        )
        assert not offline.ok  # no network outside setup
        slow = await s.exec("sleep 30", timeout_s=2)
        assert slow.timed_out
        assert await s.read_file("missing.txt") is None


async def test_regression_pipeline_in_docker(sandbox: DockerSandbox) -> None:
    cfg = RepoConfig(
        build="python -m compileall -q .",
        test="python -m pytest -q -p no:cacheprovider --junitxml=/tmp/junit.xml",
        test_report="/tmp/junit.xml",
    )
    tax = BASE_CALC.replace("def total(prices):\n    return sum(prices)\n",
                            "def total(prices, tax=0.0):\n    return round(sum(prices) * (1 + tax), 2)\n")  # fmt: skip
    good = BASE_CALC.replace("def total(prices):\n    return sum(prices)\n", GOOD_RESOLUTION)

    def snap(name: str, calc: str) -> Snapshot:
        async def load() -> bytes:
            return _tar({"calc.py": calc, "test_calc.py": MAIN_TESTS})

        return Snapshot(name, f"{name}-{hash(calc)}", load)

    pipe = VerificationPipeline(sandbox, MemoryBaselineCache())
    ok = await pipe.verify(
        "it/docker", cfg, snap("base", tax), snap("head", tax), snap("candidate", good)
    )
    assert ok.verdict is Verdict.PASS, ok.reasons
    bad = await pipe.verify("it/docker", cfg, snap("base", tax), snap("head", tax),
                            snap("candidate", good.replace("* (1 + tax)", "")))  # fmt: skip
    assert bad.verdict is Verdict.FAIL
    assert bad.comparison is not None
    assert bad.comparison.regressions == ["test_calc::test_total_with_tax"]
