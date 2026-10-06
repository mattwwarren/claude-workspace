"""Tests for cw.doctor.routed_result_wedge (#2524).

The doctor class for an ACTIVE DAEMON session whose staged result a #2458
partial route already routed (the row advanced) and that nothing ever
completed. ``cw doctor`` reports it; only ``cw doctor --reap`` closes it, and
that close flips the session alone -- never a queue row.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cw.config import (
    dev_queue_file,
    load_state,
    save_state,
    state_file,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.doctor import format_report_json, run_doctor
from cw.doctor._shared import WedgeFinding
from cw.doctor.routed_result_wedge import (
    WEDGE_ROUTED_RESULT_STRANDED,
    _audit_outbox_path,
    _check_wedge_routed_result_session,
    _emit_audit_record,
    _finalize_audit_intent,
    _read_audit_outbox,
    _retry_pending_audits,
    has_pending_routed_result_audits,
    reap_routed_result_findings,
)
from cw.doctor.wedge import _reap_wedge_findings
from cw.events import read_events
from cw.models import (
    CompletionReason,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    Session,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.leaked_workers import sweep_leaked_daemon_workers
from tests._reconcile_helpers import (
    LockProbeDaemon,
    _install_fake_daemon_roster,
    _mk_routed_session,
    _routed_last_result,
    _stamp_transcript_age,
)
from tests.conftest import _make_daemon_session

_SID = "2524"
_REF = "fake-short-id"
_STRANDED_CLASS_8 = "wedge/active-daemon-stale-no-sentinel"


def _row(
    *,
    status: QueueItemStatus = QueueItemStatus.PENDING,
    session_id: str | None = None,
    ticket_id: str = _SID,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client="client-a",
        status=status,
        stage=Stage.REVIEW,
        session_id=session_id,
    )


def _seed(
    tmp_path: Path,
    home: Path,
    *,
    tasks: list[TicketTask] | None = None,
    stale_minutes: float = 50.0,
    extra_sessions: list[Session] | None = None,
) -> Session:
    worktree = tmp_path / f"wt-{_SID}"
    _stamp_transcript_age(home, worktree, stale_minutes=stale_minutes)
    sess = _mk_routed_session(_SID, worktree)
    save_state(CwState(sessions=[sess, *(extra_sessions or [])]))
    save_dev_queue(DevQueueStore(tasks=[_row()] if tasks is None else tasks))
    return sess


def _routed_findings() -> list[WedgeFinding]:
    report = run_doctor()
    return [
        f
        for f in report.wedge_findings
        if f.wedge_class == WEDGE_ROUTED_RESULT_STRANDED
    ]


def _session(sid: str = _SID) -> Session:
    return next(s for s in load_state().sessions if s.id == sid)


def _events(event_type: OrchestratorEventType) -> list[dict[str, object]]:
    return [dict(e.payload) for e in read_events(event_types=[event_type])]


def _swap_daemon(
    monkeypatch: pytest.MonkeyPatch, daemon: FakeNativeDaemonClient
) -> None:
    daemon._live.add(_REF)
    monkeypatch.setattr(
        "cw.doctor.routed_result_wedge.get_native_daemon_client", lambda: daemon
    )


def test_finding_reported_without_reap_and_nothing_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    before = state_file().read_bytes() + dev_queue_file().read_bytes()

    findings = _routed_findings()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.session_id == _SID
    assert finding.ticket_id == _SID
    assert "cw doctor --reap" in finding.recipe
    assert f"cw spawn close --confirmed-dead {_SID}" in finding.recipe
    assert "review/pending" in finding.recipe
    assert state_file().read_bytes() + dev_queue_file().read_bytes() == before
    assert daemon.stop_calls == []


def test_reap_closes_session_and_leaves_queue_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    queue_before = dev_queue_file().read_bytes()

    report = run_doctor(reap=True)

    assert WEDGE_ROUTED_RESULT_STRANDED in [
        f.wedge_class for f in report.wedge_findings
    ]
    closed = _session()
    assert closed.status is SessionStatus.COMPLETED
    assert closed.completed_at is not None
    assert closed.completed_reason is CompletionReason.USER
    assert closed.reap_reason is ReapReason.ROUTED_RESULT_STRANDED
    assert daemon.stop_calls == [_REF]
    assert dev_queue_file().read_bytes() == queue_before
    assert _events(OrchestratorEventType.SESSION_COMPLETED) == []


def test_reap_audit_event_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    audits_at_stop: list[int] = []

    class _OrderDaemon(FakeNativeDaemonClient):
        def stop(self, short_id: str) -> None:
            audits_at_stop.append(
                len(_events(OrchestratorEventType.SESSION_REAP_AUTHORIZED))
            )
            super().stop(short_id)

    _swap_daemon(monkeypatch, _OrderDaemon())
    findings = _routed_findings()

    assert reap_routed_result_findings(findings) == [_SID]

    assert audits_at_stop == [0]
    events = read_events(event_types=[OrchestratorEventType.SESSION_REAP_AUTHORIZED])
    assert len(events) == 1
    assert events[0].correlation_id == _SID
    assert dict(events[0].payload) == {
        "session_id": _SID,
        "session_name": f"client-a/auto-dev/{_SID}",
        "client": "client-a",
        "ticket_id": _SID,
        "lane": "default",
        "authority": "operator",
        "proposed_action": "close_routed_result_session",
        "mutations": ["session_status_completed", "daemon_stopped"],
        "daemon_stop_succeeded": True,
    }


def test_reap_stops_daemon_outside_sessions_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    daemon = LockProbeDaemon()
    _swap_daemon(monkeypatch, daemon)

    assert reap_routed_result_findings(_routed_findings()) == [_SID]
    assert [lock for lock, _ in daemon.probes] == ["free"]


def test_reap_does_not_revert_running_row_of_same_ticket_claimed_by_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclusion-set regression: this class carries a ticket_id, and the
    reap tail's running_ticket_ids set is default-inclusive."""
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    daemon._live.add("newref01")
    fresh = _make_daemon_session(
        id="newsess1",
        name=f"client-a/auto-dev/{_SID}",
        worktree_path=tmp_path / "wt-new",
        surface_ref="newref01",
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    _seed(
        tmp_path,
        home,
        tasks=[_row(status=QueueItemStatus.RUNNING, session_id="newsess1")],
        extra_sessions=[fresh],
    )

    report = run_doctor(reap=True)

    assert [f.wedge_class for f in report.wedge_findings] == [
        WEDGE_ROUTED_RESULT_STRANDED
    ]
    row = load_dev_queue().tasks[0]
    assert row.status is QueueItemStatus.RUNNING
    assert row.session_id == "newsess1"
    assert _session().status is SessionStatus.COMPLETED
    assert _session("newsess1").status is SessionStatus.ACTIVE


def test_reap_runs_with_only_this_class_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reap tail's early-return gate must admit this class on its own."""
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    findings = _routed_findings()
    assert len(findings) == 1

    _reap_wedge_findings(findings)

    assert _session().status is SessionStatus.COMPLETED
    assert daemon.stop_calls == [_REF]


def test_reap_redetects_fresh_and_skips_session_that_gained_a_bound_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    findings = _routed_findings()
    save_dev_queue(
        DevQueueStore(tasks=[_row(status=QueueItemStatus.RUNNING, session_id=_SID)])
    )

    assert reap_routed_result_findings(findings) == []

    assert _session().status is SessionStatus.ACTIVE
    assert daemon.stop_calls == []
    assert _events(OrchestratorEventType.SESSION_REAP_AUTHORIZED) == []


def test_reap_skips_session_whose_transcript_went_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    findings = _routed_findings()
    _stamp_transcript_age(home, tmp_path / f"wt-{_SID}", stale_minutes=1)

    assert reap_routed_result_findings(findings) == []

    assert _session().status is SessionStatus.ACTIVE
    assert daemon.stop_calls == []


def test_reap_ignores_reap_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    findings = _routed_findings()
    monkeypatch.setattr(
        "cw.doctor.routed_result_wedge.load_orchestrator_config",
        lambda: OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY),
    )

    assert reap_routed_result_findings(findings) == [_SID]
    assert _session().status is SessionStatus.COMPLETED
    assert daemon.stop_calls == [_REF]


