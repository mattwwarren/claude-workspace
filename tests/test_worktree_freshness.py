"""Tests for cw.worktree._freshness - fetch and freshness vs. origin."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw.exceptions import WorktreeError
from cw.models import (
    ClientConfig,
)
from cw.worktree import (
    FetchOutcome,
    FetchResult,
    _fetch_default_branch,
    _git_dir,
    check_main_ff_safety,
    fast_forward_main,
    fetch_default_branch,
    fetch_feature_branch,
    is_main_behind_origin,
    is_main_checkout_dirty,
)
from tests._worktree_helpers import patch_worktree

if TYPE_CHECKING:
    from cw.worktree import FetchWarningKey


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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(
            monkeypatch,
            "_run_git",
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

        patch_worktree(
            monkeypatch,
            "_run_git",
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

        patch_worktree(
            monkeypatch,
            "_run_git",
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

        patch_worktree(
            monkeypatch,
            "_run_git",
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

        patch_worktree(
            monkeypatch,
            "_run_git",
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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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
        patch_worktree(
            monkeypatch,
            "_run_git",
            self._make_mock(detached=True),
        )
        assert check_main_ff_safety(client) == "detached"

    def test_behind_returns_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main is ancestor of origin/main only → 'behind'."""
        client = self._make_client(tmp_path)
        patch_worktree(
            monkeypatch,
            "_run_git",
            self._make_mock(main_is_ancestor=True, origin_is_ancestor=False),
        )
        assert check_main_ff_safety(client) == "behind"

    def test_ahead_returns_ahead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """origin/main is ancestor of main only → 'ahead'."""
        client = self._make_client(tmp_path)
        patch_worktree(
            monkeypatch,
            "_run_git",
            self._make_mock(main_is_ancestor=False, origin_is_ancestor=True),
        )
        assert check_main_ff_safety(client) == "ahead"

    def test_equal_returns_equal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both are mutual ancestors (equal SHAs) → 'equal'."""
        client = self._make_client(tmp_path)
        patch_worktree(
            monkeypatch,
            "_run_git",
            self._make_mock(main_is_ancestor=True, origin_is_ancestor=True),
        )
        assert check_main_ff_safety(client) == "equal"

    def test_diverged_returns_diverged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither is ancestor of the other → 'diverged'."""
        client = self._make_client(tmp_path)
        patch_worktree(
            monkeypatch,
            "_run_git",
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
        patch_worktree(monkeypatch, "_run_git", lambda *_a, **_kw: result)
        assert is_main_checkout_dirty(client) is True

    def test_returns_false_when_only_untracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only '??' lines in porcelain → returns False (untracked is safe for ff)."""
        client = self._make_client(tmp_path)
        result = MagicMock()
        result.stdout = "?? .claude/scheduled_tasks.lock\n"
        patch_worktree(monkeypatch, "_run_git", lambda *_a, **_kw: result)
        assert is_main_checkout_dirty(client) is False

    def test_returns_false_when_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty porcelain output → returns False."""
        client = self._make_client(tmp_path)
        result = MagicMock()
        result.stdout = ""
        patch_worktree(monkeypatch, "_run_git", lambda *_a, **_kw: result)
        assert is_main_checkout_dirty(client) is False

    def test_returns_false_on_git_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WorktreeError from _run_git → returns False; errors don't block dispatch."""
        client = self._make_client(tmp_path)

        def _boom(*a: object, **kw: object) -> object:
            msg = "git status failed"
            raise WorktreeError(msg)

        patch_worktree(monkeypatch, "_run_git", _boom)
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

    patch_worktree(monkeypatch, "_run_git", mock_run)


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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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


class TestFetchDefaultBranchWrapper:
    """Tests for the public ``fetch_default_branch`` wrapper (#2328).

    Named "Wrapper" (not "TestFetchDefaultBranch") to avoid colliding with the
    existing ``TestFetchDefaultBranch`` class above, which covers the private
    leaf helper ``_fetch_default_branch`` -- a same-named class here would
    silently shadow it in the module namespace and drop its tests from
    collection.
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

    def test_fetched(self, tmp_path: Path) -> None:
        """FETCHED against a real bare origin with the default branch pushed."""
        _bare, _parent, client = self._setup_repo(tmp_path)
        result = fetch_default_branch(client)
        assert result.outcome is FetchOutcome.FETCHED
        assert result.reason is None

    def test_missing_workspace_returns_failed(self, tmp_path: Path) -> None:
        """FAILED without raising when the workspace directory is absent."""
        client = ClientConfig(
            name="absent",
            workspace_path=tmp_path / "nonexistent",
            default_branch="main",
        )
        result = fetch_default_branch(client)
        assert result.outcome is FetchOutcome.FAILED
        assert result.reason is not None
        assert "workspace missing" in result.reason

    def test_default_branch_absent_on_origin_is_branch_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """BRANCH_ABSENT when the client's configured default branch does not
        exist on origin -- and, unlike ``fetch_feature_branch``, this is NOT
        quieted to DEBUG: an absent/renamed default branch is anomalous, not
        the expected never-pushed-yet state a feature branch can be in."""
        monkeypatch.setenv("LC_ALL", "C")  # the marker is git's English message
        _bare, _parent, client = self._setup_repo(tmp_path)
        client = client.model_copy(update={"default_branch": "does-not-exist"})

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            result = fetch_default_branch(client)

        assert result.outcome is FetchOutcome.BRANCH_ABSENT
        assert result.reason is not None
        assert "couldn't find remote ref" in result.reason
        assert any(r.levelno == logging.WARNING for r in caplog.records)


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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)

        result = fetch_feature_branch(client, "auto-dev/381")

        assert result == FetchResult(FetchOutcome.FAILED, "rc=1")

    def test_os_error_reason_is_carried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bare, _parent, client = self._setup_repo(tmp_path)

        def mock_run(*args: object, **kwargs: object) -> object:
            msg = "git binary vanished"
            raise FileNotFoundError(msg)

        patch_worktree(monkeypatch, "_run_git", mock_run)

        result = fetch_feature_branch(client, "auto-dev/381")

        assert result.outcome is FetchOutcome.FAILED
        assert result.reason == "git binary vanished"
