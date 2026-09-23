"""Tests for cw.worktree - Git worktree operations."""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw import native_daemon
from cw.auto_dev_result import AutoDevResult
from cw.config import save_state, state_file
from cw.events import read_events
from cw.exceptions import StaleWorktreeError, WorktreeError, WorktreeOccupiedError
from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.worktree import (
    FetchOutcome,
    FetchResult,
    RefreshOutcome,
    RefreshResult,
    ReuseRefreshReport,
    _fetch_default_branch,
    _git_dir,
    _hashed_worktree_base,
    _normalize_path,
    _Occupancy,
    _ref_exists,
    _refresh_reused_worktree,
    _register_cw_exclude,
    _resolve_remote_ref,
    _reuse_occupancy,
    _run_git,
    check_main_ff_safety,
    check_not_main_checkout,
    create_worktree,
    effective_worktree_bases,
    fast_forward_main,
    fetch_feature_branch,
    is_main_behind_origin,
    is_main_checkout_dirty,
    live_home_reason,
    live_session_worktree_paths,
    remove_worktree,
    resolve_scope_guard_default_branch,
    resolve_worktree_base,
    slugify_branch,
    unsaved_work_reason,
    worktree_has_unsaved_work,
    worktree_path_for,
)
from cw.worktree_gc import _live_worktree_paths
from tests._reconcile_helpers import _no_op_salvage_payload
from tests.conftest import git_in, push_commit_to_origin
from tests.test_result import _valid_payload

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import OrchestratorEvent
    from cw.worktree import FetchWarningKey


class TestSlugifyBranch:
    def test_slash_to_hyphen(self) -> None:
        assert slugify_branch("feat/search") == "feat-search"

    def test_multiple_slashes(self) -> None:
        assert slugify_branch("feat/ui/search") == "feat-ui-search"

    def test_backslash(self) -> None:
        assert slugify_branch("feat\\search") == "feat-search"

    def test_no_slashes(self) -> None:
        assert slugify_branch("main") == "main"

    def test_trailing_slash_stripped(self) -> None:
        assert slugify_branch("feat/") == "feat"

    def test_hash_replaced(self) -> None:
        # Regression: GitHub issue ids like "#7" used to leak through and
        # break `claude -w` worktree path validation (issue #83). The double
        # hyphen is fine — claude's segment validator allows `-`; readability
        # is the lesser concern.
        assert slugify_branch("auto-dev-#7") == "auto-dev--7"

    def test_hash_run_collapses(self) -> None:
        assert slugify_branch("auto-dev-##7") == "auto-dev--7"

    def test_leading_hash_stripped(self) -> None:
        assert slugify_branch("#7") == "7"

    def test_spaces_replaced(self) -> None:
        assert slugify_branch("feat search bar") == "feat-search-bar"

    def test_unicode_replaced(self) -> None:
        assert slugify_branch("feat-café") == "feat-caf"

    def test_dots_and_underscores_preserved(self) -> None:
        assert slugify_branch("v1.2_beta") == "v1.2_beta"


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


class TestResolveWorktreeBase:
    def test_uses_client_worktree_base(self, tmp_path: Path) -> None:
        custom_base = tmp_path / "custom-worktrees"
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=custom_base,
        )
        assert resolve_worktree_base(client) == custom_base

    def test_default_sibling_directory(self, tmp_path: Path) -> None:
        ws = tmp_path / "projects" / "my-repo"
        client = ClientConfig(name="test", workspace_path=ws)
        expected = tmp_path / "projects" / ".worktrees" / "my-repo"
        assert resolve_worktree_base(client) == expected


