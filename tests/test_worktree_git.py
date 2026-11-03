"""Tests for cw.worktree._git - shared git-subprocess leaf helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw._git import git_clean_env
from cw.exceptions import WorktreeError
from cw.models import (
    ClientConfig,
)
from cw.worktree import (
    BranchLookupError,
    DetachedHead,
    NotARepository,
    OnBranch,
    ProbeUnavailable,
    _checked_out_branch,
    _git_dir,
    check_not_main_checkout,
    probe_checked_out_branch,
)
from cw.worktree._git import _PER_ROW_REPO_FAULTS
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable

_RUN = "cw.worktree._git.subprocess.run"

# One named scenario per allowlisted phrase, positional with
# ``_PER_ROW_REPO_FAULTS``: a new allowlist entry must add a named scenario.
_REPO_FAULT_IDS = ["non-git-directory", "dangling-gitfile", "malformed-gitfile"]
assert len(_PER_ROW_REPO_FAULTS) == len(_REPO_FAULT_IDS)
# Stderr git would print for each allowlisted phrase, built from the allowlist
# itself so no test hard-codes git's wording.
_REPO_FAULT_STDERRS = [f"fatal: {phrase}: /x\n" for phrase in _PER_ROW_REPO_FAULTS]


def _stub_run(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, stderr: str
) -> None:
    """Make the probe's ``subprocess.run`` return one canned result."""

    def _fake_run(
        args: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(_RUN, _fake_run)


def _stub_raise(monkeypatch: pytest.MonkeyPatch, exc: OSError) -> None:
    """Make the probe's ``subprocess.run`` raise *exc*."""

    def _fake_run(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(_RUN, _fake_run)


def _non_repo_dirs(tmp_path: Path) -> list[Path]:
    """A plain directory and one whose ``.git`` file names a removed gitdir."""
    plain = tmp_path / "plain"
    plain.mkdir()
    dangling = tmp_path / "dangling"
    dangling.mkdir()
    (dangling / ".git").write_text("gitdir: /nonexistent/gitdir\n")
    return [plain, dangling]


class TestGitDir:
    def test_legacy_client(self, tmp_path: Path) -> None:
        client = ClientConfig(name="test", workspace_path=tmp_path / "ws")
        assert _git_dir(client) == tmp_path / "ws"

    def test_worktree_client(self, tmp_path: Path) -> None:
        client = ClientConfig(
            name="test",
            repo_path=tmp_path / "repo",
            branch="client-a",
        )
        assert _git_dir(client) == tmp_path / "repo"


class TestCheckNotMainCheckout:
    """Unit tests for check_not_main_checkout."""

    def test_raises_when_paths_are_equal(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Raises WorktreeError when worktree_path resolves to main checkout."""
        repo = make_git_repo("main-checkout")
        client = ClientConfig(name="test", workspace_path=repo)

        with pytest.raises(WorktreeError, match="main checkout"):
            check_not_main_checkout(repo, client)

    def test_raises_via_symlink(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Raises even when worktree_path is a symlink to main checkout."""
        repo = make_git_repo("main-checkout")
        symlink_path = tmp_path / "link-to-main"
        symlink_path.symlink_to(repo)
        client = ClientConfig(name="test", workspace_path=repo)

        with pytest.raises(WorktreeError, match="main checkout"):
            check_not_main_checkout(symlink_path, client)

    def test_does_not_raise_for_distinct_path(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Does not raise when worktree_path is a genuinely separate directory."""
        repo = make_git_repo("main-checkout")
        other = make_git_repo("branch-worktree")
        client = ClientConfig(name="test", workspace_path=repo)

        check_not_main_checkout(other, client)


class TestProbeCheckedOutBranch:
    """The one ``git branch --show-current`` probe (#2662, #2903).

    Classification into ``NotARepository`` vs ``ProbeUnavailable`` depends on
    git's stderr wording, so every classification assertion stubs
    ``subprocess.run``. Real git runs only where the outcome does not depend
    on wording: a branch name, detached or unborn HEAD, and an ``OSError``.
    """

    def test_returns_branch_name(self, make_git_repo: Callable[..., Path]) -> None:
        assert probe_checked_out_branch(make_git_repo("repo")) == OnBranch("main")

    def test_unborn_branch_returns_name(self, tmp_path: Path) -> None:
        unborn = tmp_path / "unborn"
        unborn.mkdir()
        git_in(unborn, "init", "-b", "feature/unborn")

        assert probe_checked_out_branch(unborn) == OnBranch("feature/unborn")

    def test_detached_head(self, make_git_repo: Callable[..., Path]) -> None:
        repo = make_git_repo("repo")
        git_in(repo, "checkout", "--detach")

        assert probe_checked_out_branch(repo) == DetachedHead()

    def test_non_utf8_branch_name_decodes_with_replacement_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        """A git ref name is bytes; a strict decode would escape the probe."""
        repo = tmp_path / "repo"
        repo.mkdir()
        bad_ref = b"refs/heads/bad-\xff"
        env = git_clean_env()
        for argv in (
            [b"git", b"init", b"-q"],
            [b"git", b"symbolic-ref", b"HEAD", bad_ref],
        ):
            subprocess.run(argv, cwd=repo, env=env, check=True, capture_output=True)

        assert probe_checked_out_branch(repo) == OnBranch("bad-�")

    def test_missing_directory_is_unavailable_with_file_not_found(
        self, tmp_path: Path
    ) -> None:
        result = probe_checked_out_branch(tmp_path / "gone")

        assert isinstance(result, ProbeUnavailable)
        assert isinstance(result.error, FileNotFoundError)

    def test_missing_git_binary_is_unavailable_with_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))

        result = probe_checked_out_branch(tmp_path)

        assert isinstance(result, ProbeUnavailable)
        assert isinstance(result.error, OSError)

    def test_non_repository_directories_never_report_a_branch(
        self, tmp_path: Path
    ) -> None:
        """Wording-free real-git smoke: no bogus branch leaks, however git words it."""
        for path in _non_repo_dirs(tmp_path):
            result = probe_checked_out_branch(path)

            assert not isinstance(result, OnBranch), path
            assert not isinstance(result, DetachedHead), path

    def test_empty_stdout_on_success_is_detached_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_run(monkeypatch, 0, "\n", "")

        assert probe_checked_out_branch(tmp_path) == DetachedHead()

    @pytest.mark.parametrize("stderr", _REPO_FAULT_STDERRS, ids=_REPO_FAULT_IDS)
    def test_allowlisted_repo_fault_is_not_a_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
    ) -> None:
        """Each per-row phrase classifies as not-a-repository, on any git."""
        _stub_run(monkeypatch, 128, "", stderr)

        assert probe_checked_out_branch(tmp_path) == NotARepository()

    @pytest.mark.parametrize(
        ("returncode", "stderr", "expected"),
        [
            (
                128,
                "fatal: something new\n",
                "git branch --show-current exited 128: fatal: something new",
            ),
            (
                128,
                "fatal: detected dubious ownership in repository at '/x'\nhint: ...",
                "git branch --show-current exited 128: fatal: detected dubious"
                " ownership in repository at '/x'",
            ),
            (
                129,
                "error: unknown option `show-current'\n",
                "git branch --show-current exited 129: error: unknown option"
                " `show-current'",
            ),
            (1, "", "git branch --show-current exited 1: "),
            (
                128,
                "x" * 300,
                "git branch --show-current exited 128: " + "x" * 120,
            ),
        ],
        ids=[
            "unrecognized",
            "dubious-ownership",
            "git-too-old",
            "empty-stderr",
            "truncated",
        ],
    )
    def test_unrecognized_nonzero_exit_is_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        stderr: str,
        expected: str,
    ) -> None:
        """Exit 128 alone is not a per-row signal: only a known phrase is."""
        _stub_run(monkeypatch, returncode, "", stderr)

        result = probe_checked_out_branch(tmp_path)

        assert isinstance(result, ProbeUnavailable)
        assert isinstance(result.error, BranchLookupError)
        assert result.error.returncode == returncode
        assert str(result.error) == expected

    @pytest.mark.parametrize(
        "exc",
        [FileNotFoundError("git"), PermissionError("denied")],
        ids=["file-not-found", "permission"],
    )
    def test_oserror_is_carried_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: OSError
    ) -> None:
        _stub_raise(monkeypatch, exc)

        result = probe_checked_out_branch(tmp_path)

        assert isinstance(result, ProbeUnavailable)
        assert result.error is exc

    def test_runs_git_with_c_locale_utf8_replace_and_without_git_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_DIR", "/x")
        seen: dict[str, object] = {}

        def _fake_run(
            args: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            seen["args"] = args
            seen.update(kwargs)
            return subprocess.CompletedProcess(args, 0, "main\n", "")

        monkeypatch.setattr(_RUN, _fake_run)

        assert probe_checked_out_branch(tmp_path) == OnBranch("main")
        env = seen["env"]
        assert isinstance(env, dict)
        assert env["LC_ALL"] == "C"
        assert not [k for k in env if k.startswith("GIT_")]
        assert seen["args"] == ["git", "branch", "--show-current"]
        assert seen["cwd"] == str(tmp_path)
        assert seen["encoding"] == "utf-8"
        assert seen["errors"] == "replace"


class TestCheckedOutBranch:
    """The lenient ``_checked_out_branch`` wrapper: a name, or ``None``.

    The real-git plain-directory, dangling-gitfile and missing-directory cases
    are wording-independent here because the wrapper returns ``None`` for
    every non-branch outcome, whichever way git classifies the failure.
    """

    @pytest.mark.parametrize(
        ("returncode", "stdout", "stderr", "expected"),
        [
            (0, "feat/x\n", "", "feat/x"),
            (0, "\n", "", None),
            *[(128, "", stderr, None) for stderr in _REPO_FAULT_STDERRS],
            (128, "", "fatal: something new\n", None),
        ],
        ids=["on-branch", "detached-head", *_REPO_FAULT_IDS, "unrecognized"],
    )
    def test_stubbed_outcomes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        stdout: str,
        stderr: str,
        expected: str | None,
    ) -> None:
        """Every non-branch outcome reads ``None``; the wrapper never raises."""
        _stub_run(monkeypatch, returncode, stdout, stderr)

        assert _checked_out_branch(tmp_path) == expected

    @pytest.mark.parametrize(
        "exc",
        [FileNotFoundError("git"), OSError("fork failed")],
        ids=["file-not-found", "oserror"],
    )
    def test_oserror_reads_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: OSError
    ) -> None:
        _stub_raise(monkeypatch, exc)

        assert _checked_out_branch(tmp_path) is None

    def test_real_branch(self, make_git_repo: Callable[..., Path]) -> None:
        assert _checked_out_branch(make_git_repo("repo")) == "main"

    def test_real_detached_head(self, make_git_repo: Callable[..., Path]) -> None:
        repo = make_git_repo("repo")
        git_in(repo, "checkout", "--detach")

        assert _checked_out_branch(repo) is None

    def test_real_non_repositories_and_missing_directory(self, tmp_path: Path) -> None:
        for path in [*_non_repo_dirs(tmp_path), tmp_path / "gone"]:
            assert _checked_out_branch(path) is None, path
