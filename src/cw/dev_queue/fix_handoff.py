"""Record the review fix-agent handoff on a dev-queue row (#2651).

The review stage's fix loop writes the fix-agent prompt and then records it on
the ticket's row, so the next reconcile tick can dispatch the fix agent from
the row (:mod:`cw.reconcile.fix_dispatch`). This module is the library half of
``cw dev-queue record-fix-handoff``; the CLI builds all user-facing text.

Only a RUNNING row accepts a handoff. Any other status is refused here because
``fix_dispatch`` drops a handoff on a non-RUNNING row as stale and pages the
operator, so a recorded handoff there could never dispatch. No event is
emitted and no audit record is written: the handoff is a row field.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.exceptions import CwError
from cw.models import PendingFixDispatch, QueueItemStatus


def record_pending_fix_dispatch(
    ticket_id: str,
    client_name: str,
    *,
    prompt: str,
    cycle: int,
    requested_by_session_id: str,
    bounded: bool = False,
) -> str | None:
    """Stamp a fix-agent handoff on the RUNNING row for *ticket_id*.

    Overwrites any handoff already on the row, as the old inline writer did.
    Returns the row's own ``session_id`` (None when the row is unstamped) so
    the caller can warn when it differs from *requested_by_session_id*.

    Raises:
        CwError: no matching row, or the row is not RUNNING (nothing written);
            a lock timeout when *bounded* is set (``LockTimeoutError`` is a
            ``CwError``).
        OSError: the queue save failed.
    """
    with _lock(bounded=bounded):
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)
        if task.status != QueueItemStatus.RUNNING:
            msg = (
                f"Cannot record the fix-agent handoff for ticket '{ticket_id}'"
                f" in client '{client_name}': the row is {task.status.value},"
                " not running, so reconcile would drop the handoff as stale."
                " Nothing was recorded."
            )
            raise CwError(msg)
        task.pending_fix_dispatch = PendingFixDispatch(
            prompt=prompt,
            label=f"fix-{ticket_id}",
            cycle=cycle,
            requested_by_session_id=requested_by_session_id,
            requested_at=datetime.now(UTC),
        )
        save_dev_queue(store)
        return task.session_id
