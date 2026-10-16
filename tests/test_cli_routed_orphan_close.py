"""Tests for cw.cli.routed_orphan_close (#2517).

``cw dev-queue approve`` (after the transition) and ``requeue`` (before it)
close a #2458 orphan -- a session whose result was already routed but that a
Stop with background work left ACTIVE -- only once its worker is confirmed
gone from the daemon roster, and fail closed otherwise.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.exceptions import CwError
from cw.history import EventType, load_history
from cw.models import (
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    QueueItemStatus,
    Session,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.liveness_page import close_command
from cw.worktree import live_home_reason
from tests._reconcile_helpers import (
    LockProbeDaemon,
    _mk_routed_session,
    _write_agent_spawn_stamp,
)
from tests.conftest import (
    _make_daemon_session,
    _make_ticket_task,
    _stop_leaves_worker_listed,
    _stop_makes_roster_unreadable,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.native_daemon import NativeDaemonClient

_MODULE = "cw.cli.routed_orphan_close"
_TICKET = "2517"
_CLIENT = "client-a"
_NAME = f"{_CLIENT}/auto-dev/{_TICKET}"

_AFTER_TRANSITION = pytest.mark.parametrize("after_transition", [False, True])


def _seed_orphans(
    tmp_path: Path,
    daemon: FakeNativeDaemonClient,
    *sids: str,
    live: bool = True,
    extra: tuple[Session, ...] = (),
) -> list[Session]:
    """Persist marker sessions for the #2517 ticket (plus *extra*); return them.

    Hermetic (N5): each worktree lives under *tmp_path*, and a live session's
    surface is the short id ``seed_live_worker(worktree)`` registered.
    """
    sessions: list[Session] = []
    for sid in sids or ("orphan-1",):
        worktree = tmp_path / f"wt-{sid}"
        surface = daemon.seed_live_worker(worktree) if live else f"gone-{sid}"
        sess = _mk_routed_session(sid, worktree, surface_ref=surface)
        sess.name = _NAME
        sessions.append(sess)
    save_state(CwState(sessions=[*sessions, *extra]))
    return sessions


def _bystander(tmp_path: Path, *, surface_ref: str = "bystander") -> Session:
    """A live DAEMON session for another ticket, not a #2517 candidate."""
    return _make_daemon_session(
        id="bystander",
        name=f"{_CLIENT}/auto-dev/9999",
        client=_CLIENT,
        worktree_path=tmp_path / "wt-bystander",
        surface_ref=surface_ref,
    )


def _seed_row(
    session_id: str,
    *,
    status: QueueItemStatus = QueueItemStatus.RUNNING,
    ticket_id: str = _TICKET,
) -> None:
    """Persist a single queue row bound to *session_id*."""
    row = _make_ticket_task(
        ticket_id=ticket_id, client=_CLIENT, status=status, session_id=session_id
    )
    save_dev_queue(DevQueueStore(tasks=[row]))


def _close(
    daemon: NativeDaemonClient | None,
    *,
    after_transition: bool,
    precheck: Callable[[frozenset[str]], None] | None = None,
) -> list[str]:
    from cw.cli.routed_orphan_close import close_routed_result_sessions_for_ticket

    return close_routed_result_sessions_for_ticket(
        _TICKET,
        _CLIENT,
        command="approve" if after_transition else "requeue",
        config=OrchestratorConfig(),
        after_transition=after_transition,
        native_daemon=daemon,
        precheck=precheck,
    )


def _status_of(session_id: str) -> SessionStatus:
    return next(s for s in load_state().sessions if s.id == session_id).status


def _records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(f"{event}:")]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _stamp(sess: Session, *, minutes_ago: float) -> None:
    assert sess.worktree_path is not None
    _write_agent_spawn_stamp(
        sess.worktree_path,
        unresolved_count=1,
        stamped_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


def _wipe_roster_on_stop(
    monkeypatch: pytest.MonkeyPatch, daemon: FakeNativeDaemonClient
) -> None:
    """A stop that empties the whole roster (a daemon restart mid-stop)."""

    def _wipe(short_id: str) -> None:
        daemon.stop_calls.append(short_id)
        daemon._live.clear()

    monkeypatch.setattr(daemon, "stop", _wipe)


@pytest.fixture(autouse=True)
def _instant_roster_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_MODULE}._ROUTED_STOP_CONFIRM_TIMEOUT_SECS", 0.0)
    monkeypatch.setattr(f"{_MODULE}._ROUTED_STOP_CONFIRM_INTERVAL_SECS", 0.0)


