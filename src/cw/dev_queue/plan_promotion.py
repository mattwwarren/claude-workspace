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
import hashlib
import re
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.models import OrchestratorEventType
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask


_BOOKKEEPING_LINE = re.compile(
    r"^<!-- plan-stage-(?:scan-round|last-evaluated|settled):.*-->\n?$",
    re.MULTILINE,
)


def _draft_fingerprint(text: str) -> str:
    """Apply the named Plan-draft fingerprint rule (#2102)."""
    stripped = _BOOKKEEPING_LINE.sub("", text)
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def promote_plan_draft(
    task: TicketTask,
    client_cfg: ClientConfig | None,
    *,
    expected_fingerprint: str | None = None,
    actor: str = "cw dev-queue approve",
) -> bool:
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
        new_fingerprint = _draft_fingerprint(draft_text)
        if (
            expected_fingerprint is not None
            and new_fingerprint != expected_fingerprint
        ):
            msg = (
                f"Cannot approve ticket {task.ticket_id!r}: the plan draft"
                f" fingerprint changed for worktree {wt_path}"
                f" (expected {expected_fingerprint}, got {new_fingerprint})."
            )
            raise ApproveGateError(msg)
        try:
            old_fingerprint = _draft_fingerprint(
                plan_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError):
            old_fingerprint = None
        atomic_write_text(plan_path, draft_text)
    except (OSError, UnicodeDecodeError) as exc:
        msg = (
            f"Cannot approve ticket {task.ticket_id!r}: promoting the"
            f" approved plan draft failed for worktree {wt_path}"
            f" ({exc.__class__.__name__}: {exc})."
        )
        raise ApproveGateError(msg) from exc
    draft_deleted = False
    with contextlib.suppress(OSError):
        draft_path.unlink()
        draft_deleted = True
    record_event(
        OrchestratorEventType.PLAN_DRAFT_PROMOTED,
        {
            "ticket_id": task.ticket_id,
            "client": task.client,
            "worktree_path": str(wt_path),
            "actor": actor,
            "old_content_fingerprint": old_fingerprint,
            "new_content_fingerprint": new_fingerprint,
            "draft_deleted": draft_deleted,
        },
        correlation_id=task.ticket_id,
    )
    return True
