"""Tests for cw.cli.routed_named_close (#2708).

``cw doctor --reap --routed-session-id <id>`` closes the routed-result session
it names without waiting for the 30-minute transcript-staleness bucket. It
reuses the approve/requeue close (``routed_orphan_close._close_one``): the
worker is stopped and confirmed gone with no lock held, and only then is the
session flipped COMPLETED. Every refusal names the session and the predicate
outcome behind it, and nothing is flipped on a refusal.

Hermetic: a fake daemon, zeroed roster polls, and a frozen clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.cli.routed_named_close import close_named_routed_result_sessions
from cw.cli.routed_orphan_close import close_routed_result_sessions_for_ticket
from cw.cli.spawn import _spawn_close_impl
from cw.config import load_state, save_state, state_file
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.exceptions import CwError, SessionsLockTimeoutError
from cw.history import EventType, load_history
from cw.models import (
    CompletionReason,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.liveness_page import close_command
from cw.reconcile.routed_result_sessions import MarkerRejection
from tests._reconcile_helpers import (
    LockProbeDaemon,
    _routed_last_result,
    _stamp_transcript_age,
    _write_agent_spawn_stamp,
)
from tests._routed_result_helpers import (
    _CLIENT,
    _TICKET,
    _bystander,
    _flat,
    _records,
    _seed_orphans,
    _seed_row,
    _status_of,
    _wipe_roster_on_stop,
)
from tests.conftest import _stop_leaves_worker_listed, _stop_makes_roster_unreadable

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cw.native_daemon import NativeDaemonClient

_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
_ORPHAN_MODULE = "cw.cli.routed_orphan_close"

pytestmark = pytest.mark.usefixtures("instant_routed_roster_polls")


@pytest.fixture(autouse=True)
def _frozen_clock() -> Iterator[None]:
    with freeze_time(_NOW):
        yield


def _named(
    daemon: NativeDaemonClient | None,
    *ids: str,
    config: OrchestratorConfig | None = None,
) -> list[str]:
    return close_named_routed_result_sessions(
        list(ids), config=config or OrchestratorConfig(), native_daemon=daemon
    )


def _session(session_id: str) -> Session:
    return next(s for s in load_state().sessions if s.id == session_id)


def _update(session_id: str, **changes: object) -> None:
    state = load_state()
    sess = next(s for s in state.sessions if s.id == session_id)
    for field, value in changes.items():
        setattr(sess, field, value)
    save_state(state)


def _fresh_transcript(sess: Session, *, minutes_ago: float = 1.0) -> None:
    assert sess.worktree_path is not None
    assert sess.surface_ref is not None
    _stamp_transcript_age(
        Path.home(),
        sess.worktree_path,
        stale_minutes=minutes_ago,
        now=_NOW,
        surface_ref=sess.surface_ref,
    )


def _spawn_stamp(sess: Session, *, minutes_ago: float) -> None:
    assert sess.worktree_path is not None
    _write_agent_spawn_stamp(
        sess.worktree_path,
        unresolved_count=1,
        stamped_at=_NOW - timedelta(minutes=minutes_ago),
    )


def _reap_events() -> list[dict[str, object]]:
    return [
        dict(e.payload)
        for e in read_events(
            event_types=[OrchestratorEventType.SESSION_REAP_AUTHORIZED]
        )
    ]


# -- the close itself --------------------------------------------------------


def test_closes_fresh_session_stop_first_outside_lock(tmp_path: Path) -> None:
    daemon = LockProbeDaemon()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _fresh_transcript(orphan)
    save_dev_queue(DevQueueStore(tasks=[]))
    queue_before = DevQueueStore(tasks=[]).model_dump()

    assert _named(daemon, orphan.id) == [orphan.id]

    # The stop precedes the flip and runs with sessions_lock free.
    assert daemon.probes == [("free", {orphan.id: SessionStatus.ACTIVE})]
    stored = _session(orphan.id)
    assert stored.status is SessionStatus.COMPLETED
    assert stored.completed_reason is CompletionReason.USER
    assert stored.reap_reason is ReapReason.ROUTED_RESULT_STRANDED
    assert load_dev_queue().model_dump() == queue_before


def test_evidence_printed_before_the_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _fresh_transcript(orphan)
    at_stop: list[str] = []
    real_stop = daemon.stop

    def _stop(short_id: str) -> None:
        at_stop.append(capsys.readouterr().err)
        real_stop(short_id)

    monkeypatch.setattr(daemon, "stop", _stop)

    assert _named(daemon, orphan.id) == [orphan.id]

    (before,) = at_stop
    before = _flat(before)
    assert (
        f"Closing routed-result session {orphan.id} for {_TICKET} ({_CLIENT}) now;"
        " the 30-minute staleness bucket is not consulted. Evidence relied on:"
    ) in before
    assert "routed result on record (consumed-route marker" in before
    assert "no occupied queue row binds it" in before
    assert "no subagent-spawn stamp younger than 30 minutes" in before
    assert "listed in the daemon roster and is about to be stopped" in before
    assert (
        "Not checked: non-subagent background work (a bash task, a build) is"
        " invisible to the drain check and ends with the worker; transcript age"
        " is not enforced."
    ) in before
    assert "For information only (never gating, ADR-0014)" in before
    assert "last transcript activity: 1 min ago" in before
    assert "newest subagent-spawn stamp: none outstanding" in before
    assert "Closed routed-result session" not in before
    after = _flat(capsys.readouterr().err)
    assert (
        f"Closed routed-result session {orphan.id} for {_TICKET} ({_CLIENT})."
        " Relied on: routed result on record; no occupied row; worker stopped and"
        " confirmed gone from the daemon roster. Not checked: non-subagent"
        " background work, transcript age."
    ) in after


def test_informational_ages_when_transcript_missing_and_stamp_old(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale stamp is shown, but past the deadline it never gates."""
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _spawn_stamp(orphan, minutes_ago=45)

    assert _named(daemon, orphan.id) == [orphan.id]

    err = _flat(capsys.readouterr().err)
    assert "last transcript activity: no transcript found" in err
    assert "newest subagent-spawn stamp: 45 min ago" in err


