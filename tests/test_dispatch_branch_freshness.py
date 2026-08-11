"""Tests for ``cw.dispatch.branch_freshness`` — ticket-branch staleness (#1823).

Mirrors ``tests/test_worktree.py``'s ``TestComputeBranchDiffScope`` fixture
shape: real ``git`` repos built in ``tmp_path`` via the shared ``make_git_repo``
fixture, with ``origin`` pointed at the repo itself so ``origin/<default>`` is a
resolvable ref without a network. Every failure mode asserts the fail-open
contract (``False`` — "not stale", never a raise), because a false positive here
parks a healthy ticket for an operator.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

_BRANCH = "dev/1823"
_SHARED_FILE = "shared.txt"
_BRANCH_ONLY_FILE = "branch_only.txt"


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
    """Self-origin repo carrying both fixture files on ``main``/``origin/main``."""
    repo = make_git_repo(name)
    _git_in(repo, "remote", "add", "origin", str(repo))
    _write_commit(repo, _SHARED_FILE, "base\n", "seed shared")
    _write_commit(repo, _BRANCH_ONLY_FILE, "base\n", "seed branch-only")
    _git_in(repo, "fetch", "origin", "main")
    return repo


def _make_stale_branch_repo(
    make_git_repo: Callable[..., Path], name: str, *, branch_touches: str
) -> Path:
    """Repo whose ``dev/1823`` branch is behind an ``origin/main`` that moved.

    The intervening ``origin/main`` commit always touches ``shared.txt``; the
    branch's own commit touches *branch_touches*. Passing ``_SHARED_FILE``
    produces the overlapping (must-gate) shape, ``_BRANCH_ONLY_FILE`` the
    disjoint (must-not-gate) shape the acceptance criteria's negative test
    requires.
    """
    repo = _seed_repo(make_git_repo, name)
    _git_in(repo, "checkout", "-b", _BRANCH)
    _write_commit(repo, branch_touches, "branch work\n", "branch work")
    _git_in(repo, "checkout", "main")
    _write_commit(repo, _SHARED_FILE, "main churn\n", "main churn")
    # Refresh origin/main so it now points past the branch's fork point.
    _git_in(repo, "fetch", "origin", "main")
    _git_in(repo, "checkout", _BRANCH)
    return repo


class TestHasOverlappingBranchStaleness:
    def test_overlapping_intervening_commit_is_detected(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#1823 reproduction: main moved and touched a file the branch touches."""
        from cw.dispatch.branch_freshness import has_overlapping_branch_staleness

        repo = _make_stale_branch_repo(
            make_git_repo, "bf-overlap", branch_touches=_SHARED_FILE
        )

        assert has_overlapping_branch_staleness(repo, "main") is True

    def test_disjoint_intervening_commit_is_not_blocked(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """Option B's narrow rule: behind main, but no file overlap → not gated."""
        from cw.dispatch.branch_freshness import has_overlapping_branch_staleness

        repo = _make_stale_branch_repo(
            make_git_repo, "bf-disjoint", branch_touches=_BRANCH_ONLY_FILE
        )

        assert has_overlapping_branch_staleness(repo, "main") is False

    def test_branch_not_behind_is_never_flagged(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """A branch forked from the current origin/main tip is fresh by definition."""
        from cw.dispatch.branch_freshness import has_overlapping_branch_staleness

        repo = _seed_repo(make_git_repo, "bf-fresh")
        _git_in(repo, "checkout", "-b", _BRANCH)
        _write_commit(repo, _SHARED_FILE, "branch work\n", "branch work")

        assert has_overlapping_branch_staleness(repo, "main") is False

    def test_missing_worktree_path_fails_open(self, tmp_path: Path) -> None:
        """None and a non-existent path both resolve to "not stale", not a raise."""
        from cw.dispatch.branch_freshness import has_overlapping_branch_staleness

        assert has_overlapping_branch_staleness(None, "main") is False
        assert has_overlapping_branch_staleness(tmp_path / "nope", "main") is False

    def test_missing_origin_ref_fails_open(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """No origin/<default> ref at all → the behind-check fails open."""
        from cw.dispatch.branch_freshness import has_overlapping_branch_staleness

        repo = make_git_repo("bf-no-origin")

        assert has_overlapping_branch_staleness(repo, "main") is False

    def test_git_merge_base_failure_fails_open(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unresolvable merge-base → "not stale", never a park."""
        from cw.dispatch import branch_freshness as bf_mod

        repo = _make_stale_branch_repo(
            make_git_repo, "bf-mergebase-fail", branch_touches=_SHARED_FILE
        )
        monkeypatch.setattr(bf_mod, "_resolve_merge_base", lambda _p, _b: None)

        assert bf_mod.has_overlapping_branch_staleness(repo, "main") is False

    def test_git_diff_name_only_failure_fails_open(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """git vanishing before the name-only diff → "not stale", never a raise."""
        from cw.dispatch import branch_freshness as bf_mod

        repo = _make_stale_branch_repo(
            make_git_repo, "bf-diff-fail", branch_touches=_SHARED_FILE
        )

        def boom(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        monkeypatch.setattr(bf_mod, "_run_git", boom)

        assert bf_mod.has_overlapping_branch_staleness(repo, "main") is False

    def test_diff_nonzero_returncode_fails_open(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bogus merge-base makes ``git diff --name-only`` exit non-zero → False."""
        from cw.dispatch import branch_freshness as bf_mod

        repo = _make_stale_branch_repo(
            make_git_repo, "bf-diff-rc", branch_touches=_SHARED_FILE
        )
        monkeypatch.setattr(bf_mod, "_resolve_merge_base", lambda _p, _b: "0" * 40)

        assert bf_mod.has_overlapping_branch_staleness(repo, "main") is False

    def test_behind_probe_oserror_fails_open(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The behind-probe's own OSError is swallowed (no git binary)."""
        from cw.dispatch import branch_freshness as bf_mod

        repo = _seed_repo(make_git_repo, "bf-behind-oserror")

        def boom(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        monkeypatch.setattr(bf_mod, "_run_git", boom)

        assert bf_mod._is_behind_default(repo, "main") is False

    def test_behind_probe_non_numeric_output_fails_open(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric rev-list count is treated as "not behind", not a crash."""
        import subprocess as sp

        from cw.dispatch import branch_freshness as bf_mod

        repo = _seed_repo(make_git_repo, "bf-behind-garbage")

        def garbage(
            *args: str, cwd: object, check: bool = True
        ) -> sp.CompletedProcess[str]:
            return sp.CompletedProcess(args=list(args), returncode=0, stdout="huh\n")

        monkeypatch.setattr(bf_mod, "_run_git", garbage)

        assert bf_mod._is_behind_default(repo, "main") is False
