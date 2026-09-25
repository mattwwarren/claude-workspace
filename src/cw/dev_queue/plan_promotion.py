"""Promote an approved ``.cw/plan-draft.md`` to ``.cw/plan.md`` (#2342).

After a regress into PLAN, the plan stage writes its reconciled plan to
``.cw/plan-draft.md`` and parks at ``plan_pending_approval`` next to the
stale-but-reviewed ``.cw/plan.md`` from the earlier run. ``cw dev-queue
approve``'s direct plan->impl advance calls :func:`promote_plan_draft` so the
IMPL stage's drift gate reads the plan the operator actually approved, rather
than the pre-reconciliation one. Promotion runs before approval is recorded;
on an I/O failure the raised error's message is the operator's recovery
channel. Worktree resolution reuses :func:`cw.worktree.resolve_task_worktree`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.models import OrchestratorEventType
from cw.plan_fingerprint import (
    compute_plan_draft_fingerprint,
    is_plan_draft_fingerprint,
)
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask


def _restore_prior_plan(plan_path: Path, old_plan_text: str | None) -> Exception | None:
    """Make one attempt to put ``.cw/plan.md`` back; return the failure, if any."""
    try:
        if old_plan_text is None:
            plan_path.unlink(missing_ok=True)
        else:
            atomic_write_text(plan_path, old_plan_text)
    except Exception as exc:  # noqa: BLE001
        # Why: fail-open cleanup. The approval abort must still be raised, and
        # its message reports this outcome as the operator's recovery channel.
        return exc
    return None


def _promotion_write_error(
    task: TicketTask,
    wt_path: Path,
    draft_path: Path,
    plan_path: Path,
    old_plan_text: str | None,
    exc: Exception,
) -> ApproveGateError:
    """Restore the prior plan once and build the abort error describing it."""
    restore_error = _restore_prior_plan(plan_path, old_plan_text)
    head = (
        f"Cannot approve ticket {task.ticket_id!r}: promoting the approved plan"
        f" draft {draft_path} to {plan_path} failed for worktree {wt_path}"
        f" ({exc.__class__.__name__}: {exc}). Nothing was recorded."
    )
    if restore_error is None:
        prior = "its prior content" if old_plan_text is not None else "absent"
        msg = (
            f"{head} {plan_path} was restored ({prior}); the draft is intact at"
            f" {draft_path}. Fix the I/O error and re-run approve."
        )
        return ApproveGateError(msg)
    msg = (
        f"{head} RESTORE FAILED: {plan_path} could not be restored"
        f" ({restore_error.__class__.__name__}: {restore_error}) and may hold"
        f" partial content. Manual recovery: the approved draft is still intact"
        f" at {draft_path}; fix the I/O error, then re-run `cw dev-queue approve"
        f" {task.ticket_id} --client {task.client}`, which promotes that draft"
        f" over {plan_path} again."
    )
    return ApproveGateError(msg)


def promote_plan_draft(
    task: TicketTask,
    client_cfg: ClientConfig | None,
    *,
    actor: str = "cw dev-queue approve",
    expected_fingerprint: str | None = None,
) -> bool:
    """Promote the task's approved ``.cw/plan-draft.md`` to ``.cw/plan.md``.

    Returns True iff a draft was promoted; no worktree or no draft returns
    False. Reading and writing are fail-loud, since a silently failed
    promotion would ship IMPL against the stale plan. A failed write gets one
    best-effort restore of the prior ``.cw/plan.md``, and the raised error
    reports whether it succeeded. Clearing the draft and emitting
    ``PLAN_DRAFT_PROMOTED`` follow a successful write and are best-effort, as
    in ``auto-dev-plan.md`` Step 1g. When a draft exists,
    ``expected_fingerprint`` must be a valid approval-session fingerprint and
    must match the draft; an absent or malformed fingerprint fails closed.

    Raises:
        ApproveGateError: reading the draft or writing ``.cw/plan.md`` failed.
    """
    wt_path = resolve_task_worktree(task, client_cfg)
    if wt_path is None:
        return False
    draft_path = wt_path / ".cw" / "plan-draft.md"
    plan_path = wt_path / ".cw" / "plan.md"
    try:
        if not draft_path.exists():
            return False
        if not isinstance(expected_fingerprint, str) or not is_plan_draft_fingerprint(
            expected_fingerprint
        ):
            msg = (
                f"Cannot approve ticket {task.ticket_id!r}: approved plan draft"
                f" at {draft_path} has no valid approval fingerprint for"
                f" worktree {wt_path}. Nothing was written or recorded."
            )
            raise ApproveGateError(msg)
        draft_text = draft_path.read_text(encoding="utf-8")
        new_fingerprint = compute_plan_draft_fingerprint(draft_text)
        if new_fingerprint != expected_fingerprint:
            msg = (
                f"Cannot approve ticket {task.ticket_id!r}: approved plan draft"
                f" fingerprint mismatch for worktree {wt_path}"
                f" (expected {expected_fingerprint}, got {new_fingerprint})."
                " Nothing was written or recorded."
            )
            raise ApproveGateError(msg)
        old_plan_text = (
            plan_path.read_text(encoding="utf-8") if plan_path.exists() else None
        )
    except (OSError, UnicodeDecodeError) as exc:
        msg = (
            f"Cannot approve ticket {task.ticket_id!r}: reading {draft_path} or"
            f" {plan_path} failed for worktree {wt_path}"
            f" ({exc.__class__.__name__}: {exc}). Nothing was written or recorded."
        )
        raise ApproveGateError(msg) from exc
    try:
        atomic_write_text(plan_path, draft_text)
    except OSError as exc:
        raise _promotion_write_error(
            task, wt_path, draft_path, plan_path, old_plan_text, exc
        ) from exc
    draft_deleted = False
    with contextlib.suppress(OSError):
        draft_path.unlink()
        draft_deleted = True
    old_fingerprint = (
        compute_plan_draft_fingerprint(old_plan_text)
        if old_plan_text is not None
        else None
    )
    with contextlib.suppress(OSError):
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
