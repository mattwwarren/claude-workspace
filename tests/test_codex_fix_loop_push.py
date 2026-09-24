"""Tests for ``cw.codex_fix_loop_push`` (#2354).

Sibling of ``test_codex_fix_loop.py`` for the push-and-verify helper split
out of ``cw.codex_fix_loop``, mirroring ``test_codex_fix_loop_convergence.py``.
Every test drives a real bare origin via the ``make_git_repo_with_origin``
factory rather than mocking git.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw import codex_fix_loop_push
from cw.codex_fix_loop_push import (
    SYNTHETIC_MISMATCH_PREFIX,
    push_and_verify_head,
    remote_branch_tip,
)
from tests.conftest import commit_tracked_file, git_in, push_commit_to_origin

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _remote_sha(origin: Path, branch: str) -> str:
    return git_in(origin, "rev-parse", f"refs/heads/{branch}")


def test_push_and_verify_head_pushes_and_matches(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> None:
    worktree, origin = make_git_repo_with_origin("wt-push-ok")
    commit_tracked_file(worktree, "fix.py", "fixed = 1\n")
    sha = git_in(worktree, "rev-parse", "HEAD")
    assert _remote_sha(origin, "feature") != sha

    push_and_verify_head(worktree, sha)

    assert _remote_sha(origin, "feature") == sha
    assert git_in(worktree, "rev-parse", "origin/feature") == sha


def test_push_and_verify_head_raises_on_push_failure(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    tmp_path: Path,
) -> None:
    worktree, _origin = make_git_repo_with_origin("wt-push-fail")
    git_in(worktree, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    commit_tracked_file(worktree, "fix.py", "fixed = 1\n")
    sha = git_in(worktree, "rev-parse", "HEAD")

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        push_and_verify_head(worktree, sha)

    assert excinfo.value.cmd[:2] == ["git", "push"]
    assert excinfo.value.stderr


def test_push_and_verify_head_raises_on_tip_mismatch(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, _origin = make_git_repo_with_origin("wt-push-mismatch")
    commit_tracked_file(worktree, "fix.py", "fixed = 1\n")
    sha = git_in(worktree, "rev-parse", "HEAD")
    monkeypatch.setattr(codex_fix_loop_push, "remote_branch_tip", lambda *_a: "f" * 40)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        push_and_verify_head(worktree, sha)

    assert excinfo.value.returncode == 1
    assert excinfo.value.stderr.startswith(SYNTHETIC_MISMATCH_PREFIX)
    assert SYNTHETIC_MISMATCH_PREFIX.startswith(
        "SYNTHETIC (tip mismatch after push, not a git failure):"
    )
    assert "origin/feature=" + "f" * 40 in excinfo.value.stderr
    assert f"expected={sha}" in excinfo.value.stderr


def test_push_and_verify_head_raises_on_concurrent_push_divergence(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    tmp_path: Path,
) -> None:
    """An out-of-band push that origin has but local lacks rejects the push."""
    worktree, origin = make_git_repo_with_origin("wt-push-diverged")
    push_commit_to_origin(origin, "feature", tmp_path / "side", "other.py")
    commit_tracked_file(worktree, "fix.py", "fixed = 1\n")
    sha = git_in(worktree, "rev-parse", "HEAD")

    with pytest.raises(subprocess.CalledProcessError):
        push_and_verify_head(worktree, sha)

    assert _remote_sha(origin, "feature") != sha


def test_push_and_verify_head_raises_on_detached_head(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> None:
    worktree, _origin = make_git_repo_with_origin("wt-push-detached")
    sha = git_in(worktree, "rev-parse", "HEAD")
    git_in(worktree, "checkout", "--detach", sha)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        push_and_verify_head(worktree, sha)

    assert excinfo.value.stderr.startswith("SYNTHETIC (detached HEAD")


def test_remote_branch_tip_returns_origin_sha(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> None:
    worktree, origin = make_git_repo_with_origin("wt-tip-ok")

    assert remote_branch_tip(worktree, "feature") == _remote_sha(origin, "feature")


def test_remote_branch_tip_none_when_branch_never_pushed(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> None:
    worktree, _origin = make_git_repo_with_origin("wt-tip-missing")

    assert remote_branch_tip(worktree, "never-pushed") is None


def test_remote_branch_tip_none_when_fetch_succeeds_but_ref_unresolvable(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> None:
    """A fetch that exits 0 but writes no tracking ref still yields None.

    A fetch refspec narrowed to ``main`` makes ``git fetch origin other``
    succeed while updating only ``FETCH_HEAD``, never ``origin/other``.
    """
    worktree, origin = make_git_repo_with_origin("wt-tip-noref")
    git_in(origin, "branch", "other", "main")
    git_in(
        worktree,
        "config",
        "remote.origin.fetch",
        "+refs/heads/main:refs/remotes/origin/main",
    )

    assert remote_branch_tip(worktree, "other") is None
