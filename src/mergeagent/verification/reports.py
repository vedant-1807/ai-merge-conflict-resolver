"""Parse test-runner output into ``{test_id: outcome}`` maps."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from xml.etree import ElementTree as ET


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


TestResults = dict[str, Outcome]


def parse_junit(xml_bytes: bytes) -> TestResults:
    root = ET.fromstring(xml_bytes)  # noqa: S314
    results: TestResults = {}
    for case in root.iter("testcase"):
        cls = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{cls}::{name}" if cls else name
        if case.find("failure") is not None or case.find("error") is not None:
            results[test_id] = Outcome.FAILED
        elif case.find("skipped") is not None:
            results[test_id] = Outcome.SKIPPED
        else:
            results[test_id] = Outcome.PASSED
    return results


def parse_go_json(output: str) -> TestResults:
    results: TestResults = {}
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("Test") and ev.get("Action") in {"pass", "fail", "skip"}:
            outcome = {"pass": Outcome.PASSED, "fail": Outcome.FAILED, "skip": Outcome.SKIPPED}
            results[f"{ev.get('Package', '')}::{ev['Test']}"] = outcome[ev["Action"]]
    return results


_LINE_NO = re.compile(r":\d+(:\d+)?:")


def normalise_diagnostics(output: str) -> list[str]:
    """Typecheck/lint diagnostics with line numbers stripped, so shifted lines still match."""
    lines = []
    for line in output.splitlines():
        if _LINE_NO.search(line) and ("error" in line.lower() or "warning" in line.lower()):
            lines.append(_LINE_NO.sub(":", line.strip()))
    return lines
