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

if TYPE_CHECKING:
    from cw.models import TicketTask


def task_attention_state(task: TicketTask) -> str | None:
    """The task's hydrated PR attention_state, or None if not hydrated/clean.

    ``pr_state`` is populated only by the async ``cw.pr_hydrate`` pass, so this
    reflects *last-hydrated* PR state: a freshly-added task, or one behind a
    lagging hydration pass, reads as None even if it would need attention once
    hydrated.
    """
    return task.pr_state.attention_state if task.pr_state is not None else None
