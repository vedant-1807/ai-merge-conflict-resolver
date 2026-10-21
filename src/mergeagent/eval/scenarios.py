"""Scenario records and small I/O helpers shared by the eval commands."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Scenario", "append_jsonl", "asdict", "mirror", "read_jsonl"]


@dataclass
class Scenario:
    repo: str
    url: str
    merge_sha: str  # the human merge commit (reference resolution)
    ours: str  # merged-in branch tip (second parent)
    theirs: str  # target branch tip (first parent)
    files: list[str] = field(default_factory=list)
    hunks: int = 0

    @property
    def id(self) -> str:
        return f"{self.repo}@{self.merge_sha[:10]}"


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def mirror(url: str, cache_dir: Path) -> Path:
    """Local bare mirror of a remote repo (re-used across mine/run/mutate)."""
    if Path(url).exists():
        return Path(url).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / (hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()[:12] + ".git")
    if dest.exists():
        subprocess.run(
            ["git", "-C", str(dest), "fetch", "--quiet", "origin", "+refs/heads/*:refs/heads/*"],
            check=True,
        )
    else:
        subprocess.run(["git", "clone", "--quiet", "--bare", url, str(dest)], check=True)
    return dest.resolve()