def test_reap_with_no_matching_findings_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    other = WedgeFinding(
        wedge_class=_STRANDED_CLASS_8,
        session_id=_SID,
        ticket_id=_SID,
        recipe="",
        state_file="",
    )

    assert reap_routed_result_findings([other]) == []
    assert _session().status is SessionStatus.ACTIVE
    assert daemon.stop_calls == []


def test_daemon_stop_failure_leaves_session_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed stop leaves the session COMPLETED; the #2481 leaked-worker
    sweep then stops the still-live worker on the next reconcile tick."""
    home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)

    class _FailingStopDaemon(FakeNativeDaemonClient):
        def stop(self, short_id: str) -> None:
            msg = f"stop {short_id} failed"
            raise RuntimeError(msg)

    _swap_daemon(monkeypatch, _FailingStopDaemon())

    assert reap_routed_result_findings(_routed_findings()) == [_SID]
    assert _session().status is SessionStatus.COMPLETED
    events = _events(OrchestratorEventType.SESSION_REAP_AUTHORIZED)
    assert len(events) == 1
    assert events[0]["daemon_stop_succeeded"] is False
    assert "daemon_stopped" not in events[0]["mutations"]

    next_tick = FakeNativeDaemonClient()
    next_tick._live.add(_REF)
    next_tick._cwd_by_id[_REF] = tmp_path / f"wt-{_SID}"
    assert sweep_leaked_daemon_workers(load_state(), daemon=next_tick) == [_REF]
    assert next_tick.stop_calls == [_REF]


def test_running_row_bound_session_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home, tasks=[_row(status=QueueItemStatus.RUNNING, session_id=_SID)])

    report = run_doctor(reap=True)

    assert WEDGE_ROUTED_RESULT_STRANDED not in [
        f.wedge_class for f in report.wedge_findings
    ]
    assert _session().status is SessionStatus.ACTIVE
    assert daemon.stop_calls == []


def test_classes_are_disjoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No-sentinel -> class 8 only; consumed marker -> this class only; a bare
    terminal result -> neither."""
    home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    shapes: dict[str, dict[str, object] | None] = {
        "nosent": None,
        "routed": _routed_last_result(),
        "bare": {"status": "shipped"},
    }
    sessions: list[Session] = []
    for sid, last_result in shapes.items():
        worktree = tmp_path / f"wt-{sid}"
        _stamp_transcript_age(home, worktree, stale_minutes=50)
        sess = _mk_routed_session(sid, worktree)
        sess.last_result = last_result
        sessions.append(sess)
    save_state(CwState(sessions=sessions))
    save_dev_queue(DevQueueStore(tasks=[]))

    report = run_doctor()

    by_session: dict[str, set[str]] = {}
    for finding in report.wedge_findings:
        by_session.setdefault(finding.session_id or "", set()).add(finding.wedge_class)
    assert by_session.get("nosent") == {_STRANDED_CLASS_8}
    assert by_session.get("routed") == {WEDGE_ROUTED_RESULT_STRANDED}
    assert "bare" not in by_session


