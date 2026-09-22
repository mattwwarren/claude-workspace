"""Tests for cw._git: the shared clean-env and head-sha primitives (#2232)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw._git import capture_head_sha, git_clean_env
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class TestGitCleanEnv:
    def test_every_git_variable_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
        monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/else")
        monkeypatch.setenv("GIT_INDEX_FILE", "/somewhere/else/.git/index")

        env = git_clean_env()

        assert not [k for k in env if k.startswith("GIT_")]

    def test_non_git_variables_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stripped env is still a usable one — PATH must reach git."""
        monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
        monkeypatch.setenv("CW_TEST_MARKER", "kept")

        env = git_clean_env()

        assert env["CW_TEST_MARKER"] == "kept"
        assert "PATH" in env


class TestCaptureHeadSha:
    def test_it_reads_the_worktree_it_was_given(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("head-sha-plain")

        assert capture_head_sha(repo) == git_in(repo, "rev-parse", "HEAD")

    @pytest.mark.parametrize("strict", [True, False])
    def test_an_inherited_git_dir_cannot_redirect_it(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        *,
        strict: bool,
    ) -> None:
        """The hook hazard, in both error policies (#2232 MUST_FIX 1).

        ``cw`` can run inside a git hook, whose ``GIT_DIR`` points at the
        hook's own repository. An unsanitized ``git rev-parse HEAD`` with
        ``cwd=<worktree>`` then answers for the DECOY — silently, with a
        perfectly well-formed sha — and that answer decides whether a settled
        finding stays suppressed.
        """
        repo = make_git_repo("head-sha-target")
        decoy = make_git_repo("head-sha-decoy")
        # make_git_repo's base commit is byte-identical in every repo it
        # builds, so the decoy needs a commit of its own to have a head the
        # assertion below can actually distinguish.
        commit_tracked_file(decoy, "decoy.py", "decoy = True\n")
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
        expected = git_in(repo, "rev-parse", "HEAD")

        assert capture_head_sha(repo, strict=strict) == expected
        assert expected != git_in(decoy, "rev-parse", "HEAD")

    def test_strict_raises_on_a_directory_that_is_not_a_repo(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        with pytest.raises(subprocess.CalledProcessError):
            capture_head_sha(plain, strict=True)

    def test_best_effort_returns_blank_on_a_directory_that_is_not_a_repo(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        assert capture_head_sha(plain, strict=False) == ""

    def test_best_effort_returns_blank_when_git_cannot_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "git is gone"
            raise OSError(msg)

        monkeypatch.setattr("cw._git.subprocess.run", _raise)

        assert capture_head_sha(tmp_path, strict=False) == ""

    def test_strict_propagates_when_git_cannot_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "git is gone"
            raise OSError(msg)

        monkeypatch.setattr("cw._git.subprocess.run", _raise)

        with pytest.raises(OSError, match="git is gone"):
            capture_head_sha(tmp_path, strict=True)
