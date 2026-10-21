"""Domain model for merge conflicts.

Orientation used everywhere in the code base: we check out the **PR head** and merge the
**base branch** into it (what GitHub's "Update branch" does). Therefore:

* ``ours``   = the PR head (the contributor's change)
* ``theirs`` = the base branch (e.g. ``main``) the PR targets
* ``base``   = the merge-base ancestor (available because we merge with ``diff3``)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FileConflictKind(StrEnum):
    CONTENT = "content"  # both modified (UU) or both added (AA): file contains markers
    DELETE_MODIFY = "delete_modify"  # one side deleted, the other modified (UD / DU)


class HunkCategory(StrEnum):
    IDENTICAL = "identical"  # both sides made the same change
    ONE_SIDED = "one_sided"  # one side equals the ancestor; take the other
    WHITESPACE = "whitespace"  # sides differ only in whitespace
    IMPORTS = "imports"  # both sides edited an import block
    APPEND_BOTH = "append_both"  # both sides only appended (changelogs, lists in docs)
    LOCKFILE = "lockfile"  # generated dependency lockfile
    DELETE_MODIFY = "delete_modify"
    SEMANTIC = "semantic"  # needs real reasoning (LLM)

    @property
    def is_trivial(self) -> bool:
        return self is not HunkCategory.SEMANTIC and self is not HunkCategory.DELETE_MODIFY


@dataclass(frozen=True, slots=True)
class ConflictHunk:
    path: str
    index: int
    start_line: int  # 1-based line number of the opening marker in the conflicted file
    end_line: int  # 1-based line number of the closing marker
    ours: str
    theirs: str
    base: str | None  # None when the merge was not performed with diff3 style
    ours_label: str = "ours"
    theirs_label: str = "theirs"
    context_before: str = ""
    context_after: str = ""

    @property
    def id(self) -> str:
        return f"{self.path}#{self.index}"

    def render(self, max_chars: int = 12_000) -> str:
        """Human/LLM readable view of the hunk with surrounding context."""
        base = self.base if self.base is not None else "<ancestor unavailable>\n"
        text = (
            f"File: {self.path} (hunk {self.index}, lines {self.start_line}-{self.end_line})\n"
            f"--- context before ---\n{self.context_before}"
            f"--- OURS: PR head ({self.ours_label}) ---\n{self.ours}"
            f"--- BASE: common ancestor ---\n{base}"
            f"--- THEIRS: target branch ({self.theirs_label}) ---\n{self.theirs}"
            f"--- context after ---\n{self.context_after}"
        )
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]\n"
        return text


@dataclass(slots=True)
class ConflictedFile:
    path: str
    kind: FileConflictKind
    # Text between hunks. Invariant: len(segments) == len(hunks) + 1, so the resolved file is
    # segments[0] + r0 + segments[1] + r1 + ... + segments[-1].
    segments: list[str]
    hunks: list[ConflictHunk]
    original: str = ""
    # For DELETE_MODIFY: which side deleted the file.
    deleted_by: str | None = None

    def __post_init__(self) -> None:
        if len(self.segments) != len(self.hunks) + 1:
            raise ValueError("segments must have exactly one more element than hunks")


@dataclass(slots=True)
class HunkResolution:
    hunk_id: str
    text: str
    strategy: str  # "deterministic:<category>" | "llm:<model>" | "human"
    rationale: str = ""
    delete_file: bool = False


@dataclass(slots=True)
class MergeConflictSet:
    files: list[ConflictedFile] = field(default_factory=list)

    @property
    def hunks(self) -> list[ConflictHunk]:
        return [h for f in self.files for h in f.hunks]

    def file(self, path: str) -> ConflictedFile:
        for f in self.files:
            if f.path == path:
                return f
        raise KeyError(path)

    def hunk(self, hunk_id: str) -> ConflictHunk:
        path, _, idx = hunk_id.rpartition("#")
        return self.file(path).hunks[int(idx)]
