from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.repo_factory import make_conflict_repo


@pytest.fixture
def conflict_repo(tmp_path: Path) -> Path:
    return make_conflict_repo(tmp_path)
