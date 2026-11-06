"""Tests for cw.doctor.repo_bare (#2973).

``repo-bare/<client>`` warns when ``core.bare=true`` is set on a checkout that
has a working tree: ``git status`` then fails with "this operation must be run
in a work tree" and the dispatch loop's auto fast-forward of ``main`` stalls.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.doctor import _deps, run_doctor
from cw.doctor.repo_bare import _check_repo_bare
from tests._commit_hook_helpers import make_hook_client
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class TestCheckRepoBare:
    def test_empty_clients(self) -> None:
        assert _check_repo_bare({}) == []

    def test_normal_repo_gives_nothing(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("acme")

        assert _check_repo_bare({"acme": make_hook_client(repo)}) == []

    def test_core_bare_true_with_a_working_tree_warns(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("acme")
        git_in(repo, "config", "core.bare", "true")

        results = _check_repo_bare({"acme": make_hook_client(repo)})

        assert [(r.name, r.ok, r.warn) for r in results] == [
            ("repo-bare/acme", True, True)
        ]
        detail = results[0].detail
        assert f"core.bare is true in the repo config of {repo}" in detail
        assert "(.git is present)" in detail
        assert "this operation must be run in a work tree" in detail
        assert f"Fix: git -C {repo} config core.bare false" in detail

    def test_reset_to_false_clears_the_finding(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("acme")
        git_in(repo, "config", "core.bare", "true")
        git_in(repo, "config", "core.bare", "false")

        assert _check_repo_bare({"acme": make_hook_client(repo)}) == []

    def test_a_genuine_bare_clone_is_not_flagged(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        source = make_git_repo("source")
        bare = tmp_path / "bare.git"
        git_in(source, "clone", "--bare", str(source), str(bare))

        assert _check_repo_bare({"acme": make_hook_client(bare)}) == []

    def test_non_existent_path_gives_nothing(self, tmp_path: Path) -> None:
        client = make_hook_client(tmp_path / "missing")

        assert _check_repo_bare({"acme": client}) == []

    def test_non_git_directory_gives_nothing(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        assert _check_repo_bare({"acme": make_hook_client(plain)}) == []

    @pytest.mark.parametrize("mode", ["oserror", "timeout", "nonzero"])
    def test_git_failure_gives_nothing(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        repo = make_git_repo("acme")
        git_in(repo, "config", "core.bare", "true")

        def _run_git(
            argv: Sequence[str],
            *,
            cwd: Path | str | None = None,
            capture_output: bool = False,
            timeout: float | None = None,
        ) -> subprocess.CompletedProcess[str]:
            del cwd, capture_output, timeout
            if mode == "oserror":
                msg = "no git"
                raise OSError(msg)
            if mode == "timeout":
                raise subprocess.TimeoutExpired(list(argv), 1.0)
            return subprocess.CompletedProcess(list(argv), 128, stdout="", stderr="")

        monkeypatch.setattr("cw.doctor.repo_bare.run_git", _run_git)

        assert _check_repo_bare({"acme": make_hook_client(repo)}) == []

    def test_worktree_mode_client_uses_repo_path(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("acme")
        git_in(repo, "config", "core.bare", "true")

        results = _check_repo_bare({"acme": make_hook_client(repo, branch="feature")})

        assert [r.name for r in results] == ["repo-bare/acme"]
        assert str(repo) in results[0].detail


def test_run_doctor_includes_repo_bare(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_git_repo("acme")
    git_in(repo, "config", "core.bare", "true")
    monkeypatch.setattr(_deps, "load_clients", lambda: {"acme": make_hook_client(repo)})

    report = run_doctor()

    check = next(c for c in report.checks if c.name == "repo-bare/acme")
    assert check.ok is True
    assert check.warn is True
