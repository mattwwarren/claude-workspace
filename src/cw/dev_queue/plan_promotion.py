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
import json
import logging
import re
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.models import OrchestratorEventType
from cw.plan_fingerprint import is_plan_draft_fingerprint
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask


_ROUND_LINE = re.compile(r"<!-- plan-stage-scan-round: [0-9]+ -->\n?")
_LAST_EVALUATED_LINE = re.compile(
    r"<!-- plan-stage-last-evaluated: "
    r"operator_comment=[^|\n]+\|body_sha=[0-9a-f]{64} -->\n?"
)
_SETTLED_LINE = re.compile(
    r"<!-- plan-stage-settled: "
    r"(?:A[0-9]+: (?:ADOPTED|ALT-[a-z])|"
    r"P[0-9]+: (?:CONFIRMED|REFUTED|DEFERRED)) -->\n?"
)

_log = logging.getLogger(__name__)


def _draft_fingerprint(text: str) -> str:
    """Apply the named Plan-draft fingerprint rule (#2102)."""
    # The bookkeeping grammar is a leading block.  In particular, a matching
    # HTML comment in the plan body is content and must remain hash material.
    round_match = _ROUND_LINE.match(text)
    if round_match is None:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    offset = round_match.end()
    last_evaluated_match = _LAST_EVALUATED_LINE.match(text, offset)
    if last_evaluated_match is not None:
        offset = last_evaluated_match.end()
    while settled_match := _SETTLED_LINE.match(text, offset):
        offset = settled_match.end()
    stripped = text[offset:]
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def _restore_after_audit_failure(
    plan_path: Path,
    old_plan_text: str | None,
    draft_path: Path,
    draft_text: str,
) -> None:
    """Restore both plan artifacts after the promotion audit could not persist."""
    if old_plan_text is None:
        with contextlib.suppress(FileNotFoundError):
            plan_path.unlink()
    else:
        atomic_write_text(plan_path, old_plan_text)
    atomic_write_text(draft_path, draft_text)


def _restoration_verified(
    plan_path: Path,
    old_plan_text: str | None,
    draft_path: Path,
    draft_text: str,
) -> bool:
    """Verify that both plan artifacts match their pre-promotion contents."""
    try:
        plan_restored = (
            not plan_path.exists()
            if old_plan_text is None
            else plan_path.read_text(encoding="utf-8") == old_plan_text
        )
        draft_restored = draft_path.read_text(encoding="utf-8") == draft_text
    except (OSError, UnicodeDecodeError):
        return False
    return plan_restored and draft_restored


def _write_recovery_record(
    recovery_path: Path,
    *,
    task: TicketTask,
    wt_path: Path,
    plan_path: Path,
    draft_path: Path,
    old_plan_text: str | None,
    draft_text: str,
    audit_error: Exception,
    restore_error: Exception,
) -> None:
    """Persist the pair's contents when audit rollback cannot be verified."""
    payload = {
        "ticket_id": task.ticket_id,
        "worktree_path": str(wt_path),
        "plan_path": str(plan_path),
        "draft_path": str(draft_path),
        "old_plan_text": old_plan_text,
        "draft_text": draft_text,
        "audit_error": f"{audit_error.__class__.__name__}: {audit_error}",
        "restore_error": f"{restore_error.__class__.__name__}: {restore_error}",
        "recovery_required": True,
    }
    serialized = json.dumps(payload, sort_keys=True) + "\n"
    try:
        atomic_write_text(recovery_path, serialized)
    except Exception:  # noqa: BLE001
        # Keep the original approval failure authoritative. The fallback is
        # deliberately non-atomic: it is only used when the atomic writer
        # itself is unavailable, and leaves an operator-visible record if the
        # filesystem still permits one.
        try:
            recovery_path.write_text(serialized, encoding="utf-8")
        except Exception:  # noqa: BLE001
            _log.critical(
                "plan promotion recovery record could not be persisted: %s",
                recovery_path,
                exc_info=True,
            )


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
    complete file, and a missing or malformed approval fingerprint is rejected
    before the write. If the promotion audit cannot be persisted, the prior
    plan and draft are restored before approval fails. Clearing the draft
    afterwards is best-effort, the same
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
    old_plan_text: str | None = None
    try:
        if not draft_path.exists():
            return False
        draft_text = draft_path.read_text(encoding="utf-8")
        new_fingerprint = _draft_fingerprint(draft_text)
        if not isinstance(expected_fingerprint, str) or not is_plan_draft_fingerprint(
            expected_fingerprint
        ):
            msg = (
                f"Cannot approve ticket {task.ticket_id!r}: the plan draft"
                f" has no valid approval fingerprint for worktree {wt_path}."
            )
            raise ApproveGateError(msg)
        if new_fingerprint != expected_fingerprint:
            msg = (
                f"Cannot approve ticket {task.ticket_id!r}: the plan draft"
                f" fingerprint changed for worktree {wt_path}"
                f" (expected {expected_fingerprint}, got {new_fingerprint})."
            )
            raise ApproveGateError(msg)
        if plan_path.exists():
            old_plan_text = plan_path.read_text(encoding="utf-8")
        old_fingerprint = (
            _draft_fingerprint(old_plan_text) if old_plan_text is not None else None
        )
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
    try:
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
    except Exception as audit_error:
        restore_error: Exception | None = None
        try:
            _restore_after_audit_failure(
                plan_path, old_plan_text, draft_path, draft_text
            )
            if not _restoration_verified(
                plan_path, old_plan_text, draft_path, draft_text
            ):
                restore_error = RuntimeError(
                    "restoration verification failed for the plan/draft pair"
                )
        except Exception as restore_exc:  # noqa: BLE001
            restore_error = restore_exc
        if restore_error is not None:
            _write_recovery_record(
                wt_path / ".cw" / "plan-promotion-recovery.json",
                task=task,
                wt_path=wt_path,
                plan_path=plan_path,
                draft_path=draft_path,
                old_plan_text=old_plan_text,
                draft_text=draft_text,
                audit_error=audit_error,
                restore_error=restore_error,
            )
            msg = (
                f"Cannot approve ticket {task.ticket_id!r}: plan draft"
                f" promotion audit failed for worktree {wt_path}, and restoring"
                f" the prior plan failed ({restore_error.__class__.__name__}:"
                f" {restore_error})."
            )
            raise ApproveGateError(msg) from restore_error
        msg = (
            f"Cannot approve ticket {task.ticket_id!r}: plan draft promotion"
            f" audit failed for worktree {wt_path}"
            f" ({audit_error.__class__.__name__}: {audit_error});"
            " the prior plan and draft were restored."
        )
        raise ApproveGateError(msg) from audit_error
    return True
