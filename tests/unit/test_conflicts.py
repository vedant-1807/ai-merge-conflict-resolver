import pytest

from mergeagent.conflicts.applier import ResolutionError, apply_resolutions, segments_preserved
from mergeagent.conflicts.models import FileConflictKind, HunkCategory, HunkResolution
from mergeagent.conflicts.parser import (
    ConflictParseError,
    delete_modify_file,
    has_conflict_markers,
    parse_conflicted_text,
)
from mergeagent.conflicts.triage import classify_hunk, complexity_score
from mergeagent.conflicts.validators import static_problems, syntax_error

DIFF3 = """import os
<<<<<<< HEAD
def total(items):
    return sum(i.price for i in items)
||||||| merged common ancestors
def total(items):
    return sum(items)
=======
def total(items, tax=0.0):
    return sum(items) * (1 + tax)
>>>>>>> main
print("done")
<<<<<<< HEAD
A = 1
=======
A = 2
>>>>>>> main
"""


def res(hunk_id: str, text: str) -> HunkResolution:
    return HunkResolution(hunk_id=hunk_id, text=text, strategy="test")


def test_parse_diff3_hunks_and_segments() -> None:
    cf = parse_conflicted_text("calc.py", DIFF3)
    assert cf.kind is FileConflictKind.CONTENT
    assert len(cf.hunks) == 2
    first, second = cf.hunks
    assert first.ours_label == "HEAD"
    assert first.theirs_label == "main"
    assert first.base == "def total(items):\n    return sum(items)\n"
    assert "tax" in first.theirs
    assert first.start_line == 2
    assert first.end_line == 11
    assert second.base is None  # merge-style hunk without ancestor section
    assert cf.segments == ["import os\n", 'print("done")\n', ""]
    assert first.context_before == "import os\n"


def test_apply_round_trip_and_newline_normalisation() -> None:
    cf = parse_conflicted_text("calc.py", DIFF3)
    merged = apply_resolutions(
        cf,
        {
            0: res("calc.py#0", "def total(items, tax=0.0):\n    return 1"),  # no trailing \n
            1: res("calc.py#1", "A = 3\n"),
        },
    )
    assert merged == 'import os\ndef total(items, tax=0.0):\n    return 1\nprint("done")\nA = 3\n'
    assert merged is not None
    assert segments_preserved(cf, merged)
    assert not has_conflict_markers(merged)


def test_apply_rejects_missing_hunks_and_leftover_markers() -> None:
    cf = parse_conflicted_text("calc.py", DIFF3)
    with pytest.raises(ResolutionError, match="unresolved"):
        apply_resolutions(cf, {0: res("calc.py#0", "x\n")})
    with pytest.raises(ResolutionError, match="markers"):
        apply_resolutions(
            cf, {0: res("calc.py#0", "<<<<<<< HEAD\nx\n"), 1: res("calc.py#1", "y\n")}
        )


def test_crlf_preserved() -> None:
    text = "a\r\n<<<<<<< HEAD\r\nb\r\n=======\r\nc\r\n>>>>>>> main\r\nd\r\n"
    cf = parse_conflicted_text("f.txt", text)
    merged = apply_resolutions(cf, {0: res("f.txt#0", "bc")})
    assert merged == "a\r\nbc\r\nd\r\n"


def test_unterminated_and_nested_conflicts_raise() -> None:
    with pytest.raises(ConflictParseError, match="unterminated"):
        parse_conflicted_text("x", "<<<<<<< HEAD\na\n=======\nb\n")
    with pytest.raises(ConflictParseError, match="nested"):
        parse_conflicted_text("x", "<<<<<<< HEAD\n<<<<<<< HEAD\n")


def test_marker_like_text_is_not_a_marker() -> None:
    assert not has_conflict_markers("Title\n=======\n<<<<<<<<< too long\n")
    assert has_conflict_markers(">>>>>>> main\n")


