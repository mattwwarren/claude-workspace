"""Real-git tests for ``cw._git.is_ancestor`` and ``commit_range_summary`` (#2943).

RFC 0016 R10: ancestry is tri-state and must be tested against real local
repositories in ``tmp_path`` (precedent ``tests/test_changelog_repair_real_git.py``).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw import _git
from cw._git import commit_range_summary, is_ancestor
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_UNKNOWN_SHA = "f" * 40
_GIT_MISSING = "git missing"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    return make_git_repo("ancestry")


def _head(repo: Path) -> str:
    return git_in(repo, "rev-parse", "HEAD")


class TestIsAncestor:
    def test_true_for_a_descendant(self, repo: Path) -> None:
        old = _head(repo)
        commit_tracked_file(repo, "a.py")
        assert is_ancestor(repo, old, _head(repo)) is True

    def test_true_for_the_same_commit(self, repo: Path) -> None:
        head = _head(repo)
        assert is_ancestor(repo, head, head) is True

    def test_false_for_a_diverged_commit(self, repo: Path) -> None:
        base = _head(repo)
        commit_tracked_file(repo, "a.py")
        old = _head(repo)
        git_in(repo, "checkout", "-b", "rewrite", base)
        commit_tracked_file(repo, "b.py")
        assert is_ancestor(repo, old, _head(repo)) is False

    def test_false_for_an_unrelated_commit(self, repo: Path) -> None:
        old = _head(repo)
        git_in(repo, "checkout", "--orphan", "unrelated")
        commit_tracked_file(repo, "c.py")
        assert is_ancestor(repo, old, _head(repo)) is False

    def test_none_for_an_unknown_object(self, repo: Path) -> None:
        assert is_ancestor(repo, _UNKNOWN_SHA, _head(repo)) is None

    def test_none_for_a_non_repo_path(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert is_ancestor(plain, _UNKNOWN_SHA, _UNKNOWN_SHA) is None

    def test_none_on_oserror(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: object, **_kwargs: object) -> object:
            raise OSError(_GIT_MISSING)

        monkeypatch.setattr(_git.subprocess, "run", _boom)
        assert is_ancestor(repo, "a" * 40, "b" * 40) is None

    def test_none_on_timeout(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _hang(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(_git.subprocess, "run", _hang)
        assert is_ancestor(repo, "a" * 40, "b" * 40, timeout=1) is None


class TestCommitRangeSummary:
    def test_lists_oneline_commits_in_range(self, repo: Path) -> None:
        old = _head(repo)
        commit_tracked_file(repo, "a.py")
        commit_tracked_file(repo, "b.py")
        summary = commit_range_summary(repo, old, _head(repo))
        lines = summary.splitlines()
        assert len(lines) == 2
        assert all(len(line.split(" ", 1)[0]) >= 7 for line in lines)

    def test_caps_with_more_suffix(self, repo: Path) -> None:
        old = _head(repo)
        for name in ("a.py", "b.py", "c.py"):
            commit_tracked_file(repo, name)
        summary = commit_range_summary(repo, old, _head(repo), limit=1)
        lines = summary.splitlines()
        assert len(lines) == 2
        assert lines[-1] == "(2 more)"

    def test_unavailable_on_git_error(self, repo: Path) -> None:
        summary = commit_range_summary(repo, _UNKNOWN_SHA, _head(repo))
        assert summary.startswith("unavailable: ")

    def test_unavailable_on_oserror(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object, **_kwargs: object) -> object:
            raise OSError(_GIT_MISSING)

        monkeypatch.setattr(_git.subprocess, "run", _boom)
        assert commit_range_summary(repo, "a", "b") == f"unavailable: {_GIT_MISSING}"
