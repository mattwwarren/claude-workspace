"""Tests for the evidence-only stalled sweep (``cw.reconcile.stalled``).

Since the process-kill-timeout removal the sweep produces exactly one
disposition — COMPLETE_FOREIGN_RESULT (#1470) — and elapsed wall-clock time
never dispositions a session. These tests pin both the retained behavior and
the removal itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    CompletionReason,
    CwState,
    LastResultSource,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    ProposedAction,
    _apply_sentinel_to_task,
    _validate_existing_result_for_routing,
)
from cw.reconcile.fix_dispatch import FIX_LOOP_PENDING_DISPATCH
from cw.reconcile.stalled import (
    _act_on_stalled_candidates,
    _detect_stalled_candidates,
)
from tests._reconcile_helpers import (
    _blocked_result_payload,
    _make_pending_fix_dispatch,
    _make_terminal_payload,
    _mk_headless_daemon_session,
    _shipped_salvage_payload,
    _stage_complete_payload,
    _write_staged_clients_yaml,
)

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
# Ten hours after start — far past every historical wall-clock budget.
_NOW = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


class _StopRecorder:
    """Minimal daemon-client stub recording stop() calls."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    def stop(self, surface_ref: str) -> None:
        self.stopped.append(surface_ref)


@pytest.fixture
def stop_recorder(monkeypatch: pytest.MonkeyPatch) -> _StopRecorder:
    recorder = _StopRecorder()
    monkeypatch.setattr(_deps, "get_native_daemon_client", lambda: recorder)
    return recorder


def _foreign_result_session(tmp_path: Path, payload: dict[str, Any]) -> CwState:
    sess = _mk_headless_daemon_session("salv-1", tmp_path / "wt", _STARTED_AT)
    sess.last_result = payload
    return CwState(sessions=[sess])


