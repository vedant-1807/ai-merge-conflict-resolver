"""Compare parent-branch test results with the candidate merge.

A candidate must not break anything that worked on either parent:

* **regression**: passes on every parent that has the test, fails/missing on the candidate;
* **lost test**: passed on a parent but no longer exists in the candidate (a resolution that
  silently deletes tests must not look green);
* **pre-existing failure**: fails on a parent too, so it is ignored;
* **fixed**: failed on a parent, passes on the candidate (reported, not required).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from mergeagent.verification.reports import Outcome, TestResults


@dataclass(slots=True)
class Comparison:
    regressions: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    pre_existing: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    candidate_passed: int = 0
    candidate_failed: int = 0

    @property
    def ok(self) -> bool:
        return not self.regressions and not self.lost


def compare_tests(parents: list[TestResults], candidate: TestResults) -> Comparison:
    cmp = Comparison()
    cmp.candidate_passed = sum(1 for o in candidate.values() if o is Outcome.PASSED)
    cmp.candidate_failed = sum(1 for o in candidate.values() if o is Outcome.FAILED)
    all_ids = set().union(*parents) if parents else set()
    for tid in sorted(all_ids | set(candidate)):
        parent_outcomes = [p[tid] for p in parents if tid in p]
        cand = candidate.get(tid)
        passed_somewhere = Outcome.PASSED in parent_outcomes
        failed_somewhere = Outcome.FAILED in parent_outcomes
        if cand is None:
            if passed_somewhere:
                cmp.lost.append(tid)
        elif cand is Outcome.FAILED:
            if passed_somewhere and not failed_somewhere:
                cmp.regressions.append(tid)
            elif failed_somewhere:
                cmp.pre_existing.append(tid)
            else:
                # New test that only exists in the candidate (should not happen for a pure
                # merge) or one that was skipped on parents: treat as a regression.
                cmp.regressions.append(tid)
        elif cand is Outcome.PASSED and failed_somewhere:
            cmp.fixed.append(tid)
    return cmp


def new_diagnostics(parents: list[list[str]], candidate: list[str]) -> list[str]:
    """Diagnostics present in the candidate more times than in any parent (multiset diff)."""
    allowed: Counter[str] = Counter()
    for p in parents:
        allowed |= Counter(p)
    extra = Counter(candidate) - allowed
    return sorted(extra.elements())
