"""Demo repository with genuine merge conflicts, plus a scripted offline "LLM" for `mergeagent demo`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

BASE_CALC = '''"""Shopping cart maths."""


def total(prices):
    return sum(prices)


def count(prices):
    return len(prices)
'''

# Target branch (main): adds a tax rate.
MAIN_CALC = '''"""Shopping cart maths."""


def total(prices, tax=0.0):
    return round(sum(prices) * (1 + tax), 2)


def count(prices):
    return len(prices)
'''

# PR branch (feature): adds a discount.
FEATURE_CALC = '''"""Shopping cart maths."""


def total(prices, discount=0.0):
    subtotal = sum(prices)
    return round(subtotal - subtotal * discount, 2)


def count(prices):
    return len(prices)
'''

BASE_TESTS = """from calc import count, total


def test_total():
    assert total([1, 2, 3]) == 6


def test_count():
    assert count([1, 2]) == 2
"""

MAIN_TESTS = (
    BASE_TESTS
    + """

def test_total_with_tax():
    assert total([10, 10], tax=0.1) == 22.0
"""
)

FEATURE_TESTS = (
    BASE_TESTS
    + """

def test_total_with_discount():
    assert total([10, 10], discount=0.25) == 15.0
"""
)

# A resolution that satisfies both sides' intent.
GOOD_RESOLUTION = """def total(prices, tax=0.0, discount=0.0):
    subtotal = sum(prices)
    discounted = subtotal - subtotal * discount
    return round(discounted * (1 + tax), 2)
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def make_conflict_repo(root: Path) -> Path:
    """Repo with branches ``main`` (target) and ``feature`` (PR) that conflict in calc.py.

    The tests file conflicts too (both sides appended a test), plus a modify/delete on
    ``legacy.py`` and an import-block conflict in ``app.py``.
    """
    repo = root / "origin"
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet", "-b", "main")
    write(repo, "calc.py", BASE_CALC)
    write(repo, "test_calc.py", BASE_TESTS)
    write(repo, "legacy.py", "VALUE = 1\n")
    write(repo, "app.py", "import os\n\n\ndef main():\n    return os.getcwd()\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")

    git(repo, "checkout", "--quiet", "-b", "feature")
    write(repo, "calc.py", FEATURE_CALC)
    write(repo, "test_calc.py", FEATURE_TESTS)
    write(repo, "legacy.py", "VALUE = 2  # still used by the feature\n")
    write(repo, "app.py", "import os\nimport json\n\n\ndef main():\n    return os.getcwd()\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "feature: discounts (closes #7)")

    git(repo, "checkout", "--quiet", "main")
    write(repo, "calc.py", MAIN_CALC)
    write(repo, "test_calc.py", MAIN_TESTS)
    (repo / "legacy.py").unlink()
    write(repo, "app.py", "import os\nimport sys\n\n\ndef main():\n    return os.getcwd()\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "main: tax support, drop legacy module")
    return repo


def scripted_responder(ctx_ref: dict[str, object]):  # type: ignore[no-untyped-def]
    """Offline stand-in for Claude that plays each agent role on the demo repository.

    It exercises the real graph, MCP tools, gateway accounting and sandbox verification;
    only the "thinking" is canned. Use a real API key for real repositories.
    """
    import json
    import re

    from mergeagent.llm.providers.base import LLMRequest
    from mergeagent.llm.providers.fake import FakeTurn

    def last_is_tool_result(req: LLMRequest) -> bool:
        content = req.messages[-1]["content"]
        return (
            isinstance(content, list) and bool(content) and content[0].get("type") == "tool_result"
        )

    def respond(req: LLMRequest) -> FakeTurn:
        ctx = ctx_ref["ctx"]
        if req.system.startswith("You are the Analyzer"):
            if not last_is_tool_result(req):
                return FakeTurn(
                    tool_calls=[
                        ("git__show_hunk", {"hunk_id": "calc.py#0"}),
                        ("issues__get_commit_messages", {"side": "ours"}),
                    ]
                )
            return FakeTurn(
                text=json.dumps(
                    {
                        "calc.py#0": {
                            "ours_intent": "PR adds an optional discount to total()",
                            "theirs_intent": "target adds an optional tax rate to total()",
                            "constraints": ["both keyword arguments must keep working"],
                            "suggested_strategy": "apply discount then tax",
                        }
                    }
                )
            )
        if req.system.startswith("You are the Resolver"):
            if last_is_tool_result(req):
                return FakeTurn(
                    text="Combined discount and tax; kept both new tests; deleted unused module."
                )
            match = re.search(r"Hunks to resolve: (.*)", req.messages[0]["content"])
            if match is None:
                raise ValueError("resolver prompt has no hunk list")
            ids = match.group(1).split(", ")
            calls = []
            for hid in ids:
                h = ctx.conflicts.hunk(hid)  # type: ignore[attr-defined]
                if hid == "calc.py#0":
                    args = {
                        "hunk_id": hid,
                        "content": GOOD_RESOLUTION,
                        "rationale": "apply discount, then tax",
                    }
                elif hid.startswith("legacy.py"):
                    args = {
                        "hunk_id": hid,
                        "content": "",
                        "rationale": "deleted on target",
                        "delete_file": True,
                    }
                else:
                    args = {
                        "hunk_id": hid,
                        "content": h.theirs + h.ours[len(h.base or "") :],
                        "rationale": "keep tests from both sides",
                    }
                calls.append(("git__write_resolution", args))
            return FakeTurn(tool_calls=calls)
        if not last_is_tool_result(req):
            return FakeTurn(tool_calls=[("git__resolution_diff", {})])
        return FakeTurn(text='{"approve": true, "concerns": []}')

    return respond
