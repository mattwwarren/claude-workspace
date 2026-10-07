"""Codex branch of the crash-only local harvest sweep (#2387, RFC 0014 A1).

A codex session that carries a :class:`LocalLivenessHandle` whose process has
died is picked up by ``_detect_local_harvest_candidates`` like any aider or
opencode session, but it is never harvested through a result synthesizer:
there is no sentinel to synthesize. ``_act_on_local_harvest_candidates``
branches it out before ``_synthesize_harvest_sentinel`` and applies an
audited four-part clean-requeue gate instead (reap_policy ``auto``, codex fix
loop off, worktree clean apart from the review verdict, HEAD unmoved since
the review's baseline). All four pass → requeue; any one fails → park. Either
way the session closes ``COMPLETED``/``CRASHED`` behind a ``SESSION_COMPLETED``
audit event recorded before any transition.

Fixtures are shared with ``test_reconcile_codex_boot.py`` through
``tests/_codex_recovery_helpers.py`` rather than re-implemented, including the
configuration constructors used by the boot tests. The orchestrator config is
injected through the sweep's ``config`` parameter (what ``reconcile()``
passes).
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw import result as cw_result
from cw.config import (
    load_clients,
    load_state,
    orchestrator_config_file,
    save_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    CodexHarvestOutcome,
    CompletionReason,
    DevQueueStore,
    LocalLivenessHandle,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    SessionStatus,
    Stage,
)
from cw.reconcile import (
    ProposedAction,
    _act_on_local_harvest_candidates,
    _detect_local_harvest_candidates,
    codex_boot,
    reconcile,
)
from cw.reconcile import local as reconcile_local
from cw.reconcile.codex_boot import (
    _PARK_REASON_DIRTY_WORKTREE,
    _PARK_REASON_FIX_LOOP_ENABLED,
    _PARK_REASON_GIT_ERROR,
    _PARK_REASON_HEAD_MOVED,
    _PARK_REASON_REAP_POLICY_NOT_AUTO,
    CLEAN_PROBE_MAX_AGE_SECONDS,
    CleanProbes,
    probe_clean_state,
)
from cw.reconcile.local import (
    CODEX_HARVEST_CLEAN_REQUEUE_REASON,
    CODEX_HARVEST_ORPHANED_DISPOSITION,
    _evaluate_codex_clean_requeue_gate,
    _harvest_codex_candidate,
    act_on_codex_harvest_candidate,
    capture_codex_harvest_probes,
)
from cw.reconcile.tasks import revert_completed_silent_tasks
from tests._codex_recovery_helpers import (
    _assert_session_closed,
    _assert_session_left_active,
    _attention_events,
    _capture_probes_for,
    _completed_events,
    _failing_record_event,
    _record_git_lock_state,
    _requeued_events,
    _seed_clean_codex_orphan,
    _task_without_base_ref,
    _use_auto_reap_policy,
    _use_config,
)
from tests.conftest import commit_tracked_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import CwState, Session, TicketTask

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_TICKET = "T-orphan"
# Far above any real pid_max, so the recycled-PID guard reads it as dead.
_DEAD_PID = 2_000_000_000
_DEAD_START_TIME_NS = 123

_AUTO = _use_auto_reap_policy()
_SIGNAL_ONLY = _use_config(reap_policy=ReapPolicy.SIGNAL_ONLY)
_AUTO_FIX_LOOP_ON = _use_auto_reap_policy(default_codex_fix_loop_enabled=True)

_GATE_KEYS = ("reap_policy_auto", "fix_loop_disabled", "worktree_clean", "head_unmoved")


def _stamp_dead_codex_liveness() -> Session:
    """Turn the seeded orphan into a dead-PID codex harvest candidate.

    ``_seed_clean_codex_orphan`` builds a daemon-surfaced session; a harvest
    candidate has no ``surface_ref`` and a ``local_liveness`` handle instead.
    """
    state = load_state()
    session = state.sessions[0].model_copy(
        update={
            "surface_ref": None,
            "local_liveness": LocalLivenessHandle(
                pid=_DEAD_PID, start_time_ns=_DEAD_START_TIME_NS, backend="codex"
            ),
        }
    )
    state.sessions[0] = session
    save_state(state)
    return session


def _seed(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> Path:
    repo, _ = _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _stamp_dead_codex_liveness()
    return repo


def _capture(state: CwState, tasks: list[TicketTask]) -> CleanProbes:
    """``reconcile()``'s lockless pre-pass for the harvest sweep (#2563).

    The probes record git facts only; which config the discarded gate result
    resolved against does not matter, so it is pinned rather than loaded.
    """
    probes = CleanProbes()
    capture_codex_harvest_probes(state, tasks, config=_AUTO, probes=probes)
    return probes


def _harvest(config: OrchestratorConfig | None) -> list[str]:
    """Run one capture + detect+act sweep, as ``reconcile()`` does."""
    state = load_state()
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    tasks = list(task_by_ticket.values())
    probes = _capture(state, tasks)
    candidates = _detect_local_harvest_candidates(state, tasks)
    if config is None:
        return _act_on_local_harvest_candidates(
            state,
            candidates,
            now=_NOW,
            task_by_ticket=task_by_ticket,
            codex_probes=probes,
        )
    return _act_on_local_harvest_candidates(
        state,
        candidates,
        now=_NOW,
        task_by_ticket=task_by_ticket,
        config=config,
        codex_probes=probes,
    )


def _gate_checks(consumer: str) -> dict[str, object]:
    payloads = _completed_events(consumer)
    assert len(payloads) == 1
    checks = payloads[0]["gate_checks"]
    assert isinstance(checks, dict)
    return checks


def _assert_codex_parked(consumer: str, reason: str, failing_check: str) -> None:
    """Parked (not requeued), session closed, and only *failing_check* failed."""
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_HARVEST_ORPHANED_DISPOSITION
    assert task.session_id is None
    assert task.codex_orphan_session_id is None
    _assert_session_closed()
    attention = _attention_events(f"{consumer}-attention")
    assert len(attention) == 1
    assert attention[0]["paused_status"] == CODEX_HARVEST_ORPHANED_DISPOSITION
    assert reason in str(attention[0]["breadcrumbs"])
    assert _requeued_events(f"{consumer}-requeued") == []
    checks = _gate_checks(f"{consumer}-completed")
    assert set(checks) == set(_GATE_KEYS)
    assert {key for key, passed in checks.items() if not passed} == {failing_check}


def test_codex_backend_is_detected_as_a_harvest_candidate(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    candidates = _detect_local_harvest_candidates(load_state())

    assert len(candidates) == 1
    assert candidates[0].proposed_action is ProposedAction.HARVEST_LOCAL_COMPLETE
    assert candidates[0].session_id == _TICKET
    assert candidates[0].ticket_id == _TICKET


def test_reap_policy_not_auto_parks(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _harvest(_SIGNAL_ONLY) == []

    _assert_codex_parked(
        "codex-harvest-signal-only",
        _PARK_REASON_REAP_POLICY_NOT_AUTO,
        "reap_policy_auto",
    )


def test_fix_loop_enabled_parks(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _harvest(_AUTO_FIX_LOOP_ON) == []

    _assert_codex_parked(
        "codex-harvest-fix-loop", _PARK_REASON_FIX_LOOP_ENABLED, "fix_loop_disabled"
    )


def test_dirty_worktree_parks(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    repo = _seed(tmp_config_dir, tmp_path, make_git_repo)
    (repo / "extra.txt").write_text("stray\n")

    assert _harvest(_AUTO) == []

    _assert_codex_parked(
        "codex-harvest-dirty", _PARK_REASON_DIRTY_WORKTREE, "worktree_clean"
    )


def test_head_moved_parks(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    repo = _seed(tmp_config_dir, tmp_path, make_git_repo)
    commit_tracked_file(repo, "extra.txt")

    assert _harvest(_AUTO) == []

    _assert_codex_parked(
        "codex-harvest-head-moved", _PARK_REASON_HEAD_MOVED, "head_unmoved"
    )


def test_unresolvable_baseline_parks_as_git_error(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    """No stage_base_ref and no local tracking ref: HEAD check is unknown.

    The tri-state ``None`` reads as a failed check in the audit dict and
    parks with the boot pass's git-error reason, never as a requeue.
    """
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    save_dev_queue(DevQueueStore(tasks=[_task_without_base_ref()]))

    assert _harvest(_AUTO) == []

    _assert_codex_parked(
        "codex-harvest-git-error", _PARK_REASON_GIT_ERROR, "head_unmoved"
    )


def test_all_checks_pass_requeues(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _harvest(_AUTO) == []

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.PENDING
    assert task.session_id is None
    assert task.disposition is None
    _assert_session_closed()
    session = load_state().sessions[0]
    assert session.completed_reason is CompletionReason.CRASHED
    assert session.completed_at == _NOW
    assert _attention_events("codex-harvest-requeue-attention") == []
    assert _requeued_events("codex-harvest-requeue") == [
        {
            "ticket_id": _TICKET,
            "client": "client-a",
            "from_stage": Stage.REVIEW,
            "to_stage": Stage.REVIEW,
            "reason": CODEX_HARVEST_CLEAN_REQUEUE_REASON,
            "session_id": _TICKET,
        }
    ]


def test_audit_event_carries_full_recovery_evidence(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    _harvest(_AUTO)

    events = read_events(
        consumer="codex-harvest-audit",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert events[0].correlation_id == _TICKET
    assert events[0].payload == {
        "session_id": _TICKET,
        "session_name": f"client-a/auto-dev/{_TICKET}",
        "ticket_id": _TICKET,
        "client": "client-a",
        "executor": "codex",
        "crashed": True,
        "salvaged": False,
        "prior_status": "active",
        "resulting_status": "completed",
        "disposition": "requeued",
        "reason": CODEX_HARVEST_CLEAN_REQUEUE_REASON,
        "gate_checks": dict.fromkeys(_GATE_KEYS, True),
        "pid": _DEAD_PID,
        "start_time_ns": _DEAD_START_TIME_NS,
    }


def test_audit_event_records_a_park_disposition(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    _harvest(_SIGNAL_ONLY)

    payloads = _completed_events("codex-harvest-audit-park")
    assert len(payloads) == 1
    assert payloads[0]["disposition"] == "parked"
    assert payloads[0]["reason"] == _PARK_REASON_REAP_POLICY_NOT_AUTO


def test_failed_audit_write_blocks_the_transition(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit before effect: a failed event write transitions nothing.

    The session stays ACTIVE and the task keeps its claim, so the next tick
    re-detects the same dead PID and completes the disposition.
    """
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    real = _failing_record_event(
        monkeypatch, reconcile_local, OrchestratorEventType.SESSION_COMPLETED
    )

    assert _harvest(_AUTO) == []

    session = _assert_session_left_active()
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == session.id
    assert _requeued_events("codex-harvest-failed-audit-requeued") == []
    assert _attention_events("codex-harvest-failed-audit-attention") == []

    monkeypatch.setattr(reconcile_local, "record_event", real)

    assert _harvest(_AUTO) == []
    _assert_session_closed()
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING
    assert len(_completed_events("codex-harvest-failed-audit-completed")) == 1


