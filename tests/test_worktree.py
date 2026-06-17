"""Tests for cw.worktree - Git worktree operations."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw.exceptions import StaleWorktreeError, WorktreeError
from cw.models import ClientConfig
from cw.worktree import (
    _fetch_default_branch,
    _git_dir,
    _register_cw_exclude,
    check_main_ff_safety,
    check_not_main_checkout,
    create_worktree,
    fast_forward_main,
    fetch_feature_branch,
    is_main_behind_origin,
    remove_worktree,
    resolve_worktree_base,
    slugify_branch,
    worktree_has_unsaved_work,
    worktree_path_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable


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

        original_run = __import__("cw.worktree", fromlist=["_run_git"])._run_git

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
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

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        result = create_worktree(client, "feat/search")
        assert result == wt_path
        # The behavior under test: an on-branch worktree is reused without a
        # `worktree add`. Assert that, not the exact verification command.
        assert not any("add" in call for call in calls)

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

    def test_new_branch_base_is_origin_main_not_operator_head(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
    ) -> None:
        """B (integration): new worktree starts from origin/main, not HEAD (#710).

        Sets up a real bare remote at C1, advances the workspace checkout to C2
        on a feature branch, then asserts the ticket worktree's HEAD == C1.
        """
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

        def _git(*args: str, cwd: Path) -> str:
            return subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                check=True,
                cwd=str(cwd),
                env=clean_env,
            ).stdout.strip()

        workspace = make_git_repo("workspace")
        c1 = _git("rev-parse", "HEAD", cwd=workspace)

        # Set up bare origin at C1 and fetch it into workspace
        origin = tmp_path / "origin.git"
        origin.mkdir()
        _git("init", "--bare", "-b", "main", cwd=origin)
        _git("remote", "add", "origin", str(origin), cwd=workspace)
        _git("push", "origin", "main", cwd=workspace)
        _git("fetch", "origin", cwd=workspace)

        # Advance workspace to C2 on an operator feature branch (simulating
        # the operator having a non-main branch checked out — the bug scenario)
        _git("checkout", "-b", "operator-feature", cwd=workspace)
        _git("commit", "--allow-empty", "-m", "operator commit C2", cwd=workspace)
        c2 = _git("rev-parse", "HEAD", cwd=workspace)
        assert c1 != c2

        client = ClientConfig(
            name="test",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
        )
        wt_path = create_worktree(client, "dev/710")

        actual_head = _git("rev-parse", "HEAD", cwd=wt_path)
        assert actual_head == c1, (
            f"Worktree HEAD should be origin/main ({c1}), got {actual_head} "
            f"(operator HEAD was {c2})"
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


class TestFetchDefaultBranch:
    def test_missing_dir_returns_false_no_raise(self, tmp_path: Path) -> None:
        """_fetch_default_branch with missing git_dir returns False, no exception."""
        missing = tmp_path / "does-not-exist"
        result = _fetch_default_branch("test-client", "main", missing)
        assert result is False

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
        """Second call for same client with warned_fetch_fail set does not log."""
        ws = tmp_path / "ws"
        ws.mkdir()
        client = ClientConfig(
            name="test-client", workspace_path=ws, default_branch="main"
        )

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock()
            result.returncode = 128
            result.stdout = ""
            result.stderr = "fatal: 'origin' does not appear to be a git repository"
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        warned_fetch_fail: set[str] = set()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            is_main_behind_origin(client, warned_fetch_fail=warned_fetch_fail)
            first_count = sum(
                1
                for r in caplog.records
                if r.levelno == logging.WARNING and "freshness_check_skip" in r.message
            )
            caplog.clear()
            is_main_behind_origin(client, warned_fetch_fail=warned_fetch_fail)
            second_count = sum(
                1
                for r in caplog.records
                if r.levelno == logging.WARNING and "freshness_check_skip" in r.message
            )

        assert first_count == 1, "Expected WARNING on first call"
        assert second_count == 0, "Expected no WARNING on second call (deduped)"


class TestFetchFeatureBranch:
    """Regression tests for fetch_feature_branch.

    Covers GitHub issue #381: the parent worktree holds a stale local ref for
    the feature branch after the impl agent pushes from an isolation sub-worktree.
    Without fetching, ``git diff FORK_POINT...origin/<branch>`` fails or returns
    an empty diff, causing reviewers to return a false BLOCK.
    """

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

        ok = fetch_feature_branch(client, "auto-dev/381")
        assert ok is True

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

    def test_missing_workspace_returns_false(self, tmp_path: Path) -> None:
        """Returns False without raising when workspace directory is absent."""
        client = ClientConfig(
            name="absent",
            workspace_path=tmp_path / "nonexistent",
            default_branch="main",
        )
        assert fetch_feature_branch(client, "auto-dev/999") is False

    def test_nonexistent_remote_branch_returns_false(self, tmp_path: Path) -> None:
        """Returns False when the remote branch does not exist."""
        _bare, _parent, client = self._setup_repo(tmp_path)
        assert fetch_feature_branch(client, "auto-dev/does-not-exist") is False

    def test_run_git_exception_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False without raising when _run_git raises WorktreeError."""
        _bare, _parent, client = self._setup_repo(tmp_path)

        def mock_run(*args: object, **kwargs: object) -> object:
            msg = "simulated git failure"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert fetch_feature_branch(client, "auto-dev/381") is False


# ---------------------------------------------------------------------------
# TestWorktreeHasUnsavedWork (#425)
# ---------------------------------------------------------------------------


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

    def test_returns_false_when_origin_branch_absent_and_at_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No origin/<branch>, branch sitting at base HEAD with 0 commits → False."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-noorigin"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args and "origin/auto-dev/noorigin" in args:
                result.returncode = 128  # origin/<branch> does NOT exist
                result.stdout = ""
            elif "log" in args and "origin/main" in " ".join(args):
                result.returncode = 0
                result.stdout = ""  # 0 commits beyond base
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert worktree_has_unsaved_work(client, "auto-dev/noorigin") is False

    def test_returns_true_when_origin_branch_absent_and_commits_beyond_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No origin/<branch>, branch has commits beyond base → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-noorigin-commits"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args and "origin/auto-dev" in " ".join(args):
                result.returncode = 128  # origin/<branch> does NOT exist
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
        """Both origin/<branch> and origin/<default_branch> absent (offline) → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-offline"
        wt_path.mkdir(parents=True)

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args:
                result.returncode = 128  # origin/<branch> does NOT exist
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
        """Level 3: origin absent, local default present, has commits → True."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-level3"
        wt_path.mkdir(parents=True)

        call_count: list[int] = [0]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args:
                result.returncode = 128  # origin/<branch> does NOT exist
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
        """Level 3: origin absent, local default present, no commits → False."""
        client = self._client(tmp_path)
        wt_path = tmp_path / "wt" / "auto-dev-level3-clean"
        wt_path.mkdir(parents=True)

        call_count: list[int] = [0]

        def mock_run(*args: str, cwd: object, check: bool = True) -> MagicMock:
            result = MagicMock(returncode=0, stderr="")
            if "status" in args:
                result.stdout = ""  # clean working tree
            elif "rev-parse" in args:
                result.returncode = 128  # origin/<branch> does NOT exist
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
        assert _has_commits_beyond_base(tmp_path) is True

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
        assert _has_commits_beyond_base(tmp_path) is False

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
        assert _has_commits_beyond_base(tmp_path) is False

    def test_nonexistent_path_returns_false(self) -> None:
        """Path that doesn't exist → False."""
        from pathlib import Path as _Path

        from cw.worktree import _has_commits_beyond_base

        assert _has_commits_beyond_base(_Path("/nonexistent/path/xyz")) is False

    def test_oserror_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OSError from _run_git (e.g. git not on PATH) → False."""
        from cw.worktree import _has_commits_beyond_base

        def mock_run(*args: str, cwd: object, check: bool = True) -> None:
            msg = "git not found"
            raise OSError(msg)

        monkeypatch.setattr("cw.worktree._run_git", mock_run)
        assert _has_commits_beyond_base(tmp_path) is False