class TestEffectiveWorktreeBases:
    def test_explicit_worktree_base_returns_singleton(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom-wt"
        client = ClientConfig(
            name="test", workspace_path=tmp_path, worktree_base=custom
        )
        bases = effective_worktree_bases(client)
        assert bases == frozenset({custom})

    def test_no_worktree_base_returns_two_paths(self, tmp_path: Path) -> None:
        """Without explicit worktree_base, both default and hash bases returned."""
        client = ClientConfig(name="test", workspace_path=tmp_path / "ws")
        bases = effective_worktree_bases(client)
        assert len(bases) == 2
        assert _hashed_worktree_base(client) in bases

    def test_no_worktree_base_includes_resolve_worktree_base(
        self, tmp_path: Path
    ) -> None:
        client = ClientConfig(name="test", workspace_path=tmp_path / "ws")
        assert resolve_worktree_base(client) in effective_worktree_bases(client)

    def test_explicit_worktree_base_no_hash_fallback(self, tmp_path: Path) -> None:
        """Explicit base → only one path, no hash fallback added."""
        custom = tmp_path / "custom"
        client = ClientConfig(
            name="test", workspace_path=tmp_path, worktree_base=custom
        )
        assert len(effective_worktree_bases(client)) == 1


class TestWorktreePathFor:
    def test_combines_base_and_slug(self, tmp_path: Path) -> None:
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        result = worktree_path_for(client, "feat/search")
        assert result == tmp_path / "wt" / "feat-search"

    def test_long_default_path_falls_back_to_hashed_base(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the default worktree path would exceed the conservative 64-char
        path-length threshold, fall back to a short hash-based base under
        ``~/.cw/wt/``."""
        monkeypatch.setattr(Path, "home", lambda: Path("/home/u"))

        # Mimic the failing real-world case from the bug report: a long
        # workspace parent + a longish repo name.
        ws = Path("/home/matthew/workspace/companies/infini-player")
        client = ClientConfig(name="infini-player", workspace_path=ws)

        result = worktree_path_for(client, "auto-dev/1")

        # Must be under the conservative 64-char path-length threshold.
        path_len = len(str(result))
        assert path_len <= 64, (
            f"path length {path_len} exceeds 64-char threshold: {result}"
        )
        # Must be under the hash-based fallback root, not the sibling default.
        assert str(result).startswith("/home/u/.cw/wt/")
        # Branch slug preserved at the tail.
        assert result.name == "auto-dev-1"

    def test_short_default_path_unchanged(self) -> None:
        """Short default paths keep the sibling-directory layout."""
        ws = Path("/p/r")
        client = ClientConfig(name="r", workspace_path=ws)
        result = worktree_path_for(client, "main")
        assert result == Path("/p/.worktrees/r/main")

    def test_hashed_fallback_is_stable_across_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """create_worktree and remove_worktree must compute the same path
        for the same client; the hash fallback must be deterministic."""
        monkeypatch.setattr(Path, "home", lambda: Path("/home/u"))
        ws = Path("/home/matthew/workspace/companies/infini-player")
        client = ClientConfig(name="infini-player", workspace_path=ws)

        first = worktree_path_for(client, "auto-dev/1")
        second = worktree_path_for(client, "auto-dev/1")
        assert first == second

    def test_client_override_used_even_when_long(self) -> None:
        """An explicit ``worktree_base`` is respected even if it makes the
        resulting path exceed the cap — user choice wins over our fallback."""
        override = Path("/this/is/a/deliberately/long/override/path/from/the/user")
        client = ClientConfig(
            name="test",
            workspace_path=Path("/ws"),
            worktree_base=override,
        )
        result = worktree_path_for(client, "feat/x")
        assert result == override / "feat-x"


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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        monkeypatch.setattr("cw.worktree.fetch_feature_branch", mock_fetch)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        monkeypatch.setattr("cw.worktree.fetch_feature_branch", mock_fetch)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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
        monkeypatch.setattr(
            "cw.worktree.worktree_path_for", lambda _client, _branch: repo
        )

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
        monkeypatch.setattr(
            "cw.worktree.worktree_path_for", lambda _client, _branch: symlink_path
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        monkeypatch.setattr("cw.worktree._register_cw_exclude", mock_register)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        monkeypatch.setattr("cw.worktree._register_cw_exclude", mock_register)
        create_worktree(client, "feat/clean-reuse")
        assert len(exclude_calls) == 0


_REUSE_BRANCH = "dev/2213"


def _seed_reuse(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> tuple[ClientConfig, Path, Path, Path]:
    """Real-git fixture for the reuse-refresh tests (#2213).

    Builds a workspace with a bare ``origin`` carrying ``main``, provisions the
    ``dev/2213`` worktree through ``create_worktree``, commits ``tracked.txt``
    in it and pushes the branch. Returns ``(client, wt, origin, workspace)``
    with the worktree clean, fully pushed, and in sync with the workspace's
    ``refs/remotes/origin/dev/2213``.
    """
    workspace = make_git_repo("workspace")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git_in(origin, "init", "--bare", "-b", "main")
    git_in(workspace, "remote", "add", "origin", str(origin))
    git_in(workspace, "push", "origin", "main")
    git_in(workspace, "fetch", "origin")
    client = ClientConfig(
        name="test",
        workspace_path=workspace,
        worktree_base=tmp_path / "wt",
    )
    wt = create_worktree(client, _REUSE_BRANCH)
    (wt / "tracked.txt").write_text("v1\n", encoding="utf-8")
    git_in(wt, "add", "tracked.txt")
    git_in(wt, "commit", "-m", "add tracked")
    git_in(wt, "push", "origin", _REUSE_BRANCH)
    return client, wt, origin, workspace


def _cw_worktree_records(
    caplog: pytest.LogCaptureFixture, min_level: int
) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == "cw.worktree" and r.levelno >= min_level
    ]


def _spy_fetch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wrap ``cw.worktree.fetch_feature_branch`` with a delegating recorder.

    Returns the list of branch names it was called with, so a test can prove
    the refresh did (or did not) touch the network.
    """
    real = fetch_feature_branch
    fetched: list[str] = []

    def spy(client: ClientConfig, branch_name: str) -> FetchResult:
        fetched.append(branch_name)
        return real(client, branch_name)

    monkeypatch.setattr("cw.worktree.fetch_feature_branch", spy)
    return fetched


def _spy_git(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[str, ...], Path]]:
    """Wrap ``cw.worktree._run_git`` with a delegating recorder of ``(args, cwd)``."""
    calls: list[tuple[tuple[str, ...], Path]] = []

    def spy(
        *args: str, cwd: Path, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        return _run_git(*args, cwd=cwd, check=check)

    monkeypatch.setattr("cw.worktree._run_git", spy)
    return calls


def _seed_session(
    workspace: Path, worktree: Path | None, status: SessionStatus
) -> None:
    """Persist one session homed on *worktree* into the (tmp) cw state."""
    save_state(
        CwState(
            sessions=[
                Session(
                    name="test/impl",
                    client="test",
                    purpose=SessionPurpose.IMPL,
                    status=status,
                    origin=SessionOrigin.DAEMON,
                    workspace_path=workspace,
                    worktree_path=worktree,
                )
            ]
        )
    )


def _force_push_rewrite(origin: Path, work_dir: Path, branch: str) -> str:
    """Replace ``origin/<branch>``'s history with a fresh commit off ``main``.

    Simulates a rewritten remote (force-push) so the worktree's pushed commit
    is no longer an ancestor of the remote tip. Returns the new tip SHA.
    """
    git_in(work_dir.parent, "clone", str(origin), str(work_dir))
    git_in(work_dir, "checkout", "-B", branch, "origin/main")
    (work_dir / "rewritten.txt").write_text("rewritten\n", encoding="utf-8")
    git_in(work_dir, "add", "rewritten.txt")
    git_in(
        work_dir,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=cw test",
        "commit",
        "-m",
        "rewritten history",
    )
    git_in(work_dir, "push", "--force", "origin", branch)
    return git_in(work_dir, "rev-parse", "HEAD")


def _session_at(name: str, status: SessionStatus, wt: Path | None) -> Session:
    return Session(
        name=name,
        client="c",
        purpose=SessionPurpose.IMPL,
        status=status,
        origin=SessionOrigin.DAEMON,
        workspace_path=Path("/repo"),
        worktree_path=wt,
    )


class TestLiveSessionWorktreePaths:
    """The session-state half of the live-path guard, shared by the worktree GC
    and the create_worktree reuse refresh (#2213)."""

    @pytest.mark.parametrize(
        "status",
        [SessionStatus.ACTIVE, SessionStatus.IDLE, SessionStatus.BACKGROUNDED],
    )
    def test_non_terminal_session_path_included(
        self, monkeypatch: pytest.MonkeyPatch, status: SessionStatus
    ) -> None:
        live = Path("/live/wt")
        state = CwState(sessions=[_session_at("c/impl", status, live)])
        monkeypatch.setattr("cw.worktree.load_state", lambda: state)

        assert live_session_worktree_paths() == frozenset({live})

    def test_terminal_and_pathless_sessions_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = CwState(
            sessions=[
                _session_at("c/done", SessionStatus.COMPLETED, Path("/done/wt")),
                _session_at("c/nopath", SessionStatus.ACTIVE, None),
            ]
        )
        monkeypatch.setattr("cw.worktree.load_state", lambda: state)

        assert live_session_worktree_paths() == frozenset()

    def test_state_load_failure_returns_none_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom() -> CwState:
            msg = "corrupt"
            raise ValueError(msg)

        monkeypatch.setattr("cw.worktree.load_state", _boom)

        with caplog.at_level("WARNING", logger="cw.worktree"):
            paths = live_session_worktree_paths()

        assert paths is None
        assert any(
            "failed to load session state" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.parametrize("kind", ["oserror", "invalid-json", "validation-error"])
    def test_expected_state_read_failures_return_none(
        self,
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        """The three failure families a state read can really raise -- I/O
        (a directory where the file should be), a JSON syntax error, and a
        pydantic ValidationError -- all degrade to None, via the real
        ``load_state`` and a real file on disk."""
        path = state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "oserror":
            path.mkdir()
        elif kind == "invalid-json":
            path.write_text("{not json", encoding="utf-8")
        else:
            path.write_text(
                json.dumps({"sessions": [{"name": "c/impl"}]}), encoding="utf-8"
            )

        with caplog.at_level("WARNING", logger="cw.worktree"):
            assert live_session_worktree_paths() is None

        assert any(
            "failed to load session state" in r.getMessage() for r in caplog.records
        )

    def test_unexpected_exception_type_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the enumerated read failures degrade to None; anything else is a
        bug that must surface rather than read as "no live sessions" (#2213)."""

        def _boom() -> CwState:
            msg = "not a state-read failure"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.worktree.load_state", _boom)

        with pytest.raises(RuntimeError, match="not a state-read failure"):
            live_session_worktree_paths()


def _write_corrupt_state() -> Path:
    """Put a syntactically invalid ``sessions.json`` on disk, for real."""
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    return path


class TestUnreadableStatePostures:
    """One shared helper, two deliberately opposite failure postures (#2213).

    The SAME unreadable ``sessions.json`` makes the reuse refresh fail CLOSED
    (a mutation must not proceed when a live session cannot be ruled out) and
    leaves the worktree GC failing OPEN (a corrupt state file must never block
    garbage collection, unchanged from before #2213). Neither is a bug in the
    other's terms; do not "fix" one into consistency with the other.
    """

    def test_reuse_refresh_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")
        fetched = _spy_fetch(monkeypatch)
        # Written only after seeding, which itself reads and writes cw state.
        _write_corrupt_state()

        caplog.clear()  # drop seed-phase records
        with (
            caplog.at_level(logging.DEBUG, logger="cw.worktree"),
            pytest.raises(WorktreeOccupiedError) as excinfo,
        ):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert excinfo.value.path == wt
        assert "unreadable" in excinfo.value.reason
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any(
            r.levelno == logging.DEBUG and "unreadable" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    def test_worktree_gc_fails_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_corrupt_state()
        monkeypatch.setattr(
            "cw.worktree_gc.load_dev_queue", lambda: MagicMock(tasks=[])
        )

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            paths = _live_worktree_paths()

        assert paths == frozenset()
        assert any(
            "failed to load session state" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )


class TestCreateWorktreeReuseRefresh:
    """#2213: with ``refresh_on_reuse=True`` the reuse path best-effort fetches
    and fast-forwards a *behind, unoccupied, clean* worktree. It never resets,
    never raises, and leaves an occupied or diverged worktree exactly as it is."""

    def test_default_reuse_does_not_fetch_or_move(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")
        fetched = _spy_fetch(monkeypatch)

        result = create_worktree(client, _REUSE_BRANCH, allow_dirty_reuse=True)

        assert result == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert (
            git_in(workspace, "rev-parse", f"refs/remotes/origin/{_REUSE_BRANCH}")
            == old_sha
        )

    @pytest.mark.parametrize("allow_dirty_reuse", [False, True])
    def test_behind_worktree_fast_forwarded_on_reuse(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        allow_dirty_reuse: bool,
    ) -> None:
        client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        new_sha = push_commit_to_origin(
            origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt"
        )
        assert new_sha != old_sha
        # Precondition: both the worktree and the workspace's tracking ref are
        # stale, so only a fetch plus fast-forward can bring HEAD to new_sha.
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert (
            git_in(workspace, "rev-parse", f"refs/remotes/origin/{_REUSE_BRANCH}")
            == old_sha
        )

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.INFO, logger="cw.worktree"):
            result = create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=allow_dirty_reuse,
                refresh_on_reuse=True,
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert git_in(workspace, "rev-parse", f"refs/heads/{_REUSE_BRANCH}") == new_sha
        assert (wt / "upstream.txt").exists()
        assert any(
            r.levelno == logging.INFO and "fast-forwarded" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.INFO)
        )

    @pytest.mark.parametrize(
        ("local_file", "local_content", "upstream_file", "upstream_content"),
        [
            pytest.param(
                "tracked.txt",
                "local churn\n",
                "upstream.txt",
                "out-of-band\n",
                id="uncommitted-nonoverlapping",
            ),
            pytest.param(
                "tracked.txt",
                "local edit\n",
                "tracked.txt",
                "upstream edit\n",
                id="uncommitted-overlapping",
            ),
            pytest.param(
                "scratch.txt",
                "untracked\n",
                "upstream.txt",
                "out-of-band\n",
                id="untracked",
            ),
        ],
    )
    def test_uncommitted_work_is_not_fast_forwarded(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        local_file: str,
        local_content: str,
        upstream_file: str,
        upstream_content: str,
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        (wt / local_file).write_text(local_content, encoding="utf-8")
        push_commit_to_origin(
            origin,
            _REUSE_BRANCH,
            tmp_path / "side",
            upstream_file,
            content=upstream_content,
        )
        fetched = _spy_fetch(monkeypatch)

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        # The occupancy check is pre-network: no fetch, HEAD and edit untouched.
        assert result == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert (wt / local_file).read_text(encoding="utf-8") == local_content
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            r.levelno == logging.DEBUG
            and str(wt) in r.getMessage()
            and "uncommitted" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    @pytest.mark.parametrize("remote_moved", [False, True])
    def test_unpushed_local_commit_is_not_moved(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        remote_moved: bool,
    ) -> None:
        """Ahead of origin (remote_moved False) or diverged from it
        (remote_moved True, an unpushed commit plus a different remote one):
        either way the unpushed commit marks the worktree occupied."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        git_in(wt, "commit", "--allow-empty", "-m", "unpushed local work")
        local_sha = git_in(wt, "rev-parse", "HEAD")
        if remote_moved:
            push_commit_to_origin(
                origin, _REUSE_BRANCH, tmp_path / "side", "theirs.txt"
            )
        fetched = _spy_fetch(monkeypatch)

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == local_sha
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            r.levelno == logging.DEBUG and "not on" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    @pytest.mark.parametrize(
        ("status", "homed_here", "expect_moved"),
        [
            pytest.param(SessionStatus.ACTIVE, True, False, id="active"),
            pytest.param(SessionStatus.IDLE, True, False, id="idle"),
            pytest.param(SessionStatus.BACKGROUNDED, True, False, id="backgrounded"),
            pytest.param(SessionStatus.COMPLETED, True, True, id="terminal-completed"),
            pytest.param(SessionStatus.ACTIVE, False, True, id="active-elsewhere"),
        ],
    )
    def test_live_session_homed_on_worktree_blocks_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        status: SessionStatus,
        homed_here: bool,
        expect_moved: bool,
    ) -> None:
        client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        new_sha = push_commit_to_origin(
            origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt"
        )
        _seed_session(workspace, wt if homed_here else tmp_path / "elsewhere", status)
        fetched = _spy_fetch(monkeypatch)

        caplog.clear()  # drop seed-phase records
        if expect_moved:
            with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
                result = create_worktree(
                    client,
                    _REUSE_BRANCH,
                    allow_dirty_reuse=True,
                    refresh_on_reuse=True,
                )
            assert result == wt
            assert git_in(wt, "rev-parse", "HEAD") == new_sha
            return
        with (
            caplog.at_level(logging.DEBUG, logger="cw.worktree"),
            pytest.raises(WorktreeOccupiedError) as excinfo,
        ):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert excinfo.value.path == wt
        assert "live session" in excinfo.value.reason
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            r.levelno == logging.DEBUG
            and str(wt) in r.getMessage()
            and "live session" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    def test_unreadable_session_state_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A mutation must not proceed when a live session cannot be ruled out."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")
        # ``cw.worktree`` imports the function lazily (import cycle), so the
        # patch target is its home module.
        monkeypatch.setattr("cw.worktree.live_session_worktree_paths", lambda: None)
        fetched = _spy_fetch(monkeypatch)

        caplog.clear()  # drop seed-phase records
        with (
            caplog.at_level(logging.DEBUG, logger="cw.worktree"),
            pytest.raises(WorktreeOccupiedError) as excinfo,
        ):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert excinfo.value.path == wt
        assert "unreadable" in excinfo.value.reason
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any(
            r.levelno == logging.DEBUG and "unreadable" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.DEBUG)
        )

    def test_diverged_after_remote_rewrite_left_alone_and_warns(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The worktree pushed commit A, then the remote history was rewritten
        to B. The stale tracking ref still equals HEAD, so occupancy passes;
        the fetch reveals the divergence and nothing is moved."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        new_tip = _force_push_rewrite(origin, tmp_path / "side", _REUSE_BRANCH)
        assert new_tip != old_sha

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.INFO, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any(
            "diverged" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )

    def test_merge_refusal_is_logged_and_leaves_worktree_alone(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``--ff-only`` can still refuse after the occupancy check passed (the
        tree was dirtied in between); the refusal degrades to as-is."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                return subprocess.CompletedProcess(
                    args, 1, "", "error: local changes would be overwritten\n"
                )
            return _run_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr("cw.worktree._run_git", spy)

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.INFO, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any(
            "refused" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )

    def test_wrong_branch_still_raises_before_any_refresh(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The branch-identity guard (a StaleWorktreeError, stricter than
        use-as-is) fires before the refresh: no fetch, worktree untouched."""
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        git_in(wt, "checkout", "-b", "dev/other")
        fetched = _spy_fetch(monkeypatch)

        with pytest.raises(StaleWorktreeError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert fetched == []
        assert git_in(wt, "branch", "--show-current") == "dev/other"

    def test_missing_path_creates_fresh_with_refresh_flag(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        client, _wt, _origin, workspace = _seed_reuse(tmp_path, make_git_repo)

        created = create_worktree(client, "dev/fresh", refresh_on_reuse=True)

        assert created.exists()
        assert git_in(created, "branch", "--show-current") == "dev/fresh"
        assert git_in(created, "rev-parse", "HEAD") == git_in(
            workspace, "rev-parse", "main"
        )

    def test_branch_not_on_origin_is_silent(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The quiet marker is git's English message; pin the locale.
        monkeypatch.setenv("LC_ALL", "C")
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt = create_worktree(client, never_pushed)
        head = git_in(wt, "rev-parse", "HEAD")

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
            result = create_worktree(
                client, never_pushed, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == head
        assert _cw_worktree_records(caplog, logging.WARNING) == []

    def test_no_origin_remote_degrades(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        workspace = make_git_repo("workspace")
        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )
        wt = create_worktree(client, _REUSE_BRANCH)
        head = git_in(wt, "rev-parse", "HEAD")

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.INFO, logger="cw.worktree"):
            result = create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == head
        assert _cw_worktree_records(caplog, logging.WARNING)

    def test_fetch_failure_skips_fast_forward_from_stale_tracking_ref(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed fetch leaves the tracking ref at whatever it was before, so
        a fast-forward "to origin" would really be a move to stale state. It is
        skipped entirely: HEAD untouched, the reason reported, nothing raised."""
        client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        new_sha = push_commit_to_origin(
            origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt"
        )
        # Another fetch (e.g. dispatch's freshness gate) advances the shared
        # tracking ref while the worktree's branch stays behind ...
        git_in(workspace, "fetch", "origin")
        tracking = f"refs/remotes/origin/{_REUSE_BRANCH}"
        assert git_in(workspace, "rev-parse", tracking) == new_sha
        assert old_sha != new_sha
        # ... then origin becomes unreachable, so the fetch inside reuse fails.
        origin.rename(tmp_path / "origin-gone.git")
        fetched = _spy_fetch(monkeypatch)

        result = _refresh_with_debug(client, caplog)

        assert result == wt
        assert fetched == [_REUSE_BRANCH]
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert not (wt / "upstream.txt").exists()
        # The reason is reported: fetch's own WARNING carries rc + git's stderr,
        # and the refresh names the skip.
        assert any(
            "fetch failed" in r.getMessage() and "rc=" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )
        assert any(
            "fast-forward skipped" in m and str(wt) in m for m in _debug_reasons(caplog)
        )

    def test_simulated_fetch_failure_leaves_head_and_reports_reason(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)
        # A stale-but-newer tracking ref is what a real failure would leave.
        git_in(workspace, "fetch", "origin")
        merges: list[tuple[str, ...]] = []

        def failing_fetch(client: ClientConfig, branch_name: str) -> FetchResult:
            return FetchResult(FetchOutcome.FAILED, "rc=128: fatal: simulated")

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                merges.append(args)
            return _run_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr("cw.worktree.fetch_feature_branch", failing_fetch)
        monkeypatch.setattr("cw.worktree._run_git", spy)

        result = _refresh_with_debug(client, caplog)

        assert result == wt
        assert merges == []  # skipped entirely, not merely refused
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert old_sha != new_sha
        # The log line names git's reason, not just that the fetch failed.
        assert any(
            "fast-forward skipped" in m and "fatal: simulated" in m
            for m in _debug_reasons(caplog)
        )

    def test_fetch_ok_but_tracking_ref_absent_is_a_no_op(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fetch can succeed without creating ``refs/remotes/origin/<branch>``
        (a narrow ``remote.origin.fetch`` refspec): no target, nothing to move."""
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        head = git_in(wt, "rev-parse", "HEAD")
        merges: list[tuple[str, ...]] = []

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                merges.append(args)
            return _run_git(*args, cwd=cwd, check=check)

        real_ref_exists = _ref_exists

        def no_tracking_ref(ref: str, git_cwd: Path) -> bool:
            if ref == f"refs/remotes/origin/{_REUSE_BRANCH}":
                return False
            return real_ref_exists(ref, git_cwd)

        monkeypatch.setattr("cw.worktree._run_git", spy)
        monkeypatch.setattr("cw.worktree._ref_exists", no_tracking_ref)
        monkeypatch.setattr(
            "cw.worktree.fetch_feature_branch",
            lambda *_args, **_kw: FetchResult(FetchOutcome.FETCHED),
        )
        monkeypatch.setattr(
            "cw.worktree._reuse_occupancy",
            lambda *_args, **_kw: _Occupancy(
                live=None, branch_mismatch=None, local=None
            ),
        )

        result = _refresh_reused_worktree(
            client,
            _REUSE_BRANCH,
            wt,
            ReuseRefreshReport(),
            ticket_id=None,
            daemon=native_daemon.get_native_daemon_client(),
        )

        assert merges == []
        assert git_in(wt, "rev-parse", "HEAD") == head
        assert result.outcome is RefreshOutcome.NOT_REFRESHED
        assert "origin/" in result.reason

    def test_no_notes_on_a_clean_refresh(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)

        _path, report = _refresh_reporting(client)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert report.notes == []
        assert report.outcome is RefreshOutcome.REFRESHED
        assert report.reason is not None

    @pytest.mark.parametrize(
        ("fetch", "moves", "note_count", "expected"),
        [
            pytest.param(
                FetchResult(FetchOutcome.FETCHED),
                True,
                0,
                RefreshOutcome.REFRESHED,
                id="fetched",
            ),
            pytest.param(
                FetchResult(FetchOutcome.BRANCH_ABSENT, "couldn't find remote ref"),
                False,
                0,
                RefreshOutcome.NOT_REFRESHED,
                id="branch-absent",
            ),
            pytest.param(
                FetchResult(FetchOutcome.FAILED, "rc=128: fatal: no route"),
                False,
                1,
                RefreshOutcome.NOT_REFRESHED,
                id="failed",
            ),
        ],
    )
    def test_fast_forward_only_on_a_fetched_outcome(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        fetch: FetchResult,
        moves: bool,
        note_count: int,
        expected: RefreshOutcome,
    ) -> None:
        """The three fetch outcomes are handled apart (#2213 round 4): only
        ``FETCHED`` fast-forwards; ``BRANCH_ABSENT`` proceeds without a refresh
        and is NOT friction; ``FAILED`` skips the fast-forward and IS reported.

        The tracking ref is already fresh in every case, so an implementation
        that fast-forwarded regardless of the outcome would visibly move HEAD."""
        client, wt, workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)
        git_in(workspace, "fetch", "origin")
        merges: list[tuple[str, ...]] = []

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                merges.append(args)
            return _run_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr("cw.worktree._run_git", spy)
        monkeypatch.setattr("cw.worktree.fetch_feature_branch", lambda _c, _b: fetch)

        _path, report = _refresh_reporting(client)

        assert git_in(wt, "rev-parse", "HEAD") == (new_sha if moves else old_sha)
        assert len(merges) == (1 if moves else 0)
        assert len(report.notes) == note_count
        assert report.outcome is expected
        assert report.reason
        if fetch.outcome is FetchOutcome.FAILED:
            # The friction note says WHY the fetch failed, not only that it did.
            assert fetch.reason is not None
            assert fetch.reason in report.notes[0]
            assert fetch.reason in report.reason

    def test_real_missing_branch_is_branch_absent_not_a_failure(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Against real git: a never-pushed branch is ``BRANCH_ABSENT`` (no
        friction note), where a genuinely failed fetch is ``FAILED``."""
        monkeypatch.setenv("LC_ALL", "C")  # the marker is git's English message
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt = create_worktree(client, never_pushed)
        report = ReuseRefreshReport()

        create_worktree(
            client,
            never_pushed,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )

        fetched = fetch_feature_branch(client, never_pushed)
        assert fetched.outcome is FetchOutcome.BRANCH_ABSENT
        assert report.notes == []
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert wt.exists()

    def test_fetch_failure_is_reported_in_one_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A REAL failed fetch (origin unreachable): one note, naming the
        worktree AND git's own reason, and HEAD untouched. The outcome is
        ``NOT_REFRESHED`` (the tree is the caller's), never a raise."""
        monkeypatch.setenv("LC_ALL", "C")  # the reason text is git's English
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        origin.rename(tmp_path / "origin-gone.git")

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert f"origin/{_REUSE_BRANCH}" in report.notes[0]
        assert "fetch" in report.notes[0]
        assert "does not appear to be a git repository" in report.notes[0]
        assert "\n" not in report.notes[0]
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "does not appear to be a git repository" in report.reason

    def test_diverged_branch_is_reported_in_one_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        _force_push_rewrite(origin, tmp_path / "side", _REUSE_BRANCH)

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert "diverged" in report.notes[0]
        assert f"origin/{_REUSE_BRANCH}" in report.notes[0]
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "diverged" in report.reason

    def test_git_refusing_the_fast_forward_is_reported_in_one_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                return subprocess.CompletedProcess(
                    args, 1, "", "error: local changes would be overwritten\nmore\n"
                )
            return _run_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr("cw.worktree._run_git", spy)

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert "local changes would be overwritten" in report.notes[0]
        assert "\n" not in report.notes[0]
        assert report.outcome is RefreshOutcome.NOT_REFRESHED

    def test_os_error_is_reported_in_one_note(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A missing git binary (OSError) out of the refresh never escapes
        ``create_worktree``, and is reported. Injected at
        ``fetch_feature_branch`` itself: ``_fetch_default_branch`` already
        swallows ``FileNotFoundError`` internally, so a fetch-level ``_run_git``
        fault would never reach the helper's own ``except OSError``."""
        client = ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )
        wt_path = tmp_path / "wt" / "feat-oserror"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            stdout = "feat/oserror\n" if "--show-current" in args else ""
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        def boom(client: ClientConfig, branch_name: str) -> FetchResult:
            msg = "git vanished"
            raise FileNotFoundError(msg)

        def clean(
            client: ClientConfig, branch: str, *, wt_path: Path | None = None
        ) -> None:
            return None

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        monkeypatch.setattr("cw.worktree.unsaved_work_reason", clean)
        monkeypatch.setattr("cw.worktree.fetch_feature_branch", boom)
        report = ReuseRefreshReport()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            result = create_worktree(
                client,
                "feat/oserror",
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                refresh_report=report,
            )

        assert result == wt_path
        assert any(
            "refresh of reused worktree failed" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )
        assert len(report.notes) == 1
        assert str(wt_path) in report.notes[0]
        assert "git vanished" in report.notes[0]
        assert "OS error" in report.notes[0]
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "git vanished" in report.reason

    def test_os_error_after_the_fast_forward_step_started_is_one_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError from the merge itself (not the fetch) is reported too,
        still as exactly one note."""
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        push_commit_to_origin(origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt")

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                msg = "git vanished mid-merge"
                raise FileNotFoundError(msg)
            return _run_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr("cw.worktree._run_git", spy)

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert "git vanished mid-merge" in report.notes[0]

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_live_occupant_raises_and_nothing_is_fetched(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        source: str,
    ) -> None:
        """Round 5: occupied is a refusal a caller CANNOT ignore. It raises
        ``WorktreeOccupiedError`` (no usable path is returned), adds no friction
        note (it is the design working, not a failure), and never fetches."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _occupy(source, workspace, wt)
        fetched = _spy_fetch(monkeypatch)

        error, report = _refresh_occupied(client)

        assert error.path == wt
        assert "live" in error.reason
        assert str(wt) in str(error)
        assert error.reason in str(error)
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert report.notes == []
        assert report.outcome is RefreshOutcome.OCCUPIED_BY_LIVE_SESSION
        assert report.reason == error.reason

    def test_unsaved_work_alone_is_not_occupied_by_a_live_session(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """Unsaved work refuses the refresh but leaves the worktree the
        caller's to use: ``NOT_REFRESHED`` (proceed), never a raise."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        (wt / "scratch.txt").write_text("churn\n", encoding="utf-8")

        path, report = _refresh_reporting(client)

        assert path == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "unsaved work" in report.reason
        assert report.notes == []

    def test_unsaved_work_does_not_mask_a_live_occupant(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """Unsaved work is exactly what a live worker leaves behind. It refuses
        the refresh first, but the live half is always evaluated."""
        client, wt, workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)
        (wt / "scratch.txt").write_text("worker output\n", encoding="utf-8")
        _occupy("roster", workspace, wt)

        error, _report = _refresh_occupied(client)

        assert "live daemon worker" in error.reason

    @pytest.mark.parametrize("kind", ["roster-invalid-json", "state-corrupt"])
    def test_unreadable_source_is_occupied_fail_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        kind: str,
    ) -> None:
        client, _wt, _workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)
        if kind == "roster-invalid-json":
            _seed_roster(raw="{not json")
        else:
            state_file().write_text("{not json", encoding="utf-8")

        error, report = _refresh_occupied(client)

        assert "unreadable" in error.reason
        assert report.outcome is RefreshOutcome.OCCUPIED_BY_LIVE_SESSION

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_occupant_appearing_during_the_fetch_raises(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        source: str,
    ) -> None:
        """The re-check before the merge refuses a live occupant too, so a
        caller is not told "free" by the first gate and then racing the second."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        real_fetch = fetch_feature_branch

        def fetch_then_occupy(client: ClientConfig, branch_name: str) -> FetchResult:
            result = real_fetch(client, branch_name)
            _occupy(source, workspace, wt)
            return result

        monkeypatch.setattr("cw.worktree.fetch_feature_branch", fetch_then_occupy)

        error, report = _refresh_occupied(client)

        assert error.path == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert report.notes == []
        assert report.outcome is RefreshOutcome.OCCUPIED_BY_LIVE_SESSION

    def test_occupied_error_is_a_worktree_error_but_not_a_stale_one(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """It IS a ``WorktreeError`` (so a broad handler still contains it), but
        a caller matching ``StaleWorktreeError`` -- the branch that removes the
        worktree -- must NOT see it."""
        client, wt, workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)
        _occupy("roster", workspace, wt)

        with pytest.raises(WorktreeError) as excinfo:
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert isinstance(excinfo.value, WorktreeOccupiedError)
        assert not isinstance(excinfo.value, StaleWorktreeError)
        assert wt.exists()

    def test_default_reuse_ignores_a_live_occupant(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """``refresh_on_reuse=False`` is unchanged: path resolution only, so it
        neither consults occupancy nor raises (``cw start`` does not opt in)."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _occupy("roster", workspace, wt)

        result = create_worktree(client, _REUSE_BRANCH, allow_dirty_reuse=True)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha

    def test_not_refreshed_dirty_returns_a_path(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        (wt / "tracked.txt").write_text("local edit\n", encoding="utf-8")

        path, report = _refresh_reporting(client)

        assert path == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert report.outcome is RefreshOutcome.NOT_REFRESHED

    def test_not_refreshed_branch_absent_returns_a_path(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LC_ALL", "C")
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt = create_worktree(client, never_pushed)
        report = ReuseRefreshReport()

        path = create_worktree(
            client,
            never_pushed,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )

        assert path == wt
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "origin/dev/never-pushed" in report.reason
        assert report.notes == []

    def test_refreshed_reports_the_move(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)

        path, report = _refresh_reporting(client)

        assert path == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert report.outcome is RefreshOutcome.REFRESHED
        assert report.reason is not None
        assert old_sha[:12] in report.reason
        assert new_sha[:12] in report.reason

    def test_equal_to_origin_is_not_refreshed_without_a_note(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        head = git_in(wt, "rev-parse", "HEAD")

        path, report = _refresh_reporting(client)

        assert path == wt
        assert git_in(wt, "rev-parse", "HEAD") == head
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert report.reason is not None
        assert "up to date" in report.reason
        assert report.notes == []

    def test_unknown_fetch_outcome_is_never_read_as_fetched(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round 5 item 3: an outcome the refresh does not know is a bug, not
        "fetched". The tracking ref is fresh here, so a fall-through to "fetched"
        would visibly fast-forward HEAD; an exhaustive ``match`` hits
        ``assert_never`` (``AssertionError``) instead and HEAD stays put. The
        stand-in is a plain string: no real enum member is added."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        git_in(workspace, "fetch", "origin")
        monkeypatch.setattr(
            "cw.worktree.fetch_feature_branch",
            lambda _c, _b: FetchResult("not-a-real-outcome"),
        )

        with pytest.raises(AssertionError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert git_in(wt, "rev-parse", "HEAD") == old_sha

    def test_unknown_refresh_outcome_never_yields_a_path(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``create_worktree`` dispatches on the refresh outcome exhaustively: an
        outcome it does not know must not be read as "proceed with this tree"."""
        client, _wt, _workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)
        monkeypatch.setattr(
            "cw.worktree._refresh_reused_worktree",
            lambda *_a, **_kw: RefreshResult("not-a-real-outcome", "stand-in"),
        )

        with pytest.raises(AssertionError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

    def test_unknown_ff_relation_never_fast_forwards(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The HEAD-vs-origin classification is matched exhaustively too: a
        relation the refresh does not know is a bug, never "behind"."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        monkeypatch.setattr("cw.worktree._ff_relation", lambda *_a, **_kw: "sideways")

        with pytest.raises(AssertionError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert git_in(wt, "rev-parse", "HEAD") == old_sha

    def test_is_main_behind_origin_rejects_an_unknown_fetch_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = ClientConfig(name="test", workspace_path=tmp_path)
        monkeypatch.setattr(
            "cw.worktree._fetch_default_branch",
            lambda *_a, **_kw: FetchResult("not-a-real-outcome"),
        )

        with pytest.raises(AssertionError):
            is_main_behind_origin(client)

    def test_refresh_report_is_optional(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        """A caller with no surface (the dispatch claim) passes none."""
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)

        create_worktree(
            client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
        )

        assert git_in(wt, "rev-parse", "HEAD") == new_sha


def _seed_roster(cwd: Path | None = None, *, raw: str | None = None) -> Path:
    """Write the (tmp-isolated) daemon roster.

    *cwd* records one live worker homed there; *raw* writes arbitrary bytes
    instead. ``tests/conftest.py`` points ``native_daemon._ROSTER_PATH`` at a
    tmp path, so this never touches the developer's real roster.
    """
    roster = native_daemon._ROSTER_PATH
    roster.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        raw
        if raw is not None
        else json.dumps({"workers": {"aaaa1111": {"pid": 1, "cwd": str(cwd)}}})
    )
    roster.write_text(payload, encoding="utf-8")
    return roster


def _make_symlink_loop(base: Path) -> Path:
    """Create two symlinks pointing at each other and return one of them."""
    first, second = base / "loop-a", base / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)
    return first


def _unnormalizable_path(
    kind: str, base: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Return a path under *base* whose ``stat()`` fails with the given errno."""
    if kind == "eloop":
        return _make_symlink_loop(base)
    if kind == "enotdir":
        blocker = base / "a-file"
        blocker.write_text("not a directory\n", encoding="utf-8")
        return blocker / "child"
    if kind == "enametoolong":
        return base / ("x" * 300)
    denied = base / "denied"
    denied.mkdir()
    real_stat = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == denied:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    return denied


def _occupy(source: str, workspace: Path, path: Path) -> None:
    """Make *path* look occupied via cw state or via the daemon roster."""
    if source == "state":
        _seed_session(workspace, path, SessionStatus.ACTIVE)
    else:
        _seed_roster(path)


def _seed_behind(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> tuple[ClientConfig, Path, Path, str, str]:
    """``_seed_reuse`` plus an upstream commit.

    Returns ``(client, wt, workspace, old_sha, new_sha)``.
    """
    client, wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
    old_sha = git_in(wt, "rev-parse", "HEAD")
    new_sha = push_commit_to_origin(
        origin, _REUSE_BRANCH, tmp_path / "side", "upstream.txt"
    )
    return client, wt, workspace, old_sha, new_sha


def _refresh_with_debug(client: ClientConfig, caplog: pytest.LogCaptureFixture) -> Path:
    caplog.clear()  # drop seed-phase records
    with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
        return create_worktree(
            client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
        )


def _refresh_reporting(client: ClientConfig) -> tuple[Path, ReuseRefreshReport]:
    """Reuse ``_REUSE_BRANCH`` with the refresh on, returning what it reported."""
    report = ReuseRefreshReport()
    path = create_worktree(
        client,
        _REUSE_BRANCH,
        allow_dirty_reuse=True,
        refresh_on_reuse=True,
        refresh_report=report,
    )
    return path, report


def _refresh_occupied(
    client: ClientConfig,
) -> tuple[WorktreeOccupiedError, ReuseRefreshReport]:
    """Reuse ``_REUSE_BRANCH`` with the refresh on, expecting the occupancy refusal.

    Returns the raised error and the report the caller passed in (which the
    refresh filled in before raising).
    """
    report = ReuseRefreshReport()
    with pytest.raises(WorktreeOccupiedError) as excinfo:
        create_worktree(
            client,
            _REUSE_BRANCH,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )
    return excinfo.value, report


def _refresh_occupied_with_debug(
    client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> WorktreeOccupiedError:
    """Like :func:`_refresh_occupied`, with DEBUG capture for the reason log."""
    caplog.clear()  # drop seed-phase records
    with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
        error, _report = _refresh_occupied(client)
    return error


def _refresh_stale_with_debug(
    client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> StaleWorktreeError:
    """Reuse ``_REUSE_BRANCH`` with the refresh on, expecting the pre-merge
    re-check's branch-mismatch refusal (:exc:`StaleWorktreeError`, not the
    occupancy path -- see :func:`_refresh_occupied_with_debug`)."""
    caplog.clear()  # drop seed-phase records
    with (
        caplog.at_level(logging.DEBUG, logger="cw.worktree"),
        pytest.raises(StaleWorktreeError) as excinfo,
    ):
        create_worktree(
            client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
        )
    return excinfo.value


def _debug_reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in _cw_worktree_records(caplog, logging.DEBUG)
        if r.levelno == logging.DEBUG
    ]


class TestReuseOccupancyRosterAndPaths:
    """#2213 round 2: the occupancy predicate consults the daemon roster as well
    as cw state, compares *resolved* paths, and fails closed on anything it
    cannot read."""

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_session_path_recorded_via_symlink_is_occupied(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        source: str,
    ) -> None:
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        alias = tmp_path / "wt-alias"
        alias.symlink_to(wt, target_is_directory=True)
        assert alias != wt
        _occupy(source, workspace, alias)

        error = _refresh_occupied_with_debug(client, caplog)

        assert error.path == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("live" in m and str(wt) in m for m in _debug_reasons(caplog))

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_worktree_reached_via_symlink_is_occupied(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        source: str,
    ) -> None:
        """The reverse direction: the session/worker recorded the real path,
        but the caller reaches the worktree through a symlinked worktree base."""
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        alias_base = tmp_path / "wt-alias"
        alias_base.symlink_to(tmp_path / "wt", target_is_directory=True)
        aliased_client = client.model_copy(update={"worktree_base": alias_base})
        _occupy(source, workspace, wt)

        error = _refresh_occupied_with_debug(aliased_client, caplog)

        assert error.path != wt
        assert error.path.resolve() == wt.resolve()
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("live" in m for m in _debug_reasons(caplog))

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_symlink_to_a_different_worktree_is_not_occupied(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        source: str,
    ) -> None:
        """Control for the symlink tests: normalization must not over-match."""
        client, wt, workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)
        other = tmp_path / "other-dir"
        other.mkdir()
        alias = tmp_path / "other-alias"
        alias.symlink_to(other, target_is_directory=True)
        _occupy(source, workspace, alias)

        _refresh_with_debug(client, caplog)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    def test_injected_daemon_worker_blocks_the_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """#2213 round 7: occupancy consults the CALLER'S daemon, not the real
        one. A worker seeded directly on an injected ``FakeNativeDaemonClient``
        -- never written to the real (tmp-isolated) roster file -- must still
        block the fast-forward, and the real roster must stay untouched."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        assert not native_daemon._ROSTER_PATH.exists()
        fake = native_daemon.FakeNativeDaemonClient()
        fake.seed_live_worker(wt)

        with pytest.raises(WorktreeOccupiedError) as excinfo:
            create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                native_daemon=fake,
            )

        assert excinfo.value.path == wt
        assert "live daemon worker" in excinfo.value.reason
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        # The real roster was never written -- the fake, not it, was consulted.
        assert not native_daemon._ROSTER_PATH.exists()

    def test_injected_daemon_unreadable_roster_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """#2213 round 7: a fake roster reporting unreadable (``None``) fails
        closed exactly like the real one, even though cw state and the real
        (tmp-isolated) roster file are both clean."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        fake = native_daemon.FakeNativeDaemonClient()
        fake.roster_unreadable = True

        with pytest.raises(WorktreeOccupiedError) as excinfo:
            create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                native_daemon=fake,
            )

        assert excinfo.value.path == wt
        assert "roster unreadable" in excinfo.value.reason
        assert git_in(wt, "rev-parse", "HEAD") == old_sha

    def test_live_daemon_worker_homed_on_worktree_blocks_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _seed_roster(wt)  # live in the roster, absent from cw state
        fetched = _spy_fetch(monkeypatch)

        error = _refresh_occupied_with_debug(client, caplog)

        assert error.path == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            "live daemon worker" in m and str(wt) in m for m in _debug_reasons(caplog)
        )

    def test_daemon_worker_homed_elsewhere_does_not_block(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)
        _seed_roster(tmp_path / "elsewhere")

        _refresh_with_debug(client, caplog)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    def test_absent_roster_means_no_live_worker_and_fast_forwards(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)
        assert not native_daemon._ROSTER_PATH.exists()
        # The isolation guard: the patched path is not the developer's real one.
        assert (
            Path.home() / ".claude" / "daemon" / "roster.json"
        ) != native_daemon._ROSTER_PATH

        _refresh_with_debug(client, caplog)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    @pytest.mark.parametrize(
        "kind", ["invalid-json", "invalid-utf8", "directory", "entry-no-cwd"]
    )
    def test_unreadable_roster_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        if kind == "invalid-json":
            _seed_roster(raw="{not json")
        elif kind == "invalid-utf8":
            _seed_roster().write_bytes(
                b'{"workers": {"aaaa1111": {"cwd": "\xff\xfe"}}}'
            )
        elif kind == "directory":
            native_daemon._ROSTER_PATH.mkdir(parents=True)  # OSError, not ENOENT
        else:
            _seed_roster(raw=json.dumps({"workers": {"aaaa1111": {"pid": 1}}}))
        fetched = _spy_fetch(monkeypatch)

        error = _refresh_occupied_with_debug(client, caplog)

        assert error.path == wt
        assert "roster unreadable" in error.reason
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("roster unreadable" in m for m in _debug_reasons(caplog))

    def test_corrupt_state_file_fails_closed(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Real corrupt sessions.json through the real reader: occupied."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        state_file().write_text("{not json", encoding="utf-8")

        error = _refresh_occupied_with_debug(client, caplog)

        assert "session state unreadable" in error.reason
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("session state unreadable" in m for m in _debug_reasons(caplog))

    @pytest.mark.parametrize(
        ("change", "expected_reason"),
        [
            pytest.param("session", "live session", id="session-appeared"),
            pytest.param("roster", "live daemon worker", id="worker-appeared"),
            pytest.param("dirty", "unsaved work", id="tree-dirtied"),
            pytest.param("branch", "dev/other", id="branch-switched"),
        ],
    )
    def test_occupancy_change_after_fetch_aborts_before_the_merge(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        change: str,
        expected_reason: str,
    ) -> None:
        """The occupancy gate runs before the (slow) fetch; the whole predicate
        runs again immediately before ``merge --ff-only``. A session or worker
        appearing raises the occupancy error; the tree being dirtied aborts to
        use-as-is; a branch switch raises ``StaleWorktreeError`` (the same
        refusal ``create_worktree``'s own identity guard gives up front). HEAD
        stays untouched in every case."""
        client, wt, workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)
        assert old_sha != new_sha
        real_fetch = fetch_feature_branch

        def fetch_then_change(client: ClientConfig, branch_name: str) -> FetchResult:
            fetched_ok = real_fetch(client, branch_name)
            if change == "session":
                _seed_session(workspace, wt, SessionStatus.ACTIVE)
            elif change == "roster":
                _seed_roster(wt)
            elif change == "dirty":
                (wt / "scratch.txt").write_text("late edit\n", encoding="utf-8")
            else:
                git_in(wt, "checkout", "-b", "dev/other")
            return fetched_ok

        merges: list[tuple[str, ...]] = []

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                merges.append(args)
            return _run_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr("cw.worktree.fetch_feature_branch", fetch_then_change)
        monkeypatch.setattr("cw.worktree._run_git", spy)

        # A live occupant (session/roster) refuses with the occupancy error; a
        # branch switch refuses with the stale-worktree error; a merely dirtied
        # tree is the caller's to use.
        if change in {"session", "roster"}:
            error = _refresh_occupied_with_debug(client, caplog)
            assert error.path == wt
            assert expected_reason in error.reason
        elif change == "branch":
            stale = _refresh_stale_with_debug(client, caplog)
            assert expected_reason in str(stale)
        else:
            result = _refresh_with_debug(client, caplog)
            assert result == wt
        assert merges == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _cw_worktree_records(caplog, logging.WARNING) == []
        assert any(
            "occupied after the fetch" in m and str(wt) in m and expected_reason in m
            for m in _debug_reasons(caplog)
        )

    def test_unchanged_occupancy_still_fast_forwards_after_recheck(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Control: the re-check must not veto a genuinely free worktree."""
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)

        _refresh_with_debug(client, caplog)

        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    def test_predicate_reports_unexpected_branch(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        """The expected-branch check lives in the one predicate, so the
        pre-mutation re-check covers it (``create_worktree``'s own guard raises
        earlier, but a checkout can land in between)."""
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        daemon = native_daemon.get_native_daemon_client()
        assert _reuse_occupancy(client, _REUSE_BRANCH, wt, daemon=daemon).reason is None

        git_in(wt, "checkout", "-b", "dev/other")

        reason = _reuse_occupancy(client, _REUSE_BRANCH, wt, daemon=daemon).reason
        assert reason is not None
        assert "dev/other" in reason

    def test_predicate_reports_detached_head_as_unexpected_branch(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        git_in(wt, "checkout", "--detach")
        daemon = native_daemon.get_native_daemon_client()

        reason = _reuse_occupancy(client, _REUSE_BRANCH, wt, daemon=daemon).reason

        assert reason is not None
        assert "detached" in reason

    @pytest.mark.parametrize("kind", ["eloop", "enotdir", "eacces", "enametoolong"])
    @pytest.mark.parametrize("side", ["target", "session", "worker"])
    def test_path_that_cannot_be_normalized_fails_closed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
        side: str,
    ) -> None:
        """Round 4: ANY ``OSError`` while normalizing a path reads as occupied,
        not just ``ELOOP``. Before, ``EACCES``, ``ENOTDIR``, ``ENAMETOOLONG``
        and friends were swallowed by the loop probe and read as "not
        occupied", which permitted the mutation. Covers the worktree being
        checked and both kinds of recorded home (session state, daemon roster).
        Real filesystem states, except ``EACCES``: ``chmod 000`` cannot deny a
        root test runner, so ``Path.stat`` is made to deny that one path."""
        bad = _unnormalizable_path(kind, tmp_path, monkeypatch)
        good = tmp_path / "wt"
        good.mkdir()
        if side == "session":
            monkeypatch.setattr(
                "cw.worktree.live_session_worktree_paths", lambda: frozenset({bad})
            )
        elif side == "worker":
            _seed_roster(bad)

        reason = live_home_reason(
            bad if side == "target" else good,
            daemon=native_daemon.get_native_daemon_client(),
        )

        assert reason is not None
        assert "cannot be resolved" in reason

    def test_normalize_path_tolerates_only_a_missing_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone"
        assert _normalize_path(missing) == missing.resolve()
        with pytest.raises(OSError, match=r"\[Errno") as excinfo:
            _normalize_path(_make_symlink_loop(tmp_path))
        assert excinfo.value.errno == errno.ELOOP

    def test_permission_denied_session_path_refuses_the_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        denied = _unnormalizable_path("eacces", tmp_path, monkeypatch)
        _seed_session(workspace, denied, SessionStatus.ACTIVE)
        fetched = _spy_fetch(monkeypatch)

        error, report = _refresh_occupied(client)

        assert error.path == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert "cannot be resolved" in error.reason
        assert report.outcome is RefreshOutcome.OCCUPIED_BY_LIVE_SESSION

    def test_missing_recorded_path_is_not_a_loop(self, tmp_path: Path) -> None:
        """Control: a recorded path whose directory is simply gone is ENOENT,
        not ELOOP, and must not read as indeterminate -- a stale entry for a
        deleted worktree would otherwise veto every refresh."""
        _seed_roster(tmp_path / "deleted-worktree")

        assert (
            live_home_reason(
                tmp_path / "wt", daemon=native_daemon.get_native_daemon_client()
            )
            is None
        )

    def test_symlink_loop_session_refuses_the_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _seed_session(workspace, _make_symlink_loop(tmp_path), SessionStatus.ACTIVE)
        fetched = _spy_fetch(monkeypatch)

        error = _refresh_occupied_with_debug(client, caplog)

        assert error.path == wt
        assert fetched == []
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert any("cannot be resolved" in m for m in _debug_reasons(caplog))


_FULL_SHA_CHARS = 40


def _ff_events() -> list[OrchestratorEvent]:
    """Every ``worktree.fast_forwarded`` event in the (test-isolated) inbox."""
    return read_events(event_types=[OrchestratorEventType.WORKTREE_FAST_FORWARDED])


def _refresh_with_ticket(client: ClientConfig, ticket_id: str | None = "2213") -> Path:
    return create_worktree(
        client,
        _REUSE_BRANCH,
        allow_dirty_reuse=True,
        refresh_on_reuse=True,
        ticket_id=ticket_id,
    )


class TestFastForwardAuditEvent:
    """#2213 round 6: a fast-forward that actually moves HEAD leaves exactly one
    ``worktree.fast_forwarded`` audit event. Every path that moves nothing
    (already current, ahead, diverged, refused, occupied, not refreshed) leaves
    none: a record per turn would be noise."""

    def test_real_fast_forward_emits_exactly_one_event_with_both_shas(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _workspace, old_sha, new_sha = _seed_behind(tmp_path, make_git_repo)
        assert len(old_sha) == len(new_sha) == _FULL_SHA_CHARS

        assert _refresh_with_ticket(client) == wt

        events = _ff_events()
        assert len(events) == 1
        event = events[0]
        assert event.type is OrchestratorEventType.WORKTREE_FAST_FORWARDED
        assert event.correlation_id == "2213"
        assert event.payload == {
            "client": "test",
            "ticket_id": "2213",
            "branch": _REUSE_BRANCH,
            "worktree_path": str(wt),
            "old_sha": old_sha,
            "new_sha": new_sha,
        }
        assert git_in(wt, "rev-parse", "HEAD") == new_sha

    def test_unknown_ticket_is_a_null_ticket_and_no_correlation_id(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, _wt, _workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)

        _refresh_with_ticket(client, ticket_id=None)

        (event,) = _ff_events()
        assert event.payload["ticket_id"] is None
        assert event.correlation_id is None

    def test_default_reuse_moves_nothing_and_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, _wt, _workspace, _old, _new = _seed_behind(tmp_path, make_git_repo)

        create_worktree(client, _REUSE_BRANCH, allow_dirty_reuse=True)

        assert _ff_events() == []

    def test_already_current_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)

        _refresh_with_ticket(client)

        assert _ff_events() == []

    def test_ahead_of_origin_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        git_in(wt, "commit", "--allow-empty", "-m", "unpushed local work")
        local_sha = git_in(wt, "rev-parse", "HEAD")

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == local_sha
        assert _ff_events() == []

    def test_diverged_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        _force_push_rewrite(origin, tmp_path / "side", _REUSE_BRANCH)

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_dirty_overlapping_worktree_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        old_sha = git_in(wt, "rev-parse", "HEAD")
        (wt / "tracked.txt").write_text("local edit\n", encoding="utf-8")
        push_commit_to_origin(
            origin,
            _REUSE_BRANCH,
            tmp_path / "side",
            "tracked.txt",
            content="upstream edit\n",
        )

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_git_refusing_the_fast_forward_emits_nothing(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                return subprocess.CompletedProcess(
                    args, 1, "", "error: local changes would be overwritten\n"
                )
            return _run_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr("cw.worktree._run_git", spy)

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    @pytest.mark.parametrize("source", ["state", "roster"])
    def test_occupied_refusal_emits_nothing(
        self, tmp_path: Path, make_git_repo: Callable[..., Path], source: str
    ) -> None:
        client, wt, workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        _occupy(source, workspace, wt)

        with pytest.raises(WorktreeOccupiedError):
            _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_failed_fetch_emits_nothing(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        monkeypatch.setattr(
            "cw.worktree.fetch_feature_branch",
            lambda *_a, **_kw: FetchResult(FetchOutcome.FAILED, "rc=128: offline"),
        )

        _refresh_with_ticket(client)

        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_merge_that_moves_nothing_emits_nothing(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A merge that succeeds ("Already up to date") but leaves HEAD where it
        was is not a fast-forward that happened: REFRESHED, but no event."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)

        def spy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge":
                return subprocess.CompletedProcess(args, 0, "Already up to date.\n", "")
            return _run_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr("cw.worktree._run_git", spy)
        report = ReuseRefreshReport()

        create_worktree(
            client,
            _REUSE_BRANCH,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
            ticket_id="2213",
        )

        assert report.outcome is RefreshOutcome.REFRESHED
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert _ff_events() == []

    def test_audit_write_failure_does_not_undo_the_fast_forward(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed audit write (``OSError``) is logged at WARNING and never
        turns a completed fast-forward into NOT_REFRESHED, a note or a raise."""
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)

        def boom(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.worktree.record_event", boom)
        report = ReuseRefreshReport()

        caplog.clear()  # drop seed-phase records
        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            result = create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                refresh_report=report,
                ticket_id="2213",
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert report.outcome is RefreshOutcome.REFRESHED
        assert report.notes == []
        assert any(
            "audit" in r.getMessage() and "disk full" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )
        assert _ff_events() == []


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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        with pytest.raises(WorktreeError):
            remove_worktree(client, "feat/dirty", force=False)


class TestIsMainBehindOrigin:
    """Tests for is_main_behind_origin."""

    # ------------------------------------------------------------------
    # Option A: real bare-repo tests
    # ------------------------------------------------------------------

    # Not tests.conftest.git_in: optional cwd and no -C, so bare
    # "git init"/"git clone" callers pass no repo at all.
    @staticmethod
    def _run_bare_git(*args: str, cwd: Path | None = None) -> str:
        """Run a git command stripped of GIT_* env vars; return stdout."""
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(cwd) if cwd else None,
            env=clean_env,
        )
        return result.stdout.strip()

    def _make_bare_origin_and_clone(
        self, tmp_path: Path
    ) -> tuple[Path, Path, ClientConfig]:
        """Create a bare origin repo, clone it, return (bare, clone, client)."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        self._run_bare_git("init", "--bare", "-b", "main", str(bare))

        clone = tmp_path / "clone"
        self._run_bare_git("clone", str(bare), str(clone))
        self._run_bare_git("config", "user.email", "test@example.com", cwd=clone)
        self._run_bare_git("config", "user.name", "cw test", cwd=clone)
        # Create an initial commit so main exists
        (clone / "README.md").write_text("init\n")
        self._run_bare_git("add", "README.md", cwd=clone)
        self._run_bare_git("commit", "-m", "initial", cwd=clone)
        self._run_bare_git("push", "origin", "main", cwd=clone)

        client = ClientConfig(
            name="test-client",
            workspace_path=clone,
            default_branch="main",
        )
        return bare, clone, client

    def test_fresh_main_returns_false(self, tmp_path: Path) -> None:
        """When local main == origin/main, returns (False, sha, sha, 0)."""
        _bare, _clone, client = self._make_bare_origin_and_clone(tmp_path)
        stale, local_sha, origin_sha, behind = is_main_behind_origin(client)
        assert stale is False
        assert local_sha == origin_sha
        assert behind == 0
        assert len(local_sha) == 40  # full SHA

    def test_stale_main_returns_true_with_counts(self, tmp_path: Path) -> None:
        """When origin has a new commit local doesn't have, returns (True, ...)."""
        bare, _clone, client = self._make_bare_origin_and_clone(tmp_path)

        # Create a second clone to push a new commit to the bare origin
        clone2 = tmp_path / "clone2"
        self._run_bare_git("clone", str(bare), str(clone2))
        self._run_bare_git("config", "user.email", "test@example.com", cwd=clone2)
        self._run_bare_git("config", "user.name", "cw test", cwd=clone2)
        (clone2 / "extra.txt").write_text("extra\n")
        self._run_bare_git("add", "extra.txt", cwd=clone2)
        self._run_bare_git("commit", "-m", "second commit", cwd=clone2)
        self._run_bare_git("push", "origin", "main", cwd=clone2)

        # Now the original clone's local main is behind origin/main
        stale, local_sha, origin_sha, behind = is_main_behind_origin(client)
        assert stale is True
        assert local_sha != origin_sha
        assert behind == 1

    # ------------------------------------------------------------------
    # Option B: patched _run_git tests
    # ------------------------------------------------------------------

    def test_no_remote_configured_returns_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fetch rc=128 (no remote) → (False, "", "", 0) + WARNING."""
        ws = tmp_path / "ws"
        ws.mkdir()  # directory must exist so the missing-dir guard doesn't fire first
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            result = MagicMock()
            result.returncode = 128
            result.stdout = ""
            result.stderr = "fatal: 'origin' does not appear to be a git repository"
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            stale, local_sha, origin_sha, behind = is_main_behind_origin(client)

        assert stale is False
        assert local_sha == ""
        assert origin_sha == ""
        assert behind == 0
        assert any("freshness_check_skip" in r.message for r in caplog.records)

    def test_fetch_failure_raises_worktreeerror_returns_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """WorktreeError on fetch → (False, "", "", 0) + WARNING."""
        ws = tmp_path / "ws"
        ws.mkdir()  # directory must exist so the missing-dir guard doesn't fire first
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        call_count = 0

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "fetch" in args:
                msg = "fetch failed"
                raise WorktreeError(msg)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "abc123\n"
            result.stderr = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            stale, local_sha, origin_sha, behind = is_main_behind_origin(client)

        assert call_count == 1  # fetch raised immediately; no further git calls
        assert stale is False
        assert local_sha == ""
        assert origin_sha == ""
        assert behind == 0
        assert any("fetch failed" in r.message for r in caplog.records)

    def test_rev_parse_failure_returns_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """WorktreeError on rev-parse → (False, "", "", 0) + WARNING."""
        ws = tmp_path / "ws"
        ws.mkdir()  # directory must exist so the missing-dir guard doesn't fire first
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            if "fetch" in args:
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                return result
            if "rev-parse" in args:
                msg = "rev-parse failed"
                raise WorktreeError(msg)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "0\n"
            result.stderr = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            stale, local_sha, origin_sha, behind = is_main_behind_origin(client)

        assert stale is False
        assert local_sha == ""
        assert origin_sha == ""
        assert behind == 0
        assert any("rev-parse/rev-list failed" in r.message for r in caplog.records)

    def test_legacy_client_uses_workspace_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All _run_git calls use _git_dir(client) as cwd."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        captured_cwds: list[object] = []
        call_index = 0

        def mock_run(
            *args: str,
            cwd: object,
            check: bool = True,
        ) -> MagicMock:
            nonlocal call_index
            captured_cwds.append(cwd)
            result = MagicMock()
            result.returncode = 0
            if "fetch" in args:
                result.stdout = ""
                result.stderr = ""
            elif "rev-list" in args:
                result.stdout = "0\n"
                result.stderr = ""
            else:
                # rev-parse calls
                result.stdout = "deadbeef" * 5 + "\n"
                result.stderr = ""
            call_index += 1
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        is_main_behind_origin(client)

        expected_cwd = _git_dir(client)
        for cwd in captured_cwds:
            assert cwd == expected_cwd


class TestGetHeadBranch:
    """Tests for get_head_branch."""

    def test_returns_branch_name(
        self,
        sample_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """symbolic-ref succeeds → returns stripped branch name."""
        from cw.worktree import get_head_branch

        monkeypatch.setattr(
            "cw.worktree._run_git",
            lambda *_a, **_kw: type("R", (), {"returncode": 0, "stdout": "main\n"})(),
        )
        assert get_head_branch(sample_client) == "main"

    def test_returns_none_on_detached_head(
        self,
        sample_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """symbolic-ref exits non-zero (detached HEAD) → returns None."""
        from cw.worktree import get_head_branch

        monkeypatch.setattr(
            "cw.worktree._run_git",
            lambda *_a, **_kw: type("R", (), {"returncode": 128, "stdout": ""})(),
        )
        assert get_head_branch(sample_client) is None

    def test_returns_none_on_empty_stdout(
        self,
        sample_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """symbolic-ref exits 0 but stdout is empty → returns None."""
        from cw.worktree import get_head_branch

        monkeypatch.setattr(
            "cw.worktree._run_git",
            lambda *_a, **_kw: type("R", (), {"returncode": 0, "stdout": ""})(),
        )
        assert get_head_branch(sample_client) is None


class TestFastForwardMain:
    """Tests for fast_forward_main."""

    @staticmethod
    def _clean_on_branch_mock(
        default_branch: str,
        old_sha: str,
        new_sha: str | None = None,
    ) -> object:
        """Return a _run_git mock for a clean, on-branch checkout.

        Handles symbolic-ref, status --porcelain, rev-parse (before/after),
        and pull in the correct order.
        """
        _pull_called = [False]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock()
            result.stderr = ""
            if "symbolic-ref" in args:
                result.stdout = default_branch + "\n"
            elif "status" in args and "--porcelain" in args:
                result.stdout = ""  # clean
            elif "pull" in args:
                _pull_called[0] = True
                result.stdout = ""
            elif "rev-parse" in args:
                # first rev-parse = before, second = after
                result.stdout = (
                    old_sha if not _pull_called[0] else (new_sha or old_sha)
                ) + "\n"
            else:
                result.stdout = ""
            return result

        return mock_run

    def test_already_up_to_date_returns_same_sha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When already current, before_sha == after_sha."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        sha = "abc123def456abc123def456abc123def456abc1"

        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._clean_on_branch_mock("main", sha),
        )

        before, after = fast_forward_main(client)
        assert before == sha
        assert after == sha

    def test_updated_returns_different_shas(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a fast-forward, before_sha != after_sha."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        old_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        new_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._clean_on_branch_mock("main", old_sha, new_sha),
        )

        before, after = fast_forward_main(client)
        assert before == old_sha
        assert after == new_sha

    def test_pull_failure_raises_worktreeerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WorktreeError raised on pull failure (non-FF or network error)."""
        from cw.exceptions import WorktreeError as _WorktreeError

        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        old_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock()
            result.stderr = ""
            if "symbolic-ref" in args:
                result.stdout = "main\n"
            elif "status" in args and "--porcelain" in args:
                result.stdout = ""  # clean
            elif "rev-parse" in args:
                result.stdout = old_sha + "\n"
            elif "pull" in args:
                msg = "would clobber existing tag"
                raise _WorktreeError(msg)
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with pytest.raises(WorktreeError, match="would clobber"):
            fast_forward_main(client)

    def test_fast_forward_main_raises_missing_workspace_error_when_dir_absent(
        self, tmp_path: Path
    ) -> None:
        """fast_forward_main raises MissingWorkspaceError when git_dir does not exist.

        The guard fires before any git ops, so no _run_git mock is needed.
        """
        from cw.exceptions import MissingWorkspaceError

        client = ClientConfig(
            name="absent-client",
            workspace_path=tmp_path / "nonexistent-workspace",
            default_branch="main",
        )

        with pytest.raises(MissingWorkspaceError, match="absent-client"):
            fast_forward_main(client)

    def test_off_branch_skips_pull_and_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ff on wrong branch skips pull and raises WorktreeError.

        No mutation must occur: pull must NOT be called (#428).
        """
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        pull_called = [False]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            if "pull" in args:
                pull_called[0] = True
            result = MagicMock()
            result.stderr = ""
            if "symbolic-ref" in args:
                result.stdout = "feature/topic\n"  # wrong branch
            elif "status" in args and "--porcelain" in args:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with pytest.raises(WorktreeError, match="main"):
            fast_forward_main(client)

        assert not pull_called[0], "pull must NOT be called when off-branch"

    def test_dirty_checkout_skips_pull_and_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ff on a dirty main checkout skips pull and raises WorktreeError.

        No mutation must occur: pull must NOT be called (#428).
        """
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        pull_called = [False]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            if "pull" in args:
                pull_called[0] = True
            result = MagicMock()
            result.stderr = ""
            if "symbolic-ref" in args:
                result.stdout = "main\n"  # correct branch
            elif "status" in args and "--porcelain" in args:
                result.stdout = " M modified_file.py\n"  # dirty
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with pytest.raises(WorktreeError, match="dirty"):
            fast_forward_main(client)

        assert not pull_called[0], "pull must NOT be called when checkout is dirty"

    def test_clean_on_branch_fast_forwards(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clean, on-branch checkout fast-forwards as before (#428 regression guard)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        old_sha = "cccccccccccccccccccccccccccccccccccccccc"
        new_sha = "dddddddddddddddddddddddddddddddddddddddd"
        pull_called = [False]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock()
            result.stderr = ""
            if "symbolic-ref" in args:
                result.stdout = "main\n"
            elif "status" in args and "--porcelain" in args:
                result.stdout = ""  # clean
            elif "pull" in args:
                pull_called[0] = True
                result.stdout = ""
            elif "rev-parse" in args:
                result.stdout = (old_sha if not pull_called[0] else new_sha) + "\n"
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        before, after = fast_forward_main(client)
        assert before == old_sha
        assert after == new_sha
        assert pull_called[0], "pull MUST be called for clean on-branch checkout"

    def test_untracked_only_with_ignore_untracked_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ignore_untracked=True: untracked-only status does not block ff."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        old_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        new_sha = "ffffffffffffffffffffffffffffffffffffffff"
        pull_called = [False]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock()
            result.stderr = ""
            if "symbolic-ref" in args:
                result.stdout = "main\n"
            elif "status" in args and "--porcelain" in args:
                result.stdout = "?? artifact.lock\n"  # untracked only
            elif "pull" in args:
                pull_called[0] = True
                result.stdout = ""
            elif "rev-parse" in args:
                result.stdout = (old_sha if not pull_called[0] else new_sha) + "\n"
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        before, after = fast_forward_main(client, ignore_untracked=True)
        assert before == old_sha
        assert after == new_sha
        assert pull_called[0], "pull MUST be called when only untracked files present"

    def test_untracked_only_without_ignore_untracked_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without ignore_untracked, untracked files still block ff (default=False)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        pull_called = [False]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            if "pull" in args:
                pull_called[0] = True
            result = MagicMock()
            result.stderr = ""
            if "symbolic-ref" in args:
                result.stdout = "main\n"
            elif "status" in args and "--porcelain" in args:
                result.stdout = "?? artifact.lock\n"  # untracked only
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with pytest.raises(WorktreeError, match="dirty"):
            fast_forward_main(client)

        assert not pull_called[0], "pull must NOT be called when ignore_untracked=False"

    def test_mixed_dirty_with_ignore_untracked_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ignore_untracked=True + modified file still blocks ff."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )
        pull_called = [False]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            if "pull" in args:
                pull_called[0] = True
            result = MagicMock()
            result.stderr = ""
            if "symbolic-ref" in args:
                result.stdout = "main\n"
            elif "status" in args and "--porcelain" in args:
                # untracked + modified — modified must still block
                result.stdout = "?? artifact.lock\n M src/cw/foo.py\n"
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with pytest.raises(WorktreeError, match="dirty"):
            fast_forward_main(client, ignore_untracked=True)

        assert not pull_called[0], "pull must NOT be called when modified files present"


class TestCheckMainFfSafety:
    """Tests for check_main_ff_safety — classifies local/origin divergence."""

    @staticmethod
    def _make_client(tmp_path: Path) -> ClientConfig:
        ws = tmp_path / "ws"
        ws.mkdir()
        return ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )

    @staticmethod
    def _make_mock(
        *,
        detached: bool = False,
        main_is_ancestor: bool = False,
        origin_is_ancestor: bool = False,
    ) -> object:
        """Build a _run_git mock for check_main_ff_safety calls.

        symbolic-ref exits non-zero when detached.
        merge-base --is-ancestor: returncode 0 = true, 1 = false.
        """

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock()
            result.stderr = ""
            result.stdout = ""
            if "symbolic-ref" in args:
                if detached:
                    result.returncode = 1
                    result.stdout = ""
                    # simulate check=False path: do NOT raise
                else:
                    result.returncode = 0
                    result.stdout = "main\n"
            elif "merge-base" in args and "--is-ancestor" in args:
                # Determine which call: main→origin or origin→main
                # args: ("merge-base", "--is-ancestor", X, Y)
                subject = args[2] if len(args) > 2 else ""
                if subject == "main":
                    # main is ancestor of origin/main?
                    result.returncode = 0 if main_is_ancestor else 1
                else:
                    # origin/main is ancestor of main?
                    result.returncode = 0 if origin_is_ancestor else 1
            return result

        return mock_run

    def test_detached_head_returns_detached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Detached HEAD → 'detached'."""
        client = self._make_client(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._make_mock(detached=True),
        )
        assert check_main_ff_safety(client) == "detached"

    def test_behind_returns_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main is ancestor of origin/main only → 'behind'."""
        client = self._make_client(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._make_mock(main_is_ancestor=True, origin_is_ancestor=False),
        )
        assert check_main_ff_safety(client) == "behind"

    def test_ahead_returns_ahead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """origin/main is ancestor of main only → 'ahead'."""
        client = self._make_client(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._make_mock(main_is_ancestor=False, origin_is_ancestor=True),
        )
        assert check_main_ff_safety(client) == "ahead"

    def test_equal_returns_equal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both are mutual ancestors (equal SHAs) → 'equal'."""
        client = self._make_client(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._make_mock(main_is_ancestor=True, origin_is_ancestor=True),
        )
        assert check_main_ff_safety(client) == "equal"

    def test_diverged_returns_diverged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither is ancestor of the other → 'diverged'."""
        client = self._make_client(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._make_mock(main_is_ancestor=False, origin_is_ancestor=False),
        )
        assert check_main_ff_safety(client) == "diverged"


class TestIsMainCheckoutDirty:
    """is_main_checkout_dirty returns True iff tracked changes exist (#766)."""

    @staticmethod
    def _make_client(tmp_path: Path) -> ClientConfig:
        ws = tmp_path / "ws"
        ws.mkdir()
        return ClientConfig(
            name="test-client",
            workspace_path=ws,
            default_branch="main",
        )

    def test_returns_true_when_tracked_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Porcelain line without '??' prefix → returns True."""
        client = self._make_client(tmp_path)
        result = MagicMock()
        result.stdout = " M src/cw/dispatch.py\n"
        monkeypatch.setattr("cw.worktree._run_git", lambda *_a, **_kw: result)
        assert is_main_checkout_dirty(client) is True

    def test_returns_false_when_only_untracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only '??' lines in porcelain → returns False (untracked is safe for ff)."""
        client = self._make_client(tmp_path)
        result = MagicMock()
        result.stdout = "?? .claude/scheduled_tasks.lock\n"
        monkeypatch.setattr("cw.worktree._run_git", lambda *_a, **_kw: result)
        assert is_main_checkout_dirty(client) is False

    def test_returns_false_when_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty porcelain output → returns False."""
        client = self._make_client(tmp_path)
        result = MagicMock()
        result.stdout = ""
        monkeypatch.setattr("cw.worktree._run_git", lambda *_a, **_kw: result)
        assert is_main_checkout_dirty(client) is False

    def test_returns_false_on_git_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WorktreeError from _run_git → returns False; errors don't block dispatch."""
        client = self._make_client(tmp_path)

        def _boom(*a: object, **kw: object) -> object:
            msg = "git status failed"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.worktree._run_git", _boom)
        assert is_main_checkout_dirty(client) is False


def _mock_fetch_stderr(monkeypatch: pytest.MonkeyPatch, stderrs: list[str]) -> None:
    """Make every ``git`` call fail with rc=128, one stderr per call in order.

    The last entry repeats once the list is exhausted, so a single-item list
    means "always this failure".
    """
    calls: list[object] = []

    def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
        stderr = stderrs[min(len(calls), len(stderrs) - 1)]
        calls.append(args)
        return MagicMock(returncode=128, stdout="", stderr=stderr)

    monkeypatch.setattr("cw.worktree._run_git", mock_run)


def _freshness_warning_count(caplog: pytest.LogCaptureFixture) -> int:
    return sum(
        1
        for r in caplog.records
        if r.levelno == logging.WARNING and "freshness_check_skip" in r.getMessage()
    )


class TestFetchDefaultBranch:
    def test_missing_dir_returns_failed_no_raise(self, tmp_path: Path) -> None:
        """_fetch_default_branch with missing git_dir is FAILED, no exception."""
        missing = tmp_path / "does-not-exist"
        result = _fetch_default_branch("test-client", "main", missing)
        assert result.outcome is FetchOutcome.FAILED
        assert result.reason is not None
        assert "workspace missing" in result.reason
        assert str(missing) in result.reason

    def test_default_branch_absent_on_origin_is_still_a_skipped_freshness_check(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``BRANCH_ABSENT`` is only a benign state for a *feature* branch. The
        freshness check reads anything but ``FETCHED`` as "cannot tell": not
        stale, and the failure still WARNs."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client", workspace_path=ws, default_branch="main"
        )

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            return MagicMock(
                returncode=128, stdout="", stderr="fatal: couldn't find remote ref main"
            )

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            result = is_main_behind_origin(client)

        assert result == (False, "", "", 0)
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_multiline_stderr_collapses_to_single_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """rc=128 with multi-line git stderr: WARNING contains no newline."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client", workspace_path=ws, default_branch="main"
        )

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock()
            result.returncode = 128
            result.stdout = ""
            result.stderr = (
                "fatal: 'origin' does not appear to be a git repository\n"
                "\n"
                "fatal: Could not read from remote repository.\n"
                "\n"
                "Please make sure you have the correct access rights\n"
                "and the repository exists.\n"
            )
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            is_main_behind_origin(client)

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warning_messages, "Expected at least one WARNING"
        for msg in warning_messages:
            assert "\n" not in msg, f"WARNING contains newline: {msg!r}"

    def test_warned_fetch_fail_deduplicates_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Second call, same client AND same failure, with the set does not log."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client", workspace_path=ws, default_branch="main"
        )
        stderr = "fatal: 'origin' does not appear to be a git repository"
        _mock_fetch_stderr(monkeypatch, [stderr])
        warned_fetch_fail: set[FetchWarningKey] = set()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            is_main_behind_origin(client, warned_fetch_fail=warned_fetch_fail)
            first_count = _freshness_warning_count(caplog)
            caplog.clear()
            is_main_behind_origin(client, warned_fetch_fail=warned_fetch_fail)
            second_count = _freshness_warning_count(caplog)

        assert first_count == 1, "Expected WARNING on first call"
        assert second_count == 0, "Expected no WARNING on second call (deduped)"
        assert warned_fetch_fail == {
            ("test-client", FetchOutcome.FAILED, f"rc=128: {stderr}")
        }

    def test_different_failure_reasons_for_one_client_warn_twice(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#2213: dedup is keyed on the failure, not just the client. An auth
        error arriving after a network error is new information, and silence
        would read as "the earlier problem persists"."""
        ws = tmp_path / "ws"
        ws.mkdir()
        network = "fatal: unable to access 'https://x/': Could not resolve host: x"
        auth = "git@x: Permission denied (publickey)."
        _mock_fetch_stderr(monkeypatch, [network, auth])
        warned: set[FetchWarningKey] = set()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            first = _fetch_default_branch("test-client", "main", ws, warned)
            second = _fetch_default_branch("test-client", "main", ws, warned)

        messages = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "freshness_check_skip" in r.getMessage()
        ]
        assert len(messages) == 2
        assert "Could not resolve host" in messages[0]
        assert "Permission denied" in messages[1]
        assert first.outcome is FetchOutcome.FAILED
        assert second.outcome is FetchOutcome.FAILED
        assert warned == {
            ("test-client", FetchOutcome.FAILED, f"rc=128: {network}"),
            ("test-client", FetchOutcome.FAILED, f"rc=128: {auth}"),
        }

    def test_same_failure_reason_twice_warns_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        stderr = "git@x: Permission denied (publickey)."
        _mock_fetch_stderr(monkeypatch, [stderr, stderr])
        warned: set[FetchWarningKey] = set()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            _fetch_default_branch("test-client", "main", ws, warned)
            _fetch_default_branch("test-client", "main", ws, warned)

        assert _freshness_warning_count(caplog) == 1
        assert len(warned) == 1

    def test_same_failure_reason_for_a_different_client_still_warns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The client stays part of the key: two clients failing identically
        are two problems."""
        ws = tmp_path / "ws"
        ws.mkdir()
        stderr = "git@x: Permission denied (publickey)."
        _mock_fetch_stderr(monkeypatch, [stderr, stderr, stderr])
        warned: set[FetchWarningKey] = set()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            _fetch_default_branch("client-a", "main", ws, warned)
            _fetch_default_branch("client-b", "main", ws, warned)
            _fetch_default_branch("client-a", "main", ws, warned)

        assert _freshness_warning_count(caplog) == 2
        assert {key[0] for key in warned} == {"client-a", "client-b"}

    def test_same_reason_under_a_different_outcome_warns_again(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The outcome is part of the key too: a set already holding this client
        and reason under ``FAILED`` does not silence the same text arriving as
        ``BRANCH_ABSENT``."""
        ws = tmp_path / "ws"
        ws.mkdir()
        stderr = "fatal: couldn't find remote ref main"
        _mock_fetch_stderr(monkeypatch, [stderr])
        reason = f"rc=128: {stderr}"
        warned: set[FetchWarningKey] = {("test-client", FetchOutcome.FAILED, reason)}

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            result = _fetch_default_branch("test-client", "main", ws, warned)

        assert result.outcome is FetchOutcome.BRANCH_ABSENT
        assert _freshness_warning_count(caplog) == 1
        assert ("test-client", FetchOutcome.BRANCH_ABSENT, reason) in warned

    def test_missing_workspace_warns_once_per_distinct_path(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A permanently-missing workspace is the same failure every tick, so the
        dedup set (which lives for the whole dispatch loop) must silence the
        repeat. A different missing path is a different reason and warns."""
        first_missing = tmp_path / "gone-a"
        second_missing = tmp_path / "gone-b"
        warned: set[FetchWarningKey] = set()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            first = _fetch_default_branch("test-client", "main", first_missing, warned)
            repeat = _fetch_default_branch("test-client", "main", first_missing, warned)
            assert _freshness_warning_count(caplog) == 1
            other = _fetch_default_branch("test-client", "main", second_missing, warned)

        assert _freshness_warning_count(caplog) == 2
        # The returned result is unchanged by the dedup.
        for result, path in ((first, first_missing), (repeat, first_missing)):
            assert result == FetchResult(
                FetchOutcome.FAILED, f"workspace missing: {path}"
            )
        assert other == FetchResult(
            FetchOutcome.FAILED, f"workspace missing: {second_missing}"
        )
        assert warned == {
            ("test-client", FetchOutcome.FAILED, f"workspace missing: {first_missing}"),
            (
                "test-client",
                FetchOutcome.FAILED,
                f"workspace missing: {second_missing}",
            ),
        }

    def test_missing_workspace_always_warns_without_a_set(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``None`` (a one-shot caller) keeps warning on every call."""
        missing = tmp_path / "gone"

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            _fetch_default_branch("test-client", "main", missing)
            _fetch_default_branch("test-client", "main", missing)

        assert _freshness_warning_count(caplog) == 2

    @pytest.mark.parametrize(
        "exc_type", [FileNotFoundError, PermissionError, WorktreeError]
    )
    def test_os_failure_warns_once_then_warns_again_for_a_different_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        exc_type: type[Exception],
    ) -> None:
        """A missing git binary (or unusable workspace) fails identically on every
        tick; it must warn once. A different reason is new information."""
        ws = tmp_path / "ws"
        ws.mkdir()
        messages = ["git: command not found", "git: command not found", "denied"]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            raise exc_type(messages.pop(0))

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        warned: set[FetchWarningKey] = set()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            first = _fetch_default_branch("test-client", "main", ws, warned)
            repeat = _fetch_default_branch("test-client", "main", ws, warned)
            assert _freshness_warning_count(caplog) == 1
            other = _fetch_default_branch("test-client", "main", ws, warned)

        assert _freshness_warning_count(caplog) == 2
        # The returned result is unchanged by the dedup.
        assert (
            first
            == repeat
            == FetchResult(FetchOutcome.FAILED, "git: command not found")
        )
        assert other == FetchResult(FetchOutcome.FAILED, "denied")
        assert warned == {
            ("test-client", FetchOutcome.FAILED, "git: command not found"),
            ("test-client", FetchOutcome.FAILED, "denied"),
        }

    def test_os_failure_always_warns_without_a_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            msg = "git: command not found"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            _fetch_default_branch("test-client", "main", ws)
            _fetch_default_branch("test-client", "main", ws)

        assert _freshness_warning_count(caplog) == 2

    @pytest.mark.parametrize(
        ("stderr", "quiet_missing_ref", "expect_warning", "expected"),
        [
            pytest.param(
                "fatal: couldn't find remote ref x",
                True,
                False,
                FetchOutcome.BRANCH_ABSENT,
                id="missing-ref-quiet-is-debug",
            ),
            pytest.param(
                "fatal: couldn't find remote ref x",
                False,
                True,
                FetchOutcome.BRANCH_ABSENT,
                id="missing-ref-default-still-warns",
            ),
            pytest.param(
                "fatal: 'origin' does not appear to be a git repository",
                True,
                True,
                FetchOutcome.FAILED,
                id="other-failure-quiet-still-warns",
            ),
        ],
    )
    def test_quiet_missing_ref_logging(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        stderr: str,
        quiet_missing_ref: bool,
        expect_warning: bool,
        expected: FetchOutcome,
    ) -> None:
        """#2213: only a *missing remote ref* is downgraded to DEBUG, and only
        when the caller opts in; every other fetch failure still WARNs. The
        outcome does not depend on the log level: a missing remote ref is
        ``BRANCH_ABSENT`` either way, everything else ``FAILED``."""
        ws = tmp_path / "ws"
        ws.mkdir()

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            return MagicMock(returncode=128, stdout="", stderr=stderr)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        warned: set[FetchWarningKey] = set()

        with caplog.at_level(logging.DEBUG, logger="cw.worktree"):
            result = _fetch_default_branch(
                "test-client",
                "x",
                ws,
                warned_fetch_fail=warned,
                quiet_missing_ref=quiet_missing_ref,
            )

        assert result.outcome is expected
        # The reason is git's own first stderr line plus the exit status.
        assert result.reason == f"rc=128: {stderr}"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert bool(warnings) is expect_warning
        if expect_warning:
            assert warned == {("test-client", expected, f"rc=128: {stderr}")}
        else:
            # Quiet path: DEBUG breadcrumb, dedupe set untouched.
            assert warned == set()
            assert any(
                r.levelno == logging.DEBUG and "freshness_check_skip" in r.getMessage()
                for r in caplog.records
            )


class TestFetchFeatureBranch:
    """Regression tests for fetch_feature_branch.

    Covers GitHub issue #381: the parent worktree holds a stale local ref for
    the feature branch after the impl agent pushes from an isolation sub-worktree.
    Without fetching, ``git diff FORK_POINT...origin/<branch>`` fails or returns
    an empty diff, causing reviewers to return a false BLOCK.
    """

    # Not tests.conftest.git_in: optional cwd and no -C, so bare
    # "git init"/"git clone" callers pass no repo at all.
    @staticmethod
    def _run_bare_git(*args: str, cwd: Path | None = None) -> str:
        """Run a git command stripped of GIT_* env vars; return stdout."""
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(cwd) if cwd else None,
            env=clean_env,
        )
        return result.stdout.strip()

    def _setup_repo(self, tmp_path: Path) -> tuple[Path, Path, ClientConfig]:
        """Create bare origin and parent clone. Returns (bare, parent, client)."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        self._run_bare_git("init", "--bare", "-b", "main", str(bare))

        parent = tmp_path / "parent"
        self._run_bare_git("clone", str(bare), str(parent))
        self._run_bare_git("config", "user.email", "test@example.com", cwd=parent)
        self._run_bare_git("config", "user.name", "cw test", cwd=parent)
        (parent / "README.md").write_text("init\n")
        self._run_bare_git("add", "README.md", cwd=parent)
        self._run_bare_git("commit", "-m", "initial", cwd=parent)
        self._run_bare_git("push", "origin", "main", cwd=parent)

        client = ClientConfig(
            name="test-client",
            workspace_path=parent,
            default_branch="main",
        )
        return bare, parent, client

    def test_stale_local_ref_fixed_by_fetch(self, tmp_path: Path) -> None:
        """Fetching after impl push makes origin/<branch> visible for diff.

        Simulates: impl agent pushes from isolation worktree, parent's local ref
        is stale, reviewer dispatched — expects non-empty diff via origin/<branch>.
        """
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        bare, parent, client = self._setup_repo(tmp_path)
        fork_point = self._run_bare_git("rev-parse", "main", cwd=parent)

        # Simulate impl agent: separate clone, create feature branch, push.
        impl = tmp_path / "impl"
        self._run_bare_git("clone", str(bare), str(impl))
        self._run_bare_git("config", "user.email", "test@example.com", cwd=impl)
        self._run_bare_git("config", "user.name", "cw test", cwd=impl)
        self._run_bare_git("checkout", "-b", "auto-dev/381", cwd=impl)
        (impl / "fix.py").write_text("# fix\n")
        self._run_bare_git("add", "fix.py", cwd=impl)
        self._run_bare_git("commit", "-m", "implement fix", cwd=impl)
        self._run_bare_git("push", "origin", "auto-dev/381", cwd=impl)

        # Parent: local branch does not exist — stale ref scenario.
        local_ref = subprocess.run(
            ["git", "rev-parse", "--verify", "refs/heads/auto-dev/381"],
            capture_output=True,
            cwd=str(parent),
            check=False,
            env=clean_env,
        )
        assert local_ref.returncode != 0, "local branch must not exist pre-fetch"

        # diff against origin/<branch> fails before fetch — unknown ref.
        diff_before = subprocess.run(
            ["git", "diff", f"{fork_point}...origin/auto-dev/381"],
            capture_output=True,
            text=True,
            cwd=str(parent),
            check=False,
            env=clean_env,
        )
        assert diff_before.returncode != 0, "diff must fail before fetch (unknown ref)"

        fetched = fetch_feature_branch(client, "auto-dev/381")
        assert fetched.outcome is FetchOutcome.FETCHED
        assert fetched.reason is None

        # After fetch, origin/auto-dev/381 is known; diff is non-empty.
        diff_after = subprocess.run(
            ["git", "diff", f"{fork_point}...origin/auto-dev/381"],
            capture_output=True,
            text=True,
            cwd=str(parent),
            check=False,
            env=clean_env,
        )
        assert diff_after.returncode == 0
        assert "fix.py" in diff_after.stdout

    def test_missing_workspace_returns_failed(self, tmp_path: Path) -> None:
        """FAILED without raising when workspace directory is absent."""
        client = ClientConfig(
            name="absent",
            workspace_path=tmp_path / "nonexistent",
            default_branch="main",
        )
        result = fetch_feature_branch(client, "auto-dev/999")
        assert result.outcome is FetchOutcome.FAILED
        assert result.reason is not None
        assert "workspace missing" in result.reason

    def test_nonexistent_remote_branch_returns_branch_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BRANCH_ABSENT (not FAILED) when the remote branch does not exist."""
        monkeypatch.setenv("LC_ALL", "C")  # the marker is git's English message
        _bare, _parent, client = self._setup_repo(tmp_path)
        result = fetch_feature_branch(client, "auto-dev/does-not-exist")
        assert result.outcome is FetchOutcome.BRANCH_ABSENT
        assert result.reason is not None
        assert "couldn't find remote ref" in result.reason

    def test_run_git_exception_returns_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILED without raising when _run_git raises WorktreeError."""
        _bare, _parent, client = self._setup_repo(tmp_path)

        def mock_run(*args: object, **kwargs: object) -> object:
            msg = "simulated git failure"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        result = fetch_feature_branch(client, "auto-dev/381")
        assert result.outcome is FetchOutcome.FAILED
        assert result.reason == "simulated git failure"

    @pytest.mark.parametrize(
        ("stderr", "expected_fragment"),
        [
            pytest.param(
                "git@github.com: Permission denied (publickey).\n"
                "fatal: Could not read from remote repository.",
                "Permission denied (publickey)",
                id="auth",
            ),
            pytest.param(
                "ssh: Could not resolve hostname github.com: Name not known\n"
                "fatal: Could not read from remote repository.",
                "Could not resolve hostname",
                id="network",
            ),
            pytest.param(
                "fatal: 'origin' does not appear to be a git repository",
                "does not appear to be a git repository",
                id="missing-remote",
            ),
        ],
    )
    def test_failed_fetch_reason_distinguishes_auth_network_and_missing_remote(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stderr: str,
        expected_fragment: str,
    ) -> None:
        """Round 5 item 4: the FAILED reason is git's first stderr line plus the
        exit status, so "not refreshed" says whether it was auth, network or a
        missing remote, and it is one line (fit for a log field or a note)."""
        _bare, _parent, client = self._setup_repo(tmp_path)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            return MagicMock(returncode=128, stdout="", stderr=stderr)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        result = fetch_feature_branch(client, "auto-dev/381")

        assert result.outcome is FetchOutcome.FAILED
        assert result.reason is not None
        assert expected_fragment in result.reason
        assert result.reason.startswith("rc=128: ")
        assert "\n" not in result.reason

    def test_failed_fetch_with_empty_stderr_still_carries_the_exit_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bare, _parent, client = self._setup_repo(tmp_path)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            return MagicMock(returncode=1, stdout="", stderr="")

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        result = fetch_feature_branch(client, "auto-dev/381")

        assert result == FetchResult(FetchOutcome.FAILED, "rc=1")

    def test_os_error_reason_is_carried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bare, _parent, client = self._setup_repo(tmp_path)

        def mock_run(*args: object, **kwargs: object) -> object:
            msg = "git binary vanished"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)

        result = fetch_feature_branch(client, "auto-dev/381")

        assert result.outcome is FetchOutcome.FAILED
        assert result.reason == "git binary vanished"


# ---------------------------------------------------------------------------
# TestWorktreeHasUnsavedWork (#425)
# ---------------------------------------------------------------------------


class TestResolveRemoteRef:
    """#2145: dispatch_fix_agent's remote-ref resolution prefers the checked-out
    branch's verified ``@{u}`` upstream over a guessed ``origin/<branch>`` name,
    falling back to the guess only when the upstream is absent or stale."""

    @staticmethod
    def _mock(
        *,
        upstream: str | None = "origin/dev/2145",
        upstream_rc: int = 0,
        verified_refs: frozenset[str] = frozenset(),
    ) -> Callable[..., MagicMock]:
        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="", stdout="")
            if "@{u}" in args:
                if upstream_rc != 0 or upstream is None:
                    result.returncode = upstream_rc or 128
                else:
                    result.stdout = f"{upstream}\n"
            elif "--verify" in args:
                ref = args[-1]
                result.returncode = 0 if ref in verified_refs else 128
            return result

        return mock_run

    def test_prefers_verified_upstream_over_origin_branch_guess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """@{u} resolves to a differently-named ref; both it and the guessed
        origin/<branch> verify -- the upstream wins."""
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._mock(
                upstream="origin/dev/renamed-slug",
                verified_refs=frozenset({"origin/dev/renamed-slug", "origin/dev/2145"}),
            ),
        )
        assert _resolve_remote_ref("dev/2145", wt_path) == "origin/dev/renamed-slug"

    def test_falls_back_to_origin_branch_when_no_upstream_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._mock(upstream=None, verified_refs=frozenset({"origin/dev/2145"})),
        )
        assert _resolve_remote_ref("dev/2145", wt_path) == "origin/dev/2145"

    def test_falls_back_to_origin_branch_when_upstream_configured_but_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._mock(
                upstream="origin/deleted-upstream",
                verified_refs=frozenset({"origin/dev/2145"}),
            ),
        )
        assert _resolve_remote_ref("dev/2145", wt_path) == "origin/dev/2145"

    def test_returns_none_when_neither_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._mock(upstream=None, verified_refs=frozenset()),
        )
        assert _resolve_remote_ref("dev/2145", wt_path) is None


class TestUnsavedWorkReason:
    """#2114: the unpushed-commit check prefers origin/<checked-out branch>
    over @{u}, and the reason names the predicate, base ref, and count."""

    def _client(self, tmp_path: Path) -> ClientConfig:
        return ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )

    def _wt(self, tmp_path: Path) -> Path:
        wt_path = tmp_path / "wt" / "dev-2114"
        wt_path.mkdir(parents=True)
        return wt_path

    @staticmethod
    def _mock(
        *,
        status: str = "",
        own_ref_exists: bool = True,
        own_log: str = "",
        upstream: str | None = "origin/main",
        default_log: str = "x\n" * 14,
        default_rc: int = 0,
    ) -> Callable[..., MagicMock]:
        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="", stdout="")
            joined = " ".join(args)
            if "status" in args:
                result.stdout = status
            elif "--show-current" in args:
                result.stdout = "dev/2114\n"
            elif "--verify" in args:
                result.returncode = 0 if own_ref_exists else 128
            elif "@{u}" in args:
                if upstream is None:
                    result.returncode = 128
                else:
                    result.stdout = f"{upstream}\n"
            elif "log" in args and "origin/dev/2114..HEAD" in joined:
                result.stdout = own_log
            elif "log" in args:
                result.returncode = default_rc
                result.stdout = default_log
            return result

        return mock_run

    def test_fully_pushed_branch_tracking_default_branch_is_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported case: @{u} = origin/main (14 commits "ahead"), tree
        clean, origin/dev/2114 == HEAD -> no unsaved work."""
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)
        monkeypatch.setattr("cw.worktree._run_git", self._mock())
        assert unsaved_work_reason(client, "dev/2114", wt_path=wt_path) is None
        assert worktree_has_unsaved_work(client, "dev/2114", wt_path=wt_path) is False

    def test_own_remote_ref_behind_head_names_ref_and_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git", self._mock(own_log="a one\nb two\n")
        )
        assert (
            unsaved_work_reason(client, "dev/2114", wt_path=wt_path)
            == "2 commit(s) not on origin/dev/2114"
        )

    def test_no_own_ref_measures_against_upstream_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without origin/<branch> the ladder is conservative, and the reason
        names the base it measured against instead of a bare verdict."""
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._mock(own_ref_exists=False, default_log="x\ny\nz\n"),
        )
        reason = unsaved_work_reason(client, "dev/2114", wt_path=wt_path)
        assert reason is not None
        assert reason.startswith("3 commit(s) ahead of origin/main")
        assert "cannot prove" in reason

    def test_no_own_ref_and_no_upstream_uses_default_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._mock(own_ref_exists=False, upstream=None, default_log=""),
        )
        assert unsaved_work_reason(client, "dev/2114", wt_path=wt_path) is None

    def test_uncommitted_paths_win_and_are_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._mock(status="?? foo.py\n M bar.py\n?? .claude/cw-context.json\n"),
        )
        assert (
            unsaved_work_reason(client, "dev/2114", wt_path=wt_path)
            == "2 uncommitted path(s)"
        )

    def test_absent_worktree_has_no_reason(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        assert unsaved_work_reason(client, "dev/absent") is None

    def test_all_refs_unresolvable_reports_offline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)
        monkeypatch.setattr(
            "cw.worktree._run_git",
            self._mock(own_ref_exists=False, upstream=None, default_rc=128),
        )
        reason = unsaved_work_reason(client, "dev/2114", wt_path=wt_path)
        assert reason == "no base ref resolvable (offline or bare clone)"

    def test_status_failure_is_reported_as_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)

        def boom(*args: str, cwd: object, check: bool = True) -> MagicMock:
            msg = "git exploded"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.worktree._run_git", boom)
        reason = unsaved_work_reason(client, "dev/2114", wt_path=wt_path)
        assert reason == "status check failed: git exploded"

    def test_log_failure_is_reported_as_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            if "status" in args:
                return MagicMock(returncode=0, stderr="", stdout="")
            msg = "log exploded"
            raise OSError(msg)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        reason = unsaved_work_reason(client, "dev/2114", wt_path=wt_path)
        assert reason == "unpushed-commit check failed: log exploded"


class TestWorktreeHasUnsavedWork:
    """Tests for worktree_has_unsaved_work."""

    def _client(self, tmp_path: Path) -> ClientConfig:
        return ClientConfig(
            name="test",
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "wt",
        )

    def test_returns_false_when_worktree_path_absent(self, tmp_path: Path) -> None:
        """No worktree on disk → nothing to lose → False."""
        client = self._client(tmp_path)
        # wt_path does NOT exist
        assert worktree_has_unsaved_work(client, "auto-dev/absent") is False

    def test_returns_true_for_uncommitted_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dirty working tree (git status --porcelain non-empty) → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-dirty"
        wt_path.mkdir(parents=True)

        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            calls.append(args)
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = " M some_file.py\n"
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/dirty") is True
        # status was the first check — we short-circuit, no log check needed
        assert any("status" in c for c in calls)

    def test_returns_true_for_unpushed_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clean working tree but commits not yet pushed → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-unpushed"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args and "origin/" in " ".join(args):
                result.returncode = 0  # origin/branch exists
                result.stdout = "abc1234\n"
            elif "log" in args:
                result.stdout = "abc1234 add feature\n"  # unpushed commit
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/unpushed") is True

    def test_returns_false_for_pushed_non_canonical_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1 (#2050): upstream resolves via ``@{u}`` on the worktree's
        actual checked-out branch, not by guessing ``origin/<branch>`` from
        the caller-supplied branch name — clean tree, upstream log empty →
        False even when the passed-in ``branch`` doesn't match the resolved
        upstream ref."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-noncanonical"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "@{u}" in args:
                result.stdout = "origin/dev/2044-liveness-gate\n"
            elif "log" in args and "origin/dev/2044-liveness-gate..HEAD" in args:
                result.stdout = ""  # upstream log empty — nothing unpushed
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "dev/2044", wt_path=wt_path) is False

    def test_returns_true_for_unpushed_commits_on_own_upstream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2a (#2050): same ``@{u}`` resolution as above, but the resolved
        upstream's log is non-empty → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-noncanonical-unpushed"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "--verify" in args:
                result.returncode = 128  # no origin/<branch> ref (#2114 level 0)
            elif "@{u}" in args:
                result.stdout = "origin/dev/2044-liveness-gate\n"
            elif "log" in args and "origin/dev/2044-liveness-gate..HEAD" in args:
                result.stdout = "abc1234 add feature\n"  # unpushed on upstream
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "dev/2044", wt_path=wt_path) is True

    def test_falls_through_to_level_2_when_upstream_log_returncode_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B2 (#2050): the upstream resolves via ``@{u}``, but the log check
        against it fails (non-zero returncode, no exception) — must fall
        through to Level 2 (``origin/<default_branch>``) rather than trusting
        the failed call's empty stdout as "nothing unpushed"."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-upstream-log-fails"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "--verify" in args:
                result.returncode = 128  # no origin/<branch> ref (#2114 level 0)
            elif "@{u}" in args:
                result.stdout = "origin/dev/2044-liveness-gate\n"
            elif "log" in args and "origin/dev/2044-liveness-gate..HEAD" in args:
                result.returncode = 1  # upstream log check itself failed
                result.stdout = ""
            elif "log" in args and "origin/main..HEAD" in args:
                result.returncode = 0
                result.stdout = "abc1234 add feature\n"  # Level 2: unpushed
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "dev/2044", wt_path=wt_path) is True

    def test_returns_false_when_no_upstream_and_at_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No upstream configured, branch sitting at base HEAD with 0 commits
        → False."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-noorigin"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "@{u}" in args:
                result.returncode = 128  # no upstream configured
                result.stdout = ""
            elif "log" in args and "origin/main" in " ".join(args):
                result.returncode = 0
                result.stdout = ""  # 0 commits beyond base
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/noorigin") is False

    def test_returns_true_when_no_upstream_and_commits_beyond_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No upstream configured, branch has commits beyond base → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-noorigin-commits"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "--verify" in args:
                result.returncode = 128  # no origin/<branch> ref (#2114 level 0)
            elif "@{u}" in args:
                result.returncode = 128  # no upstream configured
                result.stdout = ""
            elif "log" in args and "origin/main" in " ".join(args):
                result.returncode = 0
                result.stdout = "abc1234 add feature\n"  # real commit beyond base
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/noorigin-commits") is True

    def test_returns_false_when_origin_missing_and_head_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No origin/<branch> and no commits beyond base → nothing to lose → False."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-empty"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args and "origin/auto-dev" in " ".join(args):
                result.returncode = 128  # origin/<branch> does NOT exist
                result.stdout = ""
            elif "log" in args and "origin/main" in " ".join(args):
                result.returncode = 0  # origin/main exists
                result.stdout = ""  # no commits beyond base
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/empty") is False

    def test_returns_false_for_clean_pushed_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clean working tree and all commits pushed → False."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-clean"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean
            elif "rev-parse" in args and "origin/" in " ".join(args):
                result.returncode = 0  # origin/branch exists
                result.stdout = "abc1234\n"
            elif "log" in args:
                result.stdout = ""  # no unpushed commits
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/clean") is False

    def test_returns_true_on_status_git_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """git status fails → fail-safe: treat as unsaved to avoid data loss."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-err"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            if "status" in args:
                msg = "git status exploded"
                raise WorktreeError(msg)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/err") is True

    def test_returns_true_on_log_git_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """git log fails after clean status → fail-safe: treat as unsaved."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-logerr"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args and "origin/" in " ".join(args):
                result.returncode = 0
                result.stdout = "abc1234\n"
            elif "log" in args:
                msg = "git log exploded"
                raise WorktreeError(msg)
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/logerr") is True

    def test_returns_true_when_upstream_rev_parse_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``@{u}`` rev-parse raises (not status) → fail-safe: treat as unsaved."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-upstream-rev-parse-err"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            if "status" in args:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "--verify" in args:
                return MagicMock(returncode=128, stdout="", stderr="")  # no own ref
            if "@{u}" in args:
                msg = "git rev-parse @{u} exploded"
                raise WorktreeError(msg)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert (
            worktree_has_unsaved_work(client, "auto-dev/upstream-err", wt_path=wt_path)
            is True
        )

    # --- #472: .claude/ artifact filter ---

    def test_returns_false_when_only_claude_artifacts_untracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only .claude/ artifacts untracked (cw writes them) → False."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-artifacts"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = (
                    "?? .claude/cw-context.json\n?? .claude/settings.local.json\n"
                )
            elif "rev-parse" in args:
                result.returncode = 128
                result.stdout = ""
            elif "log" in args and "origin/main" in " ".join(args):
                result.returncode = 0
                result.stdout = ""  # no commits beyond base
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/artifacts") is False

    def test_returns_true_when_claude_artifacts_plus_real_untracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.claude/ artifacts + real untracked source file → still True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-mixed"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = "?? .claude/cw-context.json\n?? src/cw/new_feature.py\n"
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/mixed") is True

    # --- #481: all refs absent fail-safe ---

    def test_returns_true_when_both_origins_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No upstream and origin/<default_branch> absent (offline) → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-offline"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args:
                result.returncode = 128  # @{u} — no upstream configured
                result.stdout = ""
            elif "log" in args:
                # Both origin refs absent — non-zero for any log call
                result.returncode = 128
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/offline") is True

    def test_returns_true_when_origin_absent_and_local_default_has_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Level 3: no upstream, local default present, has commits → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-level3"
        wt_path.mkdir(parents=True)

        call_count: list[int] = [0]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args:
                result.returncode = 128  # @{u} — no upstream configured
                result.stdout = ""
            elif "log" in args:
                call_count[0] += 1
                if call_count[0] <= 1:
                    # Level 2: origin/main absent
                    result.returncode = 128
                    result.stdout = ""
                else:
                    # Level 3: local main present, commits beyond it
                    result.returncode = 0
                    result.stdout = "abc1234 local commit\n"
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/level3") is True

    def test_returns_false_when_origin_absent_and_local_default_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Level 3: no upstream, local default present, no commits → False."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-level3-clean"
        wt_path.mkdir(parents=True)

        call_count: list[int] = [0]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args:
                result.returncode = 128  # @{u} — no upstream configured
                result.stdout = ""
            elif "log" in args:
                call_count[0] += 1
                if call_count[0] <= 1:
                    # Level 2: origin/main absent
                    result.returncode = 128
                    result.stdout = ""
                else:
                    # Level 3: local main present, no commits beyond it
                    result.returncode = 0
                    result.stdout = ""
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/level3-clean") is False

    def test_wt_path_override_checks_given_path_not_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2034: an explicit ``wt_path=`` checks *that* directory's dirty
        state, not the branch's canonical ``worktree_path_for`` location —
        needed to check a foreign (non-cw) worktree holding a branch."""
        client = self._client(tmp_path)
        # The client's own canonical path for this branch: clean.
        canonical_path = tmp_path / "wt" / "auto-dev-override"
        canonical_path.mkdir(parents=True)
        # A different, dirty directory the caller wants checked instead.
        foreign_path = tmp_path / "elsewhere" / "foreign"
        foreign_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = " M dirty.py\n" if str(cwd) == str(foreign_path) else ""
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert (
            worktree_has_unsaved_work(client, "auto-dev/override", wt_path=foreign_path)
            is True
        )


class TestHasCommitsBeyondBase:
    """Tests for _has_commits_beyond_base."""

    def test_commits_present_returns_true(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_run_git returns non-empty stdout → True."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0)
            result.stdout = "abc1234 chore: add feature\n"
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "main") is True

    def test_no_commits_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_run_git returns empty stdout → False."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0)
            result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "main") is False

    def test_git_failure_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_run_git returns nonzero exit → False."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=128)
            result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "main") is False

    def test_nonexistent_path_returns_false(self) -> None:
        """Path that doesn't exist → False."""
        from pathlib import Path as _Path

        from cw.worktree import _has_commits_beyond_base

        assert _has_commits_beyond_base(_Path("/nonexistent/path/xyz"), "main") is False

    def test_oserror_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OSError from _run_git (e.g. git not on PATH) → False."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "main") is False

    def test_uses_default_branch_in_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """default_branch is passed to git log, not hardcoded 'main'."""
        from cw.worktree import _has_commits_beyond_base

        captured_args: list[str] = []

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            captured_args.extend(args)
            result = MagicMock(returncode=0)
            result.stdout = "abc1234 feat: custom branch commit\n"
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path, "dev") is True
        assert "origin/dev..HEAD" in captured_args
        assert "origin/main..HEAD" not in captured_args


# ----------------------------------------------------------------------
# #1487 — git-fact scope verification (_parse_numstat_totals,
# compute_branch_diff_scope, reconcile_result_scope)
# ----------------------------------------------------------------------


def _commit_files(repo: Path, prefix: str, *, files: int, lines: int) -> None:
    """Add *files* new files of *lines* lines each and commit them."""
    for i in range(files):
        body = "".join(f"{prefix}-{i}-{n}\n" for n in range(lines))
        (repo / f"{prefix}_{i}.txt").write_text(body, encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", f"{prefix}: {files} files")


def _make_self_origin_repo(make_git_repo: Callable[[str], Path], name: str) -> Path:
    """Create a repo whose ``origin`` remote points at itself, with origin/main set."""
    repo = make_git_repo(name)
    git_in(repo, "remote", "add", "origin", str(repo))
    git_in(repo, "fetch", "origin", "main")
    return repo


def _make_diverged_repo(
    make_git_repo: Callable[[str], Path],
    name: str,
    *,
    branch: str = "dev/1487",
    branch_files: int = 3,
    branch_lines: int = 5,
    main_files: int = 8,
    main_lines: int = 40,
) -> Path:
    """Repo where ``origin/main`` advanced *after* *branch* forked from it.

    This is the #1393 shape: a self-report computed against a stale merge-base
    inflates ``files``/``lines_actual`` by counting main's own churn. A correct
    measurement uses ``merge-base origin/main HEAD`` and sees only the branch's
    own changes (*branch_files* / ``branch_files * branch_lines``).
    """
    repo = _make_self_origin_repo(make_git_repo, name)
    git_in(repo, "checkout", "-b", branch)
    _commit_files(repo, "branchwork", files=branch_files, lines=branch_lines)
    git_in(repo, "checkout", "main")
    _commit_files(repo, "mainchurn", files=main_files, lines=main_lines)
    # Refresh origin/main so it now points past the branch's fork point.
    git_in(repo, "fetch", "origin", "main")
    git_in(repo, "checkout", branch)
    return repo


class TestParseNumstatTotals:
    def test_normal_add_and_remove_lines(self) -> None:
        from cw.worktree import _parse_numstat_totals

        out = "3\t1\ta.py\n10\t0\tb.py\n"
        assert _parse_numstat_totals(out) == (2, 14)

    def test_binary_file_dash_is_skipped(self) -> None:
        from cw.worktree import _parse_numstat_totals

        out = "-\t-\timage.png\n5\t2\tc.py\n"
        assert _parse_numstat_totals(out) == (1, 7)

    def test_empty_output_is_zero(self) -> None:
        from cw.worktree import _parse_numstat_totals

        assert _parse_numstat_totals("") == (0, 0)

    def test_short_lines_are_ignored(self) -> None:
        from cw.worktree import _parse_numstat_totals

        assert _parse_numstat_totals("garbage\n1\t1\n") == (0, 0)


class TestComputeBranchDiffScope:
    def test_measures_only_branch_changes_past_a_stale_merge_base(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """origin/main advanced after the fork → only the branch's own churn counts."""
        from cw.worktree import compute_branch_diff_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-diverged")

        scope = compute_branch_diff_scope(repo, "main")

        assert scope is not None
        assert scope["branch"] == "dev/1487"
        assert scope["files"] == 3
        assert scope["lines_actual"] == 15
        assert scope["merge_base"]

    def test_no_changes_returns_zeroes(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.worktree import compute_branch_diff_scope

        repo = _make_self_origin_repo(make_git_repo, "wt-1487-clean")
        git_in(repo, "checkout", "-b", "dev/clean")

        scope = compute_branch_diff_scope(repo, "main")

        assert scope is not None
        assert scope["files"] == 0
        assert scope["lines_actual"] == 0

    def test_missing_origin_ref_returns_none(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.worktree import compute_branch_diff_scope

        repo = make_git_repo("wt-1487-no-origin")
        assert compute_branch_diff_scope(repo, "main") is None

    def test_detached_head_returns_none(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.worktree import compute_branch_diff_scope

        repo = _make_self_origin_repo(make_git_repo, "wt-1487-detached")
        git_in(repo, "checkout", "--detach")
        assert compute_branch_diff_scope(repo, "main") is None

    def test_missing_path_returns_none(self, tmp_path: Path) -> None:
        from cw.worktree import compute_branch_diff_scope

        assert compute_branch_diff_scope(tmp_path / "nope", "main") is None

    def test_merge_base_oserror_returns_none(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """git vanishes between the branch probe and merge-base → None, no raise."""
        from cw import worktree as wt_mod

        repo = _make_self_origin_repo(make_git_repo, "wt-1487-oserror")

        def boom(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        monkeypatch.setattr(wt_mod, "_checked_out_branch", lambda _p: "dev/x")
        monkeypatch.setattr(wt_mod, "_run_git", boom)
        assert wt_mod.compute_branch_diff_scope(repo, "main") is None

    def test_numstat_oserror_returns_none(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """merge-base resolves but the numstat call raises → None, no raise."""
        from cw import worktree as wt_mod

        repo = _make_diverged_repo(make_git_repo, "wt-1487-numstat-oserror")

        def boom_on_diff(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        monkeypatch.setattr(wt_mod, "_checked_out_branch", lambda _p: "dev/x")
        monkeypatch.setattr(wt_mod, "_resolve_merge_base", lambda _p, _b: "deadbeef")
        monkeypatch.setattr(wt_mod, "_run_git", boom_on_diff)
        assert wt_mod.compute_branch_diff_scope(repo, "main") is None

    def test_numstat_nonzero_returncode_returns_none(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unresolvable merge-base ref makes git diff exit non-zero → None."""
        from cw import worktree as wt_mod

        repo = _make_diverged_repo(make_git_repo, "wt-1487-numstat-rc")

        monkeypatch.setattr(
            wt_mod, "_resolve_merge_base", lambda _p, _b: "0000000000000000000000000000"
        )
        assert wt_mod.compute_branch_diff_scope(repo, "main") is None


class TestScopeMismatchIsGross:
    def test_equal_values_are_not_gross(self) -> None:
        """Guards the 0-vs-0 case, where the ratio test has no meaningful answer."""
        from cw.worktree import _scope_mismatch_is_gross

        assert _scope_mismatch_is_gross(0, 0) is False
        assert _scope_mismatch_is_gross(7, 7) is False


def _result_with_scope(files: int, lines_actual: int) -> AutoDevResult:
    """Build a post-impl AutoDevResult carrying a self-reported scope."""
    payload = _valid_payload()
    payload["scope"] = {
        "tier": "small",
        "files": files,
        "lines_estimate": 42,
        "lines_actual": lines_actual,
        "forbidden_touched": False,
    }
    return AutoDevResult.model_validate(payload)


class TestReconcileResultScope:
    def test_pre_impl_stage_is_exempt(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """stage1_* exits have no measurable diff — leave the self-report alone."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-preimpl")
        payload = _no_op_salvage_payload()
        payload["scope"] = {
            "tier": None,
            "files": 99,
            "lines_estimate": 0,
            "lines_actual": None,
            "forbidden_touched": False,
        }
        result = AutoDevResult.model_validate(payload)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out is result
        assert out.scope.files == 99
        assert caplog.text == ""

    def test_worktree_path_none_is_a_noop(self) -> None:
        from cw.worktree import reconcile_result_scope

        result = _result_with_scope(18, 1567)
        out = reconcile_result_scope(result, worktree_path=None, default_branch="main")
        assert out is result

    def test_inflated_self_report_is_corrected_and_warns(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """#1393 acceptance shape: 18/1567 self-reported vs 3/15 measured."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-inflated")
        result = _result_with_scope(18, 1567)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15
        assert out.scope.lines_estimate == 42
        assert out.scope.tier == "small"
        assert out.scope.forbidden_touched is False
        assert "scope.files" in caplog.text
        assert "scope.lines_actual" in caplog.text

    def test_matching_self_report_is_untouched_and_silent(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-match")
        result = _result_with_scope(3, 15)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out is result
        assert caplog.text == ""

    def test_small_divergence_corrects_without_warning(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Both non-zero and within the ratio threshold → corrected, no WARNING."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-small-delta")
        result = _result_with_scope(4, 20)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15
        assert caplog.text == ""

    def test_zero_vs_nonzero_always_warns(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 0 self-report against real work is a gross mismatch at any ratio."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-zero")
        result = _result_with_scope(0, 0)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15
        assert "scope.files" in caplog.text
        assert "scope.lines_actual" in caplog.text

    def test_nonzero_vs_zero_measured_always_warns(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Self-reported work against an empty branch is the inverse gross case."""
        from cw.worktree import reconcile_result_scope

        repo = _make_self_origin_repo(make_git_repo, "wt-1487-empty-branch")
        git_in(repo, "checkout", "-b", "dev/empty")
        result = _result_with_scope(7, 300)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 0
        assert out.scope.lines_actual == 0
        assert "scope.files" in caplog.text

    def test_fields_are_evaluated_independently(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """files matches but lines is grossly wrong → only lines warns."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-independent")
        result = _result_with_scope(3, 900)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15
        assert "scope.lines_actual" in caplog.text
        assert "scope.files" not in caplog.text

    def test_lines_only_mismatch_leaves_files_untouched(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Inverse of the above: lines matches, files does not."""
        from cw.worktree import reconcile_result_scope

        repo = _make_diverged_repo(make_git_repo, "wt-1487-files-only")
        result = _result_with_scope(11, 15)

        out = reconcile_result_scope(result, worktree_path=repo, default_branch="main")

        assert out.scope.files == 3
        assert out.scope.lines_actual == 15

    def test_git_failure_leaves_result_unchanged_and_warns(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """No origin/<default_branch> → unverifiable; fail open, never raise."""
        from cw.worktree import reconcile_result_scope

        repo = make_git_repo("wt-1487-unverifiable")
        result = _result_with_scope(18, 1567)

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            out = reconcile_result_scope(
                result, worktree_path=repo, default_branch="main"
            )

        assert out is result
        assert out.scope.files == 18
        assert "scope_verification_unavailable" in caplog.text


# ----------------------------------------------------------------------
# #1487 (fix loop) — resolve_scope_guard_default_branch: the shared
# client-resolution helper both cli.stop_hook and reconcile._shared now call,
# replacing two independently-written try/except wrappers.
# ----------------------------------------------------------------------


def _write_clients_yaml(
    tmp_config_dir: Path, name: str, *, default_branch: str
) -> None:
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_text(
        "clients:\n"
        f"  {name}:\n"
        f"    workspace_path: /tmp/ws-{name}\n"
        f"    default_branch: {default_branch}\n"
    )


class TestResolveScopeGuardDefaultBranch:
    def test_configured_client_resolves_its_own_default_branch(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_clients_yaml(tmp_config_dir, "acme", default_branch="trunk")

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            branch = resolve_scope_guard_default_branch("acme", log_context="ctx")

        assert branch == "trunk"
        assert caplog.text == ""

    def test_no_clients_yaml_falls_back_to_main_and_warns(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            branch = resolve_scope_guard_default_branch("acme", log_context="ctx")

        assert branch == "main"
        assert "scope_verification_client_unresolved" in caplog.text
        assert "client=acme" in caplog.text

    def test_unknown_client_key_in_valid_yaml_falls_back_and_warns(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A well-formed clients.yaml missing the requested key is a logged fallback.

        This is the case the two independent pre-fix wrappers diverged on:
        stop_hook.py's get_client() raised CwError here (logged), but
        _shared.py's dict.get() silently returned None (unlogged). The shared
        helper must log in both cases.
        """
        _write_clients_yaml(tmp_config_dir, "beta", default_branch="develop")

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            branch = resolve_scope_guard_default_branch("acme", log_context="ctx")

        assert branch == "main"
        assert "scope_verification_client_unresolved" in caplog.text
        assert "client=acme" in caplog.text

    def test_malformed_yaml_falls_back_to_main_and_never_raises(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A real YAML syntax error must not propagate past this guard (#1487)."""
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text("clients: [\n  unterminated\n")

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            branch = resolve_scope_guard_default_branch("acme", log_context="ctx")

        assert branch == "main"
        assert "scope_verification_client_unresolved" in caplog.text

    def test_log_context_rides_along_in_the_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            resolve_scope_guard_default_branch("acme", log_context="session=sess-42")

        assert "session=sess-42" in caplog.text
