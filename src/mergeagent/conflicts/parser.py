"""Parser for git conflict markers (``merge`` and ``diff3`` styles).

Line endings are preserved (``splitlines(keepends=True)``) so re-assembling a file that had no
conflicts in a region yields byte-identical output.
"""

from __future__ import annotations

from enum import Enum, auto

from mergeagent.conflicts.models import ConflictedFile, ConflictHunk, FileConflictKind

DEFAULT_MARKER_SIZE = 7
CONTEXT_LINES = 12


class ConflictParseError(ValueError):
    pass


class _State(Enum):
    OUTSIDE = auto()
    OURS = auto()
    BASE = auto()
    THEIRS = auto()


def _marker(line: str, char: str, size: int) -> str | None:
    """Return the label if ``line`` is a conflict marker of ``char`` repeated ``size`` times."""
    body = line.rstrip("\r\n")
    prefix = char * size
    if not body.startswith(prefix):
        return None
    rest = body[size:]
    if rest == "":
        return ""
    if rest.startswith(" ") and not rest.startswith(" " + char):
        return rest[1:]
    return None


def has_conflict_markers(text: str, marker_size: int = DEFAULT_MARKER_SIZE) -> bool:
    """True when the text still contains opening/closing conflict markers."""
    for line in text.splitlines():
        if (
            _marker(line, "<", marker_size) is not None
            or _marker(line, ">", marker_size) is not None
        ):
            return True
    return False


def parse_conflicted_text(
    path: str, text: str, marker_size: int = DEFAULT_MARKER_SIZE
) -> ConflictedFile:
    lines = text.splitlines(keepends=True)
    segments: list[str] = []
    hunks: list[ConflictHunk] = []

    state = _State.OUTSIDE
    seg: list[str] = []
    ours: list[str] = []
    base: list[str] = []
    theirs: list[str] = []
    saw_base = False
    start = 0
    ours_label = theirs_label = ""

    for lineno, line in enumerate(lines, start=1):
        if state is _State.OUTSIDE:
            label = _marker(line, "<", marker_size)
            if label is not None:
                segments.append("".join(seg))
                seg, ours, base, theirs = [], [], [], []
                saw_base = False
                start, ours_label = lineno, label
                state = _State.OURS
            else:
                seg.append(line)
        elif state is _State.OURS:
            if _marker(line, "|", marker_size) is not None:
                saw_base = True
                state = _State.BASE
            elif _marker(line, "=", marker_size) == "":
                state = _State.THEIRS
            elif _marker(line, "<", marker_size) is not None:
                raise ConflictParseError(f"{path}:{lineno}: nested conflict marker")
            else:
                ours.append(line)
        elif state is _State.BASE:
            if _marker(line, "=", marker_size) == "":
                state = _State.THEIRS
            else:
                base.append(line)
        else:  # THEIRS
            label = _marker(line, ">", marker_size)
            if label is not None:
                theirs_label = label
                hunks.append(
                    ConflictHunk(
                        path=path,
                        index=len(hunks),
                        start_line=start,
                        end_line=lineno,
                        ours="".join(ours),
                        theirs="".join(theirs),
                        base="".join(base) if saw_base else None,
                        ours_label=ours_label,
                        theirs_label=theirs_label,
                    )
                )
                state = _State.OUTSIDE
            else:
                theirs.append(line)

    if state is not _State.OUTSIDE:
        raise ConflictParseError(f"{path}: unterminated conflict starting at line {start}")
    segments.append("".join(seg))

    hunks = [_with_context(h, segments) for h in hunks]
    return ConflictedFile(
        path=path, kind=FileConflictKind.CONTENT, segments=segments, hunks=hunks, original=text
    )


def _with_context(hunk: ConflictHunk, segments: list[str]) -> ConflictHunk:
    before = segments[hunk.index].splitlines(keepends=True)[-CONTEXT_LINES:]
    after = segments[hunk.index + 1].splitlines(keepends=True)[:CONTEXT_LINES]
    return ConflictHunk(
        path=hunk.path,
        index=hunk.index,
        start_line=hunk.start_line,
        end_line=hunk.end_line,
        ours=hunk.ours,
        theirs=hunk.theirs,
        base=hunk.base,
        ours_label=hunk.ours_label,
        theirs_label=hunk.theirs_label,
        context_before="".join(before),
        context_after="".join(after),
    )


def delete_modify_file(
    path: str, *, ours: str | None, theirs: str | None, base: str | None
) -> ConflictedFile:
    """Model a modify/delete conflict as a single whole-file hunk.

    The side that deleted the file contributes an empty string. A resolution with
    ``delete_file=True`` removes the file; otherwise its text becomes the file content.
    """
    if (ours is None) == (theirs is None):
        raise ValueError("exactly one side must have deleted the file")
    hunk = ConflictHunk(
        path=path,
        index=0,
        start_line=1,
        end_line=1,
        ours=ours or "",
        theirs=theirs or "",
        base=base,
        ours_label="PR head" + (" (deleted)" if ours is None else ""),
        theirs_label="target" + (" (deleted)" if theirs is None else ""),
    )
    return ConflictedFile(
        path=path,
        kind=FileConflictKind.DELETE_MODIFY,
        segments=["", ""],
        hunks=[hunk],
        deleted_by="ours" if ours is None else "theirs",
    )
