"""Dev-queue drain: batch-resume every Rule-5 availability park (RFC 0011 A4).

`drain_held_tickets` is the batch sibling of `requeue_ticket`: it selects
every BLOCKED_ON_USER row for *client* whose disposition marks it a Rule-5
availability park (RFC 0011 A1's `awaiting_operator` disposition), and
requeues each one at its own current stage. It deliberately does NOT wrap
requeue_ticket in the drain-command interpretation of RFC 0011 A4's
`detect_current_stage()` language -- that phrase names prose inside
.claude/commands/auto-dev.md, not code (see #1161 R2); the real mechanism is
requeue_ticket's same-stage default (R3, R7).

Scope: A3 force-holds (proactive stop-before-finalize, #1160) are explicitly
OUT of scope -- see DRAIN_DISPOSITIONS below and RFC 0011 A4 R11. #1702's
review-health gate is explicitly IN scope (it clears by re-running review),
even though it is not a HOLD_DISPOSITIONS member -- see DRAIN_DISPOSITIONS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

from cw.dev_queue.lifecycle import (
    FINALIZE_GATE_HELD_DISPOSITION,
    HOLD_DISPOSITIONS,
    REVIEW_HEALTH_GATE_DISPOSITION,
)
from cw.dev_queue.requeue import requeue_ticket
from cw.dev_queue.storage import load_dev_queue
from cw.exceptions import CwError

if TYPE_CHECKING:
    from cw.models import TicketTask

DrainStatus = Literal["requeued", "failed", "would_requeue"]


class DrainOutcome(TypedDict):
    """Structured per-ticket result from `drain_held_tickets`.

    `from_stage`/`to_stage` are populated only when `status == "requeued"`
    (`None` otherwise) so callers consume the stage transition directly
    instead of re-parsing it out of `detail`, which is a display string.
    """

    ticket_id: str
    client: str
    status: DrainStatus
    detail: str
    from_stage: str | None
    to_stage: str | None


# RFC 0011 A4 R11: drain's selection is the Rule-5 availability-park subset
# of HOLD_DISPOSITIONS, EXCLUDING the A3 force-hold disposition (#1160's
# FINALIZE_GATE_HELD_DISPOSITION) -- a force hold is never batch-released.
# Derived by subtraction now that #1160 has landed (the original hand-pin
# existed only because the constant wasn't importable yet); a future hold
# disposition added to HOLD_DISPOSITIONS therefore flows into drain's
# selection automatically -- exclude it here explicitly if it must not be
# batch-releasable, mirroring the A3 exclusion.
#
# #1702 adds REVIEW_HEALTH_GATE_DISPOSITION by explicit union rather than
# through HOLD_DISPOSITIONS (it is deliberately not a member of that set, see
# lifecycle.py). The two exclusion/inclusion calls have opposite answers for a
# reason: an A3 force hold is a deliberate operator stop, so batch-releasing it
# would override the operator; a review-health park is "the review that ran did
# not vouch for its own coverage", which clears by re-running review -- exactly
# what drain does. Including it here does NOT make it concierge-false-park-
# eligible; those sets are independent by design.
DRAIN_DISPOSITIONS: frozenset[str] = (
    HOLD_DISPOSITIONS - frozenset({FINALIZE_GATE_HELD_DISPOSITION})
) | frozenset({REVIEW_HEALTH_GATE_DISPOSITION})


def select_held_tickets(client: str, *, lane: str | None = None) -> list[TicketTask]:
    """Unlocked selection snapshot: Rule-5 availability parks for *client*.

    Read-only; `load_dev_queue()` takes no lock (safe for selection, per
    storage.py's docstring). Callers must treat this as a snapshot, not a
    guarantee -- a concurrent dispatch_tick reap or `cw dev-queue cancel` can
    race a selected row out from under drain_held_tickets between this read
    and the per-ticket requeue_ticket() call (R5's TOCTOU window).
    """
    store = load_dev_queue()
    return [
        t
        for t in store.tasks
        if t.client == client
        and t.disposition in DRAIN_DISPOSITIONS
        and (lane is None or t.lane == lane)
    ]


def drain_held_tickets(
    client: str, *, lane: str | None = None, dry_run: bool = False
) -> list[DrainOutcome]:
    """Batch-requeue every Rule-5 availability park for *client*.

    Continue-on-error (R5): reads the selection snapshot unlocked, then loops
    calling requeue_ticket() per ticket with NO outer lock held -- requeue_
    ticket() takes dev_queue_lock() internally per call (R4); wrapping this
    loop in `with dev_queue_lock():` self-deadlocks on the second iteration,
    since _lock() is a plain non-reentrant fcntl.flock (a fresh open() each
    call yields a distinct open-file-description, so the second flock() call
    from the same process blocks on the first, held, one).

    A RequeueStateError on one ticket (status raced away from BLOCKED_ON_USER
    between the snapshot and this call) does not abort the batch; it is
    recorded as a per-ticket "failed" outcome and the loop proceeds.

    Returns one `DrainOutcome` per selected ticket. `detail` is always a
    human-readable message (the stage transition, the target stage for a
    dry-run preview, or the error string); `from_stage`/`to_stage` are the
    structured transition fields, populated only for `status == "requeued"`.
    Empty selection returns an empty list (no-op, no side effects).
    dry_run=True performs no mutation and reports every selected row as
    "would_requeue".
    """
    selected = select_held_tickets(client, lane=lane)
    outcomes: list[DrainOutcome] = []
    for task in selected:
        if dry_run:
            outcomes.append(
                {
                    "ticket_id": task.ticket_id,
                    "client": client,
                    "status": "would_requeue",
                    "detail": task.stage.value,
                    "from_stage": None,
                    "to_stage": None,
                }
            )
            continue
        try:
            result = requeue_ticket(task.ticket_id, client)
        except CwError as exc:
            outcomes.append(
                {
                    "ticket_id": task.ticket_id,
                    "client": client,
                    "status": "failed",
                    "detail": str(exc),
                    "from_stage": None,
                    "to_stage": None,
                }
            )
            continue
        from_stage = str(result["from_stage"])
        to_stage = str(result["to_stage"])
        outcomes.append(
            {
                "ticket_id": task.ticket_id,
                "client": client,
                "status": "requeued",
                "detail": f"{from_stage} -> {to_stage}",
                "from_stage": from_stage,
                "to_stage": to_stage,
            }
        )
    return outcomes