def test_foreign_terminal_result_produces_candidate(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    state = _foreign_result_session(tmp_path, _shipped_salvage_payload())

    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.proposed_action is ProposedAction.COMPLETE_FOREIGN_RESULT
    assert candidate.ticket_id == "salv-1"
    assert candidate.routed_sentinel is not None
    assert candidate.routed_sentinel.status == "shipped"


def test_invalid_foreign_result_short_circuits_without_candidate(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A malformed terminal-looking last_result yields no disposition at all.

    The guard still short-circuits (drop-only fallthrough, #1470 R4) — it is
    not re-offered to any other classification.
    """
    state = _foreign_result_session(tmp_path, {"status": "not-a-real-status"})

    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    assert candidates == []


def test_quiet_session_is_never_dispositioned_on_elapsed_time(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Removal regression: hours past start, no sentinel → zero candidates.

    Under the removed wall-clock machinery this session would have been
    reverted/parked and its daemon stopped. Now elapsed time produces nothing.
    """
    sess = _mk_headless_daemon_session("T-9", tmp_path / "wt", _STARTED_AT)
    assert sess.last_result is None

    candidates = _detect_stalled_candidates(
        state=CwState(sessions=[sess]), task_by_ticket={}
    )

    assert candidates == []
    assert sess.status is SessionStatus.ACTIVE


def test_non_headless_session_is_skipped(tmp_config_dir: Path, tmp_path: Path) -> None:
    sess = _mk_headless_daemon_session("salv-1", tmp_path / "wt", _STARTED_AT)
    (tmp_path / "wt" / ".claude" / "cw-context.json").write_text('{"headless": false}')
    sess.last_result = _shipped_salvage_payload()

    candidates = _detect_stalled_candidates(
        state=CwState(sessions=[sess]), task_by_ticket={}
    )

    assert candidates == []


def test_act_completes_session_routes_task_and_stops_surface(
    tmp_config_dir: Path, tmp_path: Path, stop_recorder: _StopRecorder
) -> None:
    state = _foreign_result_session(tmp_path, _shipped_salvage_payload())
    store = load_dev_queue()
    store.tasks.append(
        TicketTask(
            ticket_id="salv-1", client="client-a", status=QueueItemStatus.RUNNING
        )
    )
    save_dev_queue(store)
    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    _act_on_stalled_candidates(state, candidates, now=_NOW)

    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_at == _NOW
    task = load_dev_queue().tasks[0]
    assert task.status is not QueueItemStatus.RUNNING
    assert stop_recorder.stopped == ["fake-short-id"]


def test_act_with_no_candidates_is_a_noop(
    tmp_config_dir: Path, tmp_path: Path, stop_recorder: _StopRecorder
) -> None:
    sess = _mk_headless_daemon_session("T-9", tmp_path / "wt", _STARTED_AT)
    state = CwState(sessions=[sess])

    _act_on_stalled_candidates(state, [], now=_NOW)

    assert sess.status is SessionStatus.ACTIVE
    assert stop_recorder.stopped == []


def test_foreign_result_completion_uses_evidence_not_same_stage_predicate(
    tmp_config_dir: Path, tmp_path: Path, stop_recorder: _StopRecorder
) -> None:
    """#1750: a foreign result carrying commits is productive, so it is not charged.

    Direct regression for the round-1 review finding that this site used a
    "same stage?" predicate instead of asking whether the claim produced
    anything. The shipped salvage payload carries commits, so the real
    classifier must decline the charge regardless of stage movement.
    """
    state = _foreign_result_session(tmp_path, _shipped_salvage_payload())
    store = load_dev_queue()
    store.tasks.append(
        TicketTask(
            ticket_id="salv-1", client="client-a", status=QueueItemStatus.RUNNING
        )
    )
    save_dev_queue(store)
    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    _act_on_stalled_candidates(state, candidates, now=_NOW)

    task = load_dev_queue().tasks[0]
    assert task.status is not QueueItemStatus.RUNNING
    assert task.unproductive_attempts == 0


def test_foreign_result_with_no_evidence_is_charged(
    tmp_config_dir: Path, tmp_path: Path, stop_recorder: _StopRecorder
) -> None:
    """#1750: the same site DOES charge when the sentinel shows no work done."""
    payload = _shipped_salvage_payload()
    payload["commits"] = []
    payload["review"] = {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0}
    state = _foreign_result_session(tmp_path, payload)
    store = load_dev_queue()
    store.tasks.append(
        TicketTask(
            ticket_id="salv-1", client="client-a", status=QueueItemStatus.RUNNING
        )
    )
    save_dev_queue(store)
    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    _act_on_stalled_candidates(state, candidates, now=_NOW)

    task = load_dev_queue().tasks[0]
    assert task.unproductive_attempts == 1


def test_stage_complete_foreign_result_classified_as_route_emitted_sentinel(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """#2426: a live worker's stage_complete foreign result is NOT completed outright.

    ``stage_complete`` is an INTERMEDIATE_ADVANCE_STATUSES member -- it must
    be reclassified to ROUTE_EMITTED_SENTINEL so the owning task's stage
    advances instead of landing terminal-COMPLETED.
    """
    state = _foreign_result_session(tmp_path, _stage_complete_payload())

    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.proposed_action is ProposedAction.ROUTE_EMITTED_SENTINEL
    assert candidate.routed_sentinel is not None
    assert candidate.routed_sentinel.status == "stage_complete"


def test_live_session_premises_pending_verification_defers_to_stop_hook(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """#2435: a still-live worker's EMIT_CLI-sourced result is never claimed here.

    ``cw result emit`` never flips ``session.status`` (#2382), so a genuinely
    running worker can hold a ``premises_pending_verification`` sentinel while
    ``session.status`` stays ACTIVE. That worker's own Stop hook (#536 emit
    precedence) is the routing authority for it -- the stalled sweep must not
    also offer it as a candidate, racing the Stop hook's own
    ``_apply_sentinel_to_task`` call.
    """
    payload = _make_terminal_payload("premises_pending_verification", "salv-1")
    state = _foreign_result_session(tmp_path, payload)
    state.sessions[0].last_result_source = LastResultSource.EMIT_CLI

    assert _detect_stalled_candidates(state, task_by_ticket={}) == []

    session = state.sessions[0]
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    store = load_dev_queue()
    store.tasks.append(
        TicketTask(
            ticket_id="salv-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="salv-1",
        )
    )
    save_dev_queue(store)

    validated = _validate_existing_result_for_routing(session.last_result)
    assert validated is not None
    _apply_sentinel_to_task("salv-1", session, validated)

    updated_task = load_dev_queue().tasks[0]
    assert updated_task.status == QueueItemStatus.BLOCKED_ON_USER
    assert session.status is SessionStatus.ACTIVE

    attention = read_events(
        consumer="test-2435-premises-attention",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(attention) == 1
    assert attention[0].payload["paused_status"] == "plan_parked"


def test_live_session_blocked_fix_loop_pending_dispatch_stays_running(
    tmp_path: Path,
) -> None:
    """#2435: a live worker's fix-loop-pending BlockedResult is never claimed here.

    The fix-dispatch machinery (``FIX_LOOP_PENDING_DISPATCH``) owns this
    task's row while its fix cycle is outstanding; the stalled sweep must not
    also offer the still-live session as a foreign-result candidate.
    """
    sess = _mk_headless_daemon_session("T-1", tmp_path / "wt", _STARTED_AT)
    sess.last_result = _blocked_result_payload(reason=FIX_LOOP_PENDING_DISPATCH)
    sess.last_result_source = LastResultSource.EMIT_CLI
    task = TicketTask(
        ticket_id="T-1",
        client="client-a",
        status=QueueItemStatus.RUNNING,
        session_id="T-1",
        stage=Stage.REVIEW,
        pending_fix_dispatch=_make_pending_fix_dispatch(),
    )

    candidates = _detect_stalled_candidates(
        CwState(sessions=[sess]), task_by_ticket={"T-1": task}
    )

    assert candidates == []
    assert task.status == QueueItemStatus.RUNNING
    assert task.pending_fix_dispatch is not None


def test_dead_session_stage_complete_still_advances_via_sweep(
    tmp_config_dir: Path, tmp_path: Path, stop_recorder: _StopRecorder
) -> None:
    """#2426: the incident case -- a dead IMPL worker's mid-pipeline result must
    advance IMPL -> REVIEW (leaving the task PENDING for a fresh REVIEW
    dispatch), not land it terminal-COMPLETED at IMPL.

    #2435: stamps ``last_result_source=SALVAGE_TRANSCRIPT`` to make the "dead"
    precondition explicit -- this sweep's disposition now depends on the
    result not having come from a still-live worker's own ``cw result emit``
    (``EMIT_CLI``), which defers to the Stop hook instead (see the two tests
    above).
    """
    state = _foreign_result_session(tmp_path, _stage_complete_payload())
    state.sessions[0].last_result_source = LastResultSource.SALVAGE_TRANSCRIPT
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    store = load_dev_queue()
    store.tasks.append(
        TicketTask(
            ticket_id="salv-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="salv-1",
            stage=Stage.IMPL,
        )
    )
    save_dev_queue(store)
    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    _act_on_stalled_candidates(state, candidates, now=_NOW)

    task = load_dev_queue().tasks[0]
    assert task.stage == Stage.REVIEW
    assert task.status != QueueItemStatus.COMPLETED
    assert task.status == QueueItemStatus.PENDING
    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.NORMAL
    assert stop_recorder.stopped == ["fake-short-id"]


def test_stage_complete_at_last_pipeline_stage_still_completes_task(
    tmp_config_dir: Path, tmp_path: Path, stop_recorder: _StopRecorder
) -> None:
    """#2426: a stage_complete claim at the pipeline's last stage still completes.

    ``_apply_sentinel_to_task`` -> ``apply_staged_decision`` ->
    ``_route_stage_success`` completes the task when the task is already at
    the pipeline's last stage, regardless of which sweep invoked it -- this
    pins that the reclassification does not regress the terminal-shipped
    guarantee for a last-stage stage_complete claim.
    """
    payload = _stage_complete_payload()
    payload["stage_reached"] = "stage5_post_create"
    state = _foreign_result_session(tmp_path, payload)
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    store = load_dev_queue()
    store.tasks.append(
        TicketTask(
            ticket_id="salv-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="salv-1",
            stage=Stage.FINALIZE,
        )
    )
    save_dev_queue(store)
    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    _act_on_stalled_candidates(state, candidates, now=_NOW)

    task = load_dev_queue().tasks[0]
    assert task.status == QueueItemStatus.COMPLETED
    assert task.stage == Stage.FINALIZE


def test_stage_mismatch_refusal_is_not_reoffered(
    tmp_config_dir: Path, tmp_path: Path, stop_recorder: _StopRecorder
) -> None:
    """#2426: a stale/earlier-stage stage_complete claim is refused, not completed.

    The task is already past the sentinel's claimed stage (REVIEW vs. the
    default payload's stage2_impl/IMPL) -- an earlier-stage stage-advance
    claim, which the shared staged-advance guard refuses. The session must
    stay live (not torn down) and the refusal must latch so the doomed
    candidate is never re-offered.
    """
    state = _foreign_result_session(tmp_path, _stage_complete_payload())
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    store = load_dev_queue()
    store.tasks.append(
        TicketTask(
            ticket_id="salv-1",
            client="client-a",
            status=QueueItemStatus.RUNNING,
            session_id="salv-1",
            stage=Stage.REVIEW,
        )
    )
    save_dev_queue(store)
    candidates = _detect_stalled_candidates(state, task_by_ticket={})

    _act_on_stalled_candidates(state, candidates, now=_NOW)

    session = state.sessions[0]
    assert session.status is SessionStatus.ACTIVE
    assert isinstance(session.last_result, dict)
    assert "status" in session.last_result
    assert stop_recorder.stopped == []
    task = load_dev_queue().tasks[0]
    assert task.stage == Stage.REVIEW
    assert task.status == QueueItemStatus.RUNNING

    candidates_again = _detect_stalled_candidates(state, task_by_ticket={})
    assert candidates_again == []


def test_task_already_terminal_race_completes_session_not_leaked(
    tmp_config_dir: Path, tmp_path: Path, stop_recorder: _StopRecorder
) -> None:
    """#2426 fix-cycle-1: a #2140-shape race must not leak the session.

    A concurrent authority already landed the ticket's dev-queue task
    genuinely terminal (COMPLETED) before this tick's ``_apply_sentinel_to_
    task`` lookup ran, so it reports ``routed=False, task_already_terminal=
    True``. The dead ``emit_result_on`` call previously on this arm always
    refused here -- stalled's own precondition guarantees ``session.
    last_result`` already carries a terminal sentinel (that is exactly what
    made this a candidate), so ``has_terminal_result`` always short-circuited
    it to ``refused=True`` -- leaving the session ACTIVE and the daemon
    surface running forever. The session must instead be completed directly,
    same as the ordinary ``routed=True`` success arm.
    """
    state = _foreign_result_session(tmp_path, _stage_complete_payload())
    _write_staged_clients_yaml(tmp_config_dir, "client-a")
    store = load_dev_queue()
    store.tasks.append(
        TicketTask(
            ticket_id="salv-1",
            client="client-a",
            status=QueueItemStatus.COMPLETED,
            session_id="salv-1",
            stage=Stage.IMPL,
        )
    )
    save_dev_queue(store)
    candidates = _detect_stalled_candidates(state, task_by_ticket={})
    assert len(candidates) == 1
    assert candidates[0].proposed_action is ProposedAction.ROUTE_EMITTED_SENTINEL

    _act_on_stalled_candidates(state, candidates, now=_NOW)

    session = state.sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.completed_at == _NOW
    assert session.completed_reason == CompletionReason.NORMAL
    assert stop_recorder.stopped == ["fake-short-id"]
