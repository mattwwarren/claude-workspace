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
import os
import re
from typing import TYPE_CHECKING, NoReturn

from cw.atomic import atomic_write_text
from cw.config import dev_queue_file
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


def _recovery_payload(
    *,
    task: TicketTask,
    wt_path: Path,
    plan_path: Path,
    draft_path: Path,
    recovery_path: Path,
    old_plan_text: str | None,
    draft_text: str,
    audit_error: Exception,
    restore_error: Exception,
    recovery_error: Exception | None = None,
    fallback_path: Path | None = None,
    fallback_error: Exception | None = None,
) -> dict[str, object]:
    """Build the complete state needed to recover an untracked promotion."""
    return {
        "ticket_id": task.ticket_id,
        "worktree_path": str(wt_path),
        "plan_path": str(plan_path),
        "draft_path": str(draft_path),
        "recovery_path": str(recovery_path),
        "fallback_recovery_path": (
            str(fallback_path) if fallback_path is not None else None
        ),
        "old_plan_text": old_plan_text,
        "draft_text": draft_text,
        "promoted_plan_text": draft_text,
        "audit_error": f"{audit_error.__class__.__name__}: {audit_error}",
        "restore_error": f"{restore_error.__class__.__name__}: {restore_error}",
        "recovery_error": (
            f"{recovery_error.__class__.__name__}: {recovery_error}"
            if recovery_error is not None
            else None
        ),
        "fallback_error": (
            f"{fallback_error.__class__.__name__}: {fallback_error}"
            if fallback_error is not None
            else None
        ),
        "recovery_required": True,
    }


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
    recovery_error: Exception | None = None,
    fallback_path: Path | None = None,
    fallback_error: Exception | None = None,
) -> dict[str, object]:
    """Persist the pair's contents when audit rollback cannot be verified."""
    payload = _recovery_payload(
        task=task,
        wt_path=wt_path,
        plan_path=plan_path,
        draft_path=draft_path,
        recovery_path=recovery_path,
        old_plan_text=old_plan_text,
        draft_text=draft_text,
        audit_error=audit_error,
        restore_error=restore_error,
        recovery_error=recovery_error,
        fallback_path=fallback_path,
        fallback_error=fallback_error,
    )
    atomic_write_text(recovery_path, json.dumps(payload, sort_keys=True) + "\n")
    return payload


def _fallback_recovery_path() -> Path:
    """Return the global recovery channel used when the worktree is unsafe."""
    return dev_queue_file().with_name("plan-promotion-recovery.jsonl")


