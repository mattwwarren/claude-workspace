"""Unit tests for the write-ahead and retry paths of ``cw.codex_legacy_recovery``.

Sibling of ``test_codex_legacy_recovery.py`` (split to keep that file under
the module-size ceiling). These exercise the pending-intent reconciliation,
the prior-failure verification, marker-write failure handling and client
scope resolution directly, with in-memory snapshots: the outcomes they
assert on are the marker's, so no worktree or process scan is involved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw import codex_legacy_recovery
from cw.codex_legacy_recovery import (
    REASON_EVENT_DELIVERY_FAILED,
    REASON_PENDING_RECOVERY,
    REASON_STATE_WRITE_FAILED,
    _pending_disposition,
    _pending_intended_disposition,
    _reconcile_pending,
    _record_outcome,
    _record_pending,
    _resolve_client_scope,
    _Snapshot,
    _verify_prior_failure,
    load_codex_legacy_marker,
)
from cw.exceptions import CodexLegacyRecoveryMarkerError, CwError
from cw.models import (
    ClientConfig,
    CodexLegacyDisposition,
    CodexLegacyRecoveryMarker,
    OrchestratorConfig,
    Outcome,
    PendingOutcome,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
    UnresolvedEntry,
)
from cw.reconcile.local import CODEX_HARVEST_ORPHANED_DISPOSITION
from tests.conftest import _make_daemon_session

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session

_CLIENT = "client-a"
_TICKET = "T-1"
_SID = "sess-1"


def _session(
    *,
    status: SessionStatus = SessionStatus.COMPLETED,
    recovery_disposition: str | None = None,
    worktree: Path | None = None,
) -> Session:
    return _make_daemon_session(
        id=_SID,
        name=f"{_CLIENT}/auto-dev/{_TICKET}",
        status=status,
        recovery_disposition=recovery_disposition,
        worktree_path=worktree,
    )


def _task(
    status: QueueItemStatus,
    session_id: str | None = None,
    *,
    client: str = _CLIENT,
) -> TicketTask:
    return TicketTask(
        ticket_id=_TICKET,
        client=client,
        stage=Stage.REVIEW,
        status=status,
        session_id=session_id,
    )


def _snapshot(
    sessions: list[Session],
    tasks: list[TicketTask],
    clients: dict[str, ClientConfig] | None = None,
) -> _Snapshot:
    return _Snapshot(
        sessions=sessions,
        tasks={(task.ticket_id, task.client): task for task in tasks},
        clients=clients or {},
        config=OrchestratorConfig(),
    )


def _pending(client: str = _CLIENT) -> PendingOutcome:
    return PendingOutcome(
        session_id=_SID,
        ticket_id=_TICKET,
        client=client,
        prior_status=SessionStatus.ACTIVE,
        prior_stage=Stage.REVIEW,
    )


def _failed_outcome() -> Outcome:
    return Outcome(
        session_id=_SID,
        ticket_id=_TICKET,
        client=_CLIENT,
        disposition=CodexLegacyDisposition.FAILED,
        prior_status=SessionStatus.ACTIVE,
        prior_stage=Stage.REVIEW,
    )


def _marker_with_unresolved(reason: str) -> CodexLegacyRecoveryMarker:
    return CodexLegacyRecoveryMarker(
        outcomes=[_failed_outcome()],
        unresolved=[
            UnresolvedEntry(
                session_id=_SID, client=_CLIENT, ticket_id=_TICKET, reason=reason
            )
        ],
    )


def _confirm_events(monkeypatch: pytest.MonkeyPatch, confirmed: bool) -> None:
    monkeypatch.setattr(
        codex_legacy_recovery, "_events_confirmed", lambda *_args: confirmed
    )


def _client(tmp_path: Path, name: str) -> ClientConfig:
    return ClientConfig(name=name, workspace_path=tmp_path)


# --------------------------------------------------------------------------- #
# _pending_intended_disposition / _pending_disposition
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("session", "task", "expected"),
    [
        (None, _task(QueueItemStatus.PENDING), None),
        (_session(), None, None),
        (
            _session(status=SessionStatus.ACTIVE),
            _task(QueueItemStatus.PENDING),
            None,
        ),
        (_session(), _task(QueueItemStatus.PENDING), CodexLegacyDisposition.REQUEUED),
        (
            _session(recovery_disposition=CODEX_HARVEST_ORPHANED_DISPOSITION),
            _task(QueueItemStatus.BLOCKED_ON_USER),
            CodexLegacyDisposition.PARKED,
        ),
        (
            _session(recovery_disposition=None),
            _task(QueueItemStatus.BLOCKED_ON_USER),
            None,
        ),
        (_session(), _task(QueueItemStatus.RUNNING, _SID), None),
        (_session(), _task(QueueItemStatus.PENDING, "other"), None),
    ],
    ids=[
        "session-missing",
        "row-missing",
        "session-still-live",
        "requeued",
        "parked",
        "blocked-without-orphan-disposition",
        "row-still-running",
        "pending-row-still-bound",
    ],
)
def test_pending_intended_disposition_infers_the_queue_transition(
    session: Session | None,
    task: TicketTask | None,
    expected: CodexLegacyDisposition | None,
) -> None:
    snapshot = _snapshot(
        [session] if session is not None else [],
        [task] if task is not None else [],
    )

    assert _pending_intended_disposition(_pending(), snapshot) is expected


@pytest.mark.parametrize(
    ("confirmed", "expected"),
    [(True, CodexLegacyDisposition.REQUEUED), (False, None)],
    ids=["events-confirmed", "events-missing"],
)
def test_pending_disposition_requires_events_for_an_inferred_transition(
    monkeypatch: pytest.MonkeyPatch,
    confirmed: bool,
    expected: CodexLegacyDisposition | None,
) -> None:
    _confirm_events(monkeypatch, confirmed)
    snapshot = _snapshot([_session()], [_task(QueueItemStatus.PENDING)])

    assert _pending_disposition(_pending(), snapshot) is expected


def test_pending_disposition_is_none_without_an_inferred_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _confirm_events(monkeypatch, True)
    snapshot = _snapshot([], [])

    assert _pending_disposition(_pending(), snapshot) is None


# --------------------------------------------------------------------------- #
# _reconcile_pending
# --------------------------------------------------------------------------- #


def test_reconcile_pending_leaves_another_clients_intent_alone(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    marker = CodexLegacyRecoveryMarker(pending=[_pending("client-b")])
    snapshot = _snapshot([], [], {"client-b": _client(tmp_path, "client-b")})

    _reconcile_pending(marker, snapshot, _CLIENT)

    assert [p.client for p in marker.pending] == ["client-b"]
    assert marker.outcomes == []


@pytest.mark.parametrize(
    ("confirmed", "disposition", "reason"),
    [
        (True, CodexLegacyDisposition.REQUEUED, None),
        (
            False,
            CodexLegacyDisposition.FAILED,
            REASON_EVENT_DELIVERY_FAILED,
        ),
    ],
    ids=["events-confirmed", "events-missing"],
)
def test_reconcile_pending_resolves_a_completed_operation(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    confirmed: bool,
    disposition: CodexLegacyDisposition,
    reason: str | None,
) -> None:
    _confirm_events(monkeypatch, confirmed)
    marker = CodexLegacyRecoveryMarker(pending=[_pending()])
    snapshot = _snapshot([_session()], [_task(QueueItemStatus.PENDING)])

    _reconcile_pending(marker, snapshot, _CLIENT)

    assert marker.pending == []
    [outcome] = marker.outcomes
    assert outcome.disposition is disposition
    # The recorded facts are the pre-recovery ones, not the current state.
    assert outcome.prior_status is SessionStatus.ACTIVE
    assert outcome.prior_stage is Stage.REVIEW
    assert [entry.reason for entry in marker.unresolved] == ([reason] if reason else [])
    persisted = load_codex_legacy_marker()
    assert persisted is not None
    assert persisted.pending == []


def test_reconcile_pending_keeps_an_intent_whose_act_never_ran(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A still-live candidate with its own row is retried, not failed."""
    live = _session(status=SessionStatus.ACTIVE, worktree=tmp_path / "gone")
    marker = CodexLegacyRecoveryMarker(pending=[_pending()])
    snapshot = _snapshot([live], [_task(QueueItemStatus.RUNNING, _SID)])

    _reconcile_pending(marker, snapshot, None)

    assert [p.session_id for p in marker.pending] == [_SID]
    assert marker.outcomes == []


