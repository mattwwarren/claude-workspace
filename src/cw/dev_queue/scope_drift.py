"""Operator grant of a ``plan_scope_drift`` park (GitHub #2337).

``cw dev-queue approve --scope-drift <paths>`` stamps the operator-approved
extra files and the branch's current origin HEAD onto a row parked
BLOCKED_ON_USER at IMPL with ``blocked_reason`` ``plan_scope_drift``, then
re-queues IMPL in place. A separate entry point from ``approve_ticket``
because that park carries no approval-gate status or disposition.

Split out of ``approval.py`` beside ``must_fix_override`` to keep that module
under the size ceiling (#2719). Its one dependency back into ``approval`` is the
physical-row resolver ``_resolve_approval_target``; ``approval`` never imports
this module. Its critical logs (recovery-marker write failure, audit
compensation failure, rollback failure) use this module's own logger,
``cw.dev_queue.scope_drift``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, TypedDict

from cw.atomic import atomic_write_text
from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
from cw.config import dev_queue_file, get_client
from cw.dev_queue.approval import _resolve_approval_target
from cw.dev_queue.lifecycle import _reset_for_same_stage_requeue
from cw.dev_queue.next_commands import SCOPE_DRIFT_PATHS_PLACEHOLDER, dev_queue_command
from cw.dev_queue.refusal import refusal_hint
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.gh import branch_head_sha_on_origin
from cw.models import (
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.models import TicketTask


_log = logging.getLogger(__name__)
_SCOPE_DRIFT_RECOVERY_MARKER = "scope-drift-approval-recovery.jsonl"


class ScopeDriftApproval(TypedDict):
    """What :func:`approve_scope_drift_ticket` stamped and where it moved."""

    from_stage: str
    to_stage: str
    ticket_id: str
    client: str
    extra_files: list[str]
    approved_head: str


def _write_scope_drift_recovery_marker(payload: dict[str, object]) -> None:
    """Persist an operator-visible marker when approval audit repair is uncertain."""
    path = dev_queue_file().with_name(_SCOPE_DRIFT_RECOVERY_MARKER)
    try:
        previous = path.read_text() if path.exists() else ""
        atomic_write_text(path, previous + json.dumps(payload, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        # The marker is the last-resort recovery path. If even it is unavailable,
        # the critical log is the remaining operator alert; never hide the
        # original queue/audit inconsistency behind a marker-write exception.
        _log.critical(
            "scope-drift approval recovery marker could not be persisted: %s",
            payload,
            exc_info=True,
        )


def _emit_scope_drift_compensation(
    ticket_id: str,
    client_name: str,
    approval_payload: dict[str, object],
    error: Exception,
    *,
    rolled_back: bool,
) -> None:
    """Correct a possibly-written approval event, failing loudly if repair fails."""
    payload = {
        "ticket_id": ticket_id,
        "client": client_name,
        "approval_event": OrchestratorEventType.TICKET_APPROVED.value,
        "approval_payload": approval_payload,
        "error": str(error),
        "rolled_back": rolled_back,
        "recovery_required": not rolled_back,
    }
    try:
        record_event(
            OrchestratorEventType.TICKET_APPROVAL_FAILED,
            payload,
            correlation_id=ticket_id,
        )
    except Exception:  # noqa: BLE001
        # A failed compensating write must not silently leave a potentially
        # false TICKET_APPROVED event standing alone.
        _write_scope_drift_recovery_marker(payload)
        _log.critical(
            "scope-drift approval audit compensation failed for %s/%s",
            client_name,
            ticket_id,
            exc_info=True,
        )


def approve_scope_drift_ticket(
    ticket_id: str,
    client_name: str,
    extra_files: list[str],
    *,
    bounded: bool = False,
) -> ScopeDriftApproval:
    """Grant operator-directed scope growth to a ``plan_scope_drift`` park (#2337).

    The row must be BLOCKED_ON_USER at Stage.IMPL with ``blocked_reason``
    ``plan_scope_drift`` -- a park routed by the generic Rule 5 fallthrough,
    which stamps no ``disposition``, so :func:`approve_ticket`'s gate
    predicate can never match it. Stamps the sorted, deduped *extra_files* and
    the branch's current origin HEAD SHA (the binding gate 2 checks by
    ancestry), then re-queues IMPL in place so a fresh session re-runs gate 2
    with the grant in its ``queue_metadata``.

    Returns a :class:`ScopeDriftApproval` (extra_files as stamped).

    Raises:
        ApproveGateError: if the row is not parked for plan_scope_drift at
            IMPL, *extra_files* is empty, or the branch head cannot be
            resolved on origin. Nothing is mutated on any of these paths.
        CwError: if no matching task is found.
    """
    with _lock(bounded=bounded):
        return _approve_scope_drift_locked(
            ticket_id,
            client_name,
            extra_files,
        )


def _scope_drift_command(task: TicketTask, paths: str) -> str:
    """``cw dev-queue approve <T> -c <C> --scope-drift <paths>`` for a retry."""
    return dev_queue_command("approve", task, "--scope-drift", paths)


def _approve_scope_drift_locked(
    ticket_id: str,
    client_name: str,
    extra_files: list[str],
) -> ScopeDriftApproval:
    """Lock-free body of :func:`approve_scope_drift_ticket`.

    The caller MUST already hold ``dev_queue_lock()`` (``_lock``), as for
    :func:`_approve_ticket_locked`. Every validation, including the ``gh``
    head lookup, runs before the first mutation, so a refusal leaves the row
    exactly as it was.
    """
    store = load_dev_queue()
    task = _resolve_approval_target(store, ticket_id, client_name, None)

    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}': status is"
            f" {task.status.value!r}, expected BLOCKED_ON_USER."
            f"{refusal_hint(task)}"
        )
        raise ApproveGateError(msg)
    if task.stage != Stage.IMPL:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}': stage is"
            f" {task.stage.value!r}, expected 'impl' (plan_scope_drift parks"
            " at Step 2.5 of the IMPL stage)."
            f"{refusal_hint(task)}"
        )
        raise ApproveGateError(msg)
    if task.blocked_reason != PLAN_SCOPE_DRIFT_BLOCKER_REASON:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}':"
            f" blocked_reason is {task.blocked_reason!r}, expected"
            f" {PLAN_SCOPE_DRIFT_BLOCKER_REASON!r}.{refusal_hint(task)}"
        )
        raise ApproveGateError(msg)
    approved_files = sorted(set(extra_files))
    if not approved_files:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}': no extra"
            " files given. Pass the repo-relative paths to allow, comma-separated."
            f" Retry: `{_scope_drift_command(task, SCOPE_DRIFT_PATHS_PLACEHOLDER)}`."
        )
        raise ApproveGateError(msg)

    client_cfg = get_client(client_name)
    branch = f"{client_cfg.feature_branch_prefix}/{ticket_id}"
    head_sha, _gh_available = branch_head_sha_on_origin(
        branch, cwd=_git_dir(client_cfg)
    )
    if head_sha is None:
        msg = (
            f"Cannot approve scope drift for ticket '{ticket_id}': could not"
            f" resolve the head of origin/{branch} (gh unavailable, branch not"
            " pushed, or a transient error) -- the approval has nothing to be"
            f" bound to. Retry once origin/{branch} resolves:"
            f" `{_scope_drift_command(task, ','.join(approved_files))}`."
        )
        raise ApproveGateError(msg)

    original_store = store.model_copy(deep=True)
    from_stage = task.stage.value
    task.scope_drift_approved_extra_files = approved_files
    task.scope_drift_approved_head = head_sha
    _reset_for_same_stage_requeue(task)
    approval_payload: dict[str, object] = {
        "ticket_id": ticket_id,
        "client": client_name,
        "from_stage": from_stage,
        "to_stage": task.stage.value,
        SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY: approved_files,
        SCOPE_DRIFT_APPROVED_HEAD_KEY: head_sha,
    }
    save_dev_queue(store)
    try:
        record_event(
            OrchestratorEventType.TICKET_APPROVED,
            approval_payload,
        )
    except Exception as event_error:
        try:
            save_dev_queue(original_store)
        except Exception as rollback_error:
            recovery_payload = {
                "ticket_id": ticket_id,
                "client": client_name,
                "approval_payload": approval_payload,
                "event_error": str(event_error),
                "rollback_error": str(rollback_error),
                "recovery_required": True,
            }
            _write_scope_drift_recovery_marker(recovery_payload)
            _emit_scope_drift_compensation(
                ticket_id,
                client_name,
                approval_payload,
                rollback_error,
                rolled_back=False,
            )
            _log.critical(
                "scope-drift approval rollback failed for %s/%s; operator recovery"
                " is required",
                client_name,
                ticket_id,
                exc_info=True,
            )
            raise rollback_error from event_error
        _emit_scope_drift_compensation(
            ticket_id,
            client_name,
            approval_payload,
            event_error,
            rolled_back=True,
        )
        raise
    return {
        "from_stage": from_stage,
        "to_stage": task.stage.value,
        "ticket_id": ticket_id,
        "client": client_name,
        "extra_files": approved_files,
        "approved_head": head_sha,
    }
