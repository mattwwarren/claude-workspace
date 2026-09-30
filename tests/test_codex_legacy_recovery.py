"""Tests for cw.codex_legacy_recovery — ``cw codex migrate-legacy`` (#2389).

RFC 0014 B1.

A legacy codex session predates RFC 0014 A2: it ran its review on a thread
inside ``serve`` and carries no ``local_liveness`` handle, so the A1 harvest
sweep never sees it. This one-shot recovery finds every such session still
live, scans its worktree for a codex writer first, and only then routes it
through the A1 gate-audit-close path (``act_on_codex_harvest_candidate``). A
marker file records the per-session outcomes; a complete marker is the B2 gate.

Fixtures are real git worktrees, real state/queue files, and the real audit
event log. The only patches are the process scan (via the shared helpers) and
the orchestrator config the gate resolves against.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest
from click.testing import CliRunner

from cw import codex_legacy_recovery
from cw.cli import main
from cw.codex_legacy_recovery import (
    CODEX_LEGACY_RECOVERY_REASON,
    REASON_AUDIT_WRITE_FAILED,
    REASON_CLIENT_MISSING,
    REASON_LIVE_WRITER,
    REASON_STATE_WRITE_FAILED,
    _headless_scan_kind,
    _HeadlessScanKind,
    load_codex_legacy_marker,
    run_codex_legacy_recovery,
    save_codex_legacy_marker,
)
from cw.config import (
    codex_legacy_recovery_file,
    dev_queue_file,
    load_state,
    save_state,
    state_file,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dispatch.claim import claimed_row
from cw.events import read_events
from cw.exceptions import CodexLegacyRecoveryMarkerError
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CodexLegacyDisposition,
    CodexLegacyRecoveryMarker,
    CompletionReason,
    LegacyRecoveryStatus,
    LocalLivenessHandle,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import codex_boot
from cw.reconcile import local as reconcile_local
from cw.reconcile.codex_boot import (
    CODEX_ORPHAN_CLEAN_REQUEUE_REASON,
    CODEX_ORPHAN_CLOSE_REASON,
    reap_orphaned_codex_sessions_at_boot,
)
from cw.reconcile.local import (
    CODEX_HARVEST_CLEAN_REQUEUE_REASON,
    CODEX_HARVEST_ORPHANED_DISPOSITION,
)
from tests._codex_recovery_helpers import (
    _STARTED_AT,
    _attention_events,
    _completed_events,
    _no_codex_process,
    _requeued_events,
    _use_auto_reap_policy,
)
from tests._reconcile_helpers import _mk_headless_daemon_session
from tests.conftest import (
    _make_daemon_session,
    _write_backend_clients_yaml,
    commit_tracked_file,
    git_in,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from cw.models import Session

_NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(hours=1)
_CLIENT = "client-a"
_WRITER_PID = 4242
_GATE_KEYS = ("reap_policy_auto", "fix_loop_disabled", "worktree_clean", "head_unmoved")

_RowShape = Literal["bound", "other", "linked", "none"]


# --------------------------------------------------------------------------- #
# Fixtures and seeding helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def codex_clients(tmp_config_dir: Path, tmp_path: Path) -> Path:
    """clients.yaml whose client-a runs its review stage on codex."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    _write_backend_clients_yaml(tmp_config_dir, workspace, "codex")
    return workspace


def _use_legacy_config(
    monkeypatch: pytest.MonkeyPatch, config: OrchestratorConfig | None = None
) -> OrchestratorConfig:
    """Pin the config B1's gate resolves against (``reap_policy: auto`` default)."""
    pinned = config if config is not None else _use_auto_reap_policy()
    monkeypatch.setattr(codex_legacy_recovery, "load_effective_config", lambda: pinned)
    return pinned


def _append_session(session: Session) -> None:
    state = load_state()
    state.sessions.append(session)
    save_state(state)


def _append_row(task: TicketTask) -> None:
    store = load_dev_queue()
    store.tasks.append(task)
    save_dev_queue(store)


def _row(
    ticket_id: str,
    session_id: str,
    shape: _RowShape,
    *,
    stage_base_ref: str | None = None,
    client: str = _CLIENT,
) -> TicketTask | None:
    if shape == "none":
        return None
    if shape == "linked":
        # What the boot pass leaves for a live-writer park (#2307): the row is
        # parked, its own session_id cleared, and linked back to the session.
        return TicketTask(
            ticket_id=ticket_id,
            client=client,
            stage=Stage.REVIEW,
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id=None,
            codex_orphan_session_id=session_id,
            stage_base_ref=stage_base_ref,
        )
    return TicketTask(
        ticket_id=ticket_id,
        client=client,
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
        session_id=session_id if shape == "bound" else "someone-else",
        stage_base_ref=stage_base_ref,
    )


