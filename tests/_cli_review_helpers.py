"""Shared test helpers for the ``cw.cli.review`` per-submodule test suite.

Payload builders and git helpers used by both of the split
``test_cli_review_*.py`` files. This module has no ``test_`` prefix, so pytest
does not collect it (same convention as ``tests/conftest.py``); it is imported
explicitly by the test modules that use each helper.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

from tests.conftest import commit_tracked_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# A single-file unified diff: hunk starts at new line 1, one context line
# (advances to 2), two added lines at 2 and 3. Mirrors the fixture shape in
# tests/test_codex_review.py's _MULTI_FILE_DIFF (#1236 precedent).
_CONSOLIDATE_DIFF = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken():
+    pass
"""


def _consolidate_payload(**overrides: object) -> dict[str, Any]:
    """Minimal-but-valid ``cw review consolidate`` request envelope (#1241)."""
    payload: dict[str, Any] = {
        "documents": [],
        "diff": _CONSOLIDATE_DIFF,
        "reviewed_sha": "abc1234",
        "failed_reviewers": [],
    }
    payload.update(overrides)
    return payload


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* with a GIT_*-free env, returning stdout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
        env=env,
    ).stdout


def _branch_repo(
    make_git_repo: Callable[..., Path], name: str
) -> tuple[Path, str, str]:
    """A repo with a `feature` branch one commit ahead of `main`.

    Returns ``(repo, reviewed_sha, real_diff_text)`` where *real_diff_text* is
    the verbatim ``git diff --no-color main...<reviewed_sha>`` output.
    """
    repo = make_git_repo(name)
    _git(repo, "checkout", "-b", "feature")
    commit_tracked_file(repo, "src/thing.py", "x = 1\ny = 2\n")
    reviewed_sha = _git(repo, "rev-parse", "HEAD").strip()
    real_diff = _git(repo, "diff", "--no-color", f"main...{reviewed_sha}")
    return repo, reviewed_sha, real_diff
