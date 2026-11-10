"""Tests for the lockless worktree dirty-check pre-pass (#2548).

``cw.reconcile.dirty_checks`` holds the captured checks. ``reconcile()``
fills one store before ``sessions_lock`` (backstop sessions with a RUNNING
row, then the crash-tail phantoms); the in-lock phantom detect and the
TIMED_OUT/COMPLETED backstops only look checks up. A miss defers the session
a tick and never parks it; only a completed check that found unsaved work
parks. The pre-pass wiring inside ``reconcile()`` and the roster-race miss
(M8) are in ``tests/test_reconcile_prepasses.py``; the in-lock adoption miss (M9)
is in ``tests/test_reconcile_unowned_running.py``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

import cw.config
from cw.config import dev_queue_file, load_state, save_state, state_file
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.exceptions import WorktreeError
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapReason,
    Session,
    SessionOrigin,
    SessionStatus,
    TicketTask,
    UsageLimitAct,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import _deps, dirty_checks, probe_store, reconcile
from cw.reconcile._shared import (
    _DIRTY_WORKTREE_REASON,
    ProposedAction,
    WorktreeProbeError,
    _worktree_dirty_reason_by_path,
)
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.dirty_checks import (
    DIRTY_CHECK_BUDGET_SECONDS,
    DIRTY_CHECK_LOOKAHEAD_SECONDS,
    DIRTY_CHECK_MAX_AGE_SECONDS,
    DIRTY_CHECK_MAX_PER_TICK,
    WORKTREE_PROBE_UNAVAILABLE_STATUS,
    DirtyCheckDeferredError,
    DirtyChecks,
    DirtyCheckUnavailableError,
    lookup_dirty_reason,
    partition_dirty,
    prepass_phantom_ids,
)
from cw.reconcile.phantom import (
    _act_on_phantom_candidates,
    _detect_phantom_candidates,
    capture_phantom_dirty_checks,
)
from cw.reconcile.roster_snapshot import RosterUnavailableError
from cw.reconcile.tasks import (
    backstop_targets,
    capture_backstop_dirty_checks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from cw.worktree import OnBranch, ProbeUnavailable
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._dirty_check_helpers import (
    DIRTY_CHECKS_LOGGER,
    assert_deferred,
    capture_backstops_from_disk,
    checks_with,
    detect_phantom_prefetched,
    probe_events,
    raising_helper,
    revert_completed_silent_prefetched,
    revert_timed_out_prefetched,
    unavailable_records,
)
from tests._reconcile_helpers import (
    _attention_events,
    _auto_config,
    _mk_phantom_daemon_session,
    _shipped_salvage_payload,
    _state_queue_snapshot,
    _write_salvage_transcript,
    probe_sessions_lock_free,
    stub_worktree_probe,
    use_reconcile_daemon,
)
from tests.conftest import _make_daemon_session, _make_ticket_task, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cw.reconcile._shared._worktree_evidence import WorktreeProbeKind
    from cw.worktree import BranchProbe

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

    def test_unexpected_helper_error_propagates_and_reverts_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2662: an unclassified error from the real helper is no longer a
        captured clean hit; it propagates and the row is left RUNNING."""
        _seed([_backstop_session("p3", tmp_path)], [_row("p3")])

        def _branch_boom(_path: Path) -> BranchProbe:
            msg = "branch lookup exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.reconcile._deps.probe_checked_out_branch", _branch_boom)

        with pytest.raises(RuntimeError, match="branch lookup exploded"):
            revert_timed_out_prefetched()
        assert _stored_row("p3").status is QueueItemStatus.RUNNING

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
            "cw.reconcile._deps.probe_checked_out_branch",
            lambda _p: OnBranch("auto-dev/p4"),
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


_UNRESOLVED = (
    "dirty check unavailable: checked-out branch could not be resolved"
    " (detached HEAD or not a git worktree)"
)
_PROBE_EVENT_KEYS = {
    "session_id",
    "session_name",
    "client",
    "ticket_id",
    "claude_session_id",
    "paused_status",
    "breadcrumbs",
    "crashed",
    "lane",
}


