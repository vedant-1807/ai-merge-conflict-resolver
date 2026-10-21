"""Per-repository verification config: ``.mergeagent.yml`` or auto-detection.

Example ``.mergeagent.yml``::

    image: python:3.12-slim
    setup:
      - pip install -e '.[test]'
    build: python -m compileall -q src
    typecheck: mypy src
    test: pytest -q --junitxml=/tmp/junit.xml
    test_report: /tmp/junit.xml
    acceptance:
      - python -m mypkg --version
    timeout_s: 900
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

import yaml
from pydantic import BaseModel, Field

CONFIG_FILE = ".mergeagent.yml"
JUNIT = "/tmp/mergeagent-junit.xml"  # noqa: S108 - path inside the sandbox container


class RepoConfig(BaseModel):
    image: str | None = None
    setup: list[str] = Field(default_factory=list)
    build: str | None = None
    typecheck: str | None = None
    test: str | None = None
    test_report: str | None = None  # junit xml path, or "go-json" for `go test -json` output
    acceptance: list[str] = Field(default_factory=list)
    timeout_s: int = 900
    detected: str = "config"

    def fingerprint(self) -> str:
        """Stable hash used as part of the baseline-result cache key."""
        payload = self.model_dump(exclude={"detected", "acceptance"})
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def detect(files: Iterable[str]) -> RepoConfig:
    names = set(files)
    if names & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini"} or any(
        n.endswith(".py") for n in names
    ):
        setup = []
        if names & {"pyproject.toml", "setup.py"}:
            setup.append("pip install -q -e '.[test]' 2>/dev/null || pip install -q -e . || true")
        for req in ("requirements.txt", "requirements-dev.txt", "requirements/dev.txt",
                    "requirements/tests.txt", "requirements/test.txt"):  # fmt: skip
            if req in names:
                setup.append(f"pip install -q -r {req}")
        setup.append("pip install -q pytest 2>/dev/null || true")
        typecheck = "mypy ." if names & {"mypy.ini", ".mypy.ini"} else None
        return RepoConfig(
            setup=setup,
            build="python -m compileall -q -x '(^|/)(\\.|venv|node_modules)' .",
            typecheck=typecheck,
            test=f"python -m pytest -q -p no:cacheprovider --junitxml={JUNIT}",
            test_report=JUNIT,
            detected="python",
        )
    if "package.json" in names:
        return RepoConfig(
            image="node:22-bookworm-slim",
            setup=[
                "npm ci --ignore-scripts"
                if "package-lock.json" in names
                else "npm install --ignore-scripts"
            ],
            build="npm run build --if-present",
            typecheck="npx --no-install tsc --noEmit" if "tsconfig.json" in names else None,
            test="npm test --silent",
            detected="node",
        )
    if "go.mod" in names:
        return RepoConfig(
            image="golang:1.23",
            setup=["go mod download"],
            build="go build ./...",
            typecheck="go vet ./...",
            test="go test -json ./...",
            test_report="go-json",
            detected="go",
        )
    if "Cargo.toml" in names:
        return RepoConfig(
            image="rust:1.82", setup=["cargo fetch"], build="cargo build --offline",
            test="cargo test --offline", detected="rust",
        )  # fmt: skip
    return RepoConfig(detected="none")


def load(config_text: str | None, files: Iterable[str]) -> RepoConfig:
    if config_text:
        data = yaml.safe_load(config_text) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{CONFIG_FILE} must be a mapping")
        return RepoConfig.model_validate(data)
    return detect(files)
