"""Tests for cw.worktree._unsaved - unsaved-work / dirty-tree detection."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw.exceptions import WorktreeError
from cw.models import (
    ClientConfig,
)
from cw.worktree import (
    _resolve_remote_ref,
    unsaved_work_reason,
    worktree_has_unsaved_work,
)
from tests._worktree_helpers import patch_worktree

if TYPE_CHECKING:
    from collections.abc import Callable


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
        patch_worktree(
            monkeypatch,
            "_run_git",
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
        patch_worktree(
            monkeypatch,
            "_run_git",
            self._mock(upstream=None, verified_refs=frozenset({"origin/dev/2145"})),
        )
        assert _resolve_remote_ref("dev/2145", wt_path) == "origin/dev/2145"

    def test_falls_back_to_origin_branch_when_upstream_configured_but_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        patch_worktree(
            monkeypatch,
            "_run_git",
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
        patch_worktree(
            monkeypatch,
            "_run_git",
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
        patch_worktree(monkeypatch, "_run_git", self._mock())
        assert unsaved_work_reason(client, "dev/2114", wt_path=wt_path) is None
        assert worktree_has_unsaved_work(client, "dev/2114", wt_path=wt_path) is False

    def test_own_remote_ref_behind_head_names_ref_and_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)
        patch_worktree(monkeypatch, "_run_git", self._mock(own_log="a one\nb two\n"))
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
        patch_worktree(
            monkeypatch,
            "_run_git",
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
        patch_worktree(
            monkeypatch,
            "_run_git",
            self._mock(own_ref_exists=False, upstream=None, default_log=""),
        )
        assert unsaved_work_reason(client, "dev/2114", wt_path=wt_path) is None

    def test_uncommitted_paths_win_and_are_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(tmp_path)
        wt_path = self._wt(tmp_path)
        patch_worktree(
            monkeypatch,
            "_run_git",
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
        patch_worktree(
            monkeypatch,
            "_run_git",
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

        patch_worktree(monkeypatch, "_run_git", boom)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
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

        patch_worktree(monkeypatch, "_run_git", mock_run)
        assert (
            worktree_has_unsaved_work(client, "auto-dev/override", wt_path=foreign_path)
            is True
        )