def _write_fallback_recovery_record(
    path: Path, payload: dict[str, object]
) -> None:
    """Append a fsynced recovery record outside the task worktree.

    This deliberately does not reuse ``atomic_write_text``: its failure is
    one of the conditions that sends us here, and the fallback must use an
    independent durable channel.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as recovery_file:
        recovery_file.write(json.dumps(payload, sort_keys=True) + "\n")
        recovery_file.flush()
        os.fsync(recovery_file.fileno())


def _attempt_restore(
    plan_path: Path,
    old_plan_text: str | None,
    draft_path: Path,
    draft_text: str,
    *,
    attempts: int,
) -> Exception | None:
    """Try restoration repeatedly and return the last failure, if any."""
    restore_error: Exception | None = None
    for _ in range(attempts):
        try:
            _restore_after_audit_failure(
                plan_path, old_plan_text, draft_path, draft_text
            )
            if _restoration_verified(
                plan_path, old_plan_text, draft_path, draft_text
            ):
                return None
            restore_error = RuntimeError(
                "restoration verification failed for the plan/draft pair"
            )
        except Exception as restore_exc:  # noqa: BLE001
            # A filesystem layer can fail with more than OSError. Keep the
            # audit failure as the eventual cause, but carry this failure into
            # the durable recovery state.
            restore_error = restore_exc
    return restore_error


def _raise_after_audit_failure(
    *,
    task: TicketTask,
    wt_path: Path,
    plan_path: Path,
    draft_path: Path,
    old_plan_text: str | None,
    draft_text: str,
    audit_error: Exception,
) -> NoReturn:
    """Restore or durably record every artifact after an audit failure."""
    restore_error = _attempt_restore(
        plan_path, old_plan_text, draft_path, draft_text, attempts=2
    )
    if restore_error is None:
        msg = (
            f"Cannot approve ticket {task.ticket_id!r}: plan draft promotion"
            f" audit failed for worktree {wt_path}"
            f" ({audit_error.__class__.__name__}: {audit_error});"
            " the prior plan and draft were restored."
        )
        raise ApproveGateError(msg) from audit_error

    recovery_path = wt_path / ".cw" / "plan-promotion-recovery.json"
    try:
        _write_recovery_record(
            recovery_path,
            task=task,
            wt_path=wt_path,
            plan_path=plan_path,
            draft_path=draft_path,
            old_plan_text=old_plan_text,
            draft_text=draft_text,
            audit_error=audit_error,
            restore_error=restore_error,
        )
    except Exception as recovery_exc:  # noqa: BLE001
        # Retry restoration before using the alternate channel. A transient
        # recovery-write error must not strand the promoted artifact pair.
        restore_after_recovery_error = _attempt_restore(
            plan_path, old_plan_text, draft_path, draft_text, attempts=1
        )
        if restore_after_recovery_error is None:
            msg = (
                f"Cannot approve ticket {task.ticket_id!r}: plan draft"
                f" promotion audit failed for worktree {wt_path}; recovery"
                f" persistence at {recovery_path} failed"
                f" ({recovery_exc.__class__.__name__}: {recovery_exc}),"
                " but the prior plan and draft were restored."
            )
            raise ApproveGateError(msg) from audit_error
        _raise_with_fallback_recovery(
            task=task,
            wt_path=wt_path,
            plan_path=plan_path,
            draft_path=draft_path,
            recovery_path=recovery_path,
            old_plan_text=old_plan_text,
            draft_text=draft_text,
            audit_error=audit_error,
            restore_error=restore_after_recovery_error,
            recovery_error=recovery_exc,
        )

    msg = (
        f"Cannot approve ticket {task.ticket_id!r}: plan draft"
        f" promotion audit failed for worktree {wt_path}, and restoring"
        f" the prior plan failed ({restore_error.__class__.__name__}:"
        f" {restore_error}); recovery was recorded at {recovery_path}."
    )
    raise ApproveGateError(msg) from audit_error


def _raise_with_fallback_recovery(
    *,
    task: TicketTask,
    wt_path: Path,
    plan_path: Path,
    draft_path: Path,
    recovery_path: Path,
    old_plan_text: str | None,
    draft_text: str,
    audit_error: Exception,
    restore_error: Exception,
    recovery_error: Exception,
) -> NoReturn:
    """Persist recovery outside the worktree, or report the full state."""
    fallback_path = _fallback_recovery_path()
    recovery_payload = _recovery_payload(
        task=task,
        wt_path=wt_path,
        plan_path=plan_path,
        draft_path=draft_path,
        recovery_path=recovery_path,
        old_plan_text=old_plan_text,
        draft_text=draft_text,
        audit_error=audit_error,
        restore_error=restore_error,
        recovery_error=recovery_error,
        fallback_path=fallback_path,
    )
    try:
        _write_fallback_recovery_record(fallback_path, recovery_payload)
    except Exception as fallback_error:  # noqa: BLE001
        recovery_payload = _recovery_payload(
            task=task,
            wt_path=wt_path,
            plan_path=plan_path,
            draft_path=draft_path,
            recovery_path=recovery_path,
            old_plan_text=old_plan_text,
            draft_text=draft_text,
            audit_error=audit_error,
            restore_error=restore_error,
            recovery_error=recovery_error,
            fallback_path=fallback_path,
            fallback_error=fallback_error,
        )
        msg = (
            f"Cannot approve ticket {task.ticket_id!r}: plan draft promotion"
            f" audit failed for worktree {wt_path}; restoring the prior plan"
            f" failed, and recovery persistence failed at {recovery_path} and"
            f" {fallback_path}. Complete recovery state:"
            f" {json.dumps(recovery_payload, sort_keys=True)}"
        )
        raise ApproveGateError(msg) from audit_error

    msg = (
        f"Cannot approve ticket {task.ticket_id!r}: plan draft promotion audit"
        f" failed for worktree {wt_path}; primary recovery at {recovery_path}"
        f" failed ({recovery_error.__class__.__name__}: {recovery_error}), so"
        f" recovery was persisted at {fallback_path}."
    )
    raise ApproveGateError(msg) from audit_error


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
    plan and draft are restored before approval fails; if that restoration
    cannot be verified, a recovery record is required and its persistence
    failure is surfaced as part of the approval error. Clearing the draft
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
    except Exception as audit_error:  # noqa: BLE001
        _raise_after_audit_failure(
            task=task,
            wt_path=wt_path,
            plan_path=plan_path,
            draft_path=draft_path,
            old_plan_text=old_plan_text,
            draft_text=draft_text,
            audit_error=audit_error,
        )
    return True
