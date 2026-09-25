"""Promote an approved ``.cw/plan-draft.md`` to ``.cw/plan.md`` (#2342).

After a regress into PLAN, the plan stage writes its reconciled plan to
``.cw/plan-draft.md`` and parks at ``plan_pending_approval`` next to the
stale-but-reviewed ``.cw/plan.md`` from the earlier run. ``cw dev-queue
approve``'s direct plan->impl advance calls :func:`promote_plan_draft` so the
IMPL stage's drift gate reads the plan the operator actually approved, rather
than the pre-reconciliation one.

Worktree resolution reuses :func:`cw.worktree.resolve_task_worktree`, the same
resolver ``lifecycle._local_plan_path`` uses for ``.cw/plan.md``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.exceptions import ApproveGateError
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask


def promote_plan_draft(task: TicketTask, client_cfg: ClientConfig | None) -> bool:
    """Promote the task's approved ``.cw/plan-draft.md`` to ``.cw/plan.md``.

    Returns True iff a draft was promoted. No resolvable worktree, or no
    ``.cw/plan-draft.md`` in it, is not a failure: there is nothing to
    promote, and False is returned.

    Reading the draft and writing ``.cw/plan.md`` are fail-loud: approving a
    plan whose promotion silently failed would ship IMPL against the stale
    plan this function exists to replace. The write goes through
    ``atomic_write_text``, so ``.cw/plan.md`` is either the old or the new
    complete file. Clearing the draft afterwards is best-effort, the same
    split ``auto-dev-plan.md`` Step 1g makes: once ``.cw/plan.md`` exists, a
    leftover draft is ignored by the plan stage's supersession guard.

    Raises:
        ApproveGateError: reading the draft or writing ``.cw/plan.md`` failed;
            the message names the worktree and the underlying exception.
    """
    wt_path = resolve_task_worktree(task, client_cfg)
    if wt_path is None:
        return False
    draft_path = wt_path / ".cw" / "plan-draft.md"
    plan_path = wt_path / ".cw" / "plan.md"
    try:
        if not draft_path.exists():
            return False
        draft_text = draft_path.read_text(encoding="utf-8")
        atomic_write_text(plan_path, draft_text)
    except (OSError, UnicodeDecodeError) as exc:
        msg = (
            f"Cannot approve ticket {task.ticket_id!r}: promoting the"
            f" approved plan draft failed for worktree {wt_path}"
            f" ({exc.__class__.__name__}: {exc})."
        )
        raise ApproveGateError(msg) from exc
    with contextlib.suppress(OSError):
        draft_path.unlink()
    return True
