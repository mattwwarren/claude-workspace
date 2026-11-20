"""Claim-time merge-prerequisite gate (GitHub #3791).

A row listing an unsatisfied same-repo prerequisite
(``TicketTask.merge_prerequisites``, declared by ``cw dev-queue prerequisite``)
is parked before any claim, at every stage, as a ``dependency_hold`` /
``dependency_unmerged`` on the first unsatisfied PR with ``blocked_on_pr``
set. That is exactly the shape ``cw dev-queue hold`` writes, so the existing
stale-dispatch registrar watches the PR and ``release_stale_gated_tasks``
releases the row on a verified merge, stamping the entry ``satisfied_at``.

The park is uncharged by construction: the row never leaves PENDING, so no
``attempts``, ``unproductive_attempts`` or ``regress_attempts`` is spent, the
stage is unchanged, and no spawn happens. ``occupies_lane_slot`` already frees
the lane slot of a dependency hold with ``blocked_on_pr`` set.

Unlike its sibling gates this park clears ``session_id``: the release treats a
sessionless dependency hold as an operator hold and releases it under any reap
policy, and a PENDING row can carry a stale id. It records the ``ticket.held``
audit event and pages no one: the operator declared the prerequisite.

A pure row read with no I/O beyond the queue save and the event, so it is safe
under the claim lock. The in-stage review fix-agent spawn
(``cw.reconcile.fix_dispatch``) of an already-claimed attempt is not a stage
dispatch and is not gated (a documented limit).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.auto_dev_result import DEPENDENCY_UNMERGED_BLOCKER_REASON
from cw.dev_queue import save_dev_queue, transition_task_status
from cw.dev_queue.hold import HoldResult, record_hold_event
from cw.models import QueueItemStatus
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION
from cw.models.merge_prerequisite import unsatisfied

if TYPE_CHECKING:
    from cw.models import DevQueueStore, TicketTask

_log = logging.getLogger("cw.dispatch")


def park_unmet_merge_prerequisite(
    task: TicketTask, store: DevQueueStore, *, client_name: str
) -> bool:
    """Park *task* on its first unsatisfied merge prerequisite; True iff parked.

    ``unproductive=False`` and a PENDING-to-BLOCKED transition: no RUNNING
    exit, so nothing is charged. ``pr_url`` is carried because a
    BLOCKED_ON_USER transition assigns it unconditionally, and
    ``blocked_on_pr`` is set after the transition, which clears it.
    """
    pending = unsatisfied(task.merge_prerequisites)
    if not pending:
        return False
    first = pending[0]
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=DEPENDENCY_HOLD_DISPOSITION,
        blocked_reason=DEPENDENCY_UNMERGED_BLOCKER_REASON,
        pr_url=task.pr_url,
        unproductive=False,
    )
    prior = task.session_id
    task.session_id = None
    task.blocked_on_pr = first.pr
    save_dev_queue(store)
    # State first, then event: the same order as `cw dev-queue hold`.
    record_hold_event(
        HoldResult(
            ticket_id=task.ticket_id,
            client=task.client,
            pr_number=first.pr.number,
            outcome="held",
            previous_pr=None,
            previous_disposition=None,
            previous_blocked_reason=None,
            converted_from_worker_park=False,
            cleared_session_id=prior,
            pr_repo=None,
            previous_pr_repo=None,
            via_ticket=None,
        ),
        first.repo,
    )
    _log.warning(
        "dispatch: merge-prerequisite park %s/%s until PR %s merges (%d unmet)",
        client_name,
        task.ticket_id,
        first.pr.label(),
        len(pending),
    )
    return True
