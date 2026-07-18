"""Tests for the shared ``make_git_repo`` fixture factory (#1238).

The ``base=`` keyword is additive: the live codex contract suite (R12/R14)
must build fixture repos under a home-tree base dir because snap-confined
codex cannot reach ``/tmp``. Every pre-existing positional caller
(``make_git_repo("name")``) must keep its exact ``tmp_path``-relative
behavior.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _head_ok(repo: Path) -> bool:
    """True when *repo* is a git repo with a resolvable HEAD commit."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


class TestMakeGitRepoBase:
    """The additive ``base=`` keyword-only argument on ``make_git_repo``."""

    def test_default_base_is_tmp_path(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        """No ``base=`` → repo created under ``tmp_path`` (unchanged behavior)."""
        repo = make_git_repo("wt-x")
        assert repo == tmp_path / "wt-x"
        assert _head_ok(repo)

    def test_explicit_base_overrides_tmp_path(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        """``base=`` → repo created under the given dir, not ``tmp_path``."""
        other = tmp_path / "elsewhere"
        other.mkdir()
        repo = make_git_repo("wt-y", base=other)
        assert repo == other / "wt-y"
        assert repo.parent == other
        assert _head_ok(repo)