def test_check_exits_early_without_daemon_or_config_reads_when_no_marker_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _must_not_run() -> None:
        msg = "must not be read when no session carries the consumed marker"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "cw.doctor.routed_result_wedge.get_native_daemon_client", _must_not_run
    )
    monkeypatch.setattr(
        "cw.doctor.routed_result_wedge.load_orchestrator_config", _must_not_run
    )
    sess = _mk_routed_session(_SID, tmp_path / "wt", last_result={"status": "shipped"})

    findings = _check_wedge_routed_result_session(
        CwState(sessions=[sess]), DevQueueStore(tasks=[])
    )

    assert findings == []


def test_json_report_includes_wedge_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)

    payload = json.loads(format_report_json(run_doctor()))

    classes = [f["wedge_class"] for f in payload["wedge_findings"]]
    assert WEDGE_ROUTED_RESULT_STRANDED in classes


# ---------------------------------------------------------------------------
# Durable close-audit outbox (codex fix cycles 1-3)
# ---------------------------------------------------------------------------


def _outbox_record(
    *,
    status: str = "pending_stop",
    session_id: str = _SID,
    stop_succeeded: bool | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "surface_ref": _REF,
        "status": status,
        "correlation_id": session_id,
        "payload": {
            "session_id": session_id,
            "authority": "operator",
            "proposed_action": "close_routed_result_session",
            "mutations": ["session_status_completed"],
            "daemon_stop_succeeded": stop_succeeded,
        },
    }


def _write_outbox(records: object) -> None:
    path = _audit_outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")


def _complete_seeded_session(session: Session) -> None:
    session.status = SessionStatus.COMPLETED
    save_state(CwState(sessions=[session]))


def test_no_outbox_means_no_pending_audits() -> None:
    assert has_pending_routed_result_audits() is False
    assert _read_audit_outbox() == []


def test_pending_record_is_reported_as_pending() -> None:
    _write_outbox([_outbox_record()])

    assert has_pending_routed_result_audits() is True


@pytest.mark.parametrize("raw", ["not json", '{"a": 1}', "[1, 2]"])
def test_malformed_outbox_fails_closed(raw: str) -> None:
    path = _audit_outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")

    assert has_pending_routed_result_audits() is True
    with pytest.raises(ValueError, match=r"audit outbox|Expecting value"):
        _read_audit_outbox()


def test_reap_leaves_outbox_empty_after_audit_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)

    assert reap_routed_result_findings(_routed_findings()) == [_SID]

    assert _read_audit_outbox() == []
    assert len(_events(OrchestratorEventType.SESSION_REAP_AUTHORIZED)) == 1


