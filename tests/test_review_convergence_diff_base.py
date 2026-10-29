"""Tests for the pinned review diff base (#2800).

``resolve_diff_base``, ``refresh_diff_base`` and ``verify_diff_base``
(``cw.review_convergence._diff_base``) and the ledger's ``diff_base`` field.
Git is real: every scenario is a ``make_git_repo_with_origin`` feature branch
that then merged ``origin/main`` through the shared ``branch_that_merged_main``
builder, which is exactly the shape that made the loop and the confirm round
diff from different bases.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.review_convergence import (
    ConvergenceLedger,
    load_ledger,
    refresh_diff_base,
    resolve_diff_base,
    save_ledger,
    verify_diff_base,
)
from tests._review_convergence_helpers import (
    MergedMainBranch,
    branch_that_merged_main,
    fresh_ledger,
    live_merge_base,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

_RUN_GIT = "cw.review_convergence._diff_base.run_git"
_NO_SUCH_BRANCH = "no-such-branch"


@pytest.fixture
def repo(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> Path:
    worktree, _origin = make_git_repo_with_origin("diff-base")
    return worktree


@pytest.fixture
def merged(repo: Path, tmp_path: Path) -> MergedMainBranch:
    return branch_that_merged_main(repo, tmp_path / "main-clone")


def _rewind_origin_main(repo: Path, sha: str) -> None:
    """Point the local ``origin/main`` at *sha*, as an unfetched push leaves it."""
    git_in(repo, "update-ref", "refs/remotes/origin/main", sha)


def _side_commit(repo: Path) -> str:
    """A commit on a branch off ``main`` that HEAD never reaches."""
    git_in(repo, "checkout", "-b", "side", "main")
    commit_tracked_file(repo, "side.py")
    sha = git_in(repo, "rev-parse", "HEAD")
    git_in(repo, "checkout", "feature")
    return sha


def _git_answering(
    monkeypatch: pytest.MonkeyPatch,
    match: Sequence[str],
    answer: subprocess.CompletedProcess[str] | OSError,
) -> None:
    """``run_git`` with argv starting *match* answering *answer*; real otherwise."""
    from cw._git import run_git as real

    def _run(
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if list(argv[: len(match)]) != list(match):
            return real(argv, cwd=cwd, capture_output=capture_output)
        if isinstance(answer, OSError):
            raise answer
        return answer

    monkeypatch.setattr(_RUN_GIT, _run)


class TestResolve:
    def test_returns_the_merge_base_after_a_merge_of_main(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        base = resolve_diff_base(repo, "main")

        assert base == live_merge_base(repo)
        assert base == merged.merge_base
        assert base != merged.fork_point
        pinned_diff = git_in(repo, "diff", f"{base}...HEAD")
        assert pinned_diff == git_in(repo, "diff", "origin/main...HEAD")
        assert merged.main_only_file not in pinned_diff
        # The fork-point diff is the one the bug used: it carries main's file.
        fork_diff = git_in(repo, "diff", f"{merged.fork_point}...HEAD")
        assert merged.main_only_file in fork_diff
        assert fork_diff != pinned_diff

    def test_an_explicit_ref_is_the_merge_base_with_that_ref(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        assert resolve_diff_base(repo, "main", ref=merged.fork_point) == (
            merged.fork_point
        )

    def test_an_unresolvable_default_branch_raises(self, repo: Path) -> None:
        with pytest.raises(CwError, match="git merge-base origin/no-such-branch HEAD"):
            resolve_diff_base(repo, _NO_SUCH_BRANCH)

    def test_empty_output_raises(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _git_answering(
            monkeypatch,
            ["merge-base"],
            subprocess.CompletedProcess(["git"], 0, stdout="\n", stderr=""),
        )
        with pytest.raises(CwError, match="printed no commit"):
            resolve_diff_base(repo, "main")

    def test_git_that_cannot_run_raises(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _git_answering(monkeypatch, ["merge-base"], OSError("no git"))
        with pytest.raises(CwError, match="could not run"):
            resolve_diff_base(repo, "main")


class TestRefresh:
    def test_fetches_before_resolving(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        _rewind_origin_main(repo, merged.fork_point)
        assert resolve_diff_base(repo, "main") == merged.fork_point

        assert refresh_diff_base(repo, "main") == merged.merge_base
        assert git_in(repo, "rev-parse", "origin/main") != merged.fork_point

    def test_a_missing_remote_raises(self, repo: Path) -> None:
        git_in(repo, "remote", "remove", "origin")
        with pytest.raises(CwError, match="git fetch origin main failed"):
            refresh_diff_base(repo, "main")

    def test_an_unknown_default_branch_raises(self, repo: Path) -> None:
        with pytest.raises(CwError, match="git fetch origin no-such-branch failed"):
            refresh_diff_base(repo, _NO_SUCH_BRANCH)

    def test_a_fetch_that_cannot_run_raises(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _git_answering(monkeypatch, ["fetch"], OSError("no git"))
        with pytest.raises(CwError, match="could not run"):
            refresh_diff_base(repo, "main")


class TestVerify:
    def test_accepts_the_live_base(self, repo: Path, merged: MergedMainBranch) -> None:
        assert verify_diff_base(repo, merged.merge_base, "main") == merged.merge_base

    def test_rejects_a_missing_pin(self, repo: Path, merged: MergedMainBranch) -> None:
        assert merged.merge_base
        with pytest.raises(CwError, match="records no diff base"):
            verify_diff_base(repo, None, "main")

    def test_rejects_a_string_that_is_not_a_commit(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        assert merged.merge_base
        with pytest.raises(CwError, match="is not a commit"):
            verify_diff_base(repo, "not-a-commit", "main")

    def test_rejects_a_commit_that_is_not_an_ancestor(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        side = _side_commit(repo)
        assert git_in(repo, "rev-parse", "HEAD") == merged.head
        with pytest.raises(CwError, match="is not an ancestor of HEAD"):
            verify_diff_base(repo, side, "main")

    def test_rejects_the_pre_merge_fork_point_as_stale(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        with pytest.raises(CwError, match="is stale") as raised:
            verify_diff_base(repo, merged.fork_point, "main")
        assert merged.merge_base in str(raised.value)
        assert "cw review converge diff-base" in str(raised.value)

    def test_rejects_an_unresolvable_default_branch(
        self, repo: Path, merged: MergedMainBranch
    ) -> None:
        with pytest.raises(CwError, match="git merge-base origin/no-such-branch"):
            verify_diff_base(repo, merged.merge_base, _NO_SUCH_BRANCH)

    def test_a_git_failure_checking_ancestry_raises(
        self, repo: Path, merged: MergedMainBranch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _git_answering(
            monkeypatch,
            ["merge-base", "--is-ancestor"],
            subprocess.CompletedProcess(["git"], 128, stdout="", stderr="broken\n"),
        )
        with pytest.raises(CwError, match="exit 128: broken"):
            verify_diff_base(repo, merged.merge_base, "main")


class TestLedgerField:
    def test_a_legacy_ledger_without_the_field_loads_as_none(
        self, tmp_path: Path
    ) -> None:
        raw = fresh_ledger(diff_base="abc123").model_dump(mode="json")
        raw.pop("diff_base")
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        ledger = load_ledger(path)

        assert ledger is not None
        assert ledger.diff_base is None
        assert ledger.schema_version == 1

    def test_the_field_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        save_ledger(path, fresh_ledger(diff_base="abc123"))

        ledger = load_ledger(path)

        assert isinstance(ledger, ConvergenceLedger)
        assert ledger.diff_base == "abc123"
        assert ledger.fork_point != ledger.diff_base
