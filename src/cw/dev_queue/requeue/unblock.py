"""Salvage-park clearing: ``unblock_ticket`` (#3739)."""

from __future__ import annotations

from cw.dev_queue._requeue_messages import _UNBLOCK_SALVAGE_ONLY
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.lifecycle import transition_task_status
from cw.dev_queue.next_commands import refusal_suffix
from cw.dev_queue.refusal import (
    refusal_hint,
    row_finalize_in_pipeline,
    row_pipeline_position,
)
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.exceptions import UnblockStateError
from cw.models import QueueItemStatus


def unblock_ticket(ticket_id: str, client_name: str) -> dict[str, str]:
    """Clear salvage/park markers and requeue a SALVAGE_PARKED ticket.

    Returns dict with ticket_id, client for event emission.

    Raises:
        UnblockStateError: if ticket is not park-marked (reap_reason != SALVAGE_PARKED).
        CwError: if no matching task is found.
    """
    from cw.config import load_state, save_state, sessions_lock
    from cw.models import ReapReason

    # Fast-fail pre-check outside any lock; re-validated under sessions_lock below.
    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client_name)

    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        msg = (
            f"Cannot unblock ticket '{ticket_id}': status is {task.status.value!r},"
            f" expected BLOCKED_ON_USER.{_UNBLOCK_SALVAGE_ONLY}{refusal_hint(task)}"
        )
        raise UnblockStateError(msg)

    session_id = task.session_id
    # Resolved before sessions_lock: the refusals below read no config under it.
    position = row_pipeline_position(task)
    finalize = row_finalize_in_pipeline(task)

    # Why: sessions_lock outer, dev_queue_lock inner — canonical dual-lock ordering.
    # Both saves happen under the outer lock so sessions.json is never written unless
    # dev_queue.json succeeds first (safe-fail direction for partial-commit scenarios).
    # bounded=True (#2491): operator `cw dev-queue unblock`; only a read-only
    # pre-check precedes the lock, so a timeout is a clean retry.
    with sessions_lock(bounded=True):
        state = load_state()
        session = None
        if session_id is not None:
            session = state.find_by_name_or_id(session_id)

        if session is None or session.reap_reason != ReapReason.SALVAGE_PARKED:
            if session is None:
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': session not found"
                    f" (session_id={session_id!r}). The park-marked precondition"
                    " cannot be confirmed."
                )
            else:
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': session is not park-marked"
                    f" (reap_reason={session.reap_reason!r},"
                    " expected SALVAGE_PARKED)."
                )
            # Pure suffix from the already-loaded session: no state read under
            # sessions_lock (#2692).
            suffix = refusal_suffix(
                task,
                session=session,
                pipeline_position=position,
                finalize_in_pipeline=finalize,
            )
            msg += f"{_UNBLOCK_SALVAGE_ONLY}{suffix}"
            raise UnblockStateError(msg)

        # bounded=True (#2501): explicit operator command (the orchestrator
        # session counts, ADR-0020), never an unattended loop; the inner
        # dev-queue lock is taken before any write, so a timeout is a clean
        # retry. It holds sessions_lock while it waits (up to the timeout
        # again).
        with _lock(bounded=True):
            store = load_dev_queue()
            task = _find_ticket(store, ticket_id, client_name)
            if task.status != QueueItemStatus.BLOCKED_ON_USER:
                suffix = refusal_suffix(
                    task,
                    session=session,
                    pipeline_position=position,
                    finalize_in_pipeline=finalize,
                )
                msg = (
                    f"Cannot unblock ticket '{ticket_id}': status changed to"
                    f" {task.status.value!r} concurrently. Re-check and retry."
                    f"{_UNBLOCK_SALVAGE_ONLY}{suffix}"
                )
                raise UnblockStateError(msg)
            transition_task_status(task, QueueItemStatus.PENDING)
            task.session_id = None
            task.stage_base_ref = None
            save_dev_queue(store)

        session.last_result = None
        session.reap_reason = None
        save_state(state)

    return {"ticket_id": ticket_id, "client": client_name}