@pytest.fixture
def orphan_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """``caplog`` capturing this module's WARNING lines (N2: never INFO)."""
    caplog.set_level(logging.WARNING, logger=_MODULE)
    return caplog


# -- the close itself --------------------------------------------------------


@_AFTER_TRANSITION
def test_closes_matching_orphan_once_outside_the_lock(
    tmp_path: Path,
    orphan_logs: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    after_transition: bool,
) -> None:
    daemon = LockProbeDaemon()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    closed = _close(daemon, after_transition=after_transition)

    assert closed == [orphan.id]
    assert daemon.stop_calls == [orphan.surface_ref]
    # One stop, with sessions_lock free and the session still ACTIVE: the
    # helper's own stop precedes the flip, and _spawn_close_impl adds none.
    assert daemon.probes == [("free", {orphan.id: SessionStatus.ACTIVE})]
    stored = next(s for s in load_state().sessions if s.id == orphan.id)
    assert stored.status == SessionStatus.COMPLETED
    assert stored.completed_reason == CompletionReason.USER
    (record,) = _records(orphan_logs, "routed_orphan_closed_on_resolve")
    assert record.levelno == logging.WARNING
    command = "approve" if after_transition else "requeue"
    assert record.getMessage() == (
        "routed_orphan_closed_on_resolve: ticket_id=2517 client=client-a"
        f" session_id={orphan.id} command={command}"
    )
    assert (
        f"Closed orphaned routed-result session {orphan.id} for 2517 (client-a)."
        in capsys.readouterr().out
    )


