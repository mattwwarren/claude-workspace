"""``cw dev-queue hold``: the locked core of the dependency hold (GitHub #2321).

Pure and locked: no gh call and no git call, and no subprocess, under the queue
lock (ADR-0019). The CLI verb (``cw.cli.dev_queue.hold``) verifies the PR with
gh before calling :func:`hold_ticket`, and registers the ``WatchedPr`` only
after :func:`hold_ticket` has returned, so the registration takes the queue
lock itself and never nests.

A hold parks a PENDING or BLOCKED_ON_USER row as ``dependency_hold`` with
``blocked_on_pr`` set. The serve tick releases it when that PR merges
(``cw.reconcile.tasks.stale_gate``). The verb refuses a row that already has a
PR, or whose bound session is still live, so it never orphans a ceiling session.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import DEPENDENCY_UNMERGED_BLOCKER_REASON
from cw.config import load_state, sessions_lock
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.lifecycle import transition_task_status
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.gh import parse_pr_url
from cw.models import OrchestratorEventType, QueueItemStatus, WatchedPr
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION, TERMINAL_SESSION_STATUSES

if TYPE_CHECKING:
    from cw.models import CwState, DevQueueStore, TicketTask

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HoldResult:
    """Outcome of one :func:`hold_ticket` call, echoed by the CLI verb.

    ``previous_disposition`` and ``previous_blocked_reason`` are set only when
    the row was already BLOCKED_ON_USER and this hold overwrote them. No
    ``task.transition`` event carries them (a same-status move emits none);
    the ``ticket.held`` event :func:`record_hold_event` records does, as do the
    echoed line and the INFO log.
    """

    ticket_id: str
    client: str
    pr_number: int
    outcome: Literal["held", "unchanged", "updated"]
    previous_pr: int | None
    previous_disposition: str | None
    previous_blocked_reason: str | None
    converted_from_worker_park: bool
    cleared_session_id: str | None


def _pr_label(pr_url: str) -> str:
    parsed = parse_pr_url(pr_url)
    return f"#{parsed[1]}" if parsed is not None else pr_url


def check_holdable(task: TicketTask, state: CwState) -> None:
    """Raise :class:`CwError` unless *task* may take a dependency hold (#2321).

    Checked in order: status, then a row that already has a PR (a hold applies
    before a PR exists), then a live bound session (close it first, so the hold
    never leaves an unowned ceiling session).
    """
    status = task.status
    ticket = f"{task.ticket_id} in client {task.client}"
    if status == QueueItemStatus.RUNNING:
        msg = f"ticket {ticket} is RUNNING; cancel it or wait for it to finish first"
        raise CwError(msg)
    if status == QueueItemStatus.CANCELLED:
        msg = f"ticket {ticket} is CANCELLED; requeue it with --from-cancelled first"
        raise CwError(msg)
    if status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF:
        msg = f"ticket {ticket} awaits operator sign-off; approve or requeue it first"
        raise CwError(msg)
    if status not in (QueueItemStatus.PENDING, QueueItemStatus.BLOCKED_ON_USER):
        msg = (
            f"ticket {ticket} is {status.value};"
            " a hold applies to a PENDING or BLOCKED_ON_USER row"
        )
        raise CwError(msg)
    existing_pr_url = task.pr_url if task.pr_url is not None else task.adopted_pr_url
    if existing_pr_url is not None:
        msg = (
            f"row already has PR {_pr_label(existing_pr_url)};"
            " a hold applies before a PR exists"
        )
        raise CwError(msg)
    if task.session_id is not None:
        session = state.find_by_name_or_id(task.session_id)
        if session is not None and session.status not in TERMINAL_SESSION_STATUSES:
            msg = (
                f"ticket {ticket} is bound to live session {task.session_id}"
                f" ({session.status}); close the session first"
                f" (`cw spawn close {task.session_id}`) so the hold does not leave"
                " an unowned ceiling session"
            )
            raise CwError(msg)


def existing_hold_pr(task: TicketTask) -> int | None:
    """The PR a row already holds on, or None when it is not a dependency hold."""
    if (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.disposition == DEPENDENCY_HOLD_DISPOSITION
    ):
        return task.blocked_on_pr
    return None


def is_operator_hold_on(task: TicketTask, pr_number: int) -> bool:
    """True iff *task* is already an operator hold (no session) on *pr_number*."""
    return existing_hold_pr(task) == pr_number and task.session_id is None


def _watch_collision(
    store: DevQueueStore, repo: str, pr_number: int, client: str
) -> WatchedPr | None:
    """Return the active watch on *repo*/*pr_number* owned by another client, if any.

    Emits no event and takes no lock: it reads the store the caller already
    holds. This deliberately duplicates the three-part match in
    ``register_or_adopt_watched_pr`` because this read-only check must run
    inside the hold mutation lock without changing the over-ceiling CRUD module.
    """
    for existing in store.watched_prs:
        if not (
            existing.repo == repo
            and existing.pr_number == pr_number
            and existing.status == "active"
        ):
            continue
        if existing.client is not None and existing.client != client:
            return existing
    return None


def _update_existing_hold(
    store: DevQueueStore, task: TicketTask, pr_number: int, current: int
) -> HoldResult:
    """Move or convert a dependency hold that already exists (caller holds the lock)."""
    converted = task.session_id is not None
    cleared = task.session_id
    task.session_id = None
    task.blocked_on_pr = pr_number
    task.stale_gate_detected_at = None
    save_dev_queue(store)
    _log.info(
        "dependency hold updated ticket=%s client=%s pr=%s previous_pr=%s converted=%s",
        task.ticket_id,
        task.client,
        pr_number,
        current,
        converted,
    )
    return HoldResult(
        ticket_id=task.ticket_id,
        client=task.client,
        pr_number=pr_number,
        outcome="updated",
        previous_pr=current,
        previous_disposition=None,
        previous_blocked_reason=None,
        converted_from_worker_park=converted,
        cleared_session_id=cleared,
    )


def _hold_locked(
    ticket_id: str, client: str, pr_number: int, *, repo: str, bounded: bool
) -> HoldResult:
    """The check-then-mutate window of :func:`hold_ticket`, under the queue lock."""
    # Why: sessions_lock is outermost by ADR-0019, so a session writer cannot
    # become live between this validation and clearing the queue binding.
    with sessions_lock(bounded=bounded), _lock(bounded=bounded):
        state = load_state()
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client, warn=False)
        check_holdable(task, state)
        if is_operator_hold_on(task, pr_number):
            return HoldResult(
                ticket_id=task.ticket_id,
                client=task.client,
                pr_number=pr_number,
                outcome="unchanged",
                previous_pr=pr_number,
                previous_disposition=None,
                previous_blocked_reason=None,
                converted_from_worker_park=False,
                cleared_session_id=None,
            )
        clash = _watch_collision(store, repo, pr_number, task.client)
        if clash is not None:
            msg = (
                f"PR #{pr_number} in {repo} is already watched by client"
                f" {clash.client}; refusing to hold {task.ticket_id}"
            )
            raise CwError(msg)
        current = existing_hold_pr(task)
        if current is not None:
            return _update_existing_hold(store, task, pr_number, current)
        previous_disposition = None
        previous_reason = None
        if task.status == QueueItemStatus.BLOCKED_ON_USER:
            previous_disposition = task.disposition
            previous_reason = task.blocked_reason
        cleared = task.session_id
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=DEPENDENCY_HOLD_DISPOSITION,
            blocked_reason=DEPENDENCY_UNMERGED_BLOCKER_REASON,
        )
        task.session_id = None
        task.blocked_on_pr = pr_number  # after the transition, which clears it
        save_dev_queue(store)
        _log.info(
            "dependency hold placed ticket=%s client=%s pr=%s replaced_disposition=%s"
            " replaced_blocked_reason=%s",
            task.ticket_id,
            task.client,
            pr_number,
            previous_disposition,
            previous_reason,
        )
        return HoldResult(
            ticket_id=task.ticket_id,
            client=task.client,
            pr_number=pr_number,
            outcome="held",
            previous_pr=None,
            previous_disposition=previous_disposition,
            previous_blocked_reason=previous_reason,
            converted_from_worker_park=False,
            cleared_session_id=cleared,
        )


def hold_ticket(
    ticket_id: str,
    client: str,
    pr_number: int,
    *,
    repo: str,
    bounded: bool = False,
) -> HoldResult:
    """Park a row as a dependency hold on *pr_number* (#2321).

    The row is saved before this returns. The caller registers the
    ``WatchedPr`` afterwards (outside this lock) unless the outcome is
    ``"unchanged"``. The CLI records the hold audit before registering the
    watch, so a post-mutation registration failure cannot hide the hold.
    Raises :class:`CwError` on any refusal, before mutating.
    """
    return _hold_locked(ticket_id, client, pr_number, repo=repo, bounded=bounded)


def record_hold_event(result: HoldResult, repo: str) -> None:
    """Record the ``ticket.held`` audit event for one mutating hold (#2321).

    The durable audit trail for a same-status overwrite, which emits no
    ``task.transition``. Written after the queue write and before watch
    registration (state first, then event); no handler wraps it, so a failed
    write propagates. An ``unchanged`` repeat records nothing.
    """
    if result.outcome == "unchanged":
        return
    record_event(
        OrchestratorEventType.TICKET_HELD,
        {**asdict(result), "repo": repo},
        correlation_id=result.ticket_id,
    )