def test_failed_task_disposition_after_session_close_preserves_park(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completed-session backstop honors a persisted codex park intent."""
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    from cw.dispatch import claim

    def _fail_park(**_kwargs: object) -> None:
        message = "simulated crash after session close"
        raise RuntimeError(message)

    monkeypatch.setattr(claim, "_park_running_task_blocked_on_user", _fail_park)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _harvest(_SIGNAL_ONLY)

    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.recovery_disposition == CODEX_HARVEST_ORPHANED_DISPOSITION
    assert session.recovery_reason == _PARK_REASON_REAP_POLICY_NOT_AUTO
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING

    assert revert_completed_silent_tasks() == []

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_HARVEST_ORPHANED_DISPOSITION
    assert task.session_id is None
    attention = _attention_events("codex-harvest-recovery-backstop")
    assert len(attention) == 1
    assert _PARK_REASON_REAP_POLICY_NOT_AUTO in str(attention[0]["breadcrumbs"])


def test_codex_candidate_never_reaches_git_synthesis_or_opencode_parse(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        msg = "codex harvest must not synthesize a sentinel"
        raise AssertionError(msg)

    for name in (
        "_synthesize_harvest_sentinel",
        "synthesize_git_result",
        "synthesize_opencode_result",
    ):
        monkeypatch.setattr(reconcile_local, name, _forbidden)
    monkeypatch.setattr(cw_result, "emit_result_on", _forbidden)
    for backend in ("aider", "opencode"):
        monkeypatch.setitem(reconcile_local._HARVEST_SYNTHESIZERS, backend, _forbidden)

    _harvest(_AUTO)

    session = load_state().sessions[0]
    assert session.last_result is None
    assert session.status is SessionStatus.COMPLETED


@pytest.mark.parametrize("config", [_AUTO, _SIGNAL_ONLY], ids=["requeue", "park"])
def test_codex_harvest_ticket_id_excluded_from_harvested_list(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    config: OrchestratorConfig,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _TICKET not in _harvest(config)
    _assert_session_closed()


def _assert_untouched(consumer: str) -> None:
    _assert_session_left_active()
    assert _completed_events(f"{consumer}-completed") == []
    assert _attention_events(f"{consumer}-attention") == []
    assert _requeued_events(f"{consumer}-requeued") == []


def test_missing_task_row_leaves_session_untouched(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No dev-queue row for the ticket: the gate has nothing to decide on.

    The sweep's synthetic-task fallback must not stand in for the real row.
    """
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    save_dev_queue(DevQueueStore(tasks=[]))

    with caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        assert _harvest(_AUTO) == []

    _assert_untouched("codex-harvest-no-row")
    assert _TICKET in caplog.text


def test_another_clients_row_is_never_gated(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    """Ticket ids are per-client: a same-id row of another client is no match."""
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    store = load_dev_queue()
    store.tasks[0].client = "client-b"
    save_dev_queue(store)

    assert _harvest(_AUTO) == []

    _assert_untouched("codex-harvest-other-client")
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


def test_requeue_skipped_when_the_row_was_reclaimed(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The identity-checked revert leaves a row re-claimed by a fresh session.

    The dead session still closes behind its audit event; no requeue is
    reported for a revert that did not happen.
    """
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    store = load_dev_queue()
    store.tasks[0].session_id = "fresh-session"
    save_dev_queue(store)

    with caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        assert _harvest(_AUTO) == []

    _assert_session_closed()
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == "fresh-session"
    assert _requeued_events("codex-harvest-reclaimed-requeued") == []
    assert len(_completed_events("codex-harvest-reclaimed-completed")) == 1
    assert "requeue skipped" in caplog.text


def test_unknown_client_leaves_session_untouched(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    state = load_state()
    state.sessions[0] = state.sessions[0].model_copy(update={"client": "client-zz"})
    save_state(state)

    with caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        assert _harvest(_AUTO) == []

    _assert_untouched("codex-harvest-no-client")
    assert "client-zz" in caplog.text


def test_config_param_defaults_to_effective_config_when_omitted(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    loads: list[int] = []

    def _load() -> OrchestratorConfig:
        loads.append(1)
        return _AUTO

    monkeypatch.setattr(reconcile_local, "load_effective_config", _load)

    _harvest(None)

    assert loads == [1]
    # The loaded (auto) config is what the gate ran against.
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING


# --------------------------------------------------------------------------- #
# Typed outcome (RFC 0014 B1, #2389): the act helper and the sweep wrapper
# report what happened, so cw.codex_legacy_recovery never re-derives it.
# --------------------------------------------------------------------------- #


def _act(
    config: OrchestratorConfig,
    *,
    legacy_reason: str | None = None,
    capture: bool = True,
) -> CodexHarvestOutcome:
    """Call ``act_on_codex_harvest_candidate`` directly on the seeded orphan.

    A legacy caller passes no liveness handle; the A1 sweep passes the real one.
    With *capture* (the default) the orphan's clean probe is captured first,
    as both callers do before taking the lock; without it none is passed.
    """
    state = load_state()
    session = state.sessions[0]
    assert session.worktree_path is not None
    clients = load_clients()
    task = load_dev_queue().tasks[0]
    return act_on_codex_harvest_candidate(
        state,
        session,
        None if legacy_reason is not None else session.local_liveness,
        task,
        clients["client-a"],
        clients,
        config,
        worktree=session.worktree_path,
        now=_NOW,
        legacy_reason=legacy_reason,
        probes=(
            _capture_probes_for(session.worktree_path, task, clients)
            if capture
            else None
        ),
    )


def _repoint_row(session_id: str) -> None:
    store = load_dev_queue()
    store.tasks[0].session_id = session_id
    save_dev_queue(store)


def test_act_returns_requeued(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _act(_AUTO) is CodexHarvestOutcome.REQUEUED
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING


def test_act_returns_parked(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _act(_SIGNAL_ONLY) is CodexHarvestOutcome.PARKED
    assert load_dev_queue().tasks[0].status is QueueItemStatus.BLOCKED_ON_USER


def test_act_returns_audit_failed_and_transitions_nothing(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    _failing_record_event(
        monkeypatch, reconcile_local, OrchestratorEventType.SESSION_COMPLETED
    )

    assert _act(_AUTO) is CodexHarvestOutcome.AUDIT_FAILED

    _assert_session_left_active()
    assert load_dev_queue().tasks[0].status is QueueItemStatus.RUNNING


def test_act_returns_transition_lost_when_the_revert_is_lost(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    _repoint_row("fresh-session")
    reverted: list[bool] = []
    real_requeue = reconcile_local._requeue_codex_harvest_orphan

    def _recording_requeue(session: Session, task: TicketTask) -> bool:
        result = real_requeue(session, task)
        reverted.append(result)
        return result

    monkeypatch.setattr(
        reconcile_local, "_requeue_codex_harvest_orphan", _recording_requeue
    )

    assert _act(_AUTO) is CodexHarvestOutcome.TRANSITION_LOST

    assert reverted == [False]
    _assert_session_closed()
    assert _requeued_events("codex-act-lost-revert") == []
    assert load_dev_queue().tasks[0].session_id == "fresh-session"


def test_act_returns_transition_lost_when_the_park_is_lost(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = _seed(tmp_config_dir, tmp_path, make_git_repo)
    (repo / "extra.txt").write_text("stray\n")
    _repoint_row("fresh-session")

    with caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        assert _act(_AUTO) is CodexHarvestOutcome.TRANSITION_LOST

    _assert_session_closed()
    assert _attention_events("codex-act-lost-park") == []
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == "fresh-session"
    assert "park skipped" in caplog.text


def test_act_with_legacy_reason_and_no_handle_writes_the_legacy_payload(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    _act(_SIGNAL_ONLY, legacy_reason="codex_legacy_recovery")

    [payload] = _completed_events("codex-act-legacy-payload")
    assert payload["reason"] == "codex_legacy_recovery"
    assert payload["legacy"] is True
    assert payload["detail"] == _PARK_REASON_REAP_POLICY_NOT_AUTO
    assert payload["pid"] is None
    assert payload["start_time_ns"] is None
    assert payload["disposition"] == "parked"


def _wrapper(
    *, real_task: TicketTask | None, client_name: str | None, capture: bool = True
) -> CodexHarvestOutcome:
    state = load_state()
    session = state.sessions[0]
    assert session.local_liveness is not None
    [candidate] = _detect_local_harvest_candidates(state)
    assert candidate.worktree_path is not None
    clients = load_clients()
    probes = (
        _capture_probes_for(candidate.worktree_path, load_dev_queue().tasks[0], clients)
        if capture
        else None
    )
    return _harvest_codex_candidate(
        state,
        session,
        session.local_liveness,
        candidate,
        worktree=candidate.worktree_path,
        real_task=real_task,
        client=clients[client_name] if client_name is not None else None,
        clients=clients,
        config=_AUTO,
        now=_NOW,
        probes=probes,
    )


def test_harvest_wrapper_returns_no_row_without_a_row(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    outcome = _wrapper(real_task=None, client_name="client-a")

    assert outcome is CodexHarvestOutcome.NO_ROW
    _assert_untouched("codex-wrapper-no-row")


def test_harvest_wrapper_returns_no_row_without_a_client(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    outcome = _wrapper(real_task=load_dev_queue().tasks[0], client_name=None)

    assert outcome is CodexHarvestOutcome.NO_ROW
    _assert_untouched("codex-wrapper-no-client")


def test_harvest_wrapper_returns_the_act_outcome(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    outcome = _wrapper(real_task=load_dev_queue().tasks[0], client_name="client-a")

    assert outcome is CodexHarvestOutcome.REQUEUED


# --------------------------------------------------------------------------- #
# Lockless clean probe (#2563): the codex gate's git runs in reconcile()'s
# pre-pass; under the lock the gate only reads the captured probe, and a
# missing or stale one leaves the session untouched for the next tick.
# --------------------------------------------------------------------------- #


def _sweep_untouched(probes: CleanProbes | None) -> list[str]:
    state = load_state()
    tasks = load_dev_queue().tasks
    return _act_on_local_harvest_candidates(
        state,
        _detect_local_harvest_candidates(state, tasks),
        now=_NOW,
        task_by_ticket={t.ticket_id: t for t in tasks},
        config=_AUTO,
        codex_probes=probes,
    )


def _assert_deferred(consumer: str, caplog: pytest.LogCaptureFixture) -> None:
    _assert_untouched(consumer)
    session = load_state().sessions[0]
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == session.id
    assert (
        f"codex session {session.id} ({session.client}/{task.ticket_id})"
        " has no usable clean probe" in caplog.text
    )


@pytest.mark.parametrize("unusable", ["missing", "stale"])
def test_sweep_with_an_unusable_probe_leaves_the_session_for_the_next_tick(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    unusable: str,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    probes = (
        None
        if unusable == "missing"
        else _capture(load_state(), load_dev_queue().tasks)
    )
    clock = (
        freeze_time(datetime.now(UTC) + timedelta(seconds=CLEAN_PROBE_MAX_AGE_SECONDS))
        if unusable == "stale"
        else contextlib.nullcontext()
    )
    git_calls = _record_git_lock_state(monkeypatch)

    with clock, caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        assert _sweep_untouched(probes) == []

    assert git_calls == []
    _assert_deferred(f"codex-harvest-defer-{unusable}", caplog)


def test_act_with_no_probe_returns_probe_unavailable(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    with caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        outcome = _act(_AUTO, capture=False)

    assert outcome is CodexHarvestOutcome.PROBE_UNAVAILABLE
    _assert_deferred("codex-act-no-probe", caplog)


def test_harvest_wrapper_forwards_a_missing_probe(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    outcome = _wrapper(
        real_task=load_dev_queue().tasks[0], client_name="client-a", capture=False
    )

    assert outcome is CodexHarvestOutcome.PROBE_UNAVAILABLE
    _assert_untouched("codex-wrapper-no-probe")


def _harvest_dirty(repo: Path) -> None:
    (repo / "extra.txt").write_text("stray\n")


def _harvest_head_moved(repo: Path) -> None:
    commit_tracked_file(repo, "extra.txt")


def _harvest_no_baseline(_repo: Path) -> None:
    save_dev_queue(DevQueueStore(tasks=[_task_without_base_ref()]))


def _harvest_unchanged(_repo: Path) -> None:
    """Leave the seeded orphan clean."""


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        (_harvest_unchanged, CODEX_HARVEST_CLEAN_REQUEUE_REASON),
        (_harvest_dirty, _PARK_REASON_DIRTY_WORKTREE),
        (_harvest_head_moved, _PARK_REASON_HEAD_MOVED),
        (_harvest_no_baseline, _PARK_REASON_GIT_ERROR),
    ],
    ids=["clean", "dirty", "head-moved", "git-error"],
)
def test_captured_gate_equals_the_live_gate(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    shape: Callable[[Path], None],
    reason: str,
) -> None:
    """Every ``gate_checks`` value read from the probe matches live git."""
    repo = _seed(tmp_config_dir, tmp_path, make_git_repo)
    shape(repo)
    task = load_dev_queue().tasks[0]
    clients = load_clients()
    args = (repo, task, clients["client-a"], clients, _AUTO)
    live = _evaluate_codex_clean_requeue_gate(*args, probe_clean_state)
    probes = _capture_probes_for(repo, task, clients)

    consumed = _evaluate_codex_clean_requeue_gate(*args, probes.lookup)

    assert consumed == live
    assert consumed.reason == reason


def _another_clients_row() -> None:
    store = load_dev_queue()
    store.tasks[0].client = "client-b"
    save_dev_queue(store)


def _no_row() -> None:
    save_dev_queue(DevQueueStore(tasks=[]))


def _aider_backend() -> None:
    state = load_state()
    handle = state.sessions[0].local_liveness
    assert handle is not None
    state.sessions[0].local_liveness = handle.model_copy(update={"backend": "aider"})
    save_state(state)


@pytest.mark.parametrize(
    "unprobeable",
    [_no_row, _another_clients_row, _aider_backend],
    ids=["synthetic-row", "client-mismatch", "non-codex-backend"],
)
def test_pre_pass_skips_what_the_sweep_would_not_gate(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    unprobeable: Callable[[], None],
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    unprobeable()
    git_calls = _record_git_lock_state(monkeypatch)

    probes = _capture(load_state(), load_dev_queue().tasks)

    assert probes.captured_keys == frozenset()
    assert git_calls == []


def test_pre_pass_captures_a_gateable_dead_codex_session(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    probes = _capture(load_state(), load_dev_queue().tasks)

    assert probes.captured_keys == frozenset({("client-a", _TICKET)})


def test_held_lock_sweep_runs_no_git(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    git_calls = _record_git_lock_state(monkeypatch)
    probes = _capture(load_state(), load_dev_queue().tasks)

    with sessions_lock():
        _sweep_untouched(probes)

    assert {subcommand for subcommand, _ in git_calls} == {"status", "rev-parse"}
    assert all(lock_free for _, lock_free in git_calls)
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING


def test_pre_pass_with_a_spent_budget_captures_nothing_and_does_not_raise(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    clock = {"now": 0.0}
    monkeypatch.setattr(codex_boot, "monotonic", lambda: clock["now"])
    probes = CleanProbes(budget_seconds=1.0)
    clock["now"] = 2.0
    git_calls = _record_git_lock_state(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        capture_codex_harvest_probes(
            load_state(), load_dev_queue().tasks, config=_AUTO, probes=probes
        )

    assert probes.captured_keys == frozenset()
    assert git_calls == []
    assert "1 candidate(s) deferred to the next tick" in caplog.text


def test_reconcile_requeues_a_dead_codex_session_with_git_outside_the_lock(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: one ordinary reconcile() tick gates the dead codex process
    on probes its lockless pre-pass captured, and requeues it."""
    orchestrator_config_file().parent.mkdir(parents=True, exist_ok=True)
    orchestrator_config_file().write_text("reap_policy: auto\n")
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    git_calls = _record_git_lock_state(monkeypatch)

    reconcile()

    assert {subcommand for subcommand, _ in git_calls} == {"status", "rev-parse"}
    assert all(lock_free for _, lock_free in git_calls)
    _assert_session_closed()
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING
    assert [p["reason"] for p in _requeued_events("codex-harvest-e2e")] == [
        CODEX_HARVEST_CLEAN_REQUEUE_REASON
    ]