def _files_snapshot() -> tuple[bytes, bytes]:
    """The state and dev-queue file bytes (the events inbox is excluded)."""
    return state_file().read_bytes(), dev_queue_file().read_bytes()


def _deferred_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == DIRTY_CHECKS_LOGGER
        and r.getMessage().startswith("dirty_check_deferred: ")
    ]


def _write_bad_yaml() -> None:
    cw.config.clients_file().write_text("clients: [unclosed\n", encoding="utf-8")


def _write_top_level_list() -> None:
    cw.config.clients_file().write_text("- a\n- b\n", encoding="utf-8")


def _write_no_clients() -> None:
    write_clients_yaml()


# Upper bound on a thread handshake wait. A regression fails an assertion at
# this bound instead of hanging; a passing run never waits anywhere near it.
_HANDSHAKE_TIMEOUT_SECONDS = 10.0


class _BlockedEmit:
    """Hold the first ``record_event`` call open until released (#2662).

    Installs itself over ``dirty_checks.record_event``. The first call sets
    ``entered`` and waits for ``release``; every later call, and the first
    once released, forwards to the real recorder. Event-driven, no sleeps.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail_first_with: Exception | None = None,
    ) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._fail_first_with = fail_first_with
        self._real = dirty_checks.record_event
        self._guard = threading.Lock()
        self._calls = 0
        monkeypatch.setattr(dirty_checks, "record_event", self)

    def __call__(
        self,
        event_type: OrchestratorEventType,
        payload: dict[str, object],
        *,
        correlation_id: str | None,
    ) -> object:
        with self._guard:
            self._calls += 1
            first = self._calls == 1
        if first:
            self.entered.set()
            self.release.wait(timeout=_HANDSHAKE_TIMEOUT_SECONDS)
            if self._fail_first_with is not None:
                raise self._fail_first_with
        return self._real(event_type, payload, correlation_id=correlation_id)


class TestHelperFailureContract:
    """#2662: the real helper no longer reads its own errors as clean.

    A missing directory is clean; a transient or host-global cause defers the
    row and emits one edge-triggered signal; a deterministic per-row cause
    parks the row at once with the cause named. No clock is involved.
    """

    def test_missing_directory_reads_clean_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed([_backstop_session("p", tmp_path)], [_row("p")])

        def _gone(_path: Path) -> BranchProbe:
            return ProbeUnavailable(FileNotFoundError("no such directory"))

        monkeypatch.setattr("cw.reconcile._deps.probe_checked_out_branch", _gone)

        assert revert_timed_out_prefetched() == ["p"]
        assert _stored_row("p").status is QueueItemStatus.PENDING

    def test_existing_directory_clean_still_reverts_to_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("p", tmp_path)
        assert session.worktree_path is not None
        session.worktree_path.mkdir()
        _seed([session], [_row("p")])
        stub_worktree_probe(monkeypatch)

        assert revert_timed_out_prefetched() == ["p"]
        assert _stored_row("p").status is QueueItemStatus.PENDING

    def test_transient_failure_defers_and_signals_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("p", tmp_path)
        _seed([session], [_row("p")])
        raising_helper(monkeypatch, kind="git", detail="boom")
        before = _files_snapshot()

        assert revert_timed_out_prefetched() == []

        assert _files_snapshot() == before
        row = _stored_row("p")
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == "p"
        stored = _stored_session("p")
        assert stored.status is SessionStatus.TIMED_OUT
        assert stored.reap_reason is None
        assert _attention_events("p", "p") == []
        _push().assert_not_called()
        (event,) = probe_events("probe")
        assert set(event) == _PROBE_EVENT_KEYS
        assert event["ticket_id"] is None
        assert event["lane"] is None
        assert event["claude_session_id"] is None
        assert event["crashed"] is False
        assert event["session_name"] == ""
        assert event["paused_status"] == WORKTREE_PROBE_UNAVAILABLE_STATUS
        assert event["paused_status"] == "worktree_probe_unavailable"
        assert event["client"] == "client-a"
        assert re.match(
            r"^worktree-probe:git@\d{4}-\d\d-\d\dT", str(event["session_id"])
        )
        assert event["breadcrumbs"] == "git: boom"

    def test_signal_attributes_the_first_affected_sessions_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _backstop_session("a", tmp_path)
        second = _backstop_session("b", tmp_path, client="client-b")
        _seed([first, second], [_row("a"), _row("b", client="client-b")])
        raising_helper(monkeypatch, kind="git", detail="boom")

        checks = capture_backstops_from_disk()

        assert checks.captures == 2
        (event,) = probe_events("probe")
        assert event["client"] == "client-a"
        assert event["breadcrumbs"] == "git: boom"

    def test_signal_is_edge_triggered_across_ticks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed([_backstop_session("p", tmp_path)], [_row("p")])
        raising_helper(monkeypatch, kind="git")
        for _ in range(3):
            capture_backstops_from_disk()
        assert len(probe_events("after-3-failing")) == 1

        _live(monkeypatch)
        capture_backstops_from_disk()
        assert set() == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert len(probe_events("after-clean")) == 1

        raising_helper(monkeypatch, kind="git")
        capture_backstops_from_disk()
        assert len(probe_events("after-second-episode")) == 2

        raising_helper(monkeypatch, kind="client", detail="bad yaml")
        capture_backstops_from_disk()
        events = probe_events("after-other-kind")
        assert [e["breadcrumbs"] for e in events] == [
            "git: boom",
            "git: boom",
            "client: bad yaml",
        ]

    @pytest.mark.parametrize(
        "write_config",
        [_write_bad_yaml, _write_top_level_list, _write_no_clients],
        ids=["yaml-error", "validation-error", "no-clients"],
    )
    def test_unreadable_clients_yaml_defers_every_row_parks_nothing_signals_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        write_config: Callable[[], None],
    ) -> None:
        """R8: a global clients.yaml failure must never mass-park."""
        sessions = [
            _backstop_session("a1", tmp_path),
            _backstop_session("a2", tmp_path),
            _backstop_session("b1", tmp_path, client="client-b"),
        ]
        rows = [_row("a1"), _row("a2"), _row("b1", client="client-b")]
        _seed(sessions, rows)
        write_config()
        monkeypatch.setattr(
            "cw.reconcile._deps.probe_checked_out_branch",
            lambda _p: OnBranch("auto-dev/x"),
        )
        with pytest.raises(WorktreeProbeError) as info:
            _worktree_dirty_reason_by_path("client-a", tmp_path / "wt-a1")
        err = info.value

        assert revert_timed_out_prefetched() == []
        assert revert_timed_out_prefetched() == []

        for sid in ("a1", "a2", "b1"):
            row = _stored_row(sid)
            assert row.status is QueueItemStatus.RUNNING
            assert row.session_id == sid
            assert _attention_events(f"park-{sid}", sid) == []
        _push().assert_not_called()
        (event,) = probe_events("probe")
        assert err.kind == "client"
        assert event["breadcrumbs"] == f"{err.kind}: {err.detail}"

    @pytest.mark.parametrize(
        ("status", "revert"),
        [
            (SessionStatus.TIMED_OUT, revert_timed_out_prefetched),
            (SessionStatus.COMPLETED, revert_completed_silent_prefetched),
        ],
        ids=["timed-out", "completed"],
    )
    def test_unresolved_branch_parks_immediately_naming_cause(
        self,
        tmp_path: Path,
        status: SessionStatus,
        revert: Callable[[], list[str]],
    ) -> None:
        """A real non-git directory: the strict lookup reads it unresolved."""
        session = _backstop_session("u", tmp_path, status=status)
        assert session.worktree_path is not None
        session.worktree_path.mkdir()
        _seed([session], [_row("u")])

        assert revert() == []

        row = _stored_row("u")
        assert row.status is QueueItemStatus.BLOCKED_ON_USER
        assert row.disposition == _DIRTY_WORKTREE_REASON
        assert row.session_id is None
        (page,) = _attention_events("u-tick1", "u")
        assert page["breadcrumbs"] == f"{session.worktree_path}: {_UNRESOLVED}"
        _push().assert_called_once()

        assert revert() == []

        assert len(_attention_events("u-tick2", "u")) == 1
        _push().assert_called_once()
        assert probe_events("probe") == []

    @pytest.mark.parametrize("auto", [True, False], ids=["auto", "signal-only"])
    def test_unresolved_branch_phantom_parks_regardless_of_reap_policy(
        self, tmp_path: Path, auto: bool
    ) -> None:
        phantom = _phantom("ph", tmp_path)
        assert phantom.worktree_path is not None
        phantom.worktree_path.mkdir()
        config = _auto_config() if auto else OrchestratorConfig()

        (candidate,) = detect_phantom_prefetched(
            CwState(sessions=[phantom]), {phantom.id}, now=_STARTED, config=config
        )

        assert candidate.worktree_dirty is True
        assert candidate.worktree_dirty_reason == _UNRESOLVED

    def test_unknown_client_parks_immediately_naming_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path / "ws"))
        session = _backstop_session("g", tmp_path, client="ghost")
        _seed([session], [_row("g", client="ghost")])
        monkeypatch.setattr(
            "cw.reconcile._deps.probe_checked_out_branch",
            lambda _p: OnBranch("auto-dev/g"),
        )

        assert revert_timed_out_prefetched() == []

        row = _stored_row("g")
        assert row.status is QueueItemStatus.BLOCKED_ON_USER
        (page,) = _attention_events("g", "g")
        assert page["breadcrumbs"] == (
            f"{session.worktree_path}: dirty check unavailable:"
            " client 'ghost' is not in clients.yaml"
        )
        assert probe_events("probe") == []

    def test_capture_all_continues_past_deferred_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sessions = [_backstop_session(sid, tmp_path) for sid in ("a", "b", "c")]
        raising_helper(monkeypatch, kind="git", detail="boom", only={"wt-b"})
        checks = DirtyChecks()

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            checks.capture_all(sessions)

        assert checks.captures == 3
        assert checks.lookup(sessions[0]) is None
        assert checks.lookup(sessions[2]) is None
        with pytest.raises(DirtyCheckUnavailableError):
            checks.lookup(sessions[1])
        assert _deferred_lines(caplog) == [
            "dirty_check_deferred: session=b name=client-a/auto-dev/b kind=git"
            " detail=boom; deferring to the next tick"
        ]
        assert _stop_lines(caplog) == []

    def test_deferred_session_consumes_a_cap_slot_and_pass_still_stops_at_cap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sessions = [_backstop_session(sid, tmp_path) for sid in ("a", "b", "c")]
        raising_helper(monkeypatch, kind="git", only={"wt-a"})
        checks = DirtyChecks(max_captures=2)

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            checks.capture_all(sessions)

        assert checks.captures == 2
        assert checks.lookup(sessions[1]) is None
        with pytest.raises(DirtyCheckUnavailableError):
            checks.lookup(sessions[2])
        (line,) = _stop_lines(caplog)
        assert "1 of 3 session(s) left unchecked" in line

    def test_previously_deferred_sessions_sort_after_healthy_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R11: a persistently failing row cannot starve healthy ones."""
        failing, healthy, starved = (
            _backstop_session(sid, tmp_path) for sid in ("A", "B", "C")
        )
        sessions = [failing, healthy, starved]
        raising_helper(monkeypatch, kind="git", only={"wt-A"})

        tick1 = DirtyChecks(max_captures=2)
        tick1.capture_all(sessions)
        tick1.settle_outages()
        with pytest.raises(DirtyCheckUnavailableError):
            tick1.lookup(starved)
        assert {"A"} == dirty_checks._DEFERRED_LAST_TICK

        tick2 = DirtyChecks(max_captures=2)
        tick2.capture_all(sessions)

        assert tick2.captures == 2
        assert tick2.lookup(healthy) is None
        assert tick2.lookup(starved) is None
        with pytest.raises(DirtyCheckUnavailableError):
            tick2.lookup(failing)

        dirty_checks._DEFERRED_LAST_TICK.clear()
        fresh = DirtyChecks(max_captures=1)
        fresh.capture_all(sessions)
        with pytest.raises(DirtyCheckUnavailableError):
            fresh.lookup(healthy)

    def test_deferred_ids_are_replaced_each_settle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first, second = (_backstop_session(sid, tmp_path) for sid in ("A", "B"))
        raising_helper(monkeypatch, kind="git", only={"wt-A"})
        tick1 = DirtyChecks()
        tick1.capture_all([first, second])
        tick1.settle_outages()
        assert {"A"} == dirty_checks._DEFERRED_LAST_TICK

        _live(monkeypatch)
        tick2 = DirtyChecks()
        tick2.capture_all([first, second])
        tick2.settle_outages()
        assert set() == dirty_checks._DEFERRED_LAST_TICK

        tick3 = DirtyChecks(max_captures=1)
        tick3.capture_all([first, second])
        assert tick3.lookup(first) is None
        with pytest.raises(DirtyCheckUnavailableError):
            tick3.lookup(second)

    def test_deferred_error_is_a_dirty_check_unavailable_error_and_stores_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("a", tmp_path)
        raising_helper(monkeypatch, kind="git", detail="boom")
        checks = DirtyChecks()

        with pytest.raises(DirtyCheckDeferredError) as info:
            checks.capture(session)

        assert isinstance(info.value, DirtyCheckUnavailableError)
        assert info.value.kind == "git"
        assert info.value.detail == "boom"
        with pytest.raises(DirtyCheckUnavailableError, match="no dirty check"):
            checks.lookup(session)

    def test_capped_pass_does_not_end_the_episode_or_reset_deferred_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = [_backstop_session(sid, tmp_path) for sid in ("F", "h1", "h2")]
        raising_helper(monkeypatch, kind="git", only={"wt-F"})
        for _ in range(3):
            checks = DirtyChecks(max_captures=2)
            checks.capture_all(sessions)
            checks.settle_outages()
            assert {"git"} == set(dirty_checks._ANNOUNCED_OUTAGES)
            assert {"F"} == dirty_checks._DEFERRED_LAST_TICK
        assert len(probe_events("capped")) == 1

        _live(monkeypatch)
        checks = DirtyChecks()
        checks.capture_all(sessions)
        checks.settle_outages()

        assert set() == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert set() == dirty_checks._DEFERRED_LAST_TICK
        assert len(probe_events("recovered")) == 1

    def test_signal_emit_oserror_leaves_latch_open_and_retries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = _backstop_session("a", tmp_path)
        raising_helper(monkeypatch, kind="git", detail="boom")
        real_record = dirty_checks.record_event
        failures = [OSError("inbox is read-only")]

        def _flaky(
            event_type: OrchestratorEventType,
            payload: dict[str, object],
            *,
            correlation_id: str | None,
        ) -> object:
            if failures:
                raise failures.pop()
            return real_record(event_type, payload, correlation_id=correlation_id)

        monkeypatch.setattr(dirty_checks, "record_event", _flaky)

        first = DirtyChecks()
        first.capture_all([session])
        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            first.settle_outages()

        assert set() == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert probe_events("first") == []
        assert any(
            "worktree_probe_unavailable" in r.getMessage()
            and "inbox is read-only" in r.getMessage()
            for r in caplog.records
            if r.name == DIRTY_CHECKS_LOGGER
        )

        second = DirtyChecks()
        second.capture_all([session])
        second.settle_outages()

        assert {"git"} == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert len(probe_events("second")) == 1

    def test_concurrent_settles_announce_one_episode_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("a", tmp_path)
        raising_helper(monkeypatch, kind="git", detail="boom")
        first, second = DirtyChecks(), DirtyChecks()
        first.capture_all([session])
        second.capture_all([session])
        emit = _BlockedEmit(monkeypatch)

        emitter = threading.Thread(target=first.settle_outages, daemon=True)
        emitter.start()
        assert emit.entered.wait(timeout=_HANDSHAKE_TIMEOUT_SECONDS)
        # The first settle has claimed "git" and is mid-emit; the second sees
        # the claim and stays silent.
        second.settle_outages()
        emit.release.set()
        emitter.join(timeout=_HANDSHAKE_TIMEOUT_SECONDS)

        assert not emitter.is_alive()
        assert len(probe_events("once")) == 1
        assert {"git"} == set(dirty_checks._ANNOUNCED_OUTAGES)

    def test_clean_settle_during_emission_is_not_undone_by_a_late_latch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("a", tmp_path)
        raising_helper(monkeypatch, kind="git", detail="boom")
        failing = DirtyChecks()
        failing.capture_all([session])
        emit = _BlockedEmit(monkeypatch)

        emitter = threading.Thread(target=failing.settle_outages, daemon=True)
        emitter.start()
        assert emit.entered.wait(timeout=_HANDSHAKE_TIMEOUT_SECONDS)
        # A clean tick lands while the emission is in flight and ends the
        # episode.
        DirtyChecks().settle_outages()
        assert set() == set(dirty_checks._ANNOUNCED_OUTAGES)
        emit.release.set()
        emitter.join(timeout=_HANDSHAKE_TIMEOUT_SECONDS)

        assert not emitter.is_alive()
        assert set() == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert len(probe_events("first")) == 1

        # The next real outage is a new episode and announces again.
        next_outage = DirtyChecks()
        next_outage.capture_all([session])
        next_outage.settle_outages()

        assert {"git"} == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert len(probe_events("second")) == 2

    def test_failed_emit_does_not_undo_a_later_settles_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("a", tmp_path)
        raising_helper(monkeypatch, kind="git", detail="boom")
        settle_a, settle_c = DirtyChecks(), DirtyChecks()
        settle_a.capture_all([session])
        settle_c.capture_all([session])
        emit = _BlockedEmit(monkeypatch, fail_first_with=OSError("inbox is read-only"))

        failing = threading.Thread(target=settle_a.settle_outages, daemon=True)
        failing.start()
        assert emit.entered.wait(timeout=_HANDSHAKE_TIMEOUT_SECONDS)
        # A clean tick ends the episode, then a new outage claims the kind and
        # announces it, all while A's emit is still in flight.
        DirtyChecks().settle_outages()
        settle_c.settle_outages()
        assert len(probe_events("c")) == 1
        emit.release.set()
        failing.join(timeout=_HANDSHAKE_TIMEOUT_SECONDS)

        # A's late failure must not discard C's claim: the episode is still
        # latched and a further tick in it stays silent.
        assert not failing.is_alive()
        assert {"git"} == set(dirty_checks._ANNOUNCED_OUTAGES)
        later = DirtyChecks()
        later.capture_all([session])
        later.settle_outages()
        assert len(probe_events("later")) == 1

    def test_non_oserror_emit_failure_propagates_and_unlatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _backstop_session("a", tmp_path)
        raising_helper(monkeypatch, kind="git", detail="boom")
        real_record = dirty_checks.record_event
        failures = [RuntimeError("recorder bug")]

        def _broken(
            event_type: OrchestratorEventType,
            payload: dict[str, object],
            *,
            correlation_id: str | None,
        ) -> object:
            if failures:
                raise failures.pop()
            return real_record(event_type, payload, correlation_id=correlation_id)

        monkeypatch.setattr(dirty_checks, "record_event", _broken)

        first = DirtyChecks()
        first.capture_all([session])
        with pytest.raises(RuntimeError, match="recorder bug"):
            first.settle_outages()

        assert set() == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert probe_events("first") == []

        # The episode was never announced, so the next failing tick does.
        second = DirtyChecks()
        second.capture_all([session])
        second.settle_outages()

        assert {"git"} == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert len(probe_events("second")) == 1

    @staticmethod
    def _two_kinds_failing(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> list[Session]:
        """Four sessions, two per kind: a/c fail ``git``, b/d fail ``client``.

        Each session is its own client, and its probe raises with the detail
        ``down-<sid>``, so an event names the session that hit its kind first.
        """
        kinds: dict[str, WorktreeProbeKind] = {
            "a": "git",
            "b": "client",
            "c": "git",
            "d": "client",
        }
        sessions = [
            _backstop_session(sid, tmp_path, client=f"client-{sid}") for sid in kinds
        ]

        def _raise(_client: str, path: Path | None) -> str | None:
            assert path is not None
            sid = path.name.removeprefix("wt-")
            raise WorktreeProbeError(kinds[sid], f"down-{sid}")

        monkeypatch.setattr(_LIVE_HELPER, _raise)
        return sessions

    def test_two_kinds_in_one_tick_emit_in_sorted_order_once_each(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = self._two_kinds_failing(monkeypatch, tmp_path)

        checks = DirtyChecks()
        checks.capture_all(sessions)
        checks.settle_outages()

        events = probe_events("both-kinds")
        assert [e["breadcrumbs"] for e in events] == [
            "client: down-b",
            "git: down-a",
        ]
        assert [e["client"] for e in events] == ["client-b", "client-a"]
        assert {"client", "git"} == set(dirty_checks._ANNOUNCED_OUTAGES)

    def test_first_emit_failure_releases_the_failed_and_the_unemitted_kind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = self._two_kinds_failing(monkeypatch, tmp_path)
        real_record = dirty_checks.record_event
        failures = [RuntimeError("recorder bug")]

        def _broken(
            event_type: OrchestratorEventType,
            payload: dict[str, object],
            *,
            correlation_id: str | None,
        ) -> object:
            if failures:
                raise failures.pop()
            return real_record(event_type, payload, correlation_id=correlation_id)

        monkeypatch.setattr(dirty_checks, "record_event", _broken)

        first = DirtyChecks()
        first.capture_all(sessions)
        with pytest.raises(RuntimeError, match="recorder bug"):
            first.settle_outages()

        # ``client`` (index 0) failed and ``git`` was never emitted: neither
        # stays latched, so neither episode is silently suppressed.
        assert set() == set(dirty_checks._ANNOUNCED_OUTAGES)
        assert probe_events("first") == []

        second = DirtyChecks()
        second.capture_all(sessions)
        second.settle_outages()

        events = probe_events("second")
        assert sorted(str(e["breadcrumbs"]).partition(":")[0] for e in events) == [
            "client",
            "git",
        ]
        assert {"client", "git"} == set(dirty_checks._ANNOUNCED_OUTAGES)


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
            subprocess.TimeoutExpired(["claude"], 10),
            # #2550: the snapshot-bound shared reader raises this on a failed read.
            RosterUnavailableError("x"),
        ],
        ids=["called-process", "json", "missing", "timeout", "snapshot-unavailable"],
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


_WIP = "wip.py"
_WIP_BODY = "unsaved = True\n"


def _write_unsaved_work(repo: Path) -> tuple[str, str]:
    """Write an untracked file into *repo*; return its (status, HEAD) snapshot."""
    (repo / _WIP).write_text(_WIP_BODY, encoding="utf-8")
    return git_in(repo, "status", "--porcelain"), git_in(repo, "rev-parse", "HEAD")


def _assert_intact(repo: Path, before: tuple[str, str]) -> None:
    assert repo.is_dir()
    assert (repo / _WIP).read_text(encoding="utf-8") == _WIP_BODY
    assert before[0] == f"?? {_WIP}"
    after = git_in(repo, "status", "--porcelain"), git_in(repo, "rev-parse", "HEAD")
    assert after == before


class TestStaleCleanCaptureDestroysNothing:
    """#2630: no reconcile act removes a worktree; for the phantom sweep and
    backstops the capture only selects the queue disposition.

    A real worktree dirtied after an in-bound clean capture keeps its files.
    The in-window PENDING routing is an accepted residual (#2671; see the
    ADR-0006 Amendment #2630 "Accepted residual" bullet). Removal primitives
    are not patched: callers bind them at import time and reconcile references
    none of them, so such patches would be vacuous. The
    on-disk assertions plus ``tests/test_reconcile_worktree_removal_guard.py``
    are the protection.
    """

    def test_backstop_revert_on_stale_clean_capture_leaves_dirtied_worktree_intact(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("wt-x")
        session = _backstop_session("x", tmp_path, worktree_path=repo)
        _seed([session], [_row("x")])
        checks = checks_with([session], {"x": None})
        before = _write_unsaved_work(repo)
        _forbid_live(monkeypatch)

        reverted = revert_timed_out_tasks(checks)

        _assert_intact(repo, before)
        # Accepted residual (#2671, ADR-0006 Amendment #2630): an in-bound clean
        # capture routes the dirtied tree's row to PENDING. Closing the window
        # was weighed and rejected there; this assertion pins the accepted
        # behavior.
        assert reverted == ["x"]
        assert _stored_row("x").status is QueueItemStatus.PENDING

    def test_phantom_act_on_stale_clean_capture_leaves_dirtied_worktree_intact(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = make_git_repo("wt-px")
        phantom = _phantom("px", tmp_path, worktree_path=repo)
        _seed([phantom], [_row("px")])
        checks = checks_with([phantom], {"px": None})
        before = _write_unsaved_work(repo)
        _forbid_live(monkeypatch)
        deferred = DeferredReconcileJobs()

        candidates = _detect_phantom_candidates(
            load_state(), {phantom.id}, now=_STARTED, dirty_checks=checks
        )
        (candidate,) = candidates
        assert candidate.worktree_dirty is False
        reverted, *_ = _act_on_phantom_candidates(
            load_state(),
            candidates,
            now=_STARTED,
            config=_auto_config(),
            deferred=deferred,
        )

        _assert_intact(repo, before)
        # A plain crash candidate queues no surface stop: no job could touch it.
        assert deferred.post_lock == []
        # Accepted residual (#2671, ADR-0006 Amendment #2630): an in-bound clean
        # capture routes the dirtied tree's row to PENDING. Closing the window
        # was weighed and rejected there; this assertion pins the accepted
        # behavior.
        assert reverted == ["px"]
        assert _stored_row("px").status is QueueItemStatus.PENDING

    def test_merged_phantom_stop_is_post_lock_and_does_not_depend_on_the_dirt_value(
        self,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Post-lock surface stops do not read or depend on the dirt value (a
        merged phantom's candidacy still requires a usable capture; a miss
        defers it). A DIRTY capture reaches the act through the real detect;
        only the drain stops the surface, the live helper is never hit, and the
        worktree's files stay intact."""
        daemon = FakeNativeDaemonClient()
        use_reconcile_daemon(monkeypatch, daemon)
        repo = make_git_repo("wt-mp")
        phantom = _phantom("mp", tmp_path, worktree_path=repo)
        _seed([phantom], [_row("mp")])
        before = _write_unsaved_work(repo)
        checks = checks_with([phantom], {"mp": _DIRTY})
        _forbid_live(monkeypatch)
        sink = DeferredReconcileJobs()

        (candidate,) = _detect_phantom_candidates(
            load_state(), {phantom.id}, now=_STARTED, dirty_checks=checks
        )
        assert candidate.worktree_dirty is True
        _act_on_phantom_candidates(
            load_state(),
            [candidate],
            now=_STARTED,
            config=_auto_config(),
            merged_ticket_ids=frozenset({"mp"}),
            deferred=sink,
        )

        assert [job.label for job in sink.post_lock] == ["surface_stop:dead-ref"]
        assert daemon.stop_calls == []
        assert _stored_session("mp").status is SessionStatus.COMPLETED

        run_post_lock_jobs(sink)

        assert daemon.stop_calls == ["dead-ref"]
        _assert_intact(repo, before)


class TestMergedPhantomNeedsAUsableCapture:
    @pytest.mark.parametrize("store", ["none", "empty"])
    def test_merged_phantom_without_a_usable_capture_is_not_a_candidate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        store: str,
    ) -> None:
        """Merged-ness is a function of the ticket id and is assigned AFTER
        detect by ``_split_crash_candidates`` (detect takes no merged input), so
        a merged phantom with no usable capture is dropped for the tick and
        never reaches the act. The detect assertion IS the pin (the same one
        ``TestMissSources._phantom_deferred`` makes), kept as the named pin for
        "a merged phantom's candidacy still requires a usable capture"."""
        phantom = _phantom("mp", tmp_path)
        _seed([phantom], [])
        _forbid_live(monkeypatch)
        state = load_state()
        checks = None if store == "none" else DirtyChecks()

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            candidates = _detect_phantom_candidates(
                state, {phantom.id}, now=_STARTED, dirty_checks=checks
            )

        assert candidates == []
        assert _stored_session(phantom.id).status is SessionStatus.ACTIVE
        assert len(unavailable_records(caplog, phantom.id)) == 1
