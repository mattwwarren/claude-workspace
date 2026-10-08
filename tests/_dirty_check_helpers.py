"""Capture-then-consume helpers for the lockless dirty-check pre-pass (#2548).

``reconcile()`` captures the worktree dirty checks before ``sessions_lock``;
the phantom detect and the TIMED_OUT/COMPLETED backstops only look them up,
deferring a session with no usable capture. Tests that drive those consumers
directly use these helpers to stand in for the pre-pass, so they can keep
patching the live helper's seams (``_deps.checked_out_branch``,
``_worktree_evidence.unsaved_work_reason`` and friends). Mirrors
``detect_adopt_plan_prefetched`` / ``run_gate_recipes_prefetched`` in
``tests/_reconcile_helpers.py``, kept separate for that module's size.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.models import OrchestratorConfig, QueueItemStatus
from cw.reconcile import _deps, _shared
from cw.reconcile.dirty_checks import DirtyChecks
from cw.reconcile.phantom import (
    _detect_phantom_candidates,
    capture_phantom_dirty_checks,
)
from cw.reconcile.tasks import (
    capture_backstop_dirty_checks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from tests._reconcile_helpers import _attention_events, _state_queue_snapshot

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pytest

    from cw.models import CwState, Session, TicketTask
    from cw.reconcile._shared import ReapCandidate

DIRTY_CHECKS_LOGGER = "cw.reconcile.dirty_checks"
_UNAVAILABLE = "dirty_check_unavailable"


def detect_phantom_prefetched(
    state: CwState,
    phantom_set: set[str],
    task_by_ticket: dict[str, TicketTask] | None = None,
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
) -> list[ReapCandidate]:
    """Run the phantom detect the way ``reconcile()`` does: capture, then look up."""
    effective = config if config is not None else OrchestratorConfig()
    checks = DirtyChecks()
    capture_phantom_dirty_checks(
        state,
        phantom_set,
        task_by_ticket or {},
        now=now,
        config=effective,
        checks=checks,
    )
    return _detect_phantom_candidates(
        state,
        phantom_set,
        task_by_ticket,
        now=now,
        config=config,
        dirty_checks=checks,
    )


def capture_backstops_from_disk() -> DirtyChecks:
    """Capture the backstops' dirty checks from the on-disk state and queue."""
    checks = DirtyChecks()
    capture_backstop_dirty_checks(
        load_state(), load_dev_queue().tasks, now=datetime.now(UTC), checks=checks
    )
    return checks


def revert_timed_out_prefetched() -> list[str]:
    """Capture the backstop dirty checks, then run ``revert_timed_out_tasks``."""
    return revert_timed_out_tasks(capture_backstops_from_disk())


def revert_completed_silent_prefetched() -> list[str]:
    """Capture the backstop dirty checks, then run ``revert_completed_silent_tasks``."""
    return revert_completed_silent_tasks(capture_backstops_from_disk())


def checks_with(
    sessions: list[Session], reasons: Mapping[str, str | None]
) -> DirtyChecks:
    """A ``DirtyChecks`` holding *reasons* (keyed by session id) as captures.

    The live helper is stubbed for the capture only, so no git runs.
    """
    checks = DirtyChecks()

    def _reason(_client: str, path: object) -> str | None:
        session = next(s for s in sessions if s.worktree_path == path)
        return reasons[session.id]

    with patch.object(_shared, "worktree_dirty_reason_by_path", _reason):
        for session in sessions:
            checks.capture(session)
    return checks


def unavailable_records(
    caplog: pytest.LogCaptureFixture, session_id: str
) -> list[logging.LogRecord]:
    """The ``dirty_check_unavailable`` warnings logged for *session_id*."""
    return [
        r
        for r in caplog.records
        if r.name == DIRTY_CHECKS_LOGGER
        and r.levelno == logging.WARNING
        and r.getMessage().startswith(f"{_UNAVAILABLE}: session={session_id} ")
    ]


def assert_deferred(
    *,
    before: bytes,
    ticket_id: str,
    session: Session,
    caplog: pytest.LogCaptureFixture,
    consumer: str,
) -> None:
    """The full "deferred" contract for one session (#2548).

    State, queue and events inbox are byte-identical to *before*; the row is
    still RUNNING and bound (so it is not BLOCKED_ON_USER and takes no new
    lane slot); the session's status and reap_reason are unchanged; nothing
    paged or pushed; and exactly one ``dirty_check_unavailable`` warning
    names the session.
    """
    assert _state_queue_snapshot() == before
    row = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
    assert row.status is QueueItemStatus.RUNNING
    assert row.session_id == session.id
    stored = next(s for s in load_state().sessions if s.id == session.id)
    assert stored.status is session.status
    assert stored.reap_reason == session.reap_reason
    assert _attention_events(consumer, ticket_id) == []
    push = _deps.fire_push_notification
    assert isinstance(push, MagicMock)
    push.assert_not_called()
    assert len(unavailable_records(caplog, session.id)) == 1
