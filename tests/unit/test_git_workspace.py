from pathlib import Path

import pytest

from mergeagent.conflicts.models import FileConflictKind
from mergeagent.workspace.git_workspace import GitWorkspace, MergeOutcome, _auth_env, _refspec
from mergeagent.workspace.safety import (
    UnsafeInputError,
    contained,
    safe_ref,
    safe_relpath,
    safe_repo_name,
)
from tests.fixtures.repo_factory import GOOD_RESOLUTION


async def _merged(conflict_repo: Path, tmp_path: Path) -> tuple[GitWorkspace, MergeOutcome]:
    ws = await GitWorkspace.clone(str(conflict_repo), tmp_path / "ws", refs=["main", "feature"])
    ours = await ws.resolve("feature")
    theirs = await ws.resolve("main")
    return ws, await ws.start_merge(ours, theirs)


async def test_clone_and_merge_collects_all_conflict_kinds(
    conflict_repo: Path, tmp_path: Path
) -> None:
    _, outcome = await _merged(conflict_repo, tmp_path)
    assert not outcome.clean
    conflicts = outcome.conflicts
    by_path = {f.path: f for f in conflicts.files}
    assert set(by_path) == {"app.py", "calc.py", "legacy.py", "test_calc.py"}
    assert by_path["legacy.py"].kind is FileConflictKind.DELETE_MODIFY
    assert by_path["legacy.py"].deleted_by == "theirs"
    calc = by_path["calc.py"].hunks[0]
    assert "discount" in calc.ours
    assert "tax" in calc.theirs
    assert calc.base is not None
    assert "sum(prices)" in calc.base
    assert outcome.merge_base is not None


async def test_write_resolved_commit_and_archive(conflict_repo: Path, tmp_path: Path) -> None:
    ws, outcome = await _merged(conflict_repo, tmp_path)
    cf = outcome.conflicts.file("calc.py")
    resolved = cf.segments[0] + GOOD_RESOLUTION + cf.segments[1]
    await ws.write_resolved("calc.py", resolved)
    await ws.write_resolved("legacy.py", None)
    await ws.write_resolved("app.py", "import os\nimport json\nimport sys\n")
    await ws.write_resolved("test_calc.py", "def test_x():\n    pass\n")

    diff = await ws.resolution_diff(["calc.py"])
    assert "+def total(prices, tax=0.0, discount=0.0):" in diff

    tree = await ws.write_tree()
    tar = await ws.archive(tree)
    assert len(tar) > 1024

    sha = await ws.commit_merge("Merge main into feature")
    parents = (await ws.run("rev-list", "--parents", "-n1", sha)).stdout.split()
    assert parents[1:] == [outcome.ours_sha, outcome.theirs_sha]
    assert not (ws.root / "legacy.py").exists()


async def test_read_helpers(conflict_repo: Path, tmp_path: Path) -> None:
    ws, outcome = await _merged(conflict_repo, tmp_path)
    ours = outcome.ours_sha
    theirs = outcome.theirs_sha
    assert "discount" in await ws.read_file(ours, "calc.py")
    with pytest.raises(FileNotFoundError):
        await ws.read_file(theirs, "legacy.py")
    assert "calc.py" in await ws.list_files(ours)
    assert "closes #7" in await ws.commits_between(theirs, ours)
    assert "feature: discounts" in await ws.log_for_lines(ours, "calc.py", 4, 6)
    assert "tax" in await ws.diff_refs(ours, theirs, "calc.py")


async def test_clean_merge(tmp_path: Path, conflict_repo: Path) -> None:
    ws = await GitWorkspace.clone(str(conflict_repo), tmp_path / "ws", refs=["main"])
    head = await ws.resolve("main")
    outcome = await ws.start_merge(head, head)
    assert outcome.clean


def test_refspec_and_auth_env() -> None:
    assert _refspec("main") == "+refs/heads/main:refs/remotes/origin/main"
    assert _refspec("refs/pull/3/head") == "+refs/pull/3/head:refs/remotes/origin/pull/3/head"
    sha = "a" * 40
    assert _refspec(sha) == sha
    env = _auth_env("tok")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "tok" not in env["GIT_CONFIG_VALUE_0"]  # base64 encoded, never raw
    assert _auth_env(None) == {}


@pytest.mark.parametrize("bad", ["-x", "a..b", "/abs", "a b", "x.lock", "--upload-pack=evil"])
def test_safe_ref_rejects(bad: str) -> None:
    with pytest.raises(UnsafeInputError):
        safe_ref(bad)


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", ".git/config", "", "-rf"])
def test_safe_relpath_rejects(bad: str) -> None:
    with pytest.raises(UnsafeInputError):
        safe_relpath(bad)


def test_contained_blocks_symlink_escape(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "link").symlink_to("/etc")
    with pytest.raises(UnsafeInputError):
        contained(tmp_path / "repo", "link/passwd")
    assert safe_repo_name("octo/hello-world") == "octo/hello-world"
    with pytest.raises(UnsafeInputError):
        safe_repo_name("octo/../x/y")
