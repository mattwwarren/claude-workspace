"""Tests for cw.worktree._occupancy - the reuse occupancy verdict (#2213)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw import native_daemon
from cw.config import state_file
from cw.exceptions import StaleWorktreeError, WorktreeError, WorktreeOccupiedError
from cw.models import ClientConfig, SessionStatus
from cw.worktree import (
    FetchResult,
    RefreshOutcome,
    _reuse_occupancy,
    _run_git,
    create_worktree,
    fetch_feature_branch,
)
from tests._worktree_helpers import patch_worktree
from tests._worktree_refresh_helpers import (
    _REUSE_BRANCH,
    _cw_worktree_records,
    _debug_reasons,
    _occupy,
    _refresh_occupied,
    _refresh_occupied_with_debug,
    _refresh_reporting,
    _refresh_stale_with_debug,
    _refresh_with_debug,
    _seed_behind,
    _seed_reuse,
    _seed_roster,
    _seed_session,
    _spy_fetch,
    _vouch_for_roster_worker,
)
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable


class TestCreateWorktreeReuseRefresh:
    """#2213: with ``refresh_on_reuse=True`` the reuse path best-effort fetches
    and fast-forwards a *behind, unoccupied, clean* worktree. It never resets,
    never raises, and leaves an occupied or diverged worktree exactly as it is."""

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

        patch_worktree(monkeypatch, "fetch_feature_branch", fetch_then_occupy)

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


class TestReuseOccupancyRosterAndPaths:
    """#2213 round 2: the occupancy predicate consults the daemon roster as well
    as cw state, compares *resolved* paths, and fails closed on anything it
    cannot read."""

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
                _vouch_for_roster_worker(workspace)
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

        patch_worktree(monkeypatch, "fetch_feature_branch", fetch_then_change)
        patch_worktree(monkeypatch, "_run_git", spy)

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
