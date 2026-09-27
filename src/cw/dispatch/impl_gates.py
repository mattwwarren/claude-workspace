"""The IMPL-scoped gate table for the dispatch staged-decision router (#2421).

Sibling to ``dispatch/review_gates.py``, which is explicitly scoped to the
REVIEW->FINALIZE ship checkpoint -- its module docstring and every predicate's
own docstring say gating earlier stages would park work that is simply not
finished. This module holds the one gate that must run at IMPL instead, in the
same ``_should_gate_for_*`` predicate / ``_park_*`` helper shape.

**The gate.** A FINALIZE pre-push refresh that hits a merge conflict regresses
the ticket to IMPL (Rule 5a, #770) so a fresh session can resolve it. If that
session stages the resolution but never commits it, ``MERGE_HEAD`` stays set,
the branch head never moves, and Rule 3 used to advance IMPL->REVIEW anyway --
sending an unconcluded merge on to review and back to FINALIZE, which hits the
same conflict again. ``_should_gate_for_unconcluded_finalize_regress_merge``
parks that row ``BLOCKED_ON_USER`` instead, when
``task.finalize_regress_branch_head`` shows a FINALIZE-origin regress is in
play and either:

  1. ``MERGE_HEAD`` is still present in the ticket's worktree -- regardless of
     why the regress happened; or
  2. the branch head has not moved since the regress **and** the regress was
     measured as merge-caused (``finalize_regress_merge_conflict_detected``).
     A non-merge regress (e.g. a diff-cover ``agent_block``) with no new
     commit is deliberately left to existing routing: #1717's REVIEW-side
     repeat signal already covers it.

**Fails closed on the unmeasurable branches** -- an unresolvable worktree, an
unreadable ``MERGE_HEAD`` probe, or an unreadable HEAD. This is this module's
own design decision, on the precedent of
``review_gates._should_gate_for_review_staleness``: the question is "is there
evidence the merge concluded?", and absent evidence is the problem itself. A
missing marker is not a failure -- the gate simply does not apply.

**Neither field is consumed here.** Both stay set for
``regress_repeat._consume_finalize_regress_repeat`` at the round trip's REVIEW
re-entry, which clears them together.

**Known limitation.** Wired only at ``_route_stage_success``'s single-hop site,
not the multi-hop ``_walk_stage_pointer_forward`` (mirrors #1801's accepted
limitation style). The session-level self-check in ``auto-dev-impl.md``'s
Stage 2 spawn prompt is the first line of defense; this is the backstop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.branch_ahead import current_head_sha, merge_in_progress
from cw.dev_queue import (
    UNCONCLUDED_FINALIZE_REGRESS_MERGE_GATE_DISPOSITION,
    transition_task_status,
)
from cw.events import record_event
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        TicketTask,
    )


# paused_status written to SESSION_NEEDS_ATTENTION when this gate parks a row.
# Shares its literal string value with dev_queue.lifecycle.
# UNCONCLUDED_FINALIZE_REGRESS_MERGE_GATE_DISPOSITION (task.disposition) on the
# review_gates._EMPTY_DIFF_GATE_REASON precedent -- still two constants in two
# namespaces, do not collapse them. Gate-class, so it hardcodes breadcrumbs=""
# and must stay out of BREADCRUMB_ELIGIBLE_PAUSED_STATUSES (#1729).
_UNCONCLUDED_MERGE_REASON = "unconcluded_finalize_regress_merge"


def _should_gate_for_unconcluded_finalize_regress_merge(
    task: TicketTask, clients: dict[str, ClientConfig]
) -> bool:
    """True iff an IMPL completion would advance past an unconcluded merge.

    Callers MUST scope this to ``task.stage == Stage.IMPL``. See the module
    docstring for the two branches and the fail-closed polarity. The worktree
    resolves through :func:`cw.worktree.resolve_task_worktree`, as
    ``review_gates`` does, because dispatch never stamps
    ``task.worktree_path`` on a dispatch-driven row.
    """
    if task.finalize_regress_branch_head is None:
        return False
    worktree_path = resolve_task_worktree(task, clients.get(task.client))
    if worktree_path is None:
        return True
    if merge_in_progress(worktree_path) is not False:
        return True
    if not task.finalize_regress_merge_conflict_detected:
        return False
    head_sha = current_head_sha(worktree_path)
    if head_sha is None:
        return True
    return head_sha == task.finalize_regress_branch_head


def _park_unconcluded_finalize_regress_merge_gate(task: TicketTask) -> None:
    """Park *task* BLOCKED_ON_USER for an unconcluded FINALIZE-regress merge.

    Field-for-field mirror of ``review_gates._park_empty_diff_gate``, including
    its emit-before-transition ordering. The recovery is to conclude the merge
    (commit and push it) in the ticket's worktree, then ``cw dev-queue
    requeue``.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _UNCONCLUDED_MERGE_REASON,
            "breadcrumbs": "",
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=UNCONCLUDED_FINALIZE_REGRESS_MERGE_GATE_DISPOSITION,
    )