@pytest.mark.parametrize(
    "case",
    ["session-gone", "row-unbound", "client-changed"],
)
def test_reconcile_pending_fails_an_intent_that_cannot_be_retried(
    tmp_config_dir: Path, tmp_path: Path, case: str
) -> None:
    live = _session(status=SessionStatus.ACTIVE, worktree=tmp_path / "gone")
    sessions = [] if case == "session-gone" else [live]
    row_session = "someone-else" if case == "row-unbound" else _SID
    pending = _pending("client-b") if case == "client-changed" else _pending()
    tasks = [
        _task(
            QueueItemStatus.RUNNING,
            row_session,
            client=pending.client,
        )
    ]
    marker = CodexLegacyRecoveryMarker(pending=[pending])

    _reconcile_pending(marker, _snapshot(sessions, tasks), None)

    assert marker.pending == []
    [outcome] = marker.outcomes
    assert outcome.disposition is CodexLegacyDisposition.FAILED
    assert [entry.reason for entry in marker.unresolved] == [REASON_PENDING_RECOVERY]


# --------------------------------------------------------------------------- #
# _verify_prior_failure
# --------------------------------------------------------------------------- #


def test_verify_prior_failure_ignores_a_failure_it_did_not_record() -> None:
    marker = CodexLegacyRecoveryMarker(outcomes=[_failed_outcome()])

    assert _verify_prior_failure(_failed_outcome(), marker, _snapshot([], [])) is None


