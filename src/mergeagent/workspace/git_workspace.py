"""Async, hardened wrapper around the git CLI.

Security properties (the repository being merged is untrusted input):

* hooks are disabled (``core.hooksPath=/dev/null``) and system/global config is ignored, so a
  repository cannot make git execute code on the worker;
* only ``https`` and ``file`` transports are allowed; ``ext::`` is blocked;
* credentials are injected through ``GIT_CONFIG_*`` environment variables, never argv (argv is
  visible to every process on the host via ``ps``);
* LFS smudge is skipped so large binaries are not downloaded.

No code from the repository is ever executed here: builds and tests run in the sandbox.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mergeagent.conflicts.models import ConflictedFile, MergeConflictSet
from mergeagent.conflicts.parser import delete_modify_file, parse_conflicted_text
from mergeagent.logging import get_logger
from mergeagent.workspace.safety import contained, safe_ref, safe_relpath

log = get_logger(__name__)

BOT_NAME = "mergeagent[bot]"
BOT_EMAIL = "mergeagent[bot]@users.noreply.github.com"

_HARDENING = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.ext.allow=never",
    "-c", "merge.conflictStyle=diff3",
    "-c", "merge.renames=true",
    "-c", "rerere.enabled=false",
    "-c", "advice.detachedHead=false",
    "-c", f"user.name={BOT_NAME}",
    "-c", f"user.email={BOT_EMAIL}",
]  # fmt: skip


class GitError(RuntimeError):
    def __init__(self, args: Sequence[str], code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args[:3])}... exited {code}: {stderr.strip()[:500]}")
        self.code = code
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class GitResult:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    clean: bool
    conflicts: MergeConflictSet
    unsupported: list[str]  # paths with conflicts we cannot model (binary, rename/rename...)
    ours_sha: str
    theirs_sha: str
    merge_base: str | None


def _refspec(ref: str) -> str:
    """Map a branch / PR ref to a remote-tracking refspec; SHAs are fetched as-is."""
    if re.fullmatch(r"[0-9a-f]{40,64}", ref):
        return ref
    full = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
    name = full.removeprefix("refs/heads/").removeprefix("refs/")
    return f"+{full}:refs/remotes/origin/{name}"


def _auth_env(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


class GitWorkspace:
    def __init__(self, root: Path, token: str | None = None) -> None:
        self.root = root
        self._token = token

    # ------------------------------------------------------------------ plumbing
    def _env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "https:file",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "LC_ALL": "C",
        }
        env.update(_auth_env(self._token))
        return env

    async def run(
        self, *args: str, check: bool = True, stdin: bytes | None = None, cwd: Path | None = None
    ) -> GitResult:
        raw = await self.run_bytes(*args, check=check, stdin=stdin, cwd=cwd)
        return GitResult(raw[0], raw[1].decode(errors="replace"), raw[2].decode(errors="replace"))

    async def run_bytes(
        self, *args: str, check: bool = True, stdin: bytes | None = None, cwd: Path | None = None
    ) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *_HARDENING,
            *args,
            cwd=str(cwd or self.root),
            env=self._env(),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(stdin)
        code = proc.returncode or 0
        if check and code != 0:
            raise GitError(args, code, err.decode(errors="replace"))
        return code, out, err

    # ------------------------------------------------------------------ setup
    @classmethod
    async def clone(
        cls, url: str, dest: Path, *, refs: Sequence[str], token: str | None = None
    ) -> GitWorkspace:
        """Blobless fetch of just the refs we need (full history for merge-base, lazy blobs)."""
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        ws = cls(dest, token=token)
        await ws.run("init", "--quiet")
        await ws.run("remote", "add", "origin", url)
        local = url.startswith(("file://", "/"))
        specs = [_refspec(safe_ref(r)) for r in refs]
        if local:
            # Local transports don't serve arbitrary SHAs; fetching all heads makes them reachable.
            specs = ["+refs/heads/*:refs/remotes/origin/*", *(s for s in specs if ":" in s)]
        filt = [] if local else ["--filter=blob:none"]
        await ws.run("fetch", "--quiet", "--no-tags", *filt, "origin", *specs)
        return ws

    async def resolve(self, ref: str) -> str:
        """Resolve a SHA, a fetched branch (``origin/<ref>``) or a PR ref to a commit SHA."""
        safe_ref(ref)
        name = ref.removeprefix("refs/heads/").removeprefix("refs/")
        for candidate in (ref, f"origin/{name}"):
            res = await self.run(
                "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", check=False
            )
            if res.code == 0:
                return res.stdout.strip()
        raise GitError(["rev-parse", ref], 128, f"unknown ref {ref}")

    async def merge_base(self, a: str, b: str) -> str | None:
        res = await self.run("merge-base", safe_ref(a), safe_ref(b), check=False)
        return res.stdout.strip() or None

    # ------------------------------------------------------------------ merge
    async def start_merge(self, ours: str, theirs: str) -> MergeOutcome:
        """Check out ``ours`` and merge ``theirs`` without committing; collect conflicts."""
        ours_sha, theirs_sha = safe_ref(ours), safe_ref(theirs)
        await self.run("checkout", "--quiet", "--force", "--detach", ours_sha)
        base = await self.merge_base(ours_sha, theirs_sha)
        res = await self.run("merge", "--no-commit", "--no-ff", "--quiet", theirs_sha, check=False)
        if res.code == 0:
            return MergeOutcome(True, MergeConflictSet(), [], ours_sha, theirs_sha, base)
        if res.code != 1:
            raise GitError(["merge"], res.code, res.stderr)
        conflicts, unsupported = await self._collect_conflicts()
        return MergeOutcome(False, conflicts, unsupported, ours_sha, theirs_sha, base)

    async def _unmerged_stages(self) -> dict[str, set[int]]:
        out = (await self.run("ls-files", "-u", "-z")).stdout
        stages: dict[str, set[int]] = {}
        for entry in filter(None, out.split("\0")):
            meta, _, path = entry.partition("\t")
            stages.setdefault(path, set()).add(int(meta.split()[2]))
        return stages

    async def _stage_text(self, stage: int, path: str) -> str | None:
        code, out, _ = await self.run_bytes("show", f":{stage}:{path}", check=False)
        if code != 0:
            return None
        if b"\0" in out[:8000]:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary file")
        return out.decode()

    async def _collect_conflicts(self) -> tuple[MergeConflictSet, list[str]]:
        files: list[ConflictedFile] = []
        unsupported: list[str] = []
        for path, stages in sorted((await self._unmerged_stages()).items()):
            try:
                if {2, 3} <= stages:  # both modified / both added -> markers in worktree
                    raw = contained(self.root, path).read_bytes()
                    if b"\0" in raw[:8000]:
                        unsupported.append(path)
                        continue
                    cf = parse_conflicted_text(path, raw.decode())
                    if not cf.hunks:
                        unsupported.append(path)  # e.g. mode-only conflicts
                        continue
                    files.append(cf)
                elif 1 in stages and (2 in stages) != (3 in stages):  # modify/delete
                    files.append(
                        delete_modify_file(
                            path,
                            ours=await self._stage_text(2, path),
                            theirs=await self._stage_text(3, path),
                            base=await self._stage_text(1, path),
                        )
                    )
                else:
                    unsupported.append(path)
            except (UnicodeDecodeError, ValueError) as exc:
                log.warning("conflict.unsupported", path=path, error=str(exc))
                unsupported.append(path)
        return MergeConflictSet(files), unsupported

    async def abort_merge(self) -> None:
        await self.run("merge", "--abort", check=False)

    # ------------------------------------------------------------------ reads
    async def read_file(self, ref: str, path: str, max_bytes: int = 400_000) -> str:
        code, out, err = await self.run_bytes(
            "show", f"{safe_ref(ref)}:{safe_relpath(path)}", check=False
        )
        if code != 0:
            raise FileNotFoundError(f"{path} at {ref}: {err.decode(errors='replace').strip()}")
        return out[:max_bytes].decode(errors="replace")

    async def list_files(self, ref: str) -> list[str]:
        out = (await self.run("ls-tree", "-r", "--name-only", "-z", safe_ref(ref))).stdout
        return [p for p in out.split("\0") if p]

    async def log_for_lines(self, ref: str, path: str, start: int, end: int, n: int = 5) -> str:
        res = await self.run(
            "log", f"-n{n}", "--no-patch", "--format=%h %an %as%n  %s%n%w(0,2,2)%b",
            f"-L{start},{end}:{safe_relpath(path)}", safe_ref(ref),
            check=False,
        )  # fmt: skip
        return res.stdout[:6000] if res.code == 0 else ""

    async def commits_between(self, base: str, tip: str, path: str | None = None) -> str:
        args = ["log", "-n30", "--format=%h %as %an%n  %s", f"{safe_ref(base)}..{safe_ref(tip)}"]
        if path:
            args += ["--", safe_relpath(path)]
        return (await self.run(*args, check=False)).stdout[:8000]

    async def blame(self, ref: str, path: str, start: int, end: int) -> str:
        res = await self.run(
            "blame", "-s", f"-L{start},{end}", safe_ref(ref), "--", safe_relpath(path), check=False
        )
        return res.stdout[:6000]

    async def diff_refs(self, a: str, b: str, path: str | None = None) -> str:
        args = ["diff", "--no-color", "-U3", safe_ref(a), safe_ref(b)]
        if path:
            args += ["--", safe_relpath(path)]
        return (await self.run(*args, check=False)).stdout

    # ------------------------------------------------------------------ writes
    async def write_resolved(self, path: str, content: str | None) -> None:
        """Write (or delete, if ``content`` is None) a resolved file and stage it."""
        target = contained(self.root, path)
        if content is None:
            # --ignore-unmatch keeps this idempotent across verification retries.
            await self.run("rm", "--quiet", "--force", "--ignore-unmatch", "--", safe_relpath(path))
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        await self.run("add", "--", safe_relpath(path))

    async def resolution_diff(self, paths: Sequence[str]) -> str:
        """Diff of the resolved files against the PR head (what a reviewer needs to check)."""
        if not paths:
            return ""
        return (
            await self.run(
                "diff", "--no-color", "--cached", "HEAD", "--", *map(safe_relpath, paths),
                check=False,
            )
        ).stdout  # fmt: skip

    async def write_tree(self) -> str:
        return (await self.run("write-tree")).stdout.strip()

    async def archive(self, tree_ish: str) -> bytes:
        """Tarball of a commit or tree, used to seed sandbox containers."""
        _, out, _ = await self.run_bytes("archive", "--format=tar", safe_ref(tree_ish))
        return out

    async def commit_merge(self, message: str) -> str:
        await self.run("commit", "--quiet", "--no-verify", "-m", message)
        return (await self.run("rev-parse", "HEAD")).stdout.strip()

    async def push(self, branch: str, *, force: bool = False) -> None:
        refspec = f"HEAD:refs/heads/{safe_ref(branch)}"
        args = ["push", "--quiet", "origin", refspec]
        if force:
            args.insert(1, "--force-with-lease")
        await self.run(*args)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