def _seed_legacy(
    make_git_repo: Callable[..., Path],
    ticket_id: str,
    *,
    dirty: bool = False,
    head_moved: bool = False,
    row: _RowShape = "bound",
) -> Session:
    """One live legacy codex session (no liveness handle) in a real git repo.

    The worktree is clean apart from the review verdict and HEAD sits on the
    row's ``stage_base_ref`` unless *dirty* / *head_moved* say otherwise.
    """
    repo = make_git_repo(f"wt-{ticket_id}")
    session = _mk_headless_daemon_session(ticket_id, repo, _STARTED_AT)
    commit_tracked_file(repo, ".claude/cw-context.json", '{"headless": true}')
    head_sha = git_in(repo, "rev-parse", "HEAD")
    _append_session(session)
    task = _row(ticket_id, session.id, row, stage_base_ref=head_sha)
    if task is not None:
        _append_row(task)
    (repo / ".claude" / "review-verdict.md").write_text("verdict text\n")
    if dirty:
        (repo / "scratch.txt").write_text("partial work\n")
    if head_moved:
        commit_tracked_file(repo, "later.py")
    return session


def _seed_unscannable(
    ticket_id: str,
    worktree: Path | None,
    *,
    client: str = _CLIENT,
    context: bytes | None = None,
) -> Session:
    """A live legacy session whose worktree/context is set up by hand.

    *context*, when given, is written raw to ``.claude/cw-context.json``.
    """
    if worktree is not None and context is not None:
        (worktree / ".claude").mkdir(parents=True, exist_ok=True)
        (worktree / ".claude" / "cw-context.json").write_bytes(context)
    session = _make_daemon_session(
        id=ticket_id,
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        worktree_path=worktree,
        surface_ref="fake-short-id",
        started_at=_STARTED_AT,
    )
    _append_session(session)
    task = _row(ticket_id, session.id, "bound", client=client)
    assert task is not None
    _append_row(task)
    return session


def _writers_in(monkeypatch: pytest.MonkeyPatch, *worktrees: Path | None) -> None:
    """Report a live codex writer in exactly *worktrees*, none elsewhere."""
    live = {wt.resolve() for wt in worktrees if wt is not None}

    def _scan(worktree: Path) -> list[int]:
        return [_WRITER_PID] if worktree.resolve() in live else []

    monkeypatch.setattr(codex_boot, "_codex_processes_in", _scan)


def _forbid_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    def _scan(worktree: Path) -> list[int]:
        msg = f"the process scan must not run for {worktree}"
        raise AssertionError(msg)

    monkeypatch.setattr(codex_boot, "_codex_processes_in", _scan)


def _fail_audit_for(
    monkeypatch: pytest.MonkeyPatch, ticket_id: str
) -> Callable[..., object]:
    """Fail the SESSION_COMPLETED audit write for *ticket_id* only.

    Returns the real ``record_event`` so a test can restore it for a retry.
    """
    real: Callable[..., object] = reconcile_local.record_event

    def _record(
        event_type: OrchestratorEventType,
        payload: dict[str, object],
        *args: object,
        **kwargs: object,
    ) -> object:
        if (
            event_type is OrchestratorEventType.SESSION_COMPLETED
            and payload.get("ticket_id") == ticket_id
        ):
            msg = "disk full"
            raise OSError(msg)
        return real(event_type, payload, *args, **kwargs)

    monkeypatch.setattr(reconcile_local, "record_event", _record)
    return real


def _event_types() -> list[OrchestratorEventType]:
    return [event.type for event in read_events()]


def _session(session_id: str) -> Session:
    return next(s for s in load_state().sessions if s.id == session_id)


def _task(ticket_id: str) -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)


def _dispositions(marker: CodexLegacyRecoveryMarker) -> dict[str, str]:
    return {o.session_id: o.disposition.value for o in marker.outcomes}


def _unresolved(marker: CodexLegacyRecoveryMarker) -> dict[str, str]:
    return {u.session_id: u.reason for u in marker.unresolved}


def _assert_counts_consistent(marker: CodexLegacyRecoveryMarker) -> None:
    buckets = (
        marker.requeued
        + marker.parked
        + marker.failed
        + marker.skipped_already_handled
        + marker.skipped_writer_live
    )
    assert marker.scanned == buckets == len(marker.outcomes)


def _file_bytes() -> tuple[bytes, bytes, bytes]:
    return (
        state_file().read_bytes(),
        dev_queue_file().read_bytes(),
        codex_legacy_recovery_file().read_bytes(),
    )


def _wrap_after_scan(
    monkeypatch: pytest.MonkeyPatch, effect: Callable[[], None]
) -> None:
    """Run *effect* right after B1's unlocked live-writer scan finds nothing.

    That is the window between the snapshot and the locked revalidation, the
    point a concurrent actor can change the session or its row.
    """
    real = codex_legacy_recovery.live_writer_park

    def _scan_then_race(worktree: Path) -> object:
        park = real(worktree)
        if park is None:
            effect()
        return park

    monkeypatch.setattr(codex_legacy_recovery, "live_writer_park", _scan_then_race)


# Mixed population: every disposition the recovery can reach in one run.
_MIXED_EXPECTED = {
    "T-clean": CodexLegacyDisposition.REQUEUED,
    "T-dirty": CodexLegacyDisposition.PARKED,
    "T-moved": CodexLegacyDisposition.PARKED,
    "T-other": CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED,
    "T-linked": CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED,
    "T-norow": CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED,
    "T-live": CodexLegacyDisposition.SKIPPED_WRITER_LIVE,
    "T-audit": CodexLegacyDisposition.FAILED,
}


