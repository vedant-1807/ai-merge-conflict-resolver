"""Re-assemble a conflicted file from per-hunk resolutions.

Because only hunk regions are replaced, a resolution can never touch code outside the
conflict: the "edits stay inside conflict regions" guarantee holds by construction.
"""

from __future__ import annotations

from collections.abc import Mapping

from mergeagent.conflicts.models import ConflictedFile, FileConflictKind, HunkResolution
from mergeagent.conflicts.parser import has_conflict_markers


class ResolutionError(ValueError):
    pass


def _normalise(text: str, *, followed_by_text: bool, newline: str) -> str:
    # A region that is followed by more file content must end in a newline, otherwise the
    # next line would be glued onto the resolution's last line.
    if text and followed_by_text and not text.endswith(("\n", "\r")):
        return text + newline
    return text


def _detect_newline(cf: ConflictedFile) -> str:
    return "\r\n" if "\r\n" in cf.original else "\n"


def apply_resolutions(cf: ConflictedFile, resolutions: Mapping[int, HunkResolution]) -> str | None:
    """Return the resolved file content, or ``None`` if the file should be deleted."""
    missing = [h.index for h in cf.hunks if h.index not in resolutions]
    if missing:
        raise ResolutionError(f"{cf.path}: unresolved hunks {missing}")

    if cf.kind is FileConflictKind.DELETE_MODIFY:
        res = resolutions[0]
        return None if res.delete_file else res.text

    newline = _detect_newline(cf)
    out: list[str] = [cf.segments[0]]
    for hunk in cf.hunks:
        res = resolutions[hunk.index]
        if res.delete_file:
            raise ResolutionError(f"{cf.path}: delete_file is only valid for modify/delete")
        following = cf.segments[hunk.index + 1]
        out.append(_normalise(res.text, followed_by_text=bool(following), newline=newline))
        out.append(following)
    merged = "".join(out)
    if has_conflict_markers(merged):
        raise ResolutionError(f"{cf.path}: resolution still contains conflict markers")
    return merged


def segments_preserved(cf: ConflictedFile, final_text: str) -> bool:
    """Check that every non-conflicting segment survives, in order, in ``final_text``.

    Used to audit patches that were *not* produced through :func:`apply_resolutions`
    (e.g. the human reference resolution in the eval harness).
    """
    pos = 0
    for seg in cf.segments:
        if not seg:
            continue
        found = final_text.find(seg, pos)
        if found < 0:
            return False
        pos = found + len(seg)
    return True