def test_audit_shape(tmp_path: Path, orphan_logs: pytest.LogCaptureFixture) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    assert _named(daemon, orphan.id) == [orphan.id]

    (payload,) = _reap_events()
    assert payload["mutations"] == ["session_status_completed", "daemon_stopped"]
    assert payload["daemon_stop_succeeded"] is True
    assert payload["proposed_action"] == "close_routed_result_session"
    (event,) = load_history(_CLIENT)
    assert event.event_type is EventType.SESSION_COMPLETED
    assert event.metadata["command"] == "doctor_reap"
    assert event.metadata["confirmation_result"] == "worker_gone_from_roster"
    (record,) = _records(orphan_logs, "routed_orphan_closed_on_resolve")
    assert record.getMessage() == (
        f"routed_orphan_closed_on_resolve: ticket_id={_TICKET} client={_CLIENT}"
        f" session_id={orphan.id} command=doctor_reap"
    )


@pytest.mark.parametrize(("lane", "expected"), [("fast", "fast"), (None, "default")])
def test_audit_lane_follows_session_lane(
    tmp_path: Path, lane: str | None, expected: str
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _update(orphan.id, lane=lane)

    assert _named(daemon, orphan.id) == [orphan.id]

    (payload,) = _reap_events()
    assert payload["lane"] == expected


def test_session_without_ticket_shaped_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _update(orphan.id, name=f"{_CLIENT}/impl")

    assert _named(daemon, orphan.id) == [orphan.id]

    err = _flat(capsys.readouterr().err)
    assert f"session {orphan.id} for (no ticket) ({_CLIENT})" in err
    (event,) = read_events(event_types=[OrchestratorEventType.SESSION_REAP_AUTHORIZED])
    assert event.payload["ticket_id"] == ""
    assert event.correlation_id == orphan.id


def test_flip_only_when_absent_from_trustworthy_roster(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon, live=False)

    assert _named(daemon, orphan.id) == [orphan.id]

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) is SessionStatus.COMPLETED
    err = _flat(capsys.readouterr().err)
    assert "absent from a readable, trustworthy daemon roster" in err
    assert "no stop is issued" in err
    assert "worker absent from a readable roster on two reads one interval" in err
    (payload,) = _reap_events()
    assert payload["mutations"] == ["session_status_completed"]
    assert payload["daemon_stop_succeeded"] is False


