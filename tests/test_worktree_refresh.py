"""Tests for cw.worktree._refresh - reuse refresh orchestration (#2213)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw import native_daemon
from cw.events import read_events
from cw.exceptions import StaleWorktreeError, WorktreeOccupiedError
from cw.models import ClientConfig, OrchestratorEventType, SessionStatus
from cw.worktree import (
    FetchOutcome,
    FetchResult,
    RefreshOutcome,
    RefreshResult,
    ReuseRefreshReport,
    _Occupancy,
    _ref_exists,
    _refresh_reused_worktree,
    _run_git,
    create_worktree,
    fetch_feature_branch,
    is_main_behind_origin,
)
from tests._worktree_helpers import patch_worktree
from tests._worktree_refresh_helpers import (
    _REUSE_BRANCH,
    _cw_worktree_records,
    _debug_reasons,
    _force_push_rewrite,
    _refresh_reporting,
    _refresh_with_debug,
    _seed_behind,
    _seed_reuse,
    _seed_session,
    _spy_fetch,
)
from tests.conftest import git_in, push_commit_to_origin

if TYPE_CHECKING:
    from collections.abc import Callable


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
        monkeypatch.setattr(
            "cw.worktree._refresh.live_session_worktree_paths", lambda: None
        )
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

        patch_worktree(monkeypatch, "fetch_feature_branch", failing_fetch)
        patch_worktree(monkeypatch, "_run_git", spy)

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

        patch_worktree(monkeypatch, "_run_git", spy)
        patch_worktree(monkeypatch, "_ref_exists", no_tracking_ref)
        patch_worktree(
            monkeypatch,
            "fetch_feature_branch",
            lambda *_args, **_kw: FetchResult(FetchOutcome.FETCHED),
        )
        monkeypatch.setattr(
            "cw.worktree._refresh._reuse_occupancy",
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
                # #2328: _seed_behind's dev/2213 has a real commit ("tracked.txt")
                # beyond origin/main, so a simulated BRANCH_ABSENT now reaches
                # _handle_branch_absent's own-commits arm and IS noted (unlike a
                # genuinely never-pushed, no-own-commits branch).
                FetchResult(FetchOutcome.BRANCH_ABSENT, "couldn't find remote ref"),
                False,
                1,
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

        patch_worktree(monkeypatch, "_run_git", spy)
        patch_worktree(monkeypatch, "fetch_feature_branch", lambda _c, _b: fetch)

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

        patch_worktree(monkeypatch, "_run_git", spy)

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

        patch_worktree(monkeypatch, "_run_git", mock_run)
        patch_worktree(monkeypatch, "unsaved_work_reason", clean)
        patch_worktree(monkeypatch, "fetch_feature_branch", boom)
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

        patch_worktree(monkeypatch, "_run_git", spy)

        result, report = _refresh_reporting(client)

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == old_sha
        assert len(report.notes) == 1
        assert str(wt) in report.notes[0]
        assert "git vanished mid-merge" in report.notes[0]

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
        assert f"already up to date with origin/{client.default_branch}" in (
            report.reason
        )
        assert report.notes == []

    def test_never_pushed_branch_with_no_commits_fast_forwards_to_default_branch(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """#2328: a never-pushed branch with no commits of its own is just as
        stale relative to ``origin/main`` as a pushed one -- it now gets
        fast-forwarded to the freshly fetched default branch instead of being
        silently left alone."""
        client, _wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt2 = create_worktree(client, never_pushed)
        origin_main_tip = push_commit_to_origin(
            origin, "main", tmp_path / "side-main", "advance.txt"
        )
        report = ReuseRefreshReport()

        path = create_worktree(
            client,
            never_pushed,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )

        assert path == wt2
        assert git_in(wt2, "rev-parse", "HEAD") == origin_main_tip
        assert (wt2 / "advance.txt").exists()
        assert report.outcome is RefreshOutcome.REFRESHED
        assert report.notes == []
        assert git_in(wt2, "branch", "--show-current") == never_pushed
        events = read_events(
            event_types=[OrchestratorEventType.WORKTREE_FAST_FORWARDED]
        )
        assert len(events) == 1

    def test_never_pushed_branch_with_own_commits_is_left_alone_and_noted(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A never-pushed branch that DOES have commits of its own is left
        untouched -- it may be building on a stale base, but that base is the
        caller's own work, not something to silently rebase onto.

        The pre-fetch occupancy gate (``unsaved_work_reason``) already treats
        any unpushed commit as "unsaved work" via its own fallback ladder,
        so it fires before ``_fetch_gate``/``_handle_branch_absent`` are ever
        reached; occupancy is bypassed here (mirroring
        ``test_fetch_ok_but_tracking_ref_absent_is_a_no_op`` above) to
        exercise ``_handle_branch_absent``'s own-commits note on its own
        terms -- the scenario it targets is a branch whose own upstream
        tracking ref (not ``origin/<default_branch>``) already accounts for
        its commits, so occupancy sees it as clean while it is still ahead of
        the default branch."""
        client, _wt, origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt2 = create_worktree(client, never_pushed)
        git_in(wt2, "commit", "--allow-empty", "-m", "own unpushed work")
        local_sha = git_in(wt2, "rev-parse", "HEAD")
        push_commit_to_origin(origin, "main", tmp_path / "side-main", "advance.txt")
        monkeypatch.setattr(
            "cw.worktree._refresh._reuse_occupancy",
            lambda *_args, **_kw: _Occupancy(
                live=None, branch_mismatch=None, local=None
            ),
        )
        report = ReuseRefreshReport()

        result = _refresh_reused_worktree(
            client,
            never_pushed,
            wt2,
            report,
            ticket_id=None,
            daemon=native_daemon.get_native_daemon_client(),
        )

        assert git_in(wt2, "rev-parse", "HEAD") == local_sha
        assert result.outcome is RefreshOutcome.NOT_REFRESHED
        assert len(report.notes) == 1
        assert str(wt2) in report.notes[0]
        assert "commits of its own" in report.notes[0]
        events = read_events(
            event_types=[OrchestratorEventType.WORKTREE_FAST_FORWARDED]
        )
        assert events == []

    def test_never_pushed_branch_default_fetch_failure_leaves_it_alone_and_notes(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A never-pushed, no-own-commits branch whose default-branch fetch
        fails is left alone and noted -- same posture as a failed fetch on
        the pushed-branch path (:func:`_fetch_gate`'s ``FAILED`` arm)."""
        client, _wt, _origin, _workspace = _seed_reuse(tmp_path, make_git_repo)
        never_pushed = "dev/never-pushed"
        wt2 = create_worktree(client, never_pushed)
        head = git_in(wt2, "rev-parse", "HEAD")
        patch_worktree(
            monkeypatch,
            "fetch_default_branch",
            lambda _client: FetchResult(
                FetchOutcome.FAILED, "rc=128: fatal: simulated"
            ),
        )
        report = ReuseRefreshReport()

        path = create_worktree(
            client,
            never_pushed,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )

        assert path == wt2
        assert git_in(wt2, "rev-parse", "HEAD") == head
        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert len(report.notes) == 1
        assert str(wt2) in report.notes[0]
        assert "fatal: simulated" in report.notes[0]

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
        patch_worktree(
            monkeypatch,
            "fetch_feature_branch",
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
        patch_worktree(
            monkeypatch,
            "_refresh_reused_worktree",
            lambda *_a, **_kw: RefreshResult("not-a-real-outcome", "stand-in"),
        )

        with pytest.raises(AssertionError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

    def test_is_main_behind_origin_rejects_an_unknown_fetch_outcome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = ClientConfig(name="test", workspace_path=tmp_path)
        monkeypatch.setattr(
            "cw.worktree._freshness._fetch_default_branch",
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
