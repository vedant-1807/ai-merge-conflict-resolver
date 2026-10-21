"""Deterministic triage: classify hunks, resolve the trivial ones, score the rest.

Anything resolved here costs zero LLM tokens. The complexity score feeds the model router so
that easy semantic hunks go to a small model and hard ones to a large model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import PurePosixPath

from mergeagent.conflicts.models import (
    ConflictedFile,
    ConflictHunk,
    FileConflictKind,
    HunkCategory,
    HunkResolution,
)

LOCKFILES = {
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
}
PROSE_SUFFIXES = {".md", ".rst", ".txt", ".adoc", ""}
CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".c",
    ".h", ".cc", ".cpp", ".cs", ".php", ".scala", ".swift",
}  # fmt: skip
_IMPORT_RE = re.compile(
    r"^\s*(?:import\s|from\s+\S+\s+import\s|using\s|#include\s|require\(|const\s+\w+\s*=\s*require\()"
)


@dataclass(frozen=True, slots=True)
class TriageResult:
    hunk: ConflictHunk
    category: HunkCategory
    complexity: float  # 0.0 (trivial) .. 1.0 (very hard)
    resolution: HunkResolution | None  # set when resolved deterministically


def _ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _lines(text: str | None) -> list[str]:
    return (text or "").splitlines(keepends=True)


def _is_import_block(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and all(_IMPORT_RE.match(ln) for ln in lines)


def _merge_imports(h: ConflictHunk) -> str:
    ours, theirs, base = _lines(h.ours), _lines(h.theirs), _lines(h.base)
    removed = {ln for ln in base if ln not in ours or ln not in theirs}
    merged: list[str] = []
    for ln in ours + theirs:
        if ln not in merged and ln not in removed:
            merged.append(ln)
    return "".join(merged)


def _append_both(h: ConflictHunk) -> str | None:
    if h.base is None:
        return None
    if h.ours.startswith(h.base) and h.theirs.startswith(h.base):
        ours_add = h.ours[len(h.base) :]
        theirs_add = h.theirs[len(h.base) :]
        if ours_add and theirs_add:
            return h.base + theirs_add + ours_add
    return None


def _resolve(h: ConflictHunk, category: HunkCategory, text: str, why: str) -> HunkResolution:
    return HunkResolution(
        hunk_id=h.id, text=text, strategy=f"deterministic:{category.value}", rationale=why
    )


def complexity_score(h: ConflictHunk) -> float:
    """Heuristic difficulty in [0, 1] from size, divergence and file type."""
    ours, theirs = h.ours, h.theirs
    size = len(_lines(ours)) + len(_lines(theirs))
    divergence = 1.0 - SequenceMatcher(a=ours, b=theirs, autojunk=False).ratio()
    suffix = PurePosixPath(h.path).suffix.lower()
    code_weight = 1.0 if suffix in CODE_SUFFIXES else 0.6
    no_base_penalty = 0.15 if h.base is None else 0.0
    size_term = min(size / 120.0, 1.0)
    score = code_weight * (0.45 * divergence + 0.4 * size_term) + no_base_penalty
    return round(min(max(score, 0.0), 1.0), 3)


def classify_hunk(h: ConflictHunk, kind: FileConflictKind) -> TriageResult:
    name = PurePosixPath(h.path).name
    suffix = PurePosixPath(h.path).suffix.lower()

    if kind is FileConflictKind.DELETE_MODIFY:
        return TriageResult(h, HunkCategory.DELETE_MODIFY, 0.5, None)

    if h.ours == h.theirs:
        return TriageResult(
            h, HunkCategory.IDENTICAL, 0.0,
            _resolve(h, HunkCategory.IDENTICAL, h.ours, "both sides made the same change"),
        )  # fmt: skip
    if h.base is not None and h.ours == h.base:
        return TriageResult(
            h, HunkCategory.ONE_SIDED, 0.0,
            _resolve(h, HunkCategory.ONE_SIDED, h.theirs, "only the target branch changed"),
        )  # fmt: skip
    if h.base is not None and h.theirs == h.base:
        return TriageResult(
            h, HunkCategory.ONE_SIDED, 0.0,
            _resolve(h, HunkCategory.ONE_SIDED, h.ours, "only the PR changed"),
        )  # fmt: skip
    if _ws(h.ours) == _ws(h.theirs):
        return TriageResult(
            h, HunkCategory.WHITESPACE, 0.05,
            _resolve(h, HunkCategory.WHITESPACE, h.ours, "sides differ only in whitespace"),
        )  # fmt: skip

    if name in LOCKFILES:
        if name == "go.sum":
            union = _merge_imports(h)  # line-set union is valid for go.sum
            return TriageResult(
                h, HunkCategory.LOCKFILE, 0.05,
                _resolve(h, HunkCategory.LOCKFILE, union, "go.sum line union"),
            )  # fmt: skip
        return TriageResult(
            h, HunkCategory.LOCKFILE, 0.1,
            _resolve(
                h, HunkCategory.LOCKFILE, h.theirs,
                "lockfile: take target branch; regenerate via repo setup command",
            ),
        )  # fmt: skip

    if _is_import_block(h.ours) and _is_import_block(h.theirs):
        return TriageResult(
            h, HunkCategory.IMPORTS, 0.1,
            _resolve(h, HunkCategory.IMPORTS, _merge_imports(h), "union of import statements"),
        )  # fmt: skip

    if suffix in PROSE_SUFFIXES:
        appended = _append_both(h)
        if appended is not None:
            return TriageResult(
                h, HunkCategory.APPEND_BOTH, 0.05,
                _resolve(h, HunkCategory.APPEND_BOTH, appended, "both sides appended entries"),
            )  # fmt: skip

    return TriageResult(h, HunkCategory.SEMANTIC, complexity_score(h), None)


def triage_file(cf: ConflictedFile) -> list[TriageResult]:
    return [classify_hunk(h, cf.kind) for h in cf.hunks]