def test_close_persists_durable_audit_payload(
    tmp_path: Path,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    assert _close(daemon, after_transition=True) == [orphan.id]

    (event,) = load_history(_CLIENT)
    assert event.event_type is EventType.SESSION_COMPLETED
    assert event.timestamp is not None
    assert event.metadata == {
        "actor": "operator",
        "command": "approve",
        "ticket_id": _TICKET,
        "client": _CLIENT,
        "session_id": orphan.id,
        "surface_ref": orphan.surface_ref,
        "prior_status": SessionStatus.ACTIVE.value,
        "resulting_status": SessionStatus.COMPLETED.value,
        "confirmation_result": "worker_gone_from_roster",
        "reason": "routed_result_orphan_resolved",
    }


def test_close_surfaces_audit_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _fail(*_args: object, **_kwargs: object) -> None:
        msg = "history unavailable"
        raise OSError(msg)

    monkeypatch.setattr(f"{_MODULE}.record_event", _fail)

    with pytest.raises(CwError, match="durable audit record could not be written"):
        _close(daemon, after_transition=True)

    assert _status_of(orphan.id) == SessionStatus.COMPLETED


def test_leaves_unrelated_sessions_and_other_tickets_alone(tmp_path: Path) -> None:
    daemon = FakeNativeDaemonClient()
    other_ticket = _mk_routed_session(
        "other-ticket",
        tmp_path / "wt-other-ticket",
        surface_ref=daemon.seed_live_worker(tmp_path / "wt-other-ticket"),
    )
    other_client = _mk_routed_session(
        "other-client",
        tmp_path / "wt-other-client",
        surface_ref=daemon.seed_live_worker(tmp_path / "wt-other-client"),
    )
    other_client.name = f"client-b/auto-dev/{_TICKET}"
    other_client.client = "client-b"
    (orphan,) = _seed_orphans(tmp_path, daemon, extra=(other_ticket, other_client))

    assert _close(daemon, after_transition=True) == [orphan.id]

    assert daemon.stop_calls == [orphan.surface_ref]
    assert _status_of("other-ticket") == SessionStatus.ACTIVE
    assert _status_of("other-client") == SessionStatus.ACTIVE


def test_no_candidates_touches_no_daemon_roster_or_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> NativeDaemonClient:
        pytest.fail("no candidate: nothing may be resolved or read")

    class _NoRosterDaemon(FakeNativeDaemonClient):
        def list_live_session_short_ids_fail_closed(self) -> set[str] | None:
            pytest.fail("no candidate: the roster must not be read")

    monkeypatch.setattr(f"{_MODULE}.get_native_daemon_client", _fail)
    monkeypatch.setattr(f"{_MODULE}.load_dev_queue", _fail)
    save_state(CwState(sessions=[_bystander(tmp_path)]))
    precheck_calls: list[frozenset[str]] = []

    assert _close(None, after_transition=True) == []
    assert (
        _close(
            _NoRosterDaemon(), after_transition=False, precheck=precheck_calls.append
        )
        == []
    )

    assert precheck_calls == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_lazy_client_resolution_with_a_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    monkeypatch.setattr(f"{_MODULE}.get_native_daemon_client", lambda: daemon)

    assert _close(None, after_transition=True) == [orphan.id]
    assert daemon.stop_calls == [orphan.surface_ref]


def test_queue_rows_are_never_written(tmp_path: Path) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _seed_row(orphan.id, status=QueueItemStatus.BLOCKED_ON_USER)
    before = load_dev_queue().model_dump()

    assert _close(daemon, after_transition=False) == [orphan.id]

    assert load_dev_queue().model_dump() == before


# -- unreadable / untrusted roster before any stop ----------------------------


def test_roster_unreadable_before_stop_closes_nothing(
    tmp_path: Path,
    orphan_logs: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    daemon.roster_unreadable = True

    assert _close(daemon, after_transition=False) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    err = capsys.readouterr().err
    assert orphan.id in err
    assert "the daemon roster is unreadable" in err
    assert close_command(orphan.id) in err
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert record.levelno == logging.WARNING
    assert f"session_ids={orphan.id}" in record.getMessage()
    assert "reason=roster_unreadable" in record.getMessage()


def test_roster_read_error_is_treated_as_unreadable(tmp_path: Path) -> None:
    class _RaisingRosterDaemon(FakeNativeDaemonClient):
        def list_live_session_short_ids_fail_closed(self) -> set[str] | None:
            msg = "malformed roster"
            raise ValueError(msg)

    daemon = _RaisingRosterDaemon()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    assert _close(daemon, after_transition=True) == []
    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE


@_AFTER_TRANSITION
def test_roster_absent_orphan_is_flipped_without_a_stop(
    tmp_path: Path, orphan_logs: pytest.LogCaptureFixture, after_transition: bool
) -> None:
    """R3: a readable roster without the worker proves there is none to stop;
    the flip alone clears ``worktree_occupied``."""
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon, live=False)
    assert orphan.worktree_path is not None
    assert live_home_reason(orphan.worktree_path, daemon=daemon) is not None

    assert _close(daemon, after_transition=after_transition) == [orphan.id]

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.COMPLETED
    (record,) = _records(orphan_logs, "routed_orphan_closed_on_resolve")
    assert record.levelno == logging.WARNING
    assert live_home_reason(orphan.worktree_path, daemon=daemon) is None


def test_flip_only_skipped_when_two_roster_reads_disagree(
    tmp_path: Path,
    orphan_logs: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A roster absence must be corroborated by a second read (soundness RISK:
    a partial roster mid-rewrite must not free the worktree)."""

    class _PartialRosterDaemon(FakeNativeDaemonClient):
        def __init__(self, reads: list[set[str]]) -> None:
            super().__init__()
            self.reads = reads

        def list_live_session_short_ids_fail_closed(self) -> set[str] | None:
            return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    (orphan,) = _seed_orphans(tmp_path, FakeNativeDaemonClient(), live=False)
    assert orphan.surface_ref is not None
    daemon = _PartialRosterDaemon(
        [{"unrelated"}, {"unrelated"}, {"unrelated", orphan.surface_ref}]
    )

    assert _close(daemon, after_transition=False) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    err = capsys.readouterr().err
    assert "changed between two reads" in err
    assert close_command(orphan.id) in err
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=roster_unreadable" in record.getMessage()


def test_empty_roster_with_other_live_session_is_not_flipped(
    tmp_path: Path,
    orphan_logs: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """N1 (a): an empty roster while another session is recorded live is the
    daemon-restart signature, never proof the worker is gone."""
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(
        tmp_path, daemon, live=False, extra=(_bystander(tmp_path),)
    )

    assert _close(daemon, after_transition=False) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    err = capsys.readouterr().err
    assert orphan.id in err
    assert "looks like a daemon restart" in err
    assert close_command(orphan.id) in err
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=roster_unreadable" in record.getMessage()


def test_absent_roster_file_with_other_live_session_is_not_flipped(
    tmp_path: Path,
) -> None:
    """N1 (b): an absent roster file reads as an empty set."""
    from cw.native_daemon import RealNativeDaemonClient

    (orphan,) = _seed_orphans(
        tmp_path, FakeNativeDaemonClient(), live=False, extra=(_bystander(tmp_path),)
    )
    daemon = RealNativeDaemonClient(roster_path=tmp_path / "missing.json")

    assert _close(daemon, after_transition=True) == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE


# -- the post-stop roster check (N1 + the pre-stop snapshot) -------------------


def test_stop_that_empties_whole_roster_with_other_live_worker_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1 (d): a roster that held another live worker before the stop and reads
    empty after it is the restart signature, not a confirmation."""
    daemon = FakeNativeDaemonClient()
    bystander = _bystander(
        tmp_path, surface_ref=daemon.seed_live_worker(tmp_path / "wt-bystander")
    )
    (orphan,) = _seed_orphans(tmp_path, daemon, extra=(bystander,))
    _wipe_roster_on_stop(monkeypatch, daemon)

    with pytest.raises(CwError, match="unreadable after the stop"):
        _close(daemon, after_transition=False)

    assert daemon.stop_calls == [orphan.surface_ref]
    assert _status_of(orphan.id) == SessionStatus.ACTIVE


def test_stale_unrelated_session_and_lone_orphan_worker_closes_cleanly(
    tmp_path: Path,
) -> None:
    """Soundness note: a stale ACTIVE session whose worker is long gone must
    not turn the orphan's own stop -- which empties a roster that only ever
    held the orphan -- into a refusal that can never recover."""
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon, extra=(_bystander(tmp_path),))

    assert _close(daemon, after_transition=False) == [orphan.id]

    assert daemon.stop_calls == [orphan.surface_ref]
    assert _status_of(orphan.id) == SessionStatus.COMPLETED
    assert _status_of("bystander") == SessionStatus.ACTIVE


def test_mixed_stop_and_flip_only_candidates_both_close(tmp_path: Path) -> None:
    """N1 (e): the outage exclusion is the whole candidate set, so the
    still-ACTIVE flip-only candidate never makes the stop look like an
    outage."""
    daemon = FakeNativeDaemonClient()
    live, absent = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")
    assert absent.surface_ref is not None
    daemon._live.discard(absent.surface_ref)

    closed = _close(daemon, after_transition=False)

    assert sorted(closed) == ["orphan-a", "orphan-b"]
    assert daemon.stop_calls == [live.surface_ref]
    assert _status_of("orphan-a") == SessionStatus.COMPLETED
    assert _status_of("orphan-b") == SessionStatus.COMPLETED


def test_mixed_candidates_with_third_live_worker_wiped_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for (e): a non-candidate live worker that vanishes with the
    stop makes the empty roster outage-shaped again."""
    daemon = FakeNativeDaemonClient()
    bystander = _bystander(
        tmp_path, surface_ref=daemon.seed_live_worker(tmp_path / "wt-bystander")
    )
    live, absent = _seed_orphans(
        tmp_path, daemon, "orphan-a", "orphan-b", extra=(bystander,)
    )
    assert absent.surface_ref is not None
    daemon._live.discard(absent.surface_ref)
    _wipe_roster_on_stop(monkeypatch, daemon)

    with pytest.raises(CwError, match="unreadable after the stop"):
        _close(daemon, after_transition=False)

    assert daemon.stop_calls == [live.surface_ref]
    assert _status_of("orphan-a") == SessionStatus.ACTIVE


def test_roster_unreadable_right_after_confirming_wait_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan_logs: pytest.LogCaptureFixture,
) -> None:
    """N1 (f): the extra post-wait read returning None is unconfirmed."""
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _confirm_then_break(*_args: object, **_kwargs: object) -> bool:
        daemon.roster_unreadable = True
        return True

    monkeypatch.setattr(f"{_MODULE}.wait_for_roster_presence", _confirm_then_break)

    with pytest.raises(CwError, match="unreadable after the stop"):
        _close(daemon, after_transition=False)

    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    (record,) = _records(orphan_logs, "routed_orphan_stop_unconfirmed")
    assert "reason=roster_unreadable_after_stop" in record.getMessage()


def test_wait_that_raises_is_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan_logs: pytest.LogCaptureFixture,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _raise(*_args: object, **_kwargs: object) -> bool:
        msg = "roster vanished"
        raise OSError(msg)

    monkeypatch.setattr(f"{_MODULE}.wait_for_roster_presence", _raise)

    with pytest.raises(CwError, match="still listed in the roster"):
        _close(daemon, after_transition=False)

    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    (record,) = _records(orphan_logs, "routed_orphan_stop_unconfirmed")
    assert record.exc_info is not None


# -- pins ---------------------------------------------------------------------


@_AFTER_TRANSITION
def test_running_row_pins_in_both_modes(
    tmp_path: Path,
    orphan_logs: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    after_transition: bool,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _seed_row(orphan.id)

    assert _close(daemon, after_transition=after_transition) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    err = capsys.readouterr().err
    assert err.count("pinned by") == 1
    assert "pinned by 2517 (client-a, RUNNING)" in err
    assert close_command(orphan.id) in err
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=pinned" in record.getMessage()


@pytest.mark.parametrize(("after_transition", "closes"), [(False, True), (True, False)])
def test_own_parked_row_pins_only_after_the_transition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    after_transition: bool,
    closes: bool,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _seed_row(orphan.id, status=QueueItemStatus.BLOCKED_ON_USER)

    closed = _close(daemon, after_transition=after_transition)

    assert closed == ([orphan.id] if closes else [])
    pinned_line = "pinned by 2517 (client-a, BLOCKED_ON_USER)"
    assert (pinned_line in capsys.readouterr().err) is not closes


@_AFTER_TRANSITION
def test_other_tickets_parked_row_pins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], after_transition: bool
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _seed_row(orphan.id, status=QueueItemStatus.BLOCKED_ON_USER, ticket_id="9999")

    assert _close(daemon, after_transition=after_transition) == []

    assert daemon.stop_calls == []
    assert "pinned by 9999 (client-a, BLOCKED_ON_USER)" in capsys.readouterr().err


# -- draining -----------------------------------------------------------------


def test_draining_refuses_requeue_and_stops_nothing(
    tmp_path: Path, orphan_logs: pytest.LogCaptureFixture
) -> None:
    daemon = FakeNativeDaemonClient()
    draining, idle = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")
    _stamp(draining, minutes_ago=5)

    with pytest.raises(CwError) as excinfo:
        _close(daemon, after_transition=False)

    message = _flat(str(excinfo.value))
    assert "background work is still draining" in message
    assert "The row was not requeued." in message
    assert close_command(draining.id) in message
    assert daemon.stop_calls == []
    assert _status_of(draining.id) == SessionStatus.ACTIVE
    assert _status_of(idle.id) == SessionStatus.ACTIVE
    (record,) = _records(orphan_logs, "routed_orphan_close_refused")
    assert "reason=background_work_draining" in record.getMessage()


def test_stale_spawn_stamp_is_closed_normally(tmp_path: Path) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _stamp(orphan, minutes_ago=90)

    assert _close(daemon, after_transition=False) == [orphan.id]


def test_draining_is_left_running_after_approve(
    tmp_path: Path,
    orphan_logs: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    draining, idle = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")
    _stamp(draining, minutes_ago=5)

    assert _close(daemon, after_transition=True) == [idle.id]

    assert daemon.stop_calls == [idle.surface_ref]
    assert _status_of(draining.id) == SessionStatus.ACTIVE
    err = _flat(capsys.readouterr().err)
    assert f"Left running: routed-result session {draining.id}" in err
    assert "background work is still draining" in err
    assert close_command(draining.id) in err
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=background_work_draining" in record.getMessage()


# -- a stop that does not take (R4) ---------------------------------------------


@_AFTER_TRANSITION
def test_stop_that_leaves_worker_listed_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan_logs: pytest.LogCaptureFixture,
    after_transition: bool,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _stop_leaves_worker_listed(monkeypatch, daemon)
    save_dev_queue(DevQueueStore(tasks=[]))
    before = load_dev_queue().model_dump()

    with pytest.raises(CwError) as excinfo:
        _close(daemon, after_transition=after_transition)

    message = _flat(str(excinfo.value))
    assert orphan.id in message
    assert f"claude stop {orphan.surface_ref}" in message
    assert str(daemon.roster_path) in message
    assert "still listed in the roster" in message
    if after_transition:
        assert message.startswith("Approved 2517 (client-a)")
        assert "row is released" in message
        assert "Do NOT re-run approve" in message
        assert close_command(orphan.id) in message
    else:
        assert "The row was not requeued." in message
        assert "status flip alone" in message
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    (record,) = _records(orphan_logs, "routed_orphan_stop_unconfirmed")
    assert "reason=worker_still_listed" in record.getMessage()
    assert _records(orphan_logs, "routed_orphan_closed_on_resolve") == []
    assert load_dev_queue().model_dump() == before


def test_first_closed_stays_closed_when_second_stop_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    first, second = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")

    def _stop_first_only(short_id: str) -> None:
        daemon.stop_calls.append(short_id)
        if short_id == first.surface_ref:
            daemon._live.discard(short_id)

    monkeypatch.setattr(daemon, "stop", _stop_first_only)

    with pytest.raises(CwError, match=second.id):
        _close(daemon, after_transition=False)

    assert _status_of(first.id) == SessionStatus.COMPLETED
    assert _status_of(second.id) == SessionStatus.ACTIVE
    out = capsys.readouterr().out
    assert f"Closed orphaned routed-result session {first.id}" in out


def test_retry_after_late_roster_removal_closes_by_flip_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    assert orphan.surface_ref is not None
    _stop_leaves_worker_listed(monkeypatch, daemon)
    with pytest.raises(CwError):
        _close(daemon, after_transition=False)

    daemon._live.discard(orphan.surface_ref)

    assert _close(daemon, after_transition=False) == [orphan.id]
    assert daemon.stop_calls == [orphan.surface_ref]
    assert _status_of(orphan.id) == SessionStatus.COMPLETED


@_AFTER_TRANSITION
def test_flip_error_after_confirmed_stop_refuses_then_retry_flips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan_logs: pytest.LogCaptureFixture,
    after_transition: bool,
) -> None:
    from cw.cli.spawn import _spawn_close_impl

    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    calls: list[str] = []

    def _fail_once(
        *,
        session_id: str,
        native_daemon: NativeDaemonClient | None = None,
        surface_already_stopped: bool = False,
    ) -> None:
        calls.append(session_id)
        if len(calls) == 1:
            msg = "sessions lock timed out"
            raise CwError(msg)
        _spawn_close_impl(
            session_id=session_id,
            native_daemon=native_daemon,
            surface_already_stopped=surface_already_stopped,
        )

    monkeypatch.setattr(f"{_MODULE}._spawn_close_impl", _fail_once)

    with pytest.raises(CwError) as excinfo:
        _close(daemon, after_transition=after_transition)

    message = _flat(str(excinfo.value))
    assert close_command(orphan.id) in message
    assert "sessions lock timed out" in message
    if after_transition:
        assert "Do NOT re-run approve" in message
    else:
        assert "Re-run the requeue" in message
    (record,) = _records(orphan_logs, "routed_orphan_close_failed")
    assert "error=sessions lock timed out" in record.getMessage()
    assert _status_of(orphan.id) == SessionStatus.ACTIVE

    assert _close(daemon, after_transition=after_transition) == [orphan.id]
    assert daemon.stop_calls == [orphan.surface_ref]
    assert _status_of(orphan.id) == SessionStatus.COMPLETED


def test_stop_that_makes_roster_unreadable_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan_logs: pytest.LogCaptureFixture,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _stop_makes_roster_unreadable(monkeypatch, daemon)

    with pytest.raises(CwError, match="unreadable after the stop"):
        _close(daemon, after_transition=False)

    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    (record,) = _records(orphan_logs, "routed_orphan_stop_unconfirmed")
    assert "reason=roster_unreadable_after_stop" in record.getMessage()


# -- pre-stop re-validation (R2): precheck is the injection point ---------------


def test_revalidation_sees_a_new_running_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _bind(_ids: frozenset[str]) -> None:
        _seed_row(orphan.id)

    assert _close(daemon, after_transition=False, precheck=_bind) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    assert "pinned by 2517" in capsys.readouterr().err


def test_revalidation_sees_fresh_draining_stamp(tmp_path: Path) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _fresh_stamp(_ids: frozenset[str]) -> None:
        _stamp(orphan, minutes_ago=1)

    with pytest.raises(CwError, match="background work is still draining"):
        _close(daemon, after_transition=False, precheck=_fresh_stamp)

    assert daemon.stop_calls == []


def test_revalidation_leaves_fresh_draining_session_running_after_approve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _fresh_stamp(_ids: frozenset[str]) -> None:
        _stamp(orphan, minutes_ago=1)

    assert _close(daemon, after_transition=True, precheck=_fresh_stamp) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    assert "Left running" in capsys.readouterr().err


def test_revalidation_skips_a_session_no_longer_a_candidate(
    tmp_path: Path, orphan_logs: pytest.LogCaptureFixture
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _complete(_ids: frozenset[str]) -> None:
        state = load_state()
        state.sessions[0].status = SessionStatus.COMPLETED
        save_state(state)

    assert _close(daemon, after_transition=False, precheck=_complete) == []

    assert daemon.stop_calls == []
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=no_longer_candidate" in record.getMessage()
    assert orphan.id in record.getMessage()


def test_revalidation_skips_when_roster_turns_unreadable(
    tmp_path: Path, orphan_logs: pytest.LogCaptureFixture
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _break(_ids: frozenset[str]) -> None:
        daemon.roster_unreadable = True

    assert _close(daemon, after_transition=False, precheck=_break) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=roster_unreadable_before_stop" in record.getMessage()


def test_revalidation_reclassifies_an_emptied_roster_as_untrusted(
    tmp_path: Path, orphan_logs: pytest.LogCaptureFixture
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon, extra=(_bystander(tmp_path),))

    def _empty(_ids: frozenset[str]) -> None:
        daemon._live.clear()

    assert _close(daemon, after_transition=False, precheck=_empty) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=roster_unreadable" in record.getMessage()


def test_precheck_refusal_propagates_before_any_stop(tmp_path: Path) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _refuse(_ids: frozenset[str]) -> None:
        msg = "requeue would refuse"
        raise CwError(msg)

    with pytest.raises(CwError, match="requeue would refuse"):
        _close(daemon, after_transition=False, precheck=_refuse)

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) == SessionStatus.ACTIVE


def test_precheck_gets_exactly_the_stop_and_flip_only_ids(tmp_path: Path) -> None:
    daemon = FakeNativeDaemonClient()
    live, absent, pinned = _seed_orphans(
        tmp_path, daemon, "orphan-a", "orphan-b", "orphan-c"
    )
    assert absent.surface_ref is not None
    daemon._live.discard(absent.surface_ref)
    _seed_row(pinned.id, ticket_id="9999")
    calls: list[frozenset[str]] = []

    _close(daemon, after_transition=False, precheck=calls.append)

    assert calls == [frozenset({live.id, absent.id})]


def test_precheck_not_called_without_targets(tmp_path: Path) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _seed_row(orphan.id)
    calls: list[frozenset[str]] = []

    assert _close(daemon, after_transition=False, precheck=calls.append) == []
    assert calls == []


def test_module_never_takes_sessions_lock() -> None:
    """ADR-0019 / #2547: the helper stops a daemon and polls the roster, so it
    must never hold ``sessions_lock`` itself; ``_spawn_close_impl`` (guarded in
    ``tests/test_spawn.py``) takes it for the flip only."""
    import ast
    import inspect

    from cw.cli import routed_orphan_close

    tree = ast.parse(inspect.getsource(routed_orphan_close))
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "sessions_lock" not in names | imported
    assert "dev_queue_lock" not in names | imported
