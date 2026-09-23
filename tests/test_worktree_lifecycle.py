"""Tests for cw.worktree._lifecycle - create_worktree / remove_worktree."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw.exceptions import StaleWorktreeError, WorktreeError
from cw.models import (
    ClientConfig,
)
from cw.worktree import (
    _register_cw_exclude,
    _run_git,
    create_worktree,
    remove_worktree,
)
from tests._worktree_helpers import patch_worktree
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable


class TestRegisterCwExclude:
    """Tests for _register_cw_exclude — idempotent .cw/ exclude registration."""

    def test_appends_cw_pattern_to_exclude(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """First call writes .cw/ to $GIT_COMMON_DIR/info/exclude."""
        repo = make_git_repo("test-repo")
        _register_cw_exclude(repo)
        exclude = repo / ".git" / "info" / "exclude"
        assert ".cw/" in exclude.read_text().splitlines()

    def test_idempotent_second_call_does_not_duplicate(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Calling twice does not produce duplicate .cw/ lines."""
        repo = make_git_repo("test-repo")
        _register_cw_exclude(repo)
        _register_cw_exclude(repo)
        exclude = repo / ".git" / "info" / "exclude"
        assert exclude.read_text().splitlines().count(".cw/") == 1

    def test_pattern_already_present_is_left_unchanged(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Pre-existing .cw/ line is not duplicated."""
        repo = make_git_repo("test-repo")
        info_dir = repo / ".git" / "info"
        info_dir.mkdir(exist_ok=True)
        (info_dir / "exclude").write_text(".cw/\n")
        _register_cw_exclude(repo)
        assert (info_dir / "exclude").read_text().splitlines().count(".cw/") == 1

    def test_creates_info_dir_when_missing(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """info/ directory and exclude file are created if absent."""
        repo = make_git_repo("test-repo")
        info_dir = repo / ".git" / "info"
        import shutil

        shutil.rmtree(info_dir, ignore_errors=True)
        _register_cw_exclude(repo)
        assert (info_dir / "exclude").exists()
        assert ".cw/" in (info_dir / "exclude").read_text().splitlines()

    def test_does_not_touch_gitignore(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """The committed .gitignore is never modified."""
        repo = make_git_repo("test-repo")
        gitignore = repo / ".gitignore"
        original = "*.pyc\n__pycache__/\n"
        gitignore.write_text(original)
        _register_cw_exclude(repo)
        assert gitignore.read_text() == original

    def test_git_failure_logs_warning_and_does_not_raise(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """WorktreeError from _run_git is swallowed with a WARNING."""
        from cw.exceptions import WorktreeError

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            msg = "git not available"
            raise WorktreeError(msg)

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            _register_cw_exclude(tmp_path)  # must not raise
        assert any("_register_cw_exclude" in r.message for r in caplog.records)

    def test_oserror_logs_warning_and_does_not_raise(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """OSError from file I/O is swallowed with a WARNING."""
        repo = make_git_repo("test-repo")

        original_run = _run_git

        def mock_run(
            *args: str, cwd: Path, check: bool = True
        ) -> MagicMock | subprocess.CompletedProcess[str]:
            if "rev-parse" in args and "--git-common-dir" in args:
                return original_run(*args, cwd=cwd, check=check)
            return MagicMock(returncode=0, stdout="", stderr="")

        patch_worktree(monkeypatch, "_run_git", mock_run)
        info_dir = repo / ".git" / "info"
        info_dir.mkdir(exist_ok=True)
        exclude = info_dir / "exclude"
        exclude.write_text("")
        exclude.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="cw.worktree"):
                _register_cw_exclude(repo)  # must not raise
        finally:
            exclude.chmod(0o644)
        assert any("_register_cw_exclude" in r.message for r in caplog.records)


class TestCreateWorktree:
    def test_idempotent_existing_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Existing worktree on the requested branch is reused after a single
        branch-verification call — no ``worktree add`` (#404)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-search"
        wt_path.mkdir(parents=True)

        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            calls.append(args)
            result = MagicMock(returncode=0, stderr="")
            if "branch" in args and "--show-current" in args:
                # _checked_out_branch: branch matches.
                result.stdout = "feat/search\n"
            elif "status" in args:
                # worktree_has_unsaved_work: clean working tree.
                result.stdout = ""
            elif "rev-parse" in args and any("origin/" in a for a in args):
                # origin/<branch> exists.
                result.returncode = 0
                result.stdout = "abc1234\n"
            elif "log" in args:
                # No unpushed commits.
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        fetch_calls: list[tuple[ClientConfig, str]] = []

        def mock_fetch(client: ClientConfig, branch_name: str) -> bool:
            fetch_calls.append((client, branch_name))
            return True

        patch_worktree(monkeypatch, "_run_git", mock_run)
        patch_worktree(monkeypatch, "fetch_feature_branch", mock_fetch)
        result = create_worktree(client, "feat/search")
        assert result == wt_path
        # The behavior under test: an on-branch worktree is reused without a
        # `worktree add`. Assert that, not the exact verification command.
        assert not any("add" in call for call in calls)
        # Default reuse (refresh_on_reuse=False) is path resolution only: no
        # network fetch and no fast-forward (#2213).
        assert fetch_calls == []
        assert not any("merge" in call for call in calls)

    def test_existing_worktree_wrong_branch_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-existing worktree on a *different* branch is stale: refuse to
        reuse it rather than feed the worker a prior run's commits (#404)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "auto-dev-399"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            # Existing worktree is checked out on a different branch.
            return MagicMock(returncode=0, stdout="auto-dev/201\n", stderr="")

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(StaleWorktreeError, match="stale worktree"):
            create_worktree(client, "auto-dev/399")

    def test_existing_path_not_a_worktree_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A leftover plain directory (``git branch --show-current`` exits
        non-zero) is treated as stale and refused (#404)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "auto-dev-399"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            # `git branch --show-current` fails: not a git worktree.
            return MagicMock(returncode=128, stdout="", stderr="not a git repo")

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(StaleWorktreeError, match="stale worktree"):
            create_worktree(client, "auto-dev/399")

    def test_existing_worktree_detached_head_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Detached HEAD: ``git branch --show-current`` exits 0 with empty
        stdout. _checked_out_branch maps that to None, which mismatches any
        requested branch and is refused (#404). Distinct code path from the
        non-zero-returncode case above."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "auto-dev-399"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            # Detached HEAD: success exit, but no current branch name.
            return MagicMock(returncode=0, stdout="\n", stderr="")

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(StaleWorktreeError, match="detached HEAD"):
            create_worktree(client, "auto-dev/399")

    def test_existing_worktree_git_unavailable_raises_stale_not_oserror(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If git itself cannot be invoked (OSError), _checked_out_branch
        swallows it and returns None, so create_worktree raises
        StaleWorktreeError rather than letting the OSError leak to the caller
        — honouring the helper's no-raise contract (#404)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "auto-dev-399"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            msg = "git binary not found"
            raise FileNotFoundError(msg)

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(StaleWorktreeError):
            create_worktree(client, "auto-dev/399")

    def test_creates_new_branch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A: new branch + origin/main resolvable → worktree add uses origin/main
        as start-point, not operator HEAD (#710)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="", stdout="")
            if "rev-parse" in args and any("refs/heads/" in a for a in args):
                result.returncode = 128  # branch doesn't exist locally
            elif "rev-parse" in args and any("refs/remotes/origin/" in a for a in args):
                result.returncode = 128  # branch doesn't exist on remote either
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.returncode = 0  # origin/main resolves
                result.stdout = "abc1234\n"
            else:
                result.returncode = 0
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        result = create_worktree(client, "feat/new")
        assert result == tmp_path / "wt" / "feat-new"
        wt_add_calls = [c for c in git_calls if "worktree" in c and "add" in c]
        assert len(wt_add_calls) == 1
        assert "-b" in wt_add_calls[0]
        # The fix: start-point must be origin/main, not absent or HEAD-based
        assert "origin/main" in wt_add_calls[0]

    def test_uses_existing_branch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="")
            result.returncode = 0  # branch exists
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        create_worktree(client, "feat/existing")
        wt_add_calls = [c for c in git_calls if "worktree" in c and "add" in c]
        assert len(wt_add_calls) == 1
        assert "-b" not in wt_add_calls[0]
        # E: existing-branch path — no origin/ start-point added (#710 regression guard)
        assert not any("origin/" in a for a in wt_add_calls[0])
        # AC1 (#2032): a present local ref must short-circuit before any
        # remote check — no fetch, no refs/remotes/ lookup at all.
        assert not any("fetch" in c for c in git_calls)
        assert not any("refs/remotes" in a for c in git_calls for a in c)

    def test_remote_only_branch_resumes_remote_tip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC2: local ref absent but origin/<branch> exists — worktree add
        resumes the remote tip via `-b <branch> <path> origin/<branch>`,
        not `_resolve_branch_start_point`'s origin/main ladder (#2032)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []
        fetch_calls: list[tuple[ClientConfig, str]] = []

        def mock_fetch(fetch_client: ClientConfig, branch_name: str) -> bool:
            fetch_calls.append((fetch_client, branch_name))
            return True

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="", stdout="")
            if "rev-parse" in args and any("refs/heads/" in a for a in args):
                result.returncode = 128  # branch doesn't exist locally
            elif "rev-parse" in args and any("refs/remotes/origin/" in a for a in args):
                result.returncode = 0  # branch exists on the remote
                result.stdout = "abc1234\n"
            else:
                result.returncode = 0
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        patch_worktree(monkeypatch, "fetch_feature_branch", mock_fetch)
        wt_path = tmp_path / "wt" / "feat-remote-only"
        result = create_worktree(client, "feat/remote-only")
        assert result == wt_path
        assert fetch_calls == [(client, "feat/remote-only")]
        wt_add_calls = [c for c in git_calls if "worktree" in c and "add" in c]
        assert len(wt_add_calls) == 1
        assert list(wt_add_calls[0]) == [
            "worktree",
            "add",
            "-b",
            "feat/remote-only",
            str(wt_path),
            "origin/feat/remote-only",
        ]

    def test_new_branch_base_is_origin_main_not_operator_head(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """B (integration): new worktree starts from origin/main, not HEAD (#710).

        Sets up a real bare remote at C1, advances the workspace checkout to C2
        on a feature branch, then asserts the ticket worktree's HEAD == C1.
        """
        workspace = make_git_repo("workspace")
        c1 = git_in(workspace, "rev-parse", "HEAD")

        # Set up bare origin at C1 and fetch it into workspace
        origin = tmp_path / "origin.git"
        origin.mkdir()
        git_in(origin, "init", "--bare", "-b", "main")
        git_in(workspace, "remote", "add", "origin", str(origin))
        git_in(workspace, "push", "origin", "main")
        git_in(workspace, "fetch", "origin")

        # Advance workspace to C2 on an operator feature branch (simulating
        # the operator having a non-main branch checked out — the bug scenario)
        git_in(workspace, "checkout", "-b", "operator-feature")
        git_in(workspace, "commit", "--allow-empty", "-m", "operator commit C2")
        c2 = git_in(workspace, "rev-parse", "HEAD")
        assert c1 != c2

        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )
        wt_path = create_worktree(client, "dev/710")

        actual_head = git_in(wt_path, "rev-parse", "HEAD")
        assert actual_head == c1, (
            f"Worktree HEAD should be origin/main ({c1}), got {actual_head} "
            f"(operator HEAD was {c2})"
        )

    def test_stale_local_ref_resumes_remote_history_after_delete(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """AC2+AC5 (integration): after `git branch -D <branch>` locally
        (the exact auto-dev-review.md:291 fix-loop-reset sequence),
        create_worktree must resume the branch's pushed history via
        origin/<branch> — not silently start a fresh branch from
        origin/main and discard real, already-pushed work (#2032)."""
        workspace = make_git_repo("workspace")

        # Bare origin with main pushed.
        origin = tmp_path / "origin.git"
        origin.mkdir()
        git_in(origin, "init", "--bare", "-b", "main")
        git_in(workspace, "remote", "add", "origin", str(origin))
        git_in(workspace, "push", "origin", "main")
        git_in(workspace, "fetch", "origin")

        # Create the feature branch with a distinguishing commit, push it,
        # then delete the local ref — reproducing the exact
        # auto-dev-review.md:291 fix-loop reset (git branch -D after a
        # review restart) while origin still has the branch's real history.
        git_in(workspace, "checkout", "-b", "dev/2032-feature")
        git_in(workspace, "commit", "--allow-empty", "-m", "real feature work")
        feature_sha = git_in(workspace, "rev-parse", "HEAD")
        git_in(workspace, "push", "origin", "dev/2032-feature")
        git_in(workspace, "checkout", "main")
        git_in(workspace, "branch", "-D", "dev/2032-feature")

        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )
        wt_path = create_worktree(client, "dev/2032-feature")

        actual_head = git_in(wt_path, "rev-parse", "HEAD")
        assert actual_head == feature_sha, (
            f"Worktree HEAD should resume the pushed branch ({feature_sha}), "
            f"got {actual_head} — branch was likely recreated from "
            f"origin/main instead of resumed from its remote history"
        )

    def test_new_branch_falls_back_to_local_default_when_origin_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """C: origin/main absent (rc≠0), local main present (rc=0) → start-point is
        the local default branch, not origin/ (#710 offline fallback)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="", stdout="")
            if "rev-parse" in args and any("refs/heads/" in a for a in args):
                result.returncode = 128  # ticket branch doesn't exist
            elif "rev-parse" in args and any("refs/remotes/origin/" in a for a in args):
                result.returncode = 128  # branch doesn't exist on remote either
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.returncode = 128  # origin/main absent (offline)
            elif "rev-parse" in args:
                result.returncode = 0  # local main exists
                result.stdout = "abc1234\n"
            else:
                result.returncode = 0
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        create_worktree(client, "feat/new")
        wt_add_calls = [c for c in git_calls if "worktree" in c and "add" in c]
        assert len(wt_add_calls) == 1
        assert "-b" in wt_add_calls[0]
        assert "main" in wt_add_calls[0]
        assert not any("origin/" in a for a in wt_add_calls[0])

    def test_new_branch_raises_when_no_base_resolvable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D: both origin/main and local main absent → WorktreeError; no HEAD fallback
        (#710 — an unresolvable base must hard-fail, never silently use HEAD)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            return MagicMock(stderr="", stdout="", returncode=128)

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(WorktreeError):
            create_worktree(client, "feat/new")
        # Critically: no worktree add was attempted — no HEAD fallback
        assert not any("worktree" in c and "add" in c for c in git_calls)

    def test_create_worktree_rejects_path_equal_to_main_checkout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Guard against #300: create_worktree must refuse if wt_path == git_cwd.

        When worktree_path_for degenerately returns the client's own git
        directory, the guard must fire before any git operation to prevent
        accidental overwrites of the main checkout.
        """
        repo = make_git_repo("main-checkout")
        client = ClientConfig(
            name="test",
            workspace_path=repo,
            worktree_base=tmp_path / "wt",
        )

        # Simulate the degenerate case: worktree_path_for returns the repo itself.
        patch_worktree(monkeypatch, "worktree_path_for", lambda _client, _branch: repo)

        with pytest.raises(WorktreeError, match="main checkout"):
            create_worktree(client, "auto-dev-300")

    def test_create_worktree_rejects_symlink_to_main_checkout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """Guard catches symlinks: resolve() normalises a symlink to main checkout."""
        repo = make_git_repo("main-checkout")
        symlink_path = tmp_path / "link-to-main"
        symlink_path.symlink_to(repo)

        client = ClientConfig(
            name="test",
            workspace_path=repo,
            worktree_base=tmp_path / "wt",
        )
        patch_worktree(
            monkeypatch, "worktree_path_for", lambda _client, _branch: symlink_path
        )

        with pytest.raises(WorktreeError, match="main checkout"):
            create_worktree(client, "auto-dev-300")

    def test_idempotent_clean_reuse_returns_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clean reused worktree (no unsaved work) returns the path (#426)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-clean"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "branch" in args and "--show-current" in args:
                result.stdout = "feat/clean\n"
            elif "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.returncode = 0
                result.stdout = "abc1234\n"
            elif "log" in args:
                result.stdout = ""  # no unpushed commits
            else:
                result.stdout = ""
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        result = create_worktree(client, "feat/clean")
        assert result == wt_path

    def test_dirty_reuse_raises_stale_worktree_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dirty reused worktree (uncommitted changes) raises StaleWorktreeError (#426).

        See: #426."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-dirty"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "branch" in args and "--show-current" in args:
                result.stdout = "feat/dirty\n"
            elif "status" in args:
                result.stdout = " M modified_file.py\n"  # uncommitted changes
            else:
                result.stdout = ""
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(StaleWorktreeError, match="unsaved work"):
            create_worktree(client, "feat/dirty")

    def test_dirty_reuse_allowed_returns_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """allow_dirty_reuse tolerates same-branch dirty reuse (#712 staged pipeline).

        The staged pipeline reuses one per-ticket worktree across stages; a prior
        stage legitimately leaves uncommitted churn (e.g. uv.lock). With
        allow_dirty_reuse the reuse returns the path instead of raising.
        """
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "dev-662"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "branch" in args and "--show-current" in args:
                result.stdout = "dev/662\n"
            elif "status" in args:
                result.stdout = " M uv.lock\n"  # cross-stage churn
            else:
                result.stdout = ""
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        result = create_worktree(client, "dev/662", allow_dirty_reuse=True)
        assert result == wt_path

    def test_dirty_reuse_allowed_still_refuses_branch_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """allow_dirty_reuse relaxes the unsaved-work guard ONLY — a foreign
        branch at the path is still refused (cross-ticket protection intact)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "dev-662"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "branch" in args and "--show-current" in args:
                result.stdout = "dev/999\n"  # foreign branch
            else:
                result.stdout = ""
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(StaleWorktreeError, match="stale worktree"):
            create_worktree(client, "dev/662", allow_dirty_reuse=True)

    def test_unpushed_commits_reuse_raises_stale_worktree_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dirty reused worktree (unpushed commits) raises StaleWorktreeError (#426)."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-unpushed"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "branch" in args and "--show-current" in args:
                result.stdout = "feat/unpushed\n"
            elif "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.returncode = 0
                result.stdout = "abc1234\n"
            elif "log" in args:
                result.stdout = "abc1234 add feature\n"  # unpushed commit
            else:
                result.stdout = ""
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(StaleWorktreeError, match="unsaved work"):
            create_worktree(client, "feat/unpushed")

    def test_registers_cw_exclude_on_new_worktree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_register_cw_exclude is called once when a new worktree is created."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        exclude_calls: list[Path] = []

        def mock_register(git_cwd: Path) -> None:
            exclude_calls.append(git_cwd)

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            result = MagicMock(stderr="", stdout="")
            if "rev-parse" in args and any("refs/heads/" in a for a in args):
                result.returncode = 128  # ticket branch doesn't exist locally
            elif "rev-parse" in args and any("refs/remotes/origin/" in a for a in args):
                result.returncode = 128  # branch doesn't exist on remote either
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.returncode = 0  # origin/main resolves (start-point ladder)
                result.stdout = "abc1234\n"
            else:
                result.returncode = 0
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        monkeypatch.setattr(
            "cw.worktree._lifecycle._register_cw_exclude", mock_register
        )
        create_worktree(client, "feat/new")
        assert len(exclude_calls) == 1

    def test_does_not_register_exclude_on_idempotent_reuse(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_register_cw_exclude is NOT called when an existing worktree is reused."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-clean-reuse"
        wt_path.mkdir(parents=True)
        exclude_calls: list[Path] = []

        def mock_register(git_cwd: Path) -> None:
            exclude_calls.append(git_cwd)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "branch" in args and "--show-current" in args:
                result.stdout = "feat/clean-reuse\n"
            elif "status" in args:
                result.stdout = ""
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.stdout = "abc1234\n"
            elif "log" in args:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        monkeypatch.setattr(
            "cw.worktree._lifecycle._register_cw_exclude", mock_register
        )
        create_worktree(client, "feat/clean-reuse")
        assert len(exclude_calls) == 0


class TestSubmoduleInit:
    def test_submodule_init_when_gitmodules_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        # Create .gitmodules to trigger submodule init
        (ws / ".gitmodules").write_text("[submodule]\n")

        client = ClientConfig(
            name="test",
            workspace_path=ws,
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="", stdout="")
            if "rev-parse" in args and any("refs/heads/" in a for a in args):
                result.returncode = 128  # ticket branch doesn't exist locally
            elif "rev-parse" in args and any("refs/remotes/origin/" in a for a in args):
                result.returncode = 128  # branch doesn't exist on remote either
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.returncode = 0  # origin/main resolves
                result.stdout = "abc1234\n"
            else:
                result.returncode = 0
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        create_worktree(client, "feat/new")

        # Should have: rev-parse, worktree add, submodule update
        submodule_calls = [c for c in git_calls if "submodule" in c]
        assert len(submodule_calls) == 1
        assert "update" in submodule_calls[0]
        assert "--init" in submodule_calls[0]
        assert "--recursive" in submodule_calls[0]

    def test_no_submodule_init_without_gitmodules(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        # No .gitmodules file

        client = ClientConfig(
            name="test",
            workspace_path=ws,
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            result = MagicMock(stderr="", stdout="")
            if "rev-parse" in args and any("refs/heads/" in a for a in args):
                result.returncode = 128  # ticket branch doesn't exist locally
            elif "rev-parse" in args and any("refs/remotes/origin/" in a for a in args):
                result.returncode = 128  # branch doesn't exist on remote either
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.returncode = 0  # origin/main resolves
                result.stdout = "abc1234\n"
            else:
                result.returncode = 0
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        create_worktree(client, "feat/new")

        submodule_calls = [c for c in git_calls if "submodule" in c]
        assert len(submodule_calls) == 0

    def test_worktree_client_uses_repo_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worktree-mode client uses repo_path for git cwd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        client = ClientConfig(
            name="test",
            repo_path=repo,
            branch="client-a",
            worktree_base=tmp_path / "wt",
        )
        git_cwds: list[object] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_cwds.append(cwd)
            result = MagicMock(stderr="", stdout="")
            if "rev-parse" in args and any("refs/heads/" in a for a in args):
                result.returncode = 128  # ticket branch doesn't exist locally
            elif "rev-parse" in args and any("refs/remotes/origin/" in a for a in args):
                result.returncode = 128  # branch doesn't exist on remote either
            elif "rev-parse" in args and any("origin/" in a for a in args):
                result.returncode = 0  # origin/main resolves
                result.stdout = "abc1234\n"
            else:
                result.returncode = 0
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)
        create_worktree(client, "client-a")

        # All git commands should use repo_path, not workspace_path
        for cwd in git_cwds:
            assert str(cwd) == str(repo)


class TestCreateWorktreeBranchHeldElsewhere:
    """#2034: a foreign (non-cw) worktree already checked out on the
    requested branch — e.g. an orphaned harness Agent(isolation="worktree")
    workspace (#2017). ``git worktree add`` fails with a real
    "already used by worktree at '<path>'" fatal line; create_worktree must
    surface a targeted, actionable error instead of a bare re-raise, without
    ever touching the foreign worktree.

    Uses the real-git ``make_git_repo`` fixture (not a mocked ``_run_git``)
    because git's exact stderr shape is the thing under test — a hand-typed
    fixture would just re-assert our own assumption about that shape.
    """

    def _make_foreign_holder(
        self, tmp_path: Path, workspace: Path, branch: str
    ) -> Path:
        """Create a real worktree on *branch* OUTSIDE the client's worktree_base.

        Simulates a harness agent worktree at ``.claude/worktrees/agent-<id>``
        — a location cw's own worktree_base/GC scan never covers.
        """
        holder = tmp_path / "external-harness" / "agent-abc123"
        holder.parent.mkdir(parents=True)
        git_in(workspace, "worktree", "add", str(holder), "-b", branch)
        return holder

    def test_holder_path_named_in_error(
        self, tmp_path: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.exceptions import BranchHeldByWorktreeError

        workspace = make_git_repo("workspace")
        holder = self._make_foreign_holder(tmp_path, workspace, "dev/2034")

        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )

        with pytest.raises(BranchHeldByWorktreeError) as exc_info:
            create_worktree(client, "dev/2034")

        exc = exc_info.value
        assert exc.holder_path == holder
        assert str(holder) in str(exc)

    def test_holder_never_removed(
        self, tmp_path: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.exceptions import BranchHeldByWorktreeError

        workspace = make_git_repo("workspace")
        holder = self._make_foreign_holder(tmp_path, workspace, "dev/2034")

        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )

        with pytest.raises(BranchHeldByWorktreeError):
            create_worktree(client, "dev/2034")

        # AC #3 by construction: the foreign worktree must still be on disk
        # AND still registered with git — never force-removed as a side effect.
        assert holder.exists()
        listing = git_in(workspace, "worktree", "list")
        assert str(holder) in listing

    def test_clean_holder_message_says_clean(
        self, tmp_path: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.exceptions import BranchHeldByWorktreeError

        workspace = make_git_repo("workspace")
        holder = self._make_foreign_holder(tmp_path, workspace, "dev/2034")
        # holder has no uncommitted changes and nothing unpushed beyond origin.

        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )

        with pytest.raises(BranchHeldByWorktreeError) as exc_info:
            create_worktree(client, "dev/2034")

        msg = str(exc_info.value)
        assert "clean" in msg.lower()
        assert f"git worktree remove {holder}" in msg

    def test_dirty_holder_message_warns(
        self, tmp_path: Path, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.exceptions import BranchHeldByWorktreeError

        workspace = make_git_repo("workspace")
        holder = self._make_foreign_holder(tmp_path, workspace, "dev/2034")
        (holder / "uncommitted.txt").write_text("wip")

        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )

        with pytest.raises(BranchHeldByWorktreeError) as exc_info:
            create_worktree(client, "dev/2034")

        msg = str(exc_info.value)
        assert "safe to remove" not in msg.lower()
        assert "verify" in msg.lower()
        assert f"git worktree remove {holder}" in msg

    def test_unparseable_collision_message_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A WorktreeError from ``_run_git`` whose text doesn't match the
        known collision shape must re-raise unchanged — no mis-wrap, no
        crash. Mirrors the mocking style of
        test_existing_worktree_git_unavailable_raises_stale_not_oserror."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(stderr="", stdout="")
            if "worktree" in args and "add" in args:
                msg = (
                    "Git command failed: git worktree add x y\n"
                    "fatal: some other failure"
                )
                raise WorktreeError(msg)
            if "rev-parse" in args and any(
                a.startswith(("refs/heads/", "refs/remotes/")) for a in args
            ):
                result.returncode = 128  # branch doesn't exist locally or on remote
            else:
                result.returncode = 0  # origin/main start-point resolves
                result.stdout = "abc1234\n"
            return result

        patch_worktree(monkeypatch, "_run_git", mock_run)

        with pytest.raises(WorktreeError, match="some other failure") as exc_info:
            create_worktree(client, "dev/2034")

        from cw.exceptions import BranchHeldByWorktreeError

        assert not isinstance(exc_info.value, BranchHeldByWorktreeError)


class TestRemoveWorktree:
    def test_removes_existing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-search"
        wt_path.mkdir(parents=True)

        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            return MagicMock(returncode=0, stderr="")

        patch_worktree(monkeypatch, "_run_git", mock_run)
        remove_worktree(client, "feat/search")
        assert any("remove" in call for call in git_calls)

    def test_noop_if_not_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            return MagicMock(returncode=0, stderr="")

        patch_worktree(monkeypatch, "_run_git", mock_run)
        remove_worktree(client, "feat/nonexistent")
        assert len(git_calls) == 0

    def test_force_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-dirty"
        wt_path.mkdir(parents=True)

        git_calls: list[tuple[str, ...]] = []

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            git_calls.append(args)
            return MagicMock(returncode=0, stderr="")

        patch_worktree(monkeypatch, "_run_git", mock_run)
        remove_worktree(client, "feat/dirty", force=True)
        assert any("--force" in call for call in git_calls)

    def test_raises_on_dirty_without_force(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from subprocess import CalledProcessError

        from cw.exceptions import WorktreeError

        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-dirty"
        wt_path.mkdir(parents=True)

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            if "--force" not in args:
                msg = (
                    "fatal: 'feat-dirty' contains modified or untracked files, "
                    "use --force to delete it"
                )
                err = CalledProcessError(
                    128,
                    " ".join(args),
                    stderr=msg,
                )
                wt_msg = "Git command failed"
                raise WorktreeError(wt_msg) from err
            return MagicMock(returncode=0, stderr="")

        patch_worktree(monkeypatch, "_run_git", mock_run)
        with pytest.raises(WorktreeError):
            remove_worktree(client, "feat/dirty", force=False)