def test_verify_prior_failure_ignores_a_non_recoverable_reason() -> None:
    marker = _marker_with_unresolved("live_writer")

    assert _verify_prior_failure(_failed_outcome(), marker, _snapshot([], [])) is None


@pytest.mark.parametrize(
    ("session", "task"),
    [
        (None, _task(QueueItemStatus.PENDING)),
        (_session(), None),
        (
            _session(status=SessionStatus.ACTIVE),
            _task(QueueItemStatus.PENDING),
        ),
        (_session(), _task(QueueItemStatus.PENDING, "someone-else")),
        (_session(), _task(QueueItemStatus.BLOCKED_ON_USER)),
        (
            _session(recovery_disposition="something-else"),
            _task(QueueItemStatus.PENDING),
        ),
    ],
    ids=[
        "session-gone",
        "row-gone",
        "session-not-completed",
        "row-still-bound",
        "no-intended-transition",
        "requeue-blocked-by-recovery-disposition",
    ],
)
def test_verify_prior_failure_stays_failed_until_the_transition_is_visible(
    monkeypatch: pytest.MonkeyPatch,
    session: Session | None,
    task: TicketTask | None,
) -> None:
    _confirm_events(monkeypatch, True)
    marker = _marker_with_unresolved(REASON_STATE_WRITE_FAILED)
    snapshot = _snapshot(
        [session] if session is not None else [],
        [task] if task is not None else [],
    )

    resolution = _verify_prior_failure(_failed_outcome(), marker, snapshot)

    assert resolution is not None
    assert resolution.disposition is CodexLegacyDisposition.FAILED
    assert resolution.reason == REASON_STATE_WRITE_FAILED


@pytest.mark.parametrize(
    ("session", "task", "expected"),
    [
        (
            _session(),
            _task(QueueItemStatus.PENDING),
            CodexLegacyDisposition.REQUEUED,
        ),
        (
            _session(recovery_disposition=CODEX_HARVEST_ORPHANED_DISPOSITION),
            _task(QueueItemStatus.BLOCKED_ON_USER),
            CodexLegacyDisposition.PARKED,
        ),
    ],
    ids=["requeued", "parked"],
)
def test_verify_prior_failure_resolves_once_state_and_events_agree(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    task: TicketTask,
    expected: CodexLegacyDisposition,
) -> None:
    _confirm_events(monkeypatch, True)
    marker = _marker_with_unresolved(REASON_EVENT_DELIVERY_FAILED)

    resolution = _verify_prior_failure(
        _failed_outcome(), marker, _snapshot([session], [task])
    )

    assert resolution is not None
    assert resolution.disposition is expected
    assert resolution.reason is None


