"""Operator override of a codex MUST_FIX park (GitHub #2205).

``cw dev-queue approve --override-must-fix --reason ...`` records a durable,
audited decision to ship a branch past a codex background review's BLOCKING
verdict. The record lives on :attr:`TicketTask.must_fix_override`, a field the
PENDING reset of ``requeue --stage finalize`` does not clear (unlike
``blocked_reason``), and is bound to the verdict it was given for: its
``reviewed_sha`` and the :func:`cw.review_debt.fingerprint_v1` identity of every
MUST_FIX finding on it. FINALIZE's ``check_must_fix_override.py`` honors it only
while the live verdict and HEAD still match.

Stamp-only: the row keeps its status and stage, and the operator still runs
``cw dev-queue requeue --stage finalize``. Split out of ``approval.py`` beside
``plan_promotion``/``requeue`` to keep that module under the size ceiling.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from pydantic import ValidationError

from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import get_client
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import ApproveGateError
from cw.models import MustFixOverride, OrchestratorEventType, QueueItemStatus
from cw.operator_identity import resolve_operator_login
from cw.review_debt import fingerprint_v1
from cw.review_findings import REVIEW_VERDICT_JSON_RELATIVE_PATH, ReviewVerdictEnvelope
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask
    from cw.review_findings import ReviewVerdict


_log = logging.getLogger(__name__)


class MustFixOverrideApproval(TypedDict):
    """What :func:`approve_must_fix_override_ticket` stamped."""

    ticket_id: str
    client: str
    stage: str
    actor: str
    reviewed_sha: str
    finding_ids: list[tuple[str, str]]


def approve_must_fix_override_ticket(
    ticket_id: str,
    client_name: str,
    reason: str,
) -> MustFixOverrideApproval:
    """Record an operator override of a ``codex_must_fix_findings`` park.

    Raises:
        ApproveGateError: if *reason* is blank, the row is not parked
            BLOCKED_ON_USER for ``codex_must_fix_findings``, its worktree or
            ``.claude/review-verdict.json`` cannot be resolved, the verdict
            belongs to another ticket or is not blocking, or a MUST_FIX finding
            has no fingerprint. Nothing is recorded on any of these paths.
        CwError: if no matching task is found.
    """
    with _lock():
        return _approve_must_fix_override_locked(ticket_id, client_name, reason)


def _approve_must_fix_override_locked(
    ticket_id: str,
    client_name: str,
    reason: str,
) -> MustFixOverrideApproval:
    """Lock-free body of :func:`approve_must_fix_override_ticket`.

    The caller MUST already hold ``dev_queue_lock()`` (``_lock``). Every
    validation runs before the first write, so a refusal leaves no trace.

    Event-first ordering (the ``revoke_plan_approval`` precedent, #2394): the
    audit event is recorded before the row is saved, so a failed audit write
    leaves no override. A save that fails after the event leaves the row
    without an override too, which FINALIZE treats as "still blocked" -- the
    event then truthfully records an attempt, and an ERROR line names it.
    """
    stripped_reason = reason.strip()
    if not stripped_reason:
        msg = (
            f"Cannot override MUST_FIX for ticket '{ticket_id}': --reason is"
            " blank. State why shipping past the findings is acceptable."
        )
        raise ApproveGateError(msg)

    store = load_dev_queue()
    task = _find_ticket(store, ticket_id, client_name)
    _require_codex_must_fix_park(task)
    verdict = _load_owned_verdict(task, client_name)
    finding_ids = _finding_ids(ticket_id, verdict)

    client_cfg = get_client(client_name)
    actor = resolve_operator_login(client_cfg)
    if actor is None:
        _log.warning(
            "MUST_FIX override for %s/%s: operator login unresolved; recording"
            " an empty actor",
            client_name,
            ticket_id,
        )
        actor = ""

    stage = task.stage.value
    record_event(
        OrchestratorEventType.TICKET_APPROVED,
        {
            "ticket_id": ticket_id,
            "client": client_name,
            "from_stage": stage,
            "to_stage": stage,
            "actor": actor,
            "reason": stripped_reason,
            "reviewed_sha": verdict.reviewed_sha,
            "finding_ids": [list(pair) for pair in finding_ids],
        },
        correlation_id=ticket_id,
    )
    task.must_fix_override = MustFixOverride(
        actor=actor,
        reason=stripped_reason,
        reviewed_sha=verdict.reviewed_sha,
        finding_ids=finding_ids,
        recorded_at=datetime.now(UTC),
    )
    try:
        save_dev_queue(store)
    except Exception:
        _log.error(
            "MUST_FIX override save failed for %s/%s after TICKET_APPROVED was"
            " recorded; the row carries no override, so FINALIZE still blocks",
            client_name,
            ticket_id,
            exc_info=True,
        )
        raise
    return {
        "ticket_id": ticket_id,
        "client": client_name,
        "stage": stage,
        "actor": actor,
        "reviewed_sha": verdict.reviewed_sha,
        "finding_ids": finding_ids,
    }


def _require_codex_must_fix_park(task: TicketTask) -> None:
    """Raise unless *task* is parked BLOCKED_ON_USER for codex MUST_FIX."""
    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': status is"
            f" {task.status.value!r}, expected BLOCKED_ON_USER."
        )
        raise ApproveGateError(msg)
    if task.blocked_reason != CODEX_MUST_FIX_FINDINGS:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}':"
            f" blocked_reason is {task.blocked_reason!r}, expected"
            f" {CODEX_MUST_FIX_FINDINGS!r}. Use plain 'approve' or 'requeue'"
            " for other parks."
        )
        raise ApproveGateError(msg)


def _verdict_path(task: TicketTask, client_name: str) -> Path:
    """Path of *task*'s worktree ``.claude/review-verdict.json``."""
    worktree = resolve_task_worktree(task, get_client(client_name))
    if worktree is None:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': its"
            " worktree could not be resolved, so there is no review verdict to"
            " bind the override to."
        )
        raise ApproveGateError(msg)
    return worktree / REVIEW_VERDICT_JSON_RELATIVE_PATH