def test_flip_only_uncorroborated_roster_change_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class _PartialRosterDaemon(FakeNativeDaemonClient):
        def __init__(self, reads: list[set[str]]) -> None:
            super().__init__()
            self.reads = reads

        def list_live_session_short_ids_fail_closed(self) -> set[str] | None:
            return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    (orphan,) = _seed_orphans(tmp_path, FakeNativeDaemonClient(), live=False)
    assert orphan.surface_ref is not None
    daemon = _PartialRosterDaemon([{"unrelated"}, {"unrelated", orphan.surface_ref}])

    assert _named(daemon, orphan.id) == []

    assert daemon.stop_calls == []
    assert _status_of(orphan.id) is SessionStatus.ACTIVE
    assert "changed between two reads" in _flat(capsys.readouterr().err)


# -- refusals before any stop -------------------------------------------------


def test_unknown_id_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    orphan_logs: pytest.LogCaptureFixture,
) -> None:
    def _fail() -> NativeDaemonClient:
        pytest.fail("an unknown id must not resolve a daemon client")

    monkeypatch.setattr("cw.cli.routed_named_close.get_native_daemon_client", _fail)
    _seed_orphans(tmp_path, FakeNativeDaemonClient())
    before = state_file().read_bytes()

    assert _named(None, "ghost") == []

    err = _flat(capsys.readouterr().err)
    assert "Cannot close routed-result session ghost" in err
    assert "no session with that exact id is recorded in cw state" in err
    assert "cw list" in err
    assert state_file().read_bytes() == before
    (record,) = _records(orphan_logs, "routed_named_close_refused")
    assert record.getMessage() == (
        "routed_named_close_refused: session_id=ghost reason=unknown_session"
    )


_INELIGIBLE: list[tuple[MarkerRejection, dict[str, object], str]] = [
    (
        MarkerRejection.NOT_DAEMON,
        {"origin": SessionOrigin.USER},
        "it is a user-origin session, not a daemon worker",
    ),
    (
        MarkerRejection.NOT_LIVE,
        {"status": SessionStatus.COMPLETED},
        "its status is completed, not active or idle",
    ),
    (
        MarkerRejection.ORCHESTRATOR,
        {"purpose": SessionPurpose.ORCHESTRATE},
        "it is the orchestrator session",
    ),
    (
        MarkerRejection.NO_ROUTED_RESULT,
        {"last_result": {"status": "stage_complete"}},
        "no routed result is on record (its last result carries no"
        " consumed-route marker, #2458)",
    ),
    (
        MarkerRejection.REFUSAL_LATCHED,
        {"last_result": _routed_last_result(sentinel_advance_refused=True)},
        "a stage refusal is latched on it; the refusal pages own that shape",
    ),
    (
        MarkerRejection.NO_SURFACE,
        {"surface_ref": None},
        "it has no daemon surface; doctor class 9 owns that shape",
    ),
]


@pytest.mark.parametrize(
    ("rejection", "changes", "reason"),
    _INELIGIBLE,
    ids=[rejection.value for rejection, _, _ in _INELIGIBLE],
)
def test_ineligible_refused_for_each_rejection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    orphan_logs: pytest.LogCaptureFixture,
    rejection: MarkerRejection,
    changes: dict[str, object],
    reason: str,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _update(orphan.id, **changes)
    before = state_file().read_bytes()

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert (
        f"Cannot close routed-result session {orphan.id} for {_TICKET}"
        f" ({_CLIENT}): {reason}."
    ) in err
    assert daemon.stop_calls == []
    assert state_file().read_bytes() == before
    (record,) = _records(orphan_logs, "routed_named_close_refused")
    assert f"reason={rejection.value}" in record.getMessage()


@pytest.mark.parametrize(
    "status", [QueueItemStatus.RUNNING, QueueItemStatus.BLOCKED_ON_USER]
)
def test_pinned_by_occupied_row_names_row(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    orphan_logs: pytest.LogCaptureFixture,
    status: QueueItemStatus,
) -> None:
    """A parked row of the same ticket pins too (approve semantics)."""
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _seed_row(orphan.id, status=status)
    before = state_file().read_bytes()

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert f"session {orphan.id} for {_TICKET}" in err
    assert f"pinned by {_TICKET} ({_CLIENT}, {status.name})" in err
    assert daemon.stop_calls == []
    assert state_file().read_bytes() == before
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=pinned" in record.getMessage()