def test_verify_prior_failure_stays_failed_when_events_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _confirm_events(monkeypatch, False)
    marker = _marker_with_unresolved(REASON_EVENT_DELIVERY_FAILED)

    resolution = _verify_prior_failure(
        _failed_outcome(),
        marker,
        _snapshot([_session()], [_task(QueueItemStatus.PENDING)]),
    )

    assert resolution is not None
    assert resolution.disposition is CodexLegacyDisposition.FAILED
    assert resolution.reason == REASON_EVENT_DELIVERY_FAILED


# --------------------------------------------------------------------------- #
# Marker write failures
# --------------------------------------------------------------------------- #


def _fail_marker_save(monkeypatch: pytest.MonkeyPatch) -> None:
    def _save(_marker: CodexLegacyRecoveryMarker) -> None:
        msg = "read-only file system"
        raise OSError(msg)

    monkeypatch.setattr(codex_legacy_recovery, "save_codex_legacy_marker", _save)


def test_record_outcome_write_failure_restores_the_intent_and_raises(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = CodexLegacyRecoveryMarker(pending=[_pending()])
    _fail_marker_save(monkeypatch)

    with pytest.raises(CodexLegacyRecoveryMarkerError, match=_SID) as excinfo:
        _record_outcome(marker, _failed_outcome(), "audit_write_failed")

    assert isinstance(excinfo.value.__cause__, OSError)
    assert marker.outcomes == []
    assert marker.unresolved == []
    assert [p.session_id for p in marker.pending] == [_SID]
    assert marker.scanned == 0
    assert marker.failed == 0


def test_record_pending_write_failure_restores_the_marker_and_reraises(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker_with_unresolved("live_writer")
    marker.scanned = marker.failed = 1
    _fail_marker_save(monkeypatch)

    with pytest.raises(OSError, match="read-only file system"):
        _record_pending(marker, _pending())

    assert [o.session_id for o in marker.outcomes] == [_SID]
    assert [u.session_id for u in marker.unresolved] == [_SID]
    assert marker.pending == []
    assert (marker.scanned, marker.failed) == (1, 1)


# --------------------------------------------------------------------------- #
# Client scope resolution
# --------------------------------------------------------------------------- #


def test_scope_rejects_client_with_all_clients(tmp_path: Path) -> None:
    clients = {_CLIENT: _client(tmp_path, _CLIENT)}

    with pytest.raises(CwError, match="mutually exclusive"):
        _resolve_client_scope(CodexLegacyRecoveryMarker(), clients, _CLIENT, True)


def test_scope_rejects_all_clients_alone(tmp_path: Path) -> None:
    clients = {_CLIENT: _client(tmp_path, _CLIENT)}

    with pytest.raises(CwError, match="--all-clients is not supported"):
        _resolve_client_scope(CodexLegacyRecoveryMarker(), clients, None, True)


def test_scope_rejects_an_unconfigured_client(tmp_path: Path) -> None:
    clients = {_CLIENT: _client(tmp_path, _CLIENT)}

    with pytest.raises(CwError, match="unknown client 'nope'"):
        _resolve_client_scope(CodexLegacyRecoveryMarker(), clients, "nope", False)


def test_scope_requires_a_choice_among_several_clients(tmp_path: Path) -> None:
    marker = CodexLegacyRecoveryMarker()
    clients = {name: _client(tmp_path, name) for name in ("client-a", "client-b")}

    with pytest.raises(CwError, match="select exactly one client"):
        _resolve_client_scope(marker, clients, None, False)

    assert marker.client_scope is None


def test_scope_defaults_to_the_only_client_and_records_it(tmp_path: Path) -> None:
    marker = CodexLegacyRecoveryMarker()
    clients = {_CLIENT: _client(tmp_path, _CLIENT)}

    assert _resolve_client_scope(marker, clients, None, False) == _CLIENT
    assert marker.client_scope == _CLIENT


def test_scope_is_empty_when_no_client_is_configured() -> None:
    marker = CodexLegacyRecoveryMarker()

    assert _resolve_client_scope(marker, {}, None, False) == ""
    assert marker.client_scope == ""
