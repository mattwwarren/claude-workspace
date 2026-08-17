"""Tests for ``cw.branch_ahead`` — commits-ahead-of-default measurement (#1870).

Mirrors ``tests/test_dispatch_branch_freshness.py``'s fixture shape: real
``git`` repos built in ``tmp_path`` via the shared ``make_git_repo`` fixture,
with ``origin`` pointed at the repo itself so ``origin/<default>`` is a
resolvable ref without a network.

The contract under test is three-valued, which is the whole reason this module
exists separately from ``branch_freshness``: ``0`` ("measured, nothing ahead" —
the gate fires), a positive int ("measured, real work present"), and ``None``
("unmeasurable" — the gate fails open). Every failure mode below asserts
``None``, never ``0``, because conflating the two would park every ticket whose
worktree git state cannot be read.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

_BRANCH = "dev/1870"
_FILE = "work.txt"


def _git_in(repo: Path, *args: str) -> str:
    """Run git in *repo* with a GIT_*-stripped env, returning stripped stdout."""
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=clean_env,
    )
    return result.stdout.strip()


def _write_commit(repo: Path, name: str, body: str, message: str) -> None:
    """Write *body* to *name* under *repo* and commit it."""
    (repo / name).write_text(body, encoding="utf-8")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-m", message)


def _seed_repo(make_git_repo: Callable[..., Path], name: str) -> Path:
    """Self-origin repo with a resolvable ``origin/main`` and a ``dev/1870`` branch.

    The branch is created at the ``origin/main`` tip and carries no commits of
    its own — the zero-diff shape this ticket exists to catch. Callers that
    want an ahead branch commit onto it afterwards.
    """
    repo = make_git_repo(name)
    _git_in(repo, "remote", "add", "origin", str(repo))
    _write_commit(repo, _FILE, "base\n", "seed")
    _git_in(repo, "fetch", "origin", "main")
    _git_in(repo, "checkout", "-b", _BRANCH)
    return repo


class TestCommitsAheadOfDefault:
    def test_branch_with_no_commits_measures_zero(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#1870 reproduction: a branch at the origin/main tip is ahead by 0."""
        from cw.branch_ahead import commits_ahead_of_default

        repo = _seed_repo(make_git_repo, "ba-empty")

        assert commits_ahead_of_default(repo, "main") == 0

    def test_branch_with_commits_measures_the_count(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """A real branch reports its own commit count, never 0."""
        from cw.branch_ahead import commits_ahead_of_default

        repo = _seed_repo(make_git_repo, "ba-ahead")
        _write_commit(repo, _FILE, "first\n", "branch work 1")
        _write_commit(repo, _FILE, "second\n", "branch work 2")

        assert commits_ahead_of_default(repo, "main") == 2

    def test_missing_worktree_path_is_unmeasurable(self, tmp_path: Path) -> None:
        """None and a non-existent path resolve to None ("unmeasurable"), not 0."""
        from cw.branch_ahead import commits_ahead_of_default

        assert commits_ahead_of_default(None, "main") is None
        assert commits_ahead_of_default(tmp_path / "nope", "main") is None

    def test_missing_origin_ref_is_unmeasurable(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """No origin/<default> ref at all → rev-list exits non-zero → None."""
        from cw.branch_ahead import commits_ahead_of_default

        repo = make_git_repo("ba-no-origin")

        assert commits_ahead_of_default(repo, "main") is None

    def test_git_failure_is_unmeasurable(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing git binary (OSError) is swallowed into None, never raised."""
        from cw import branch_ahead as ba_mod

        repo = _seed_repo(make_git_repo, "ba-oserror")

        def boom(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        monkeypatch.setattr(ba_mod, "_run_git", boom)

        assert ba_mod.commits_ahead_of_default(repo, "main") is None

    def test_non_numeric_count_is_unmeasurable(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed rev-list count is unmeasurable, not a crash and not 0."""
        import subprocess as sp

        from cw import branch_ahead as ba_mod

        repo = _seed_repo(make_git_repo, "ba-garbage")

        def garbage(
            *args: str, cwd: object, check: bool = True
        ) -> sp.CompletedProcess[str]:
            return sp.CompletedProcess(args=list(args), returncode=0, stdout="huh\n")

        monkeypatch.setattr(ba_mod, "_run_git", garbage)

        assert ba_mod.commits_ahead_of_default(repo, "main") is None