def test_draining_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    orphan_logs: pytest.LogCaptureFixture,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _spawn_stamp(orphan, minutes_ago=5)
    before = state_file().read_bytes()

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert f"Left running: routed-result session {orphan.id}" in err
    assert "background work is still draining" in err
    assert daemon.stop_calls == []
    assert state_file().read_bytes() == before
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=background_work_draining" in record.getMessage()


def test_roster_untrusted_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    orphan_logs: pytest.LogCaptureFixture,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(
        tmp_path, daemon, live=False, extra=(_bystander(tmp_path),)
    )
    before = state_file().read_bytes()

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert orphan.id in err
    assert "looks like a daemon restart" in err
    assert daemon.stop_calls == []
    assert state_file().read_bytes() == before
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=roster_unreadable" in record.getMessage()


def test_roster_unreadable_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    orphan_logs: pytest.LogCaptureFixture,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    daemon.roster_unreadable = True
    before = state_file().read_bytes()

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert orphan.id in err
    assert "the daemon roster became unreadable just before the stop" in err
    assert daemon.stop_calls == []
    assert state_file().read_bytes() == before
    (record,) = _records(orphan_logs, "routed_orphan_close_skipped")
    assert "reason=roster_unreadable_before_stop" in record.getMessage()


# -- a stop that does not take (R4) ---------------------------------------------


def _assert_nothing_flipped(orphan: Session) -> None:
    assert _status_of(orphan.id) is SessionStatus.ACTIVE
    assert _reap_events() == []
    assert load_history(_CLIENT) == []


def test_failed_stop_worker_still_listed_refuses_and_flips_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    orphan_logs: pytest.LogCaptureFixture,
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _stop_leaves_worker_listed(monkeypatch, daemon)

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert (
        f"Cannot close routed-result session {orphan.id} for {_TICKET} ({_CLIENT}):"
        " its worker is still listed in the roster. Its recorded status was not"
        " changed."
    ) in err
    assert f"claude stop {orphan.surface_ref}" in err
    assert "Re-run this command after the stop works" in err
    assert daemon.stop_calls == [orphan.surface_ref]
    _assert_nothing_flipped(orphan)
    (record,) = _records(orphan_logs, "routed_orphan_stop_unconfirmed")
    assert "command=doctor_reap" in record.getMessage()


def test_failed_stop_roster_unreadable_after_stop_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _stop_makes_roster_unreadable(monkeypatch, daemon)

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert orphan.id in err
    assert "the daemon roster is unreadable after the stop" in err
    _assert_nothing_flipped(orphan)


def test_failed_stop_roster_wiped_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    bystander = _bystander(
        tmp_path, surface_ref=daemon.seed_live_worker(tmp_path / "wt-bystander")
    )
    (orphan,) = _seed_orphans(tmp_path, daemon, extra=(bystander,))
    _wipe_roster_on_stop(monkeypatch, daemon)

    assert _named(daemon, orphan.id) == []

    assert "unreadable after the stop" in _flat(capsys.readouterr().err)
    _assert_nothing_flipped(orphan)


def test_flip_failure_after_stop_refuses_and_retry_flips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    calls: list[str] = []

    def _fail_once(
        *,
        session_id: str,
        native_daemon: NativeDaemonClient | None = None,
        surface_already_stopped: bool = False,
        reap_reason: ReapReason | None = None,
    ) -> None:
        calls.append(session_id)
        if len(calls) == 1:
            msg = "state write failed"
            raise CwError(msg)
        _spawn_close_impl(
            session_id=session_id,
            native_daemon=native_daemon,
            surface_already_stopped=surface_already_stopped,
            reap_reason=reap_reason,
        )

    monkeypatch.setattr(f"{_ORPHAN_MODULE}._spawn_close_impl", _fail_once)

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert "state write failed" in err
    assert (
        "The worker is already stopped and the session's status is unchanged."
        " Re-run this command: the retry finds the worker gone from the roster"
        " and closes the session by a status flip alone."
    ) in err
    assert close_command(orphan.id) in err
    assert _status_of(orphan.id) is SessionStatus.ACTIVE

    assert _named(daemon, orphan.id) == [orphan.id]
    assert daemon.stop_calls == [orphan.surface_ref]
    assert _session(orphan.id).reap_reason is ReapReason.ROUTED_RESULT_STRANDED