def _load_owned_verdict(task: TicketTask, client_name: str) -> ReviewVerdict:
    """Read the worktree's verdict; require it to be this ticket's and blocking."""
    path = _verdict_path(task, client_name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': could not"
            f" read {path} ({exc.__class__.__name__}: {exc})."
        )
        raise ApproveGateError(msg) from exc
    try:
        envelope = ReviewVerdictEnvelope.model_validate_json(text)
    except ValidationError as exc:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': {path} is"
            f" not a valid review verdict ({exc.error_count()} validation"
            " error(s))."
        )
        raise ApproveGateError(msg) from exc
    if envelope.ticket_id != task.ticket_id:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': {path}"
            f" belongs to ticket {envelope.ticket_id!r}, so it is stale or"
            " foreign."
        )
        raise ApproveGateError(msg)
    if not envelope.verdict.blocking:
        msg = (
            f"Cannot override MUST_FIX for ticket '{task.ticket_id}': the"
            f" verdict in {path} is not blocking -- there is nothing to"
            " override."
        )
        raise ApproveGateError(msg)
    return envelope.verdict


def _finding_ids(ticket_id: str, verdict: ReviewVerdict) -> list[tuple[str, str]]:
    """Sorted, deduped fingerprints of *verdict*'s MUST_FIX findings.

    Fails closed on a finding with no fingerprint (``file == "N/A"``): the
    override would otherwise look complete while leaving it uncovered.
    """
    ids: set[tuple[str, str]] = set()
    for finding in verdict.must_fix:
        fingerprint = fingerprint_v1(finding.file, finding.summary)
        if fingerprint is None:
            msg = (
                f"Cannot override MUST_FIX for ticket '{ticket_id}': finding"
                f" {finding.summary!r} has no diff anchor (file"
                f" {finding.file!r}), so it has no identity to bind the override"
                " to. Resolve it, or re-review once it is anchored."
            )
            raise ApproveGateError(msg)
        ids.add(fingerprint)
    return sorted(ids)
