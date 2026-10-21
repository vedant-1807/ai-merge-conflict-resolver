"""Input hardening for anything that reaches a git command line or the filesystem."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

# Conservative subset of git-check-ref-format plus full/short SHAs.
_REF_RE = re.compile(r"^(?!.*\.\.)(?!.*//)(?!/)(?!.*/$)(?!.*\.lock$)[A-Za-z0-9._/\-]{1,200}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}$")


class UnsafeInputError(ValueError):
    pass


def safe_ref(ref: str) -> str:
    if ref.startswith("-") or not (_REF_RE.match(ref) or _SHA_RE.match(ref)):
        raise UnsafeInputError(f"refusing unsafe git ref: {ref!r}")
    return ref


def safe_repo_name(full_name: str) -> str:
    if not _REPO_RE.match(full_name):
        raise UnsafeInputError(f"invalid repository name: {full_name!r}")
    return full_name


def safe_relpath(path: str) -> str:
    """Validate a repository-relative POSIX path (no absolute paths, no traversal)."""
    p = PurePosixPath(path)
    if not path or path.startswith("-") or p.is_absolute() or ".." in p.parts or "\x00" in path:
        raise UnsafeInputError(f"refusing unsafe path: {path!r}")
    if p.parts and p.parts[0] == ".git":
        raise UnsafeInputError("refusing to touch .git internals")
    return str(p)


def contained(root: Path, relpath: str) -> Path:
    """Resolve ``relpath`` under ``root`` and make sure symlinks don't escape it."""
    target = (root / safe_relpath(relpath)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise UnsafeInputError(f"path escapes workspace: {relpath!r}")
    return target
