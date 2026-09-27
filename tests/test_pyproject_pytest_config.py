"""Pin pytest's tmp_path retention config so it cannot be dropped silently (#2474).

A full unit-test run left ~220K files under pytest's default basetemp on the
shared 1M-inode /tmp tmpfs; three runs exhausted it on 2026-09-27 and took
down every headless worker on the host. `[tool.pytest.ini_options]` in
`pyproject.toml` now caps retention to failed-test dirs only, at most one
run deep. This is a regression test for that fix: an edit that removes or
loosens either option should fail this test rather than silently
reintroducing the leak.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


class TestPytestTmpPathRetentionConfig:
    """`[tool.pytest.ini_options]` caps tmp_path retention (#2474)."""

    def test_retention_policy_is_failed(self) -> None:
        with _PYPROJECT_PATH.open("rb") as fh:
            data = tomllib.load(fh)
        pytest_options = data["tool"]["pytest"]["ini_options"]
        assert pytest_options["tmp_path_retention_policy"] == "failed"

    def test_retention_count_is_one(self) -> None:
        with _PYPROJECT_PATH.open("rb") as fh:
            data = tomllib.load(fh)
        pytest_options = data["tool"]["pytest"]["ini_options"]
        assert pytest_options["tmp_path_retention_count"] == 1
