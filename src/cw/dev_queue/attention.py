"""The shared "does this task need operator attention?" predicate.

Relocated out of ``cw.cli.dev_queue.tasks`` (#1644) so business-logic callers
can reach it: ``cw.statusline`` renders the same ``!N`` count that ``cw
dev-queue status``/``tasks`` surfaces as ``NEEDS_ATTN``, and it must not import
from ``cw.cli.*`` (the CLI depends on business logic, never the reverse).
Duplicating the one-liner would create two independently-driftable definitions
of "needs attention".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import LivenessBucket, SessionStatus

if TYPE_CHECKING:
    from cw.models import Session, TicketTask

# The ATTENTION cell for a row whose session the liveness sweep paged as dead
# (#2153). Exactly the column's 18-char width. Deliberately NOT part of the PR
# vocabulary (``cw.pr_hydrate.PrAttentionState``): it is session-derived, and
# it feeds neither ``NEEDS_ATTN`` nor the statusline counts.
DEAD_SESSION_PAGED_STATE = "dead_session_paged"

_PAGEABLE_SESSION_STATUSES = frozenset({SessionStatus.ACTIVE, SessionStatus.IDLE})


def session_attention_state(task: TicketTask, session: Session | None) -> str | None:
    """``DEAD_SESSION_PAGED_STATE`` iff *task*'s own session holds a live page.

    True only for the session the row is bound to (``session.id ==
    task.session_id``), still ACTIVE/IDLE, latched at ``STALE_45M`` with a
    stamped page evidence key. The key clears only when the bucket leaves
    ``STALE_45M``, so the cell can outlast the page's distress (a sentinel
    landing at the top bucket): display only, nothing acts on it.
    """
    if session is None or session.id != task.session_id:
        return None
    if session.status not in _PAGEABLE_SESSION_STATUSES:
        return None
    if session.liveness_bucket is not LivenessBucket.STALE_45M:
        return None
    if session.liveness_attention_evidence_key is None:
        return None
    return DEAD_SESSION_PAGED_STATE


def task_attention_state(task: TicketTask) -> str | None:
    """The task's hydrated PR attention_state, or None if not hydrated/clean.

    ``pr_state`` is populated only by the async ``cw.pr_hydrate`` pass, so this
    reflects *last-hydrated* PR state: a task whose PR exists (``pr_url`` set)
    but that has not yet been hydrated reads as None even if it would need
    attention once hydrated. ``cw.statusline`` surfaces that unknown state as a
    separate ``?N`` count (#1672); ``NEEDS_ATTN`` deliberately does not.
    """
    return task.pr_state.attention_state if task.pr_state is not None else None
