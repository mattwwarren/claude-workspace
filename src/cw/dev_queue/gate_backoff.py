"""Back off a dev-queue row that keeps hitting the same merge gate (#2717).

A Rule 5 park for ``merge_gate_blocked`` (any or no blocker reason), or for a
``prior_pipeline_pr_open`` blocker under any status, is a *gate-block*: the
ticket cannot progress until some other PR moves. Without a delay the row
flaps PENDING -> RUNNING -> parked on every requeue, re-paging each time. This
module stamps :attr:`cw.models.TicketTask.gate_block` at the park and pushes
the shared ``next_eligible_at`` claim clock out by a doubling delay
(60 s, 120 s, 240 s, 480 s, then 900 s), so the claim screen's existing
``next_eligible_at`` check holds the row off with no change of its own.

ADR-0014 invariant 1: every write here happens at a routing event (a sentinel
arrived) or an evidence-driven release, never by comparing elapsed time. The
run of consecutive parks is keyed on a fingerprint
(``status|blocker_reason|blocking_pr|stage|stage_base_ref``); any change in it
restarts the run at 1.

The clock is shared with the usage-limit and spawn-error backoffs, so this
module never erases another writer's window: a park only ever pushes
``next_eligible_at`` later (``max``), and a clear only removes it while it
still equals the gate's own ``until`` stamp.

A leaf module, the ``gates`` shape: it imports ``cw._util``,
``cw.auto_dev_result``, ``cw.dev_queue.gates`` and ``cw.models.gate_block``
only. It must NOT import ``cw.queue_rows`` (which imports ``cw.dev_queue``,
a cycle) or ``cw.dispatch``; ``requeue`` and ``cw.reconcile.tasks`` import it
at module level. Hence the parser's ``BLOCKER_REASON_PRIOR_PIPELINE_PR_OPEN``
rather than ``cw.queue_rows``' textually identical local literal (a test pins
the two equal).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from cw._util import exponential_backoff_seconds
from cw.auto_dev_result import BLOCKER_REASON_PRIOR_PIPELINE_PR_OPEN
from cw.dev_queue.gates import MERGE_GATE_BLOCKED_STATUS
from cw.models.gate_block import GateBlockBackoff

if TYPE_CHECKING:
    from cw.models import TicketTask

GATE_BLOCK_BACKOFF_INITIAL_SECONDS: Final = 60
GATE_BLOCK_BACKOFF_CAP_SECONDS: Final = 900

_FINGERPRINT_SEPARATOR = "|"


def is_gate_block(status: str | None, blocker_reason: str | None) -> bool:
    """True iff a routed outcome is a gate-block park (#2717).

    ``merge_gate_blocked`` with any or no blocker reason, or a
    ``prior_pipeline_pr_open`` blocker under any status. ``stale_dispatch``,
    ``automerge_not_armed`` and every other ``blocked`` reason are excluded.
    """
    return (
        status == MERGE_GATE_BLOCKED_STATUS
        or blocker_reason == BLOCKER_REASON_PRIOR_PIPELINE_PR_OPEN
    )


def _fingerprint(
    task: TicketTask,
    *,
    status: str | None,
    blocker_reason: str | None,
    blocking_pr: int | None,
) -> str:
    """The key two parks must share to count as consecutive. Pure."""
    parts = (
        status,
        blocker_reason,
        None if blocking_pr is None else str(blocking_pr),
        task.stage.value,
        task.stage_base_ref,
    )
    return _FINGERPRINT_SEPARATOR.join("" if part is None else part for part in parts)


def gate_block_page_note(count: int, next_eligible_at: datetime) -> str:
    """The ``occurrence=N next_eligible_at=<iso>`` breadcrumb suffix. Pure.

    *count* is the true, unbounded run length; only the delay saturates. The
    instant renders in UTC with whole seconds (``+00:00`` suffix).
    """
    iso = next_eligible_at.astimezone(UTC).replace(microsecond=0).isoformat()
    return f"occurrence={count} next_eligible_at={iso}"


def record_gate_block(
    task: TicketTask,
    *,
    status: str | None,
    blocker_reason: str | None,
    blocking_pr: int | None,
    now: datetime,
) -> str:
    """Stamp a gate-block park on *task* and return its page note.

    Extends the run when the fingerprint matches the previous record, else
    restarts it at 1. Sets ``next_eligible_at`` to ``now + backoff(count)``,
    or keeps an existing later window (a usage-limit wait) via ``max``;
    ``gate_block.until`` is always the gate's own instant. The returned note
    names the effective ``next_eligible_at`` the claim screen will honour.
    ``spawn_error_count``, ``attempts`` and ``unproductive_attempts`` are not
    touched.
    """
    fingerprint = _fingerprint(
        task, status=status, blocker_reason=blocker_reason, blocking_pr=blocking_pr
    )
    prior = task.gate_block
    count = prior.count + 1 if prior and prior.fingerprint == fingerprint else 1
    delay = exponential_backoff_seconds(
        count,
        initial_seconds=GATE_BLOCK_BACKOFF_INITIAL_SECONDS,
        cap_seconds=GATE_BLOCK_BACKOFF_CAP_SECONDS,
    )
    until = now + timedelta(seconds=delay)
    existing = task.next_eligible_at
    effective = until if existing is None else max(existing, until)
    task.next_eligible_at = effective
    task.gate_block = GateBlockBackoff(
        count=count, fingerprint=fingerprint, until=until
    )
    return gate_block_page_note(count, effective)


def clear_gate_block_wait(task: TicketTask) -> None:
    """Drop the gate's own wait from *task*, keeping the run count.

    Clears ``next_eligible_at`` only while it still equals
    ``gate_block.until``: a usage-limit or spawn-error window written over it
    (or a row with no gate record at all) is left alone. Called by operator
    ``requeue`` and the Variant B AUTO release, both evidence-driven.
    """
    record = task.gate_block
    if record is not None and task.next_eligible_at == record.until:
        task.next_eligible_at = None


def reset_gate_block_unless_gate(
    task: TicketTask, status: str | None, blocker_reason: str | None
) -> None:
    """End the run on a non-gate-block routed outcome.

    Clears ``gate_block`` only; ``next_eligible_at`` is deliberately kept
    (#2717 Assumption 7), since another writer may own it.
    """
    if not is_gate_block(status, blocker_reason):
        task.gate_block = None