def test_delete_modify() -> None:
    cf = delete_modify_file("old.py", ours=None, theirs="x = 1\n", base="x = 0\n")
    assert cf.deleted_by == "ours"
    deleted = HunkResolution("old.py#0", "", "test", delete_file=True)
    assert apply_resolutions(cf, {0: deleted}) is None
    assert apply_resolutions(cf, {0: res("old.py#0", "x = 1\n")}) == "x = 1\n"
    with pytest.raises(ValueError, match="exactly one"):
        delete_modify_file("a", ours="1", theirs="2", base=None)


@pytest.mark.parametrize(
    ("ours", "theirs", "base", "path", "expected_cat", "expected_text"),
    [
        ("x = 1\n", "x = 1\n", "x = 0\n", "a.py", HunkCategory.IDENTICAL, "x = 1\n"),
        ("x = 0\n", "x = 2\n", "x = 0\n", "a.py", HunkCategory.ONE_SIDED, "x = 2\n"),
        ("x = 3\n", "x = 0\n", "x = 0\n", "a.py", HunkCategory.ONE_SIDED, "x = 3\n"),
        ("x  =  1\n", "x = 1\n", "y\n", "a.py", HunkCategory.WHITESPACE, "x  =  1\n"),
        (
            "import os\nimport re\n",
            "import os\nimport sys\n",
            "import os\n",
            "a.py",
            HunkCategory.IMPORTS,
            "import os\nimport re\nimport sys\n",
        ),
        (
            "- a\n- ours\n",
            "- a\n- theirs\n",
            "- a\n",
            "CHANGES.md",
            HunkCategory.APPEND_BOTH,
            "- a\n- theirs\n- ours\n",
        ),
        ("v1\n", "v2\n", "v0\n", "uv.lock", HunkCategory.LOCKFILE, "v2\n"),
    ],
)
def test_triage_deterministic(
    ours: str,
    theirs: str,
    base: str,
    path: str,
    expected_cat: HunkCategory,
    expected_text: str,
) -> None:
    text = f"<<<<<<< HEAD\n{ours}||||||| base\n{base}=======\n{theirs}>>>>>>> main\n"
    hunk = parse_conflicted_text(path, text).hunks[0]
    result = classify_hunk(hunk, FileConflictKind.CONTENT)
    assert result.category is expected_cat
    assert result.resolution is not None
    assert result.resolution.text == expected_text
    assert result.category.is_trivial


def test_import_removed_on_one_side_is_dropped() -> None:
    text = (
        "<<<<<<< HEAD\nimport os\nimport json\n||||||| base\nimport os\nimport re\n"
        "=======\nimport re\nimport sys\n>>>>>>> main\n"
    )
    hunk = parse_conflicted_text("a.py", text).hunks[0]
    result = classify_hunk(hunk, FileConflictKind.CONTENT)
    assert result.resolution is not None
    assert result.resolution.text == "import json\nimport sys\n"


def test_semantic_conflict_scored() -> None:
    cf = parse_conflicted_text("calc.py", DIFF3)
    result = classify_hunk(cf.hunks[0], cf.kind)
    assert result.category is HunkCategory.SEMANTIC
    assert result.resolution is None
    assert 0.0 < result.complexity <= 1.0
    assert complexity_score(cf.hunks[0]) == result.complexity


def test_syntax_validators() -> None:
    assert syntax_error("a.py", "def f(:\n") is not None
    assert syntax_error("a.py", "def f():\n    pass\n") is None
    assert syntax_error("a.json", "{bad") is not None
    assert syntax_error("a.yaml", "a: [1, 2") is not None
    assert syntax_error("a.toml", "x = ") is not None
    assert syntax_error("a.unknown", "anything") is None
    assert static_problems({"a.py": "x = (\n", "b.py": None, "c.txt": "<<<<<<< HEAD\n"}) == [
        "a.py:1: '(' was never closed",
        "c.txt: conflict markers remain",
    ]
