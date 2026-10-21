"""Re-export of the demo repository builder (lives in the package so `mergeagent demo` can use it)."""

from mergeagent.demo import (
    BASE_CALC,
    BASE_TESTS,
    FEATURE_CALC,
    GOOD_RESOLUTION,
    MAIN_CALC,
    MAIN_TESTS,
    make_conflict_repo,
)

__all__ = [
    "BASE_CALC",
    "BASE_TESTS",
    "FEATURE_CALC",
    "GOOD_RESOLUTION",
    "MAIN_CALC",
    "MAIN_TESTS",
    "make_conflict_repo",
]
