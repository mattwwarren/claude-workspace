"""Tests for the lockless worktree dirty-check pre-pass (#2548).

``cw.reconcile.dirty_checks`` holds the captured checks. ``reconcile()``
fills one store before ``sessions_lock`` (backstop sessions with a RUNNING
row, then the crash-tail phantoms); the in-lock phantom detect and the
TIMED_OUT/COMPLETED backstops only look checks up. A miss defers the session
a tick and never parks it; only a completed check that found unsaved work
parks. The pre-pass wiring inside ``reconcile()`` and the roster-race miss
(M8) are in ``tests/test_reconcile_core.py``; the in-lock adoption miss (M9)
is in ``tests/test_reconcile_unowned_running.py``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.exceptions import WorktreeError
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    QueueItemStatus,
    ReapReason,
    Session,
    SessionOrigin,
    SessionStatus,
    TicketTask,
    UsageLimitAct,
)
from cw.reconcile import _deps, dirty_checks, probe_store, reconcile
from cw.reconcile._shared import _DIRTY_WORKTREE_REASON, ProposedAction
from cw.reconcile.dirty_checks import (
    DIRTY_CHECK_BUDGET_SECONDS,
    DIRTY_CHECK_LOOKAHEAD_SECONDS,
    DIRTY_CHECK_MAX_AGE_SECONDS,
    DIRTY_CHECK_MAX_PER_TICK,
    DirtyChecks,
    DirtyCheckUnavailableError,
    lookup_dirty_reason,
    partition_dirty,
    prepass_phantom_ids,
)
from cw.reconcile.phantom import (
    _detect_phantom_candidates,
    capture_phantom_dirty_checks,
)
from cw.reconcile.tasks import (
    backstop_targets,
    capture_backstop_dirty_checks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from tests._clients_yaml import write_clients_yaml
from tests._dirty_check_helpers import (
    DIRTY_CHECKS_LOGGER,
    assert_deferred,
    capture_backstops_from_disk,
    checks_with,
    detect_phantom_prefetched,
    revert_completed_silent_prefetched,
    revert_timed_out_prefetched,
    unavailable_records,
)
from tests._reconcile_helpers import (
    _attention_events,
    _mk_phantom_daemon_session,
    _shipped_salvage_payload,
    _state_queue_snapshot,
    _write_salvage_transcript,
    probe_sessions_lock_free,
)
from tests.conftest import _make_daemon_session, _make_ticket_task, commit_tracked_file

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
_STARTED = datetime(2026, 1, 1, tzinfo=UTC)
_CLIENT = "client-a"
_DIRTY = "2 uncommitted path(s)"
_LIVE_HELPER = "cw.reconcile._shared.worktree_dirty_reason_by_path"
# The completion grace the backstops honor (tasks._COMPLETION_ROUTING_GRACE_SECONDS).
_GRACE_SECONDS = 60
# A pre-pass that captured at the cap leaves this many sessions unchecked.
_OVER_CAP = DIRTY_CHECK_MAX_PER_TICK + 1


def _backstop_session(
    sid: str,
    tmp_path: Path,
    *,
    status: SessionStatus = SessionStatus.TIMED_OUT,
    worktree: bool = True,
    **overrides: object,
) -> Session:
    """A terminal DAEMON session the backstops act on (no completion grace)."""
    fields: dict[str, object] = {
        "id": sid,
        "name": f"{_CLIENT}/auto-dev/{sid}",
        "status": status,
        "surface_ref": None,
        "worktree_path": tmp_path / f"wt-{sid}" if worktree else None,
    }
    fields.update(overrides)
    return _make_daemon_session(**fields)


def _phantom(sid: str, tmp_path: Path, **overrides: object) -> Session:
    """A crash-tail DAEMON phantom with a worktree."""
    session = _mk_phantom_daemon_session(
        sid, _STARTED, worktree_path=tmp_path / f"wt-{sid}"
    )
    return session.model_copy(update=overrides)


def _row(sid: str, **overrides: object) -> TicketTask:
    fields: dict[str, object] = {
        "ticket_id": sid,
        "client": _CLIENT,
        "status": QueueItemStatus.RUNNING,
        "session_id": sid,
    }
    fields.update(overrides)
    return _make_ticket_task(**fields)


def _seed(sessions: list[Session], rows: list[TicketTask]) -> None:
    save_state(CwState(sessions=sessions))
    save_dev_queue(DevQueueStore(tasks=rows))


def _stored_row(ticket_id: str) -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)


def _stored_session(sid: str) -> Session:
    return next(s for s in load_state().sessions if s.id == sid)


def _live(
    monkeypatch: pytest.MonkeyPatch, reasons: Mapping[str, str | None] | None = None
) -> list[str]:
    """Stub the live helper: the reason for each worktree's ``wt-<sid>`` name.

    Returns the list of session ids it was called for, in order.
    """
    calls: list[str] = []

    def _check(_client: str, path: Path) -> str | None:
        sid = path.name.removeprefix("wt-")
        calls.append(sid)
        return (reasons or {}).get(sid)

    monkeypatch.setattr(_LIVE_HELPER, _check)
    return calls


def _forbid_live(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_git(_client: str, _path: object) -> str | None:
        msg = "the in-lock consumer must never run the live dirty check"
        raise AssertionError(msg)

    monkeypatch.setattr(_LIVE_HELPER, _no_git)


def _push() -> MagicMock:
    push = _deps.fire_push_notification
    assert isinstance(push, MagicMock)
    return push


def _stop_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == DIRTY_CHECKS_LOGGER and "capture stopped" in r.getMessage()
    ]


class TestBounds:
    def test_constants(self) -> None:
        assert DIRTY_CHECK_MAX_PER_TICK == 12
        assert DIRTY_CHECK_BUDGET_SECONDS == 30.0
        assert DIRTY_CHECK_MAX_AGE_SECONDS == 90.0
        assert DIRTY_CHECK_LOOKAHEAD_SECONDS == 15.0

    def test_budget_leaves_headroom_under_the_freshness_bound(self) -> None:
        """Budget, plus a ~10 s in-flight check, plus the 15 s in-lock roster
        call before the consumers, stays under the age bound."""
        in_flight_check_seconds = 10.0
        roster_timeout_seconds = 15.0
        assert (
            DIRTY_CHECK_BUDGET_SECONDS
            + in_flight_check_seconds
            + roster_timeout_seconds
            < DIRTY_CHECK_MAX_AGE_SECONDS
        )


class TestDirtyChecksStore:
    def test_capture_runs_the_live_check_once_and_lookup_never(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("a", tmp_path)
        calls = _live(monkeypatch, {"a": _DIRTY})
        checks = DirtyChecks()

        assert checks.capture(session) == _DIRTY
        assert calls == ["a"]
        _forbid_live(monkeypatch)
        assert checks.lookup(session) == _DIRTY
        assert lookup_dirty_reason(checks, session) == _DIRTY
        assert checks.captures == 1

    def test_captured_clean_is_a_hit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = _backstop_session("a", tmp_path)
        checks = checks_with([session], {"a": None})
        _forbid_live(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            assert lookup_dirty_reason(checks, session) is None

        assert unavailable_records(caplog, "a") == []

    def test_no_worktree_needs_no_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("a", tmp_path, worktree=False)
        _forbid_live(monkeypatch)
        checks = DirtyChecks(max_captures=0)

        assert checks.capture(session) is None
        assert checks.lookup(session) is None
        assert lookup_dirty_reason(None, session) is None
        assert checks.captures == 0

    def test_cap_refuses_the_next_capture_before_checking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = [_backstop_session(f"s{i}", tmp_path) for i in range(_OVER_CAP)]
        calls = _live(monkeypatch)
        checks = DirtyChecks(max_captures=DIRTY_CHECK_MAX_PER_TICK)
        for session in sessions[:-1]:
            checks.capture(session)

        with pytest.raises(
            DirtyCheckUnavailableError,
            match=r"the 12-check per-tick dirty-check cap is reached; "
            r"client-a/auto-dev/s12 \(s12\) was not checked",
        ):
            checks.capture(sessions[-1])

        assert len(calls) == DIRTY_CHECK_MAX_PER_TICK
        assert checks.captures == DIRTY_CHECK_MAX_PER_TICK
        assert checks.max_captures == DIRTY_CHECK_MAX_PER_TICK

    def test_spent_budget_refuses_before_checking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(probe_store, "monotonic", lambda: clock["now"])
        checks = DirtyChecks(budget_seconds=DIRTY_CHECK_BUDGET_SECONDS)
        clock["now"] = DIRTY_CHECK_BUDGET_SECONDS
        _forbid_live(monkeypatch)

        with pytest.raises(
            DirtyCheckUnavailableError,
            match=r"the 30s dirty-check budget is spent; "
            r"client-a/auto-dev/a \(a\) was not checked",
        ):
            checks.capture(_backstop_session("a", tmp_path))

        assert checks.budget_seconds == DIRTY_CHECK_BUDGET_SECONDS
        assert checks.captures == 0

    def test_failing_check_counts_toward_the_cap_and_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("a", tmp_path)

        def _boom(_client: str, _path: object) -> str | None:
            msg = "a checker bug"
            raise RuntimeError(msg)

        monkeypatch.setattr(_LIVE_HELPER, _boom)
        checks = DirtyChecks(max_captures=1)

        with pytest.raises(RuntimeError, match="a checker bug"):
            checks.capture(session)

        assert checks.captures == 1
        with pytest.raises(DirtyCheckUnavailableError, match="cap is reached"):
            checks.capture(session)
        with pytest.raises(DirtyCheckUnavailableError, match="no dirty check"):
            checks.lookup(session)

    def test_changed_worktree_path_misses(self, tmp_path: Path) -> None:
        session = _backstop_session("a", tmp_path)
        checks = checks_with([session], {"a": None})
        moved = session.model_copy(update={"worktree_path": tmp_path / "elsewhere"})

        with pytest.raises(
            DirtyCheckUnavailableError,
            match=r"no dirty check was captured for client-a/auto-dev/a \(a\) "
            "at this worktree",
        ):
            checks.lookup(moved)

    def test_git_commit_invalidates_a_clean_capture(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("generation")
        session = _backstop_session("git", tmp_path, worktree_path=repo)
        _live(monkeypatch, {"git": None})
        checks = DirtyChecks()

        assert checks.capture(session) is None
        commit_tracked_file(repo, "committed.txt")

        with pytest.raises(
            DirtyCheckUnavailableError,
            match="the worktree changed since its dirty check",
        ):
            checks.lookup(session)

    @pytest.mark.parametrize("reason", [None, _DIRTY])
    def test_strict_freshness_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str | None
    ) -> None:
        """89.9 s is usable; exactly 90.0 s and a negative age are stale, for
        a clean capture and a dirty one alike."""
        session = _backstop_session("a", tmp_path)
        _live(monkeypatch, {"a": reason})
        checks = DirtyChecks()
        with freeze_time(_NOW) as clock:
            checks.capture(session)
            clock.tick(DIRTY_CHECK_MAX_AGE_SECONDS - 0.1)
            assert checks.lookup(session) == reason
            clock.tick(0.1)
            with pytest.raises(
                DirtyCheckUnavailableError, match=r"unusable at age 90\.0s"
            ):
                checks.lookup(session)
            clock.move_to(_NOW - timedelta(seconds=1))
            with pytest.raises(
                DirtyCheckUnavailableError, match=r"unusable at age -1\.0s"
            ):
                checks.lookup(session)

    def test_none_store_misses_a_worktree_session_and_logs_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _backstop_session("a", tmp_path)

        with (
            caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER),
            pytest.raises(DirtyCheckUnavailableError),
        ):
            lookup_dirty_reason(None, session)

        (record,) = unavailable_records(caplog, "a")
        assert record.name == "cw.reconcile.dirty_checks"
        assert record.getMessage() == (
            "dirty_check_unavailable: session=a name=client-a/auto-dev/a;"
            " deferring to the next tick: no dirty check was captured for"
            " client-a/auto-dev/a (a) at this worktree"
        )


class TestCaptureAll:
    def test_logs_one_stop_line_with_the_unchecked_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sessions = [_backstop_session(f"s{i}", tmp_path) for i in range(_OVER_CAP)]
        sessions.insert(0, _backstop_session("no-wt", tmp_path, worktree=False))
        calls = _live(monkeypatch)
        checks = DirtyChecks(max_captures=DIRTY_CHECK_MAX_PER_TICK)

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            checks.capture_all(sessions)

        assert calls == [f"s{i}" for i in range(DIRTY_CHECK_MAX_PER_TICK)]
        (line,) = _stop_lines(caplog)
        assert line == (
            "reconcile: dirty-check capture stopped: the 12-check per-tick"
            " dirty-check cap is reached; client-a/auto-dev/s12 (s12) was not"
            " checked; 1 of 13 session(s) left unchecked and deferred to the"
            " next tick"
        )

    def test_no_stop_line_when_everything_fits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _live(monkeypatch)
        checks = DirtyChecks(max_captures=1)

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            checks.capture_all([_backstop_session("a", tmp_path)])

        assert _stop_lines(caplog) == []
        assert checks.captures == 1


class TestPartitionDirty:
    def test_dirty_clean_miss_and_none_store(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        dirty = _backstop_session("dirty", tmp_path)
        clean = _backstop_session("clean", tmp_path)
        missed = _backstop_session("missed", tmp_path)
        bare = _backstop_session("bare", tmp_path, worktree=False)
        checks = checks_with([dirty, clean], {"dirty": _DIRTY, "clean": None})

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            assert partition_dirty(checks, [dirty, clean, missed, bare]) == (
                {"dirty": _DIRTY},
                {"missed"},
            )
            assert partition_dirty(None, [dirty, bare]) == ({}, {"dirty"})

        assert len(unavailable_records(caplog, "missed")) == 1


class TestParkOnlyOnDirt:
    """Note 1: only a completed check that found unsaved work parks."""

    def test_captured_dirty_parks_at_both_sites_and_pages_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed([_backstop_session("p1", tmp_path)], [_row("p1")])
        _live(monkeypatch, {"p1": _DIRTY, "phantom-p1": _DIRTY})

        assert revert_timed_out_prefetched() == []

        row = _stored_row("p1")
        assert row.status is QueueItemStatus.BLOCKED_ON_USER
        assert row.disposition == _DIRTY_WORKTREE_REASON
        assert row.session_id is None
        (page,) = _attention_events("p1-tick1", "p1")
        assert page["breadcrumbs"] == f"{tmp_path / 'wt-p1'}: {_DIRTY}"
        _push().assert_called_once()

        assert revert_timed_out_prefetched() == []

        assert len(_attention_events("p1-tick2", "p1")) == 1
        _push().assert_called_once()

        phantom = _phantom("phantom-p1", tmp_path)
        (candidate,) = detect_phantom_prefetched(
            CwState(sessions=[phantom]), {phantom.id}, now=_STARTED
        )
        assert candidate.proposed_action is ProposedAction.CRASH_COMPLETE
        assert candidate.worktree_dirty is True
        assert candidate.worktree_dirty_reason == _DIRTY

    def test_captured_clean_reverts_to_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(
            [_backstop_session("p2", tmp_path, status=SessionStatus.COMPLETED)],
            [_row("p2")],
        )
        _live(monkeypatch)

        assert revert_completed_silent_prefetched() == ["p2"]

        assert _stored_row("p2").status is QueueItemStatus.PENDING
        assert _stored_session("p2").reap_reason is ReapReason.COMPLETED_BACKSTOP
        assert _attention_events("p2", "p2") == []

        phantom = _phantom("phantom-p2", tmp_path)
        (candidate,) = detect_phantom_prefetched(
            CwState(sessions=[phantom]), {phantom.id}, now=_STARTED
        )
        assert candidate.worktree_dirty is False

    def test_helper_exception_is_captured_clean_and_reverts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery 4: an error escaping to the helper's own fail-safe is a
        captured clean hit, exactly as the in-lock check behaved."""
        _seed([_backstop_session("p3", tmp_path)], [_row("p3")])

        def _branch_boom(_path: Path) -> str:
            msg = "branch lookup exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.reconcile._deps.checked_out_branch", _branch_boom)

        assert revert_timed_out_prefetched() == ["p3"]
        assert _stored_row("p3").status is QueueItemStatus.PENDING

    def test_inner_git_failure_is_captured_dirty_and_parks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery 4: a git failure inside unsaved_work_reason comes back as
        a reason string, so it is a captured dirty hit and parks, as today."""
        session = _backstop_session("p4", tmp_path)
        assert session.worktree_path is not None
        session.worktree_path.mkdir()
        _seed([session], [_row("p4")])
        monkeypatch.setattr(
            "cw.reconcile._deps.checked_out_branch", lambda _p: "auto-dev/p4"
        )
        monkeypatch.setattr(
            "cw.reconcile._shared._worktree_evidence.get_client",
            lambda name: ClientConfig(name=name, workspace_path=tmp_path / "ws"),
        )

        def _git_boom(*_args: object, **_kwargs: object) -> object:
            msg = "git exploded"
            raise WorktreeError(msg)

        monkeypatch.setattr("cw.worktree._unsaved._run_git", _git_boom)

        assert revert_timed_out_prefetched() == []

        assert _stored_row("p4").status is QueueItemStatus.BLOCKED_ON_USER
        (page,) = _attention_events("p4", "p4")
        assert "status check failed: git exploded" in str(page["breadcrumbs"])


class TestMissSources:
    """One test per miss source. Each asserts the full deferred contract."""

    def _phantom_deferred(
        self,
        state: CwState,
        session: Session,
        checks: DirtyChecks | None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.clear()
        before = _state_queue_snapshot()
        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            candidates = _detect_phantom_candidates(
                state, {session.id}, now=_STARTED, dirty_checks=checks
            )
        assert candidates == []
        assert _state_queue_snapshot() == before
        assert _stored_session(session.id).status is SessionStatus.ACTIVE
        assert len(unavailable_records(caplog, session.id)) == 1

    @pytest.mark.parametrize(
        ("status", "revert"),
        [
            (SessionStatus.TIMED_OUT, revert_timed_out_tasks),
            (SessionStatus.COMPLETED, revert_completed_silent_tasks),
        ],
    )
    @pytest.mark.parametrize("store", ["none", "empty"])
    def test_absent_key_defers_at_both_sites(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        status: SessionStatus,
        revert: Callable[[DirtyChecks | None], list[str]],
        store: str,
    ) -> None:
        """M1: nothing captured (a ``None`` store or an empty one)."""
        session = _backstop_session("m1", tmp_path, status=status)
        phantom = _phantom("m1-phantom", tmp_path)
        _seed([session, phantom], [_row("m1")])
        _forbid_live(monkeypatch)
        checks = None if store == "none" else DirtyChecks()
        before = _state_queue_snapshot()

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            assert revert(checks) == []

        assert_deferred(
            before=before,
            ticket_id="m1",
            session=session,
            caplog=caplog,
            consumer="m1",
        )
        self._phantom_deferred(load_state(), phantom, checks, caplog)

    def test_cap_reached_defers_unchecked_sessions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M2: the 13th backstop session is not checked, so it defers."""
        sessions = [_backstop_session(f"s{i}", tmp_path) for i in range(_OVER_CAP)]
        _seed(sessions, [_row(s.id) for s in sessions])
        _live(monkeypatch)
        checks = DirtyChecks(max_captures=DIRTY_CHECK_MAX_PER_TICK)

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            capture_backstop_dirty_checks(
                load_state(), load_dev_queue().tasks, now=_now(), checks=checks
            )
            _forbid_live(monkeypatch)
            reverted = revert_timed_out_tasks(checks)

        assert reverted == [f"s{i}" for i in range(DIRTY_CHECK_MAX_PER_TICK)]
        assert len(_stop_lines(caplog)) == 1
        unchecked = sessions[-1]
        row = _stored_row(unchecked.id)
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == unchecked.id
        assert _stored_session(unchecked.id).reap_reason is None
        assert _attention_events("m2", unchecked.id) == []
        _push().assert_not_called()
        assert len(unavailable_records(caplog, unchecked.id)) == 1

    def test_spent_budget_defers_unchecked_sessions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M3: the 30 s budget is spent before the session is checked."""
        session = _backstop_session("m3", tmp_path)
        _seed([session], [_row("m3")])
        clock = {"now": 0.0}
        monkeypatch.setattr(probe_store, "monotonic", lambda: clock["now"])
        checks = DirtyChecks(budget_seconds=DIRTY_CHECK_BUDGET_SECONDS)
        clock["now"] = DIRTY_CHECK_BUDGET_SECONDS
        _forbid_live(monkeypatch)
        before = _state_queue_snapshot()

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            capture_backstop_dirty_checks(
                load_state(), load_dev_queue().tasks, now=_now(), checks=checks
            )
            assert revert_timed_out_tasks(checks) == []

        assert len(_stop_lines(caplog)) == 1
        assert_deferred(
            before=before, ticket_id="m3", session=session, caplog=caplog, consumer="m3"
        )

    @pytest.mark.parametrize("reason", [None, _DIRTY])
    def test_stale_capture_defers_at_both_sites(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        reason: str | None,
    ) -> None:
        """M4: a capture at the freshness bound, clean or dirty, only defers."""
        session = _backstop_session("m4", tmp_path)
        phantom = _phantom("m4-phantom", tmp_path)
        _seed([session, phantom], [_row("m4")])
        _live(monkeypatch, {"m4": reason, "m4-phantom": reason})
        with freeze_time(_NOW) as clock:
            checks = capture_backstops_from_disk()
            capture_phantom_dirty_checks(
                load_state(),
                {phantom.id},
                {},
                now=_NOW,
                config=OrchestratorConfig(),
                checks=checks,
            )
            assert checks.captures == 2
            clock.tick(DIRTY_CHECK_MAX_AGE_SECONDS)
            _forbid_live(monkeypatch)
            before = _state_queue_snapshot()

            with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
                assert revert_timed_out_tasks(checks) == []

            assert_deferred(
                before=before,
                ticket_id="m4",
                session=session,
                caplog=caplog,
                consumer="m4",
            )
            self._phantom_deferred(load_state(), phantom, checks, caplog)

    def test_changed_worktree_path_defers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M5: the session's worktree changed between capture and the lock."""
        session = _backstop_session("m5", tmp_path)
        _seed([session], [_row("m5")])
        _live(monkeypatch)
        checks = capture_backstops_from_disk()
        moved = session.model_copy(update={"worktree_path": tmp_path / "moved"})
        _seed([moved], [_row("m5")])
        _forbid_live(monkeypatch)
        before = _state_queue_snapshot()

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            assert revert_timed_out_tasks(checks) == []

        assert_deferred(
            before=before, ticket_id="m5", session=moved, caplog=caplog, consumer="m5"
        )

    def test_sweep_terminalized_session_without_completed_at_defers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M6: ACTIVE at the pre-pass, set TIMED_OUT by an in-lock sweep with
        no completed_at (so no grace), it was never in the capture set."""
        active = _backstop_session("m6", tmp_path, status=SessionStatus.ACTIVE)
        _seed([active], [_row("m6")])
        _live(monkeypatch)
        checks = capture_backstops_from_disk()
        assert checks.captures == 0
        timed_out = active.model_copy(update={"status": SessionStatus.TIMED_OUT})
        _seed([timed_out], [_row("m6")])
        _forbid_live(monkeypatch)
        before = _state_queue_snapshot()

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            assert revert_timed_out_tasks(checks) == []

        assert_deferred(
            before=before,
            ticket_id="m6",
            session=timed_out,
            caplog=caplog,
            consumer="m6",
        )

    def test_row_turned_running_after_prepass_defers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M7: the row was not RUNNING when the pre-pass read the queue."""
        session = _backstop_session("m7", tmp_path)
        _seed([session], [_row("m7", status=QueueItemStatus.PENDING, session_id=None)])
        _live(monkeypatch)
        checks = capture_backstops_from_disk()
        assert checks.captures == 0
        save_dev_queue(DevQueueStore(tasks=[_row("m7")]))
        _forbid_live(monkeypatch)
        before = _state_queue_snapshot()

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            assert revert_timed_out_tasks(checks) == []

        assert_deferred(
            before=before, ticket_id="m7", session=session, caplog=caplog, consumer="m7"
        )

    def test_row_turned_running_after_the_in_lock_read_is_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M7, in-lock variant: a row that turns RUNNING after the backstop's
        own queue pre-read is outside the set it acts on, so it waits a tick
        unchecked rather than being reverted unchecked."""
        session = _backstop_session("m7b", tmp_path)
        _seed([session], [_row("m7b", status=QueueItemStatus.PENDING, session_id=None)])
        _forbid_live(monkeypatch)
        real_partition = partition_dirty

        def _partition_then_claim(
            checks: DirtyChecks | None, sessions: list[Session]
        ) -> tuple[dict[str, str], set[str]]:
            save_dev_queue(DevQueueStore(tasks=[_row("m7b")]))
            return real_partition(checks, sessions)

        monkeypatch.setattr("cw.reconcile.tasks.partition_dirty", _partition_then_claim)

        assert revert_timed_out_tasks(DirtyChecks()) == []

        row = _stored_row("m7b")
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == "m7b"
        assert _stored_session("m7b").reap_reason is None
        assert _attention_events("m7b", "m7b") == []


def _now() -> datetime:
    return datetime.now(UTC)


class TestPhantomCapture:
    def _mixed_state(
        self, tmp_path: Path
    ) -> tuple[CwState, set[str], dict[str, Session]]:
        home = Path.home()
        crash = _phantom("crash", tmp_path)
        not_phantom = _phantom("not-phantom", tmp_path)
        user = _phantom("user", tmp_path, origin=SessionOrigin.USER)
        no_worktree = _phantom("no-wt", tmp_path, worktree_path=None)
        routed = _phantom(
            "routed", tmp_path, last_result=_shipped_salvage_payload("routed")
        )
        salvage = _phantom("salvage", tmp_path, surface_ref="fake-short-id")
        assert salvage.worktree_path is not None
        _write_salvage_transcript(
            home,
            salvage.worktree_path,
            "csid-salvage",
            _shipped_salvage_payload("salvage"),
        )
        sessions = [crash, not_phantom, user, no_worktree, routed, salvage]
        state = CwState(sessions=sessions)
        save_state(state)
        save_dev_queue(DevQueueStore(tasks=[]))
        phantoms = {s.id for s in sessions} - {not_phantom.id}
        return state, phantoms, {s.id: s for s in sessions}

    def test_captures_only_crash_tail_daemon_phantoms_with_a_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state, phantoms, _ = self._mixed_state(tmp_path)
        calls = _live(monkeypatch)
        checks = DirtyChecks()

        capture_phantom_dirty_checks(
            state,
            phantoms,
            {},
            now=_STARTED,
            config=OrchestratorConfig(),
            checks=checks,
        )

        assert calls == ["crash"]

    def test_detect_never_defers_a_session_the_capture_selected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Drift guard: capture and detect share one predicate, so every
        session the detect dirty-checks was captured."""
        state, phantoms, by_id = self._mixed_state(tmp_path)
        _live(monkeypatch)
        checks = DirtyChecks()
        capture_phantom_dirty_checks(
            state,
            phantoms,
            {},
            now=_STARTED,
            config=OrchestratorConfig(),
            checks=checks,
        )
        _forbid_live(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            candidates = _detect_phantom_candidates(
                state, phantoms, now=_STARTED, dirty_checks=checks
            )

        assert not [r for r in caplog.records if r.name == DIRTY_CHECKS_LOGGER]
        actions = {c.session_id: c.proposed_action for c in candidates}
        assert actions == {
            "crash": ProposedAction.CRASH_COMPLETE,
            "user": ProposedAction.CRASH_COMPLETE,
            "no-wt": ProposedAction.CRASH_COMPLETE,
            "routed": ProposedAction.ROUTE_EMITTED_SENTINEL,
            "salvage": ProposedAction.SALVAGE_COMPLETION,
        }
        assert by_id["user"].origin is SessionOrigin.USER

    def test_running_row_phantoms_are_captured_before_parked_ones(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Starvation guard: 13 parked phantoms listed first cannot take the
        whole cap from a phantom whose row is still RUNNING."""
        parked = [_phantom(f"parked-{i}", tmp_path) for i in range(_OVER_CAP)]
        running = _phantom("running", tmp_path)
        sessions = [*parked, running]
        rows = {
            s.id: _row(s.id, status=QueueItemStatus.BLOCKED_ON_USER, session_id=None)
            for s in parked
        }
        rows["running"] = _row("running")
        state = CwState(sessions=sessions)
        _seed(sessions, list(rows.values()))
        calls = _live(monkeypatch)
        checks = DirtyChecks(max_captures=DIRTY_CHECK_MAX_PER_TICK)

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            capture_phantom_dirty_checks(
                state,
                {s.id for s in sessions},
                rows,
                now=_STARTED,
                config=OrchestratorConfig(),
                checks=checks,
            )

        assert calls[0] == "running"
        assert len(calls) == DIRTY_CHECK_MAX_PER_TICK
        assert "2 of 14 session(s) left unchecked" in _stop_lines(caplog)[0]
        _forbid_live(monkeypatch)
        (candidate,) = _detect_phantom_candidates(
            state, {"running"}, rows, now=_STARTED, dirty_checks=checks
        )
        assert candidate.proposed_action is ProposedAction.CRASH_COMPLETE


class TestBackstopTargets:
    def test_narrowed_to_running_non_exempt_rows(self, tmp_path: Path) -> None:
        running = _backstop_session("running", tmp_path)
        pending = _backstop_session("pending", tmp_path)
        exempt = _backstop_session("exempt", tmp_path)
        user = _backstop_session("user", tmp_path, origin=SessionOrigin.USER)
        now = _now()
        act = UsageLimitAct(
            session_id="exempt",
            branch="auto",
            started_at=now,
            reset_at=None,
            until=now + timedelta(minutes=30),
        )
        state = CwState(sessions=[running, pending, exempt, user])
        rows = [
            _row("running"),
            _row("pending", status=QueueItemStatus.PENDING),
            _row("exempt", usage_limit_act=act),
            _row("user"),
        ]

        targets, running_ids = backstop_targets(state, rows, SessionStatus.TIMED_OUT)

        assert [s.id for s in targets] == ["running", "pending", "exempt"]
        assert running_ids == {"running"}

    def test_lookahead_captures_a_session_about_to_leave_the_grace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session 10 s inside the 60 s completion grace is outside a plain
        selection but inside the pre-pass's 15 s lookahead."""
        now = _now()
        session = _backstop_session(
            "near",
            tmp_path,
            completed_at=now - timedelta(seconds=_GRACE_SECONDS - 10),
        )
        state = CwState(sessions=[session])
        rows = [_row("near")]

        assert backstop_targets(state, rows, SessionStatus.TIMED_OUT, now=now)[0] == []
        lookahead = now + timedelta(seconds=DIRTY_CHECK_LOOKAHEAD_SECONDS)
        targets, running_ids = backstop_targets(
            state, rows, SessionStatus.TIMED_OUT, now=lookahead
        )
        assert [s.id for s in targets] == ["near"]
        assert running_ids == {"near"}

        calls = _live(monkeypatch)
        checks = DirtyChecks()
        capture_backstop_dirty_checks(state, rows, now=now, checks=checks)
        assert calls == ["near"]

    def test_capture_covers_timed_out_and_completed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timed_out = _backstop_session("to", tmp_path)
        completed = _backstop_session("cs", tmp_path, status=SessionStatus.COMPLETED)
        active = _backstop_session("live", tmp_path, status=SessionStatus.ACTIVE)
        state = CwState(sessions=[completed, active, timed_out])
        calls = _live(monkeypatch)

        capture_backstop_dirty_checks(
            state,
            [_row("to"), _row("cs"), _row("live")],
            now=_now(),
            checks=DirtyChecks(),
        )

        assert calls == ["to", "cs"]


class _Roster:
    """A ``claude agents --json`` stand-in that counts its calls."""

    def __init__(self, *short_ids: str, error: BaseException | None = None) -> None:
        self.short_ids = short_ids
        self.error = error
        self.calls = 0

    def __call__(self) -> list[dict[str, object]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [{"sessionId": f"{sid}-0000-4000"} for sid in self.short_ids]


def _live_daemon(sid: str, tmp_path: Path, **overrides: object) -> Session:
    fields: dict[str, object] = {
        "id": sid,
        "name": f"{_CLIENT}/auto-dev/{sid}",
        "surface_ref": f"ref-{sid}"[:8],
        "worktree_path": tmp_path / f"wt-{sid}",
        "started_at": _STARTED,
    }
    fields.update(overrides)
    return _make_daemon_session(**fields)


class TestPrepassPhantomIds:
    def test_no_roster_call_without_a_live_daemon_worktree_session(
        self, tmp_path: Path
    ) -> None:
        state = CwState(
            sessions=[
                _live_daemon("user", tmp_path, origin=SessionOrigin.USER),
                _live_daemon("bare", tmp_path, worktree_path=None),
                _live_daemon("done", tmp_path, status=SessionStatus.TIMED_OUT),
            ]
        )
        roster = _Roster()

        assert (
            prepass_phantom_ids(state, roster=roster, acting=frozenset(), now=_NOW)
            == set()
        )
        assert roster.calls == 0

    @pytest.mark.parametrize(
        "error",
        [
            subprocess.CalledProcessError(1, ["claude"]),
            json.JSONDecodeError("bad", "doc", 0),
            FileNotFoundError("claude"),
            subprocess.TimeoutExpired(["claude"], 15),
        ],
        ids=["called-process", "json", "missing", "timeout"],
    )
    def test_roster_errors_give_no_phantoms(
        self, tmp_path: Path, error: BaseException
    ) -> None:
        state = CwState(sessions=[_live_daemon("a", tmp_path)])
        roster = _Roster(error=error)

        assert (
            prepass_phantom_ids(state, roster=roster, acting=frozenset(), now=_NOW)
            == set()
        )
        assert roster.calls == 1

    def test_empty_roster_with_live_surfaces_is_an_outage(self, tmp_path: Path) -> None:
        state = CwState(sessions=[_live_daemon("a", tmp_path)])

        assert (
            prepass_phantom_ids(state, roster=_Roster(), acting=frozenset(), now=_NOW)
            == set()
        )

    def test_phantoms_minus_acts_in_flight(self, tmp_path: Path) -> None:
        live = _live_daemon("live", tmp_path)
        dead = _live_daemon("dead", tmp_path)
        acting = _live_daemon("acting", tmp_path)
        state = CwState(sessions=[live, dead, acting])
        roster = _Roster(str(live.surface_ref))

        assert prepass_phantom_ids(
            state, roster=roster, acting=frozenset({"acting"}), now=_NOW
        ) == {"dead"}

    def test_spawn_grace_lookahead(self, tmp_path: Path) -> None:
        """A session 10 s inside the 30 s spawn grace is included (it will
        likely be a phantom by the time the lock is held); one 20 s inside
        it is not."""
        near = _live_daemon("near", tmp_path, started_at=_NOW - timedelta(seconds=20))
        fresh = _live_daemon("fresh", tmp_path, started_at=_NOW - timedelta(seconds=10))
        decoy = _live_daemon("decoy", tmp_path)
        state = CwState(sessions=[near, fresh, decoy])

        assert prepass_phantom_ids(
            state,
            roster=_Roster(str(decoy.surface_ref)),
            acting=frozenset(),
            now=_NOW,
        ) == {"near"}


class TestNoSubprocessUnderTheLock:
    def test_real_git_dirty_worktree_is_checked_once_with_the_lock_free(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end, with real git and a genuinely dirty worktree: the live
        check runs exactly once, lock-free, and the row parks. The autouse
        lock-invariant harness fails the test on any in-lock git."""
        worktree = make_git_repo("wt-real")
        (worktree / "unsaved.py").write_text("x = 1\n", encoding="utf-8")
        write_clients_yaml(ClientConfig(name=_CLIENT, workspace_path=tmp_path / "ws"))
        session = _backstop_session("real", tmp_path, worktree_path=worktree)
        _seed([session], [_row("real")])
        real_check = dirty_checks._shared.worktree_dirty_reason_by_path
        lock_free: list[bool] = []

        def _recording(client: str, path: Path | None) -> str | None:
            lock_free.append(probe_sessions_lock_free())
            return real_check(client, path)

        monkeypatch.setattr(_LIVE_HELPER, _recording)

        report = reconcile()

        assert lock_free == [True]
        assert report.reverted_ticket_ids == []
        row = _stored_row("real")
        assert row.status is QueueItemStatus.BLOCKED_ON_USER
        assert row.disposition == _DIRTY_WORKTREE_REASON
        (page,) = _attention_events("real", "real")
        assert "1 uncommitted path(s)" in str(page["breadcrumbs"])