def _seed_mixed(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., object]:
    """Seed the mixed population; returns the real ``record_event``."""
    _seed_legacy(make_git_repo, "T-clean")
    _seed_legacy(make_git_repo, "T-dirty", dirty=True)
    _seed_legacy(make_git_repo, "T-moved", head_moved=True)
    _seed_legacy(make_git_repo, "T-other", row="other")
    _seed_legacy(make_git_repo, "T-linked", row="linked")
    _seed_legacy(make_git_repo, "T-norow", row="none")
    live = _seed_legacy(make_git_repo, "T-live")
    _seed_legacy(make_git_repo, "T-audit")
    _writers_in(monkeypatch, live.worktree_path)
    return _fail_audit_for(monkeypatch, "T-audit")


# --------------------------------------------------------------------------- #
# Mixed population, marker lifecycle
# --------------------------------------------------------------------------- #


def test_mixed_population_counts_every_disposition(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _seed_mixed(make_git_repo, monkeypatch)

    report = run_codex_legacy_recovery(now=_NOW)

    marker = report.marker
    assert report.status is LegacyRecoveryStatus.PARTIAL
    assert _dispositions(marker) == {k: v.value for k, v in _MIXED_EXPECTED.items()}
    assert (
        marker.scanned,
        marker.requeued,
        marker.parked,
        marker.failed,
        marker.skipped_already_handled,
        marker.skipped_writer_live,
    ) == (8, 1, 2, 1, 3, 1)
    _assert_counts_consistent(marker)
    assert marker.completed_at is None
    assert _unresolved(marker) == {
        "T-live": REASON_LIVE_WRITER,
        "T-audit": REASON_AUDIT_WRITE_FAILED,
    }
    # Acted-on sessions close; everything else is left exactly as found.
    for ticket_id in ("T-clean", "T-dirty", "T-moved"):
        closed = _session(ticket_id)
        assert closed.status is SessionStatus.COMPLETED
        assert closed.completed_reason is CompletionReason.CRASHED
        assert closed.completed_at == _NOW
    for ticket_id in ("T-other", "T-linked", "T-norow", "T-live", "T-audit"):
        assert _session(ticket_id).status is SessionStatus.ACTIVE
    assert _task("T-clean").status is QueueItemStatus.PENDING
    assert _task("T-dirty").status is QueueItemStatus.BLOCKED_ON_USER
    assert _task("T-dirty").disposition == CODEX_HARVEST_ORPHANED_DISPOSITION
    assert _task("T-moved").status is QueueItemStatus.BLOCKED_ON_USER
    assert _task("T-other").session_id == "someone-else"
    assert _task("T-linked").status is QueueItemStatus.BLOCKED_ON_USER
    assert _task("T-live").status is QueueItemStatus.RUNNING
    assert _task("T-audit").status is QueueItemStatus.RUNNING


def test_outcomes_record_prior_status_and_stage(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _seed_mixed(make_git_repo, monkeypatch)

    marker = run_codex_legacy_recovery(now=_NOW).marker

    by_id = {o.session_id: o for o in marker.outcomes}
    assert by_id["T-clean"].prior_status is SessionStatus.ACTIVE
    assert by_id["T-clean"].prior_stage is Stage.REVIEW
    assert by_id["T-clean"].ticket_id == "T-clean"
    assert by_id["T-clean"].client == _CLIENT
    # No row, no stage to record.
    assert by_id["T-norow"].prior_stage is None


def test_partial_run_retries_only_unresolved_then_completes(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    real_record_event = _seed_mixed(make_git_repo, monkeypatch)
    run_codex_legacy_recovery(now=_NOW)

    # Clear both causes: the writer exits, the audit log is writable again.
    _writers_in(monkeypatch)
    monkeypatch.setattr(reconcile_local, "record_event", real_record_event)
    report = run_codex_legacy_recovery(now=_LATER)

    marker = report.marker
    assert report.status is LegacyRecoveryStatus.COMPLETED
    assert marker.completed_at == _LATER
    assert marker.unresolved == []
    assert _dispositions(marker)["T-live"] == CodexLegacyDisposition.REQUEUED
    assert _dispositions(marker)["T-audit"] == CodexLegacyDisposition.REQUEUED
    assert (marker.scanned, marker.requeued, marker.parked, marker.failed) == (
        8,
        3,
        2,
        0,
    )
    assert (marker.skipped_already_handled, marker.skipped_writer_live) == (3, 0)
    _assert_counts_consistent(marker)
    # Resolved sessions were not touched a second time.
    assert len(_completed_events("retry-completed")) == 5
    assert len(_requeued_events("retry-requeued")) == 3


def test_completed_marker_makes_a_third_run_a_no_op(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    real_record_event = _seed_mixed(make_git_repo, monkeypatch)
    run_codex_legacy_recovery(now=_NOW)
    _writers_in(monkeypatch)
    monkeypatch.setattr(reconcile_local, "record_event", real_record_event)
    completed = run_codex_legacy_recovery(now=_LATER)
    before = _file_bytes()
    events_before = len(read_events())

    report = run_codex_legacy_recovery(now=_LATER + timedelta(days=1))

    assert report.status is LegacyRecoveryStatus.ALREADY_COMPLETED
    assert report.marker == completed.marker
    assert _file_bytes() == before
    assert len(read_events()) == events_before
    # A fresh loader read (a new process) sees the same persisted marker.
    assert load_codex_legacy_marker() == completed.marker


def test_empty_population_completes_with_zero_counts(
    codex_clients: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_legacy_config(monkeypatch)

    report = run_codex_legacy_recovery(now=_NOW)

    assert report.status is LegacyRecoveryStatus.COMPLETED
    assert report.marker.completed_at == _NOW
    assert report.marker.scanned == 0
    assert load_codex_legacy_marker() == report.marker


def test_marker_resolves_under_the_isolated_state_dir(tmp_path: Path) -> None:
    assert codex_legacy_recovery_file().is_relative_to(tmp_path)


# --------------------------------------------------------------------------- #
# Audit payload and observability
# --------------------------------------------------------------------------- #


def test_clean_case_audit_payload_is_the_legacy_shape(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    _seed_legacy(make_git_repo, "T-clean")

    run_codex_legacy_recovery(now=_NOW)

    [payload] = _completed_events("legacy-audit")
    assert payload["legacy"] is True
    assert payload["reason"] == CODEX_LEGACY_RECOVERY_REASON
    assert payload["detail"] == CODEX_HARVEST_CLEAN_REQUEUE_REASON
    assert payload["pid"] is None
    assert payload["start_time_ns"] is None
    assert payload["executor"] == "codex"
    assert payload["crashed"] is True
    assert payload["disposition"] == "requeued"
    assert payload["prior_status"] == SessionStatus.ACTIVE.value
    gate_checks = payload["gate_checks"]
    assert isinstance(gate_checks, dict)
    assert tuple(gate_checks) == _GATE_KEYS
    assert all(gate_checks.values())


def test_clean_case_emits_one_audit_and_one_requeue(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-clean")

    run_codex_legacy_recovery(now=_NOW)

    # TASK_TRANSITION is the row transition primitive's own record.
    assert _event_types() == [
        OrchestratorEventType.SESSION_COMPLETED,
        OrchestratorEventType.TASK_TRANSITION,
        OrchestratorEventType.TICKET_REQUEUED,
    ]
    [requeued] = _requeued_events("legacy-obs-requeue")
    assert requeued["reason"] == CODEX_HARVEST_CLEAN_REQUEUE_REASON
    assert requeued["session_id"] == session.id


def test_parked_case_emits_one_audit_and_one_attention(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    _seed_legacy(make_git_repo, "T-dirty", dirty=True)

    run_codex_legacy_recovery(now=_NOW)

    assert _event_types() == [
        OrchestratorEventType.SESSION_COMPLETED,
        OrchestratorEventType.TASK_TRANSITION,
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
    ]
    [attention] = _attention_events("legacy-obs-park")
    assert attention["paused_status"] == CODEX_HARVEST_ORPHANED_DISPOSITION


# --------------------------------------------------------------------------- #
# Races
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dirty", [False, True], ids=["requeue", "park"])
def test_row_moved_after_revalidation_is_already_handled(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    dirty: bool,
) -> None:
    """The row is re-pointed between B1's locked revalidation and the callee's
    own dev_queue_lock: the transition is lost, never doubled."""
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-race", dirty=dirty)
    real = codex_legacy_recovery.stale_snapshot_reason

    def _stale_then_race(fresh: Session, snapshot: Session, ticket_id: str) -> object:
        reason = real(fresh, snapshot, ticket_id)
        if reason is None:
            store = load_dev_queue()
            store.tasks[0].session_id = "usurper"
            save_dev_queue(store)
        return reason

    monkeypatch.setattr(
        codex_legacy_recovery, "stale_snapshot_reason", _stale_then_race
    )

    marker = run_codex_legacy_recovery(now=_NOW).marker

    assert _dispositions(marker) == {
        session.id: CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
    }
    _assert_counts_consistent(marker)
    assert marker.completed_at == _NOW
    assert len(_completed_events("race-completed")) == 1
    assert _requeued_events("race-requeued") == []
    assert _attention_events("race-attention") == []
    task = _task("T-race")
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == "usurper"
    assert _session(session.id).status is SessionStatus.COMPLETED


def test_boot_sweep_winning_the_race_is_already_handled(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_auto_reap_policy(monkeypatch)
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-race")
    _wrap_after_scan(monkeypatch, reap_orphaned_codex_sessions_at_boot)

    marker = run_codex_legacy_recovery(now=_NOW).marker

    assert _dispositions(marker) == {
        session.id: CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
    }
    [completed] = _completed_events("boot-race-completed")
    assert completed["reason"] == CODEX_ORPHAN_CLOSE_REASON
    [requeued] = _requeued_events("boot-race-requeued")
    assert requeued["reason"] == CODEX_ORPHAN_CLEAN_REQUEUE_REASON


def _complete_session(session_id: str) -> Callable[[], None]:
    def _effect() -> None:
        state = load_state()
        state.sessions[0].status = SessionStatus.COMPLETED
        assert state.sessions[0].id == session_id
        save_state(state)

    return _effect


def _resume_session() -> None:
    state = load_state()
    state.sessions[0].resumed_at = _NOW
    save_state(state)


def _drop_session() -> None:
    state = load_state()
    state.sessions.clear()
    save_state(state)


def _link_row() -> None:
    store = load_dev_queue()
    store.tasks[0].codex_orphan_session_id = store.tasks[0].session_id
    save_dev_queue(store)


@pytest.mark.parametrize(
    "effect",
    [_complete_session("T-race"), _resume_session, _drop_session, _link_row],
    ids=["completed", "resumed", "removed", "linked"],
)
def test_session_or_row_changed_before_the_lock_is_already_handled(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    effect: Callable[[], None],
) -> None:
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-race")
    _wrap_after_scan(monkeypatch, effect)

    marker = run_codex_legacy_recovery(now=_NOW).marker

    assert _dispositions(marker) == {
        session.id: CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
    }
    assert marker.completed_at == _NOW
    assert _event_types() == []


# --------------------------------------------------------------------------- #
# Lock story
# --------------------------------------------------------------------------- #


def test_act_runs_under_sessions_lock_without_dev_queue_lock(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    _seed_legacy(make_git_repo, "T-clean")
    held = {"sessions": False, "dev_queue": False}
    log: list[str] = []
    real_sessions = codex_legacy_recovery.sessions_lock
    real_dev_queue = codex_legacy_recovery.dev_queue_lock
    real_callee_dev_queue = claimed_row.dev_queue_lock
    real_act: Callable[..., object] = (
        codex_legacy_recovery.act_on_codex_harvest_candidate
    )

    @contextlib.contextmanager
    def _sessions() -> Iterator[None]:
        with real_sessions():
            held["sessions"] = True
            log.append("sessions+")
            try:
                yield
            finally:
                held["sessions"] = False
                log.append("sessions-")

    def _recording_dev_queue(
        real: Callable[[], contextlib.AbstractContextManager[None]], tag: str
    ) -> Callable[[], contextlib.AbstractContextManager[None]]:
        @contextlib.contextmanager
        def _lock() -> Iterator[None]:
            assert held["sessions"], f"{tag} dev_queue_lock taken outside sessions_lock"
            with real():
                held["dev_queue"] = True
                log.append(f"{tag}+")
                try:
                    yield
                finally:
                    held["dev_queue"] = False
                    log.append(f"{tag}-")

        return _lock

    def _act(*args: object, **kwargs: object) -> object:
        assert held["sessions"]
        assert not held["dev_queue"]
        log.append("act")
        return real_act(*args, **kwargs)

    monkeypatch.setattr(codex_legacy_recovery, "sessions_lock", _sessions)
    monkeypatch.setattr(
        codex_legacy_recovery,
        "dev_queue_lock",
        _recording_dev_queue(real_dev_queue, "revalidate"),
    )
    monkeypatch.setattr(
        claimed_row,
        "dev_queue_lock",
        _recording_dev_queue(real_callee_dev_queue, "callee"),
    )
    monkeypatch.setattr(codex_legacy_recovery, "act_on_codex_harvest_candidate", _act)

    run_codex_legacy_recovery(now=_NOW)

    assert log == [
        "sessions+",
        "revalidate+",
        "revalidate-",
        "act",
        "callee+",
        "callee-",
        "sessions-",
    ]


# --------------------------------------------------------------------------- #
# Error and edge paths
# --------------------------------------------------------------------------- #


def test_state_write_failure_is_failed_and_leaves_the_session_live(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-clean")

    def _fail_save(_state: object) -> None:
        msg = "read-only file system"
        raise OSError(msg)

    monkeypatch.setattr(reconcile_local, "save_state", _fail_save)

    marker = run_codex_legacy_recovery(now=_NOW).marker

    assert _dispositions(marker) == {session.id: CodexLegacyDisposition.FAILED}
    assert _unresolved(marker) == {session.id: REASON_STATE_WRITE_FAILED}
    assert marker.completed_at is None
    assert _session(session.id).status is SessionStatus.ACTIVE
    assert _task("T-clean").status is QueueItemStatus.RUNNING
    # Audit before effect: the attempt is on record even though it failed.
    assert len(_completed_events("state-write-failed")) == 1


@pytest.mark.parametrize("remove_row", [False, True], ids=["row", "no-row"])
def test_missing_client_config_is_failed(
    codex_clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remove_row: bool,
) -> None:
    _use_legacy_config(monkeypatch)
    _forbid_scan(monkeypatch)
    worktree = tmp_path / "wt-ghost"
    worktree.mkdir()
    session = _seed_unscannable(
        "T-ghost", worktree, client="client-gone", context=b'{"headless": true}'
    )
    if remove_row:
        store = load_dev_queue()
        store.tasks = [task for task in store.tasks if task.ticket_id != "T-ghost"]
        save_dev_queue(store)

    marker = run_codex_legacy_recovery(now=_NOW).marker

    assert _dispositions(marker) == {session.id: CodexLegacyDisposition.FAILED}
    assert _unresolved(marker) == {session.id: REASON_CLIENT_MISSING}
    assert marker.completed_at is None
    assert _event_types() == []


def test_non_codex_backend_is_not_scanned(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_backend_clients_yaml(tmp_config_dir, workspace, CLAUDE_NATIVE_BACKEND)
    _use_legacy_config(monkeypatch)
    _forbid_scan(monkeypatch)
    _seed_legacy(make_git_repo, "T-claude")

    report = run_codex_legacy_recovery(now=_NOW)

    assert report.status is LegacyRecoveryStatus.COMPLETED
    assert report.marker.scanned == 0
    assert report.marker.outcomes == []
    assert _session("T-claude").status is SessionStatus.ACTIVE


def test_sessions_outside_the_legacy_predicate_are_not_scanned(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handle-bearing (A1's), non-DAEMON, non-live and unparseable-name
    sessions are not B1's; none is counted."""
    _use_legacy_config(monkeypatch)
    _forbid_scan(monkeypatch)
    for ticket_id in ("T-a1", "T-user", "T-done", "T-noname"):
        _seed_legacy(make_git_repo, ticket_id)
    state = load_state()
    by_id = {s.id: s for s in state.sessions}
    by_id["T-a1"].local_liveness = LocalLivenessHandle(
        pid=1, start_time_ns=1, backend="codex"
    )
    by_id["T-user"].origin = SessionOrigin.USER
    by_id["T-done"].status = SessionStatus.COMPLETED
    by_id["T-noname"].name = f"{_CLIENT}/impl"
    save_state(state)

    report = run_codex_legacy_recovery(now=_NOW)

    assert report.marker.scanned == 0


def test_inconclusive_scan_is_skipped_writer_live(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    monkeypatch.setattr(codex_boot, "_codex_processes_in", lambda _wt: None)
    session = _seed_legacy(make_git_repo, "T-unsure")

    marker = run_codex_legacy_recovery(now=_NOW).marker

    assert _dispositions(marker) == {
        session.id: CodexLegacyDisposition.SKIPPED_WRITER_LIVE
    }
    assert _unresolved(marker) == {session.id: REASON_LIVE_WRITER}
    assert _session(session.id).status is SessionStatus.ACTIVE
    assert _event_types() == []


def test_unresolved_session_no_longer_live_on_retry_is_already_handled(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-live")
    _writers_in(monkeypatch, session.worktree_path)
    run_codex_legacy_recovery(now=_NOW)
    _complete_session(session.id)()

    report = run_codex_legacy_recovery(now=_LATER)

    assert report.status is LegacyRecoveryStatus.COMPLETED
    assert _dispositions(report.marker) == {
        session.id: CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
    }
    assert report.marker.scanned == 1


def test_unresolved_session_no_longer_on_codex_at_retry_is_already_handled(
    tmp_config_dir: Path,
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session an earlier run counted must still resolve, not vanish."""
    _use_legacy_config(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-live")
    _writers_in(monkeypatch, session.worktree_path)
    run_codex_legacy_recovery(now=_NOW)
    _write_backend_clients_yaml(tmp_config_dir, codex_clients, CLAUDE_NATIVE_BACKEND)

    report = run_codex_legacy_recovery(now=_LATER)

    assert report.status is LegacyRecoveryStatus.COMPLETED
    assert _dispositions(report.marker) == {
        session.id: CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
    }


def test_unresolved_session_gone_from_state_on_retry_is_already_handled(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-live")
    _writers_in(monkeypatch, session.worktree_path)
    run_codex_legacy_recovery(now=_NOW)
    _drop_session()

    report = run_codex_legacy_recovery(now=_LATER)

    assert report.status is LegacyRecoveryStatus.COMPLETED
    [outcome] = report.marker.outcomes
    assert outcome.disposition is CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
    # The first run's snapshot facts survive the resolution.
    assert outcome.prior_status is SessionStatus.ACTIVE
    assert outcome.prior_stage is Stage.REVIEW


def test_incomplete_marker_without_unresolved_still_scans_unseen_sessions(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run killed between sessions leaves an incomplete marker listing only
    the sessions it reached; the retry must not complete over the rest."""
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    save_codex_legacy_marker(CodexLegacyRecoveryMarker())
    session = _seed_legacy(make_git_repo, "T-unseen")

    report = run_codex_legacy_recovery(now=_NOW)

    assert _dispositions(report.marker) == {session.id: CodexLegacyDisposition.REQUEUED}
    assert report.status is LegacyRecoveryStatus.COMPLETED


@pytest.mark.parametrize(
    "content",
    ["{not json", '{"schema_version": 1, "bogus": true}', '{"scanned": "many"}'],
    ids=["malformed-json", "unknown-field", "wrong-type"],
)
def test_corrupt_marker_is_a_hard_error(
    codex_clients: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    _use_legacy_config(monkeypatch)
    path = codex_legacy_recovery_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    with pytest.raises(CodexLegacyRecoveryMarkerError, match=str(path)):
        run_codex_legacy_recovery(now=_NOW)

    assert path.read_text() == content


# --------------------------------------------------------------------------- #
# Cannot-tell headless cases (settled A2, ALT-b) — through the real scan path
# --------------------------------------------------------------------------- #


def _no_worktree(tmp_path: Path) -> Path | None:
    del tmp_path
    return None


def _deleted_worktree(tmp_path: Path) -> Path | None:
    return tmp_path / "wt-deleted"


def _bare_worktree(tmp_path: Path) -> Path | None:
    worktree = tmp_path / "wt-bare"
    worktree.mkdir()
    return worktree


@pytest.mark.parametrize(
    ("make_worktree", "context", "reason"),
    [
        (_no_worktree, None, _HeadlessScanKind.WORKTREE_UNSET),
        (_deleted_worktree, None, _HeadlessScanKind.WORKTREE_MISSING),
        (_bare_worktree, None, _HeadlessScanKind.CONTEXT_MISSING),
        (_bare_worktree, b"{not json", _HeadlessScanKind.CONTEXT_UNREADABLE),
        (_bare_worktree, b"\xff\xfe", _HeadlessScanKind.CONTEXT_UNREADABLE),
        (_bare_worktree, b"[1, 2]", _HeadlessScanKind.CONTEXT_UNREADABLE),
    ],
    ids=[
        "worktree-unset",
        "worktree-missing",
        "context-missing",
        "context-malformed",
        "context-invalid-utf8",
        "context-not-a-dict",
    ],
)
def test_cannot_tell_headless_parks_as_skipped_writer_live(
    codex_clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_worktree: Callable[[Path], Path | None],
    context: bytes | None,
    reason: _HeadlessScanKind,
) -> None:
    _use_legacy_config(monkeypatch)
    _forbid_scan(monkeypatch)
    session = _seed_unscannable("T-blind", make_worktree(tmp_path), context=context)
    state_before = state_file().read_bytes()
    queue_before = dev_queue_file().read_bytes()

    report = run_codex_legacy_recovery(now=_NOW)

    marker = report.marker
    assert report.status is LegacyRecoveryStatus.PARTIAL
    assert _dispositions(marker) == {
        session.id: CodexLegacyDisposition.SKIPPED_WRITER_LIVE
    }
    assert _unresolved(marker) == {session.id: reason.value}
    assert (marker.scanned, marker.skipped_writer_live) == (1, 1)
    assert marker.completed_at is None
    assert state_file().read_bytes() == state_before
    assert dev_queue_file().read_bytes() == queue_before
    assert _event_types() == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads mode 000 files"
)
def test_unreadable_context_file_parks_as_context_unreadable(
    codex_clients: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_legacy_config(monkeypatch)
    _forbid_scan(monkeypatch)
    worktree = _bare_worktree(tmp_path)
    assert worktree is not None
    session = _seed_unscannable("T-locked", worktree, context=b'{"headless": true}')
    context_path = worktree / ".claude" / "cw-context.json"
    context_path.chmod(0)
    try:
        marker = run_codex_legacy_recovery(now=_NOW).marker
    finally:
        context_path.chmod(0o600)

    assert _unresolved(marker) == {
        session.id: _HeadlessScanKind.CONTEXT_UNREADABLE.value
    }


def test_readable_non_headless_context_is_excluded(
    codex_clients: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_legacy_config(monkeypatch)
    _forbid_scan(monkeypatch)
    worktree = _bare_worktree(tmp_path)
    _seed_unscannable("T-interactive", worktree, context=b'{"headless": false}')

    report = run_codex_legacy_recovery(now=_NOW)

    assert report.status is LegacyRecoveryStatus.COMPLETED
    assert report.marker.scanned == 0
    assert report.marker.outcomes == []
    assert report.marker.unresolved == []


def test_cannot_tell_session_resolves_once_its_context_is_restored(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-blind")
    assert session.worktree_path is not None
    context_path = session.worktree_path / ".claude" / "cw-context.json"
    context_path.unlink()
    first = run_codex_legacy_recovery(now=_NOW).marker
    assert _unresolved(first) == {session.id: _HeadlessScanKind.CONTEXT_MISSING.value}

    git_in(session.worktree_path, "checkout", "--", ".claude/cw-context.json")
    report = run_codex_legacy_recovery(now=_LATER)

    assert report.status is LegacyRecoveryStatus.COMPLETED
    assert _dispositions(report.marker) == {session.id: CodexLegacyDisposition.REQUEUED}
    assert report.marker.scanned == 1


def test_retry_of_a_now_non_headless_session_is_already_handled(
    codex_clients: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_legacy_config(monkeypatch)
    _forbid_scan(monkeypatch)
    worktree = _bare_worktree(tmp_path)
    assert worktree is not None
    session = _seed_unscannable("T-blind", worktree, context=b"{not json")
    run_codex_legacy_recovery(now=_NOW)
    (worktree / ".claude" / "cw-context.json").write_text('{"headless": false}')

    report = run_codex_legacy_recovery(now=_LATER)

    assert report.status is LegacyRecoveryStatus.COMPLETED
    assert _dispositions(report.marker) == {
        session.id: CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
    }
    assert report.marker.scanned == 1


@pytest.mark.parametrize(
    ("make_worktree", "context", "expected"),
    [
        (_no_worktree, None, _HeadlessScanKind.WORKTREE_UNSET),
        (_deleted_worktree, None, _HeadlessScanKind.WORKTREE_MISSING),
        (_bare_worktree, None, _HeadlessScanKind.CONTEXT_MISSING),
        (_bare_worktree, b"{not json", _HeadlessScanKind.CONTEXT_UNREADABLE),
        (_bare_worktree, b"\xff\xfe", _HeadlessScanKind.CONTEXT_UNREADABLE),
        (_bare_worktree, b'"headless"', _HeadlessScanKind.CONTEXT_UNREADABLE),
        (_bare_worktree, b'{"headless": true}', _HeadlessScanKind.HEADLESS),
        (_bare_worktree, b'{"headless": false}', _HeadlessScanKind.NOT_HEADLESS),
        (_bare_worktree, b"{}", _HeadlessScanKind.NOT_HEADLESS),
    ],
    ids=[
        "unset",
        "missing",
        "context-missing",
        "malformed",
        "invalid-utf8",
        "non-dict",
        "headless",
        "not-headless",
        "no-headless-key",
    ],
)
def test_headless_scan_kind(
    tmp_path: Path,
    make_worktree: Callable[[Path], Path | None],
    context: bytes | None,
    expected: _HeadlessScanKind,
) -> None:
    worktree = make_worktree(tmp_path)
    if worktree is not None and context is not None:
        (worktree / ".claude").mkdir(parents=True)
        (worktree / ".claude" / "cw-context.json").write_bytes(context)
    session = _make_daemon_session(worktree_path=worktree)

    assert _headless_scan_kind(session) is expected


# --------------------------------------------------------------------------- #
# R8: the existing commands reverse each disposition
# --------------------------------------------------------------------------- #


def test_existing_commands_reverse_each_disposition(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_legacy_config(monkeypatch)
    _seed_mixed(make_git_repo, monkeypatch)
    marker = run_codex_legacy_recovery(now=_NOW).marker
    runner = CliRunner()

    # PARKED -> `cw dev-queue requeue` (not disposition-gated).
    requeue = runner.invoke(
        main, ["dev-queue", "requeue", "T-dirty", "--client", _CLIENT]
    )
    assert requeue.exit_code == 0, requeue.output
    parked = _task("T-dirty")
    assert parked.status is QueueItemStatus.PENDING
    assert parked.session_id is None
    assert parked.stage_base_ref is None
    requeued = [
        p for p in _requeued_events("r8-requeue") if p["ticket_id"] == "T-dirty"
    ]
    assert [p["reason"] for p in requeued] == ["cli_requeue"]

    # REQUEUED -> `cw dev-queue cancel`.
    cancel = runner.invoke(main, ["dev-queue", "cancel", "T-clean", "-c", _CLIENT])
    assert cancel.exit_code == 0, cancel.output
    assert _task("T-clean").status is QueueItemStatus.CANCELLED

    # The closed session is not reopened by either.
    for ticket_id in ("T-dirty", "T-clean"):
        closed = _session(ticket_id)
        assert closed.status is SessionStatus.COMPLETED
        assert closed.completed_reason is CompletionReason.CRASHED

    # `unblock` is for SALVAGE_PARKED sessions only; it does not apply here.
    unblock = runner.invoke(main, ["dev-queue", "unblock", "T-moved", "-c", _CLIENT])
    assert unblock.exit_code != 0
    assert _task("T-moved").status is QueueItemStatus.BLOCKED_ON_USER

    # The marker carries what an operator needs to decide the reversal.
    by_id = {o.session_id: o for o in marker.outcomes}
    assert by_id["T-dirty"].disposition is CodexLegacyDisposition.PARKED
    assert by_id["T-dirty"].prior_status is SessionStatus.ACTIVE
    assert by_id["T-dirty"].prior_stage is Stage.REVIEW


def test_marker_round_trips_through_json(tmp_config_dir: Path) -> None:
    marker = CodexLegacyRecoveryMarker(scanned=1, failed=1)
    save_codex_legacy_marker(marker)

    raw = json.loads(codex_legacy_recovery_file().read_text())

    assert raw["schema_version"] == 1
    assert load_codex_legacy_marker() == marker


@pytest.mark.parametrize("error", [OSError("log unreadable"), ValueError("bad line")])
def test_unreadable_event_log_after_the_act_is_failed_for_retry(
    codex_clients: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """The act ran but its events cannot be confirmed: resolve nothing."""
    _use_legacy_config(monkeypatch)
    _no_codex_process(monkeypatch)
    session = _seed_legacy(make_git_repo, "T-clean")

    def _unreadable(**_kwargs: object) -> list[object]:
        raise error

    monkeypatch.setattr(codex_legacy_recovery, "read_events", _unreadable)

    marker = run_codex_legacy_recovery(now=_NOW).marker

    assert _dispositions(marker) == {session.id: CodexLegacyDisposition.FAILED}
    assert _unresolved(marker) == {session.id: "event_delivery_failed"}
    assert marker.completed_at is None
