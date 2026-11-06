"""The one-shot attention clock for a recovered red PR's auto-merge re-arm.

Recovery's row marker is cleared only after finalize reports a verified arm or
PR hydration confirms MERGED/CLOSED. A continuously BLOCKED_ON_USER row gets
one attention event after 30 minutes; ``cw watchdog tick`` runs every 15
minutes, so normal alert latency is 30-45 minutes after the park began.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus, TicketTask

AUTO_MERGE_REARM_OWED_PARK_MINUTES = 30
_PARK_THRESHOLD = timedelta(minutes=AUTO_MERGE_REARM_OWED_PARK_MINUTES)
_PAUSED_STATUS = "auto_merge_rearm_owed"


def clear_auto_merge_rearm_owed(task: TicketTask) -> None:
    """Clear the owed marker and its one-shot attention latch."""
    task.auto_merge_rearm_owed_at = None
    task.auto_merge_rearm_attention_at = None


def clear_after_verified_finalize_rearm(
    task: TicketTask, *, was_finalizing: bool, auto_merge_verified: bool
) -> bool:
    """Clear only when the accepted FINALIZE result read back auto-merge on."""
    if (
        was_finalizing
        and auto_merge_verified
        and task.auto_merge_rearm_owed_at is not None
    ):
        clear_auto_merge_rearm_owed(task)
        return True
    return False


def mark_auto_merge_rearm_owed(
    task: TicketTask, *, expected_pr_url: str, now: datetime | None = None
) -> bool:
    """Mark *task* only while it still references the disarmed PR.

    The recovery verb can lose its locked row revalidation after GitHub has
    already disarmed auto-merge. In that refusal path, the marker still needs
    to land on the same row/PR so cancellation or an existing park cannot
    strand the disarmed PR silently. Never attach it after the row's PR changed.
    """
    if task.pr_url != expected_pr_url:
        return False
    if task.auto_merge_rearm_owed_at is None:
        at = now if now is not None else datetime.now(UTC)
        task.auto_merge_rearm_owed_at = at
        task.auto_merge_rearm_attention_at = None
    emit_cancelled_rearm_owed_attention(task, now=now)
    return True


def emit_cancelled_rearm_owed_attention(
    task: TicketTask, *, now: datetime | None = None
) -> bool:
    """Emit the one cancellation page when an owed row is already cancelled."""
    if (
        task.status is not QueueItemStatus.CANCELLED
        or task.auto_merge_rearm_owed_at is None
        or task.auto_merge_rearm_attention_at is not None
    ):
        return False
    at = now if now is not None else datetime.now(UTC)
    task.auto_merge_rearm_attention_at = at
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        rearm_owed_attention_payload(
            task,
            reason="cancelled",
            breadcrumbs=(
                f"row {task.ticket_id} was cancelled while its recovered PR "
                f"{task.pr_url} still owes an auto-merge re-arm; inspect the "
                "PR or requeue through cw."
            ),
        ),
        correlation_id=task.ticket_id,
    )
    return True


def rearm_owed_attention_payload(
    task: TicketTask, *, reason: str, breadcrumbs: str
) -> dict[str, object]:
    """Build the canonical attention payload for a re-arm-owed row."""
    return {
        "session_id": task.session_id or "",
        "session_name": "",
        "client": task.client,
        "ticket_id": task.ticket_id,
        "claude_session_id": None,
        "paused_status": _PAUSED_STATUS,
        "breadcrumbs": breadcrumbs,
        "crashed": False,
        "lane": task.lane,
        "reason": reason,
        "pr_url": task.pr_url,
        "rearm_owed_at": (
            task.auto_merge_rearm_owed_at.isoformat()
            if task.auto_merge_rearm_owed_at is not None
            else None
        ),
    }


def run_rearm_owed_attention_sweep(*, now: datetime | None = None) -> list[str]:
    """Page once for each owed row parked continuously for at least 30m.

    ``completed_at`` is the current BLOCKED_ON_USER episode's start (status
    transitions stamp it). Missing timestamps fail closed. The attention
    timestamp is persisted under the queue lock before releasing the row, so
    repeated watchdog ticks do not re-emit the same page.
    """
    at = now if now is not None else datetime.now(UTC)
    pending: list[tuple[str, str, dict[str, object]]] = []
    with _lock():
        store = load_dev_queue()
        for task in store.tasks:
            parked_at = task.completed_at
            if (
                task.status is not QueueItemStatus.BLOCKED_ON_USER
                or task.auto_merge_rearm_owed_at is None
                or task.auto_merge_rearm_attention_at is not None
                or parked_at is None
                or at - parked_at < _PARK_THRESHOLD
            ):
                continue
            task.auto_merge_rearm_attention_at = at
            breadcrumbs = (
                f"red-shipped recovery disarmed auto-merge on {task.pr_url}; row "
                "has remained parked for at least "
                f"{AUTO_MERGE_REARM_OWED_PARK_MINUTES} "
                "minutes. Requeue/review through cw so finalize can re-arm it."
            )
            pending.append(
                (
                    task.ticket_id,
                    task.client,
                    rearm_owed_attention_payload(
                        task, reason="parked_past_threshold", breadcrumbs=breadcrumbs
                    ),
                )
            )
        if pending:
            save_dev_queue(store)

    for ticket_id, _client, payload in pending:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload,
            correlation_id=ticket_id,
        )
    return [ticket_id for ticket_id, _client, _payload in pending]
