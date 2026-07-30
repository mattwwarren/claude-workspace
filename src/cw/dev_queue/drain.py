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
OUT of scope -- see DRAIN_DISPOSITIONS below and RFC 0011 A4 R11.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue.lifecycle import AWAITING_OPERATOR_DISPOSITION
from cw.dev_queue.requeue import requeue_ticket
from cw.dev_queue.storage import load_dev_queue
from cw.exceptions import CwError

if TYPE_CHECKING:
    from cw.models import TicketTask

# RFC 0011 A4 R11 (2026-07-30): drain's selection is the Rule-5 availability-
# park subset of HOLD_DISPOSITIONS, EXCLUDING the A3 force-hold disposition
# (#1160's FINALIZE_GATE_HELD_DISPOSITION, which extends HOLD_DISPOSITIONS in
# place per lifecycle.py:82's comment). That constant does not exist on main
# yet (verified 2026-07-30) -- pinning DRAIN_DISPOSITIONS to the single known
# Rule-5 member directly is arithmetically identical to `HOLD_DISPOSITIONS -
# {force_hold_disposition}` both today and after #1160 lands, and avoids
# importing a symbol that isn't importable yet. Follow-up when #1160 merges:
# no code change needed here; just confirm test_drain_excludes_a3_force_hold
# (tests/test_dev_queue.py) still asserts against the real constant name/value.
DRAIN_DISPOSITIONS: frozenset[str] = frozenset({AWAITING_OPERATOR_DISPOSITION})


def select_held_tickets(
    client: str, *, lane: str | None = None
) -> list[TicketTask]:
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
) -> list[dict[str, str]]:
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

    Returns one outcome dict per selected ticket:
    {"ticket_id", "client", "status": "requeued"|"failed"|"would_requeue",
     "detail": <from_stage -> to_stage, or the error message>}.
    Empty selection returns an empty list (no-op, no side effects).
    dry_run=True performs no mutation and reports every selected row as
    "would_requeue".
    """
    selected = select_held_tickets(client, lane=lane)
    outcomes: list[dict[str, str]] = []
    for task in selected:
        if dry_run:
            outcomes.append(
                {
                    "ticket_id": task.ticket_id,
                    "client": client,
                    "status": "would_requeue",
                    "detail": task.stage.value,
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
                }
            )
            continue
        outcomes.append(
            {
                "ticket_id": task.ticket_id,
                "client": client,
                "status": "requeued",
                "detail": f"{result['from_stage']} -> {result['to_stage']}",
            }
        )
    return outcomes