def test_sessions_lock_timeout_after_confirmed_stop_refuses_with_retry_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    first, second = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")

    def _timeout(**_kwargs: object) -> None:
        msg = "sessions lock held"
        raise SessionsLockTimeoutError(
            msg, lock_path=tmp_path / "sessions.lock", waited_s=60.0
        )

    monkeypatch.setattr(f"{_ORPHAN_MODULE}._spawn_close_impl", _timeout)

    assert _named(daemon, first.id, second.id) == []

    err = _flat(capsys.readouterr().err)
    assert f"Cannot close routed-result session {first.id}" in err
    assert "sessions lock held" in err
    assert "The worker is already stopped and the session's status is unchanged" in (
        err
    )
    assert f"Not attempted: {second.id}" in err
    assert daemon.stop_calls == [first.surface_ref]
    assert _status_of(first.id) is SessionStatus.ACTIVE
    assert _status_of(second.id) is SessionStatus.ACTIVE


def test_sessions_lock_timeout_on_the_last_id_names_nothing_unattempted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    def _timeout(**_kwargs: object) -> None:
        msg = "sessions lock held"
        raise SessionsLockTimeoutError(
            msg, lock_path=tmp_path / "sessions.lock", waited_s=60.0
        )

    monkeypatch.setattr(f"{_ORPHAN_MODULE}._spawn_close_impl", _timeout)

    assert _named(daemon, orphan.id) == []

    err = _flat(capsys.readouterr().err)
    assert "sessions lock held" in err
    assert "Not attempted" not in err


def test_mixed_ids_close_only_the_closable_and_report_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon = FakeNativeDaemonClient()
    closable, pinned = _seed_orphans(tmp_path, daemon, "orphan-a", "orphan-b")
    _seed_row(pinned.id)

    closed = _named(daemon, "ghost", pinned.id, closable.id)

    assert closed == [closable.id]
    assert daemon.stop_calls == [closable.surface_ref]
    assert _status_of(pinned.id) is SessionStatus.ACTIVE
    err = _flat(capsys.readouterr().err)
    assert "Cannot close routed-result session ghost" in err
    assert f"session {pinned.id} for {_TICKET} ({_CLIENT}) is pinned by" in err
    assert f"Closed routed-result session {closable.id}" in err


@pytest.mark.parametrize("policy", list(ReapPolicy))
def test_close_ignores_reap_policy(tmp_path: Path, policy: ReapPolicy) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    closed = _named(daemon, orphan.id, config=OrchestratorConfig(reap_policy=policy))

    assert closed == [orphan.id]


@pytest.mark.parametrize("after_transition", [False, True])
def test_approve_and_requeue_closes_leave_reap_reason_unset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], after_transition: bool
) -> None:
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)

    closed = close_routed_result_sessions_for_ticket(
        _TICKET,
        _CLIENT,
        command="approve" if after_transition else "requeue",
        config=OrchestratorConfig(),
        after_transition=after_transition,
        native_daemon=daemon,
    )

    assert closed == [orphan.id]
    assert _session(orphan.id).reap_reason is None
    out = capsys.readouterr().out
    assert (
        f"Closed orphaned routed-result session {orphan.id} for {_TICKET}"
        f" ({_CLIENT}).\n"
    ) == out


def test_close_without_reap_reason_preserves_the_existing_one(
    tmp_path: Path,
) -> None:
    """``_spawn_close_impl`` treats ``reap_reason=None`` as "no change": a plain
    close must not wipe a reason an earlier reap already stamped."""
    daemon = FakeNativeDaemonClient()
    (orphan,) = _seed_orphans(tmp_path, daemon)
    _update(orphan.id, reap_reason=ReapReason.IDLE_STALL)

    _spawn_close_impl(session_id=orphan.id, surface_already_stopped=True)

    stored = _session(orphan.id)
    assert stored.status is SessionStatus.COMPLETED
    assert stored.reap_reason is ReapReason.IDLE_STALL


def test_module_never_takes_sessions_lock() -> None:
    """ADR-0019: the named close stops a daemon and polls the roster, so it
    must never hold ``sessions_lock`` itself; ``_spawn_close_impl`` takes it
    for the flip only."""
    import ast
    import inspect

    from cw.cli import routed_named_close

    tree = ast.parse(inspect.getsource(routed_named_close))
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
