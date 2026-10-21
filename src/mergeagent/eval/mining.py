"""Mine benchmark scenarios: merge commits whose parents produce real text conflicts."""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from mergeagent.eval.scenarios import Scenario, append_jsonl, asdict, mirror
from mergeagent.workspace.git_workspace import GitWorkspace


async def mine_repo(
    name: str,
    url: str,
    branch: str,
    cache_dir: Path,
    *,
    target: int,
    max_hunks: int,
    max_candidates: int,
) -> list[Scenario]:
    src = mirror(url, cache_dir)
    ws = await GitWorkspace.clone(str(src), Path(tempfile.mkdtemp()) / "ws", refs=[branch])
    found: list[Scenario] = []
    try:
        out = (
            await ws.run(
                "rev-list", "--merges", "--parents", f"-n{max_candidates}", f"origin/{branch}"
            )
        ).stdout
        for line in out.splitlines():
            shas = line.split()
            if len(shas) != 3:  # skip octopus merges
                continue
            merge, first, second = shas
            outcome = await ws.start_merge(second, first)  # ours = merged branch, theirs = target
            if not outcome.clean and not outcome.unsupported:
                n = len(outcome.conflicts.hunks)
                if 0 < n <= max_hunks:
                    found.append(Scenario(name, url, merge, second, first,
                                          [f.path for f in outcome.conflicts.files], n))  # fmt: skip
            await ws.abort_merge()
            await ws.run("reset", "--quiet", "--hard", check=False)
            if len(found) >= target:
                break
    finally:
        ws.cleanup()
    return found


async def mine_dataset(dataset: Path, out: Path, cache_dir: Path) -> int:
    cfg = yaml.safe_load(dataset.read_text())
    out.unlink(missing_ok=True)
    total = 0
    for r in cfg["repos"]:
        scenarios = await mine_repo(
            r["name"], r["url"], r.get("branch", "main"), cache_dir,
            target=cfg.get("target_per_repo", 20), max_hunks=cfg.get("max_hunks", 12),
            max_candidates=cfg.get("max_candidates_per_repo", 4000),
        )  # fmt: skip
        for s in scenarios:
            append_jsonl(out, asdict(s))
        total += len(scenarios)
    return total
