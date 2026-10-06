"""Tests for cw.worktree._fast_forward - reuse fast-forward (#2213, #2233)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import WorktreeOccupiedError
from cw.models import OrchestratorEventType, SessionStatus
from cw.worktree import (
    FetchOutcome,
    FetchResult,
    RefreshOutcome,
    ReuseRefreshReport,
    _run_git,
    create_worktree,
    unsaved_work_reason,
)
from tests._worktree_helpers import patch_worktree
from tests._worktree_refresh_helpers import (
    _FULL_SHA_CHARS,
    _REUSE_BRANCH,
    _cw_worktree_records,
    _debug_reasons,
    _ff_events,
    _force_push_rewrite,
    _occupy,
    _refresh_with_ticket,
    _seed_behind,
    _seed_behind_with_submodule,
    _seed_behind_with_two_submodules,
    _seed_reuse,
    _seed_session,
    _spy_git,
)
from tests.conftest import git_in, push_commit_to_origin

if TYPE_CHECKING:
    from collections.abc import Callable


class TestCreateWorktreeReuseRefresh:
    """#2213: with ``refresh_on_reuse=True`` the reuse path best-effort fetches
    and fast-forwards a *behind, unoccupied, clean* worktree. It never resets,
    never raises, and leaves an occupied or diverged worktree exactly as it is."""

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

        patch_worktree(monkeypatch, "_run_git", spy)

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

    def test_unknown_ff_relation_never_fast_forwards(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The HEAD-vs-origin classification is matched exhaustively too: a
        relation the refresh does not know is a bug, never "behind"."""
        client, wt, _workspace, old_sha, _new = _seed_behind(tmp_path, make_git_repo)
        patch_worktree(monkeypatch, "_ff_relation", lambda *_a, **_kw: "sideways")

        with pytest.raises(AssertionError):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert git_in(wt, "rev-parse", "HEAD") == old_sha


class TestReuseSubmoduleSync:
    """#2233: a reuse fast-forward that brings in ``.gitmodules`` changes syncs
    submodules; one that doesn't leaves repos without submodules untouched."""

    def test_submodule_sync_after_fast_forward_that_adds_gitmodules(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, _workspace, old_sha, new_sha, _sub = _seed_behind_with_submodule(
            tmp_path, make_git_repo, monkeypatch
        )
        assert old_sha != new_sha

        result = create_worktree(
            client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
        )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert (wt / ".gitmodules").exists()
        assert (wt / "sub" / "README.md").exists()

    def test_no_submodule_call_without_gitmodules(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, wt, _workspace, _old, new_sha = _seed_behind(tmp_path, make_git_repo)
        calls = _spy_git(monkeypatch)

        result = create_worktree(
            client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
        )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha
        assert not any(args and args[0] == "submodule" for args, _cwd in calls)

    def test_occupancy_change_after_ff_aborts_before_submodule_sync(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A session appearing between the fast-forward and the submodule sync
        aborts the WHOLE refresh -- the ff already moved HEAD (not undone),
        but the submodule sync never runs and the caller gets
        ``WorktreeOccupiedError``, exactly the "abort" contract #2213 gives a
        caller that would otherwise spawn/dispatch/mutate an occupied tree."""
        client, wt, workspace, _old_sha, new_sha, _sub = _seed_behind_with_submodule(
            tmp_path, make_git_repo, monkeypatch
        )

        def merge_then_occupy(
            *args: str, cwd: Path, check: bool = True
        ) -> subprocess.CompletedProcess[str]:
            result = _run_git(*args, cwd=cwd, check=check)
            if args and args[0] == "merge":
                _seed_session(workspace, wt, SessionStatus.ACTIVE)
            return result

        patch_worktree(monkeypatch, "_run_git", merge_then_occupy)

        with (
            caplog.at_level(logging.DEBUG, logger="cw.worktree"),
            pytest.raises(WorktreeOccupiedError),
        ):
            create_worktree(
                client, _REUSE_BRANCH, allow_dirty_reuse=True, refresh_on_reuse=True
            )

        assert git_in(wt, "rev-parse", "HEAD") == new_sha  # ff not undone
        assert not (wt / "sub" / "README.md").exists()  # sync never ran
        assert any("submodule sync skipped" in m for m in _debug_reasons(caplog))

    def test_submodule_sync_failure_is_reported_as_friction_and_does_not_abort(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client, wt, _workspace, _old_sha, new_sha, sub_origin = (
            _seed_behind_with_submodule(tmp_path, make_git_repo, monkeypatch)
        )
        shutil.rmtree(sub_origin)  # submodule clone will fail: origin is gone
        report = ReuseRefreshReport()

        with caplog.at_level(logging.WARNING, logger="cw.worktree"):
            result = create_worktree(
                client,
                _REUSE_BRANCH,
                allow_dirty_reuse=True,
                refresh_on_reuse=True,
                refresh_report=report,
            )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha  # ff still landed
        assert not (wt / "sub" / "README.md").exists()  # sync failed, no clone
        assert len(report.notes) == 1
        assert "submodule sync" in report.notes[0]
        assert str(wt) in report.notes[0]
        assert any(
            "submodule sync" in r.getMessage()
            for r in _cw_worktree_records(caplog, logging.WARNING)
        )

    def test_submodule_sync_failure_can_leave_worktree_dirty_for_next_refresh(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed ``git submodule update`` does not just leave submodules
        stale -- it can leave the SUPERPROJECT itself uncommitted-dirty
        (#2233 SHOULD_FIX). Verified empirically: with two submodules and
        only one origin deleted, ``git`` registers (fetches into
        ``.git/modules/``) whichever submodule it reaches before the
        failure, but its checkout pass never runs for EITHER submodule --
        so ``git status --porcelain`` on the superproject itself goes from
        clean to non-empty purely from that partial registration, with no
        submodule actually checked out. The next reuse refresh's own
        occupancy check (:func:`unsaved_work_reason`) then reads this
        worktree as having unsaved work and declines to fast-forward it
        again until a human intervenes."""
        client, wt, _workspace, _old_sha, new_sha, _ok_origin, broken_origin = (
            _seed_behind_with_two_submodules(tmp_path, make_git_repo, monkeypatch)
        )
        assert unsaved_work_reason(client, _REUSE_BRANCH, wt_path=wt) is None
        shutil.rmtree(broken_origin)  # one submodule's clone will fail
        report = ReuseRefreshReport()

        result = create_worktree(
            client,
            _REUSE_BRANCH,
            allow_dirty_reuse=True,
            refresh_on_reuse=True,
            refresh_report=report,
        )

        assert result == wt
        assert git_in(wt, "rev-parse", "HEAD") == new_sha  # ff still landed
        assert len(report.notes) == 1
        assert "partial" in report.notes[0]
        assert unsaved_work_reason(client, _REUSE_BRANCH, wt_path=wt) is not None


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

        patch_worktree(monkeypatch, "_run_git", spy)

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
        patch_worktree(
            monkeypatch,
            "fetch_feature_branch",
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

        patch_worktree(monkeypatch, "_run_git", spy)
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

        monkeypatch.setattr("cw.worktree._refresh.record_event", boom)
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
