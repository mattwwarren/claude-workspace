"""Tests for cw._git: the shared git-subprocess primitives (#2232, #2264)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw._git import capture_head_sha, git_clean_env, git_output, run_git
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

    def test_ref_parameter_reads_an_arbitrary_ref(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """``ref`` names what to resolve (#2285); omitting it still means HEAD."""
        repo = make_git_repo("head-sha-ref")
        git_in(repo, "branch", "side")
        commit_tracked_file(repo, "moved.py")

        side_sha = capture_head_sha(repo, ref="side")

        assert side_sha == git_in(repo, "rev-parse", "side")
        assert side_sha != git_in(repo, "rev-parse", "HEAD")
        assert capture_head_sha(repo, ref="main") == git_in(repo, "rev-parse", "main")
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

    def test_timeout_is_forwarded_and_defaults_to_unbounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []

        def _record(
            *_args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            seen.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="abc\n")

        monkeypatch.setattr("cw._git.subprocess.run", _record)

        assert capture_head_sha(tmp_path, timeout=2.5) == "abc"
        assert capture_head_sha(tmp_path) == "abc"
        assert seen == [2.5, None]

    def test_best_effort_returns_blank_when_git_hangs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timeout is a failure like any other: blank under strict=False."""

        def _hang(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr("cw._git.subprocess.run", _hang)

        assert capture_head_sha(tmp_path, strict=False, timeout=1) == ""

    def test_strict_propagates_when_git_hangs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _hang(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr("cw._git.subprocess.run", _hang)

        with pytest.raises(subprocess.TimeoutExpired):
            capture_head_sha(tmp_path, strict=True, timeout=1)


_RUN_TIMEOUT = 4.0


def _hostile_git_env(
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> tuple[Path, Path]:
    """Build a target repo plus a decoy whose ``GIT_DIR`` the env points at.

    The decoy carries a commit of its own so its HEAD differs from the
    target's (``make_git_repo``'s base commit is byte-identical everywhere).
    """
    repo = make_git_repo(f"{prefix}-target")
    decoy = make_git_repo(f"{prefix}-decoy")
    commit_tracked_file(decoy, "decoy.py", "decoy = True\n")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    return repo, decoy


class TestRunGit:
    def test_it_runs_git_in_the_cwd_it_was_given(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("run-git-plain")

        completed = run_git(
            ["rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True
        )

        assert completed.stdout.strip() == git_in(repo, "rev-parse", "HEAD")

    def test_an_inherited_git_dir_cannot_redirect_it(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, decoy = _hostile_git_env(make_git_repo, monkeypatch, "run-git")
        expected = git_in(repo, "rev-parse", "HEAD")

        via_cwd = run_git(["rev-parse", "HEAD"], cwd=repo, capture_output=True)
        via_dash_c = run_git(
            ["-C", str(repo), "rev-parse", "HEAD"], capture_output=True
        )

        assert via_cwd.stdout.strip() == expected
        assert via_dash_c.stdout.strip() == expected
        assert expected != git_in(decoy, "rev-parse", "HEAD")

    def test_an_env_kwarg_is_a_type_error(self, tmp_path: Path) -> None:
        """The seam's whole point: no caller can hand git its own env."""
        bad_kwargs: dict[str, object] = {"env": {}}

        with pytest.raises(TypeError, match="env"):
            run_git(["status"], cwd=tmp_path, **bad_kwargs)

    def test_kwargs_are_forwarded_with_a_clean_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
        seen: list[tuple[object, dict[str, object]]] = []

        def _record(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append((args, kwargs))
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

        monkeypatch.setattr("cw._git.subprocess.run", _record)

        run_git(
            ["status"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=False,
            timeout=_RUN_TIMEOUT,
        )
        run_git(["status"])

        (argv, kwargs), (_, defaults) = seen
        assert argv == ["git", "status"]
        assert kwargs["cwd"] == tmp_path
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is False
        assert kwargs["timeout"] == _RUN_TIMEOUT
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert not [k for k in env if k.startswith("GIT_")]
        assert defaults["cwd"] is None
        assert defaults["check"] is False
        assert defaults["capture_output"] is False
        assert defaults["text"] is True
        assert defaults["timeout"] is None


class TestGitOutput:
    def test_it_returns_decoded_stdout_from_the_cwd_it_was_given(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("git-output-plain")

        out = git_output(["rev-parse", "HEAD"], cwd=repo)

        assert isinstance(out, str)
        assert out.strip() == git_in(repo, "rev-parse", "HEAD")

    def test_an_inherited_git_dir_cannot_redirect_it(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, decoy = _hostile_git_env(make_git_repo, monkeypatch, "git-output")
        expected = git_in(repo, "rev-parse", "HEAD")

        assert git_output(["rev-parse", "HEAD"], cwd=repo).strip() == expected
        assert git_output(["-C", str(repo), "rev-parse", "HEAD"]).strip() == expected
        assert expected != git_in(decoy, "rev-parse", "HEAD")

    def test_a_failing_command_raises_called_process_error(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        with pytest.raises(subprocess.CalledProcessError):
            git_output(["rev-parse", "HEAD"], cwd=plain)

    def test_an_env_kwarg_is_a_type_error(self, tmp_path: Path) -> None:
        bad_kwargs: dict[str, object] = {"env": {}}

        with pytest.raises(TypeError, match="env"):
            git_output(["status"], cwd=tmp_path, **bad_kwargs)

    def test_kwargs_are_forwarded_with_a_clean_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
        seen: list[tuple[object, dict[str, object]]] = []

        def _record(args: object, **kwargs: object) -> str:
            seen.append((args, kwargs))
            return "out\n"

        monkeypatch.setattr("cw._git.subprocess.check_output", _record)

        assert git_output(["status"], cwd=tmp_path, timeout=_RUN_TIMEOUT) == "out\n"
        assert git_output(["status"]) == "out\n"

        (argv, kwargs), (_, defaults) = seen
        assert argv == ["git", "status"]
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout"] == _RUN_TIMEOUT
        assert kwargs["text"] is True
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert not [k for k in env if k.startswith("GIT_")]
        assert defaults["cwd"] is None
        assert defaults["timeout"] is None