def test_reap_reuses_a_preexisting_audit_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record left by an earlier interrupted close is not duplicated."""
    home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    _write_outbox([_outbox_record()])

    assert reap_routed_result_findings(_routed_findings()) == [_SID]

    assert _read_audit_outbox() == []
    assert len(_events(OrchestratorEventType.SESSION_REAP_AUTHORIZED)) == 1


def test_retry_completes_a_pending_stop_for_a_closed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _complete_seeded_session(_seed(tmp_path, home))
    _write_outbox([_outbox_record()])

    _retry_pending_audits(daemon)

    assert daemon.stop_calls == [_REF]
    (event,) = _events(OrchestratorEventType.SESSION_REAP_AUTHORIZED)
    assert event["daemon_stop_succeeded"] is True
    assert event["mutations"] == ["session_status_completed", "daemon_stopped"]
    assert _read_audit_outbox() == []


def test_retry_records_a_failed_stop_in_the_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _complete_seeded_session(_seed(tmp_path, home))
    _write_outbox([_outbox_record()])

    class _FailingStopDaemon(FakeNativeDaemonClient):
        def stop(self, short_id: str) -> None:
            msg = f"stop {short_id} failed"
            raise RuntimeError(msg)

    _retry_pending_audits(_FailingStopDaemon())

    (event,) = _events(OrchestratorEventType.SESSION_REAP_AUTHORIZED)
    assert event["daemon_stop_succeeded"] is False
    assert event["daemon_stop_error"] == "daemon stop failed"
    assert event["mutations"] == ["session_status_completed"]


def test_retry_leaves_intent_pending_while_session_is_still_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _seed(tmp_path, home)
    _write_outbox([_outbox_record(), _outbox_record(session_id="ghost")])

    _retry_pending_audits(daemon)

    assert daemon.stop_calls == []
    assert [r["session_id"] for r in _read_audit_outbox()] == [_SID, "ghost"]
    assert _events(OrchestratorEventType.SESSION_REAP_AUTHORIZED) == []


def test_retry_skips_a_record_whose_state_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _write_outbox([_outbox_record()])

    def _boom() -> CwState:
        msg = "state unreadable"
        raise OSError(msg)

    monkeypatch.setattr("cw.doctor.routed_result_wedge.load_state", _boom)

    _retry_pending_audits(daemon)

    assert daemon.stop_calls == []
    assert len(_read_audit_outbox()) == 1


def test_retry_emits_a_pending_event_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, _daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    _write_outbox([_outbox_record(status="pending_event", stop_succeeded=True)])

    assert reap_routed_result_findings([]) == []

    (event,) = _events(OrchestratorEventType.SESSION_REAP_AUTHORIZED)
    assert event["daemon_stop_succeeded"] is True
    assert _read_audit_outbox() == []


def test_retry_with_unreadable_outbox_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, daemon = _install_fake_daemon_roster(tmp_path, monkeypatch)
    path = _audit_outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    _retry_pending_audits(daemon)

    assert daemon.stop_calls == []


def test_finalize_without_a_durable_intent_returns_none() -> None:
    assert (
        _finalize_audit_intent(
            "no-such-session",
            mutations=["session_status_completed"],
            stop_succeeded=True,
            stop_error=None,
        )
        is None
    )


def test_emit_failure_keeps_the_record_in_the_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _outbox_record(status="pending_event")
    _write_outbox([record])

    def _inbox_down(*_args: object, **_kwargs: object) -> None:
        msg = "event inbox unavailable"
        raise OSError(msg)

    monkeypatch.setattr("cw.doctor.routed_result_wedge.record_event", _inbox_down)

    assert _emit_audit_record(record) is False
    assert len(_read_audit_outbox()) == 1


def test_outbox_cleanup_failure_after_emit_still_reports_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _outbox_record(status="pending_event")
    _write_outbox([record])

    def _write_fails(_records: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(
        "cw.doctor.routed_result_wedge._write_audit_outbox", _write_fails
    )

    assert _emit_audit_record(record) is True
    assert len(_events(OrchestratorEventType.SESSION_REAP_AUTHORIZED)) == 1


def test_finalize_skips_records_for_other_sessions() -> None:
    _write_outbox([_outbox_record(session_id="other"), _outbox_record()])

    record = _finalize_audit_intent(
        _SID,
        mutations=["session_status_completed", "daemon_stopped"],
        stop_succeeded=True,
        stop_error=None,
    )

    assert record is not None
    assert record["session_id"] == _SID
    assert record["status"] == "pending_event"
    by_id = {r["session_id"]: r for r in _read_audit_outbox()}
    assert by_id["other"]["status"] == "pending_stop"
    assert by_id[_SID]["status"] == "pending_event"
