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
from cw.models import (
    CwState,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import _deps
from cw.reconcile._shared import ProposedAction
from cw.reconcile.stalled import (
    _act_on_stalled_candidates,
    _detect_stalled_candidates,
)
from tests._reconcile_helpers import (
    _mk_headless_daemon_session,
    _shipped_salvage_payload,
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


def test_in_flight_codex_review_is_never_dispositioned(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """#1727: a healthy backgrounded codex REVIEW is not swept out from under itself.

    Before #1727 a codex review ran inside ``dispatch_tick``'s own call stack,
    so this sweep could never observe one mid-flight. Now that the review runs
    on a background thread, the session sits ACTIVE with no ``last_result``
    for the whole review — exactly the shape a wall-clock sweep would have
    reaped. Pinned here against a REVIEW-stage task so a future
    re-introduction of elapsed-time dispositioning has to break this test.
    """
    sess = _mk_headless_daemon_session("T-review", tmp_path / "wt", _STARTED_AT)
    assert sess.last_result is None
    task = TicketTask(
        ticket_id="T-review",
        client="client-a",
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
    )

    candidates = _detect_stalled_candidates(
        state=CwState(sessions=[sess]), task_by_ticket={"T-review": task}
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
