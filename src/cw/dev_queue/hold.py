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

Since #3634 ``blocked_on_pr`` is a typed :class:`~cw.models.pr_ref.PrRef`: a
same-repo number or an explicit ``owner/repo#N``. :func:`resolve_ticket_pr`
turns a dependency ticket into its recorded PR once, at hold time, and
:func:`restamp_unvetted_hold_ref` is the compare-and-set the stale-dispatch
registrar uses to vet a worker-supplied explicit ref (D7).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import DEPENDENCY_UNMERGED_BLOCKER_REASON
from cw.config import load_state, sessions_lock
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.dispositions import AWAITING_OPERATOR_DISPOSITION
from cw.dev_queue.lifecycle import transition_task_status
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.gh import parse_pr_url
from cw.models import OrchestratorEventType, QueueItemStatus, WatchedPr
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION, TERMINAL_SESSION_STATUSES
from cw.models.events import watched_pr_collides
from cw.models.pr_ref import PrRef

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

    ``pr_repo`` and ``previous_pr_repo`` are ``None`` for a same-repo ref (and
    ``previous_pr_repo`` also when there was no prior hold) and the
    ``owner/repo`` slug for an explicit one. ``via_ticket`` is the dependency
    ticket of a ``--until-ticket-merged`` hold, else ``None`` (#3634).
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
    pr_repo: str | None = None
    previous_pr_repo: str | None = None
    via_ticket: str | None = None


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


def existing_hold_pr(task: TicketTask) -> PrRef | None:
    """The PR a row already holds on, or None when it is not a dependency hold."""
    if (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.disposition == DEPENDENCY_HOLD_DISPOSITION
    ):
        return task.blocked_on_pr
    return None


def is_operator_hold_on(task: TicketTask, ref: PrRef) -> bool:
    """True iff *task* is already an operator hold (no session) on *ref*.

    Refs compare by :meth:`PrRef.key`, so the repo slug is case-insensitive.
    """
    existing = existing_hold_pr(task)
    return (
        existing is not None and existing.key() == ref.key() and task.session_id is None
    )


def _where(ref: PrRef, repo: str) -> str:
    """``PR #9 in foo/bar`` for a same-repo ref, ``PR o/r#9`` for an explicit one."""
    if ref.repo is None:
        return f"PR #{ref.number} in {repo}"
    return f"PR {ref.label()}"


def resolve_ticket_pr(
    store: DevQueueStore, dep_ticket_id: str, holder: str, client: str
) -> tuple[PrRef, str]:
    """Resolve dependency ticket *dep_ticket_id* to its recorded PR (#3634).

    Pure: reads *store* only, no gh and no git. Returns the explicit-form ref
    (repo from the PR url) and the dependency's ``ticket_id``. The row's
    ``pr_url`` is read, then ``adopted_pr_url``; ``status`` is never read, so a
    COMPLETED dependency is not merge evidence. Raises :class:`CwError` for a
    self-reference, a missing row, no PR, or a url that is not a github pull
    request url.
    """
    if dep_ticket_id == holder:
        msg = (
            f"ticket {holder} cannot be held on its own PR;"
            " name a different ticket with --until-ticket-merged"
        )
        raise CwError(msg)
    row = _find_ticket(store, dep_ticket_id, client, warn=False)
    pr_url = row.pr_url if row.pr_url is not None else row.adopted_pr_url
    if pr_url is None:
        msg = (
            f"ticket {row.ticket_id} in client {client} has no PR yet;"
            " hold on the PR number once it exists (--until-pr-merged)"
        )
        raise CwError(msg)
    parsed = parse_pr_url(pr_url)
    ref = None if parsed is None else PrRef.parse(f"{parsed[0]}#{parsed[1]}")
    if ref is None:
        msg = (
            f"ticket {row.ticket_id} in client {client} records PR url {pr_url},"
            " which is not a github pull request url;"
            " hold on the PR with --until-pr-merged"
        )
        raise CwError(msg)
    return ref, row.ticket_id


def restamp_unvetted_hold_ref(
    ticket_id: str, client: str, expected: PrRef, replacement: PrRef | None
) -> bool:
    """Compare-and-set a worker park's explicit hold ref (#3634, D7).

    Under the queue lock only (no git, no gh, no sessions lock), re-read the
    live row and write *replacement* only when it is still a BLOCKED_ON_USER
    ``dependency_hold`` worker park whose ``blocked_on_pr`` equals *expected*.
    A ``None`` replacement also sets disposition ``awaiting_operator``: the
    no-id park, which keeps its lane slot and is never auto-released. Returns
    whether it wrote.
    """
    with _lock():
        store = load_dev_queue()
        try:
            task = _find_ticket(store, ticket_id, client, warn=False)
        except CwError:
            return False
        if (
            task.status != QueueItemStatus.BLOCKED_ON_USER
            or task.disposition != DEPENDENCY_HOLD_DISPOSITION
            or task.blocked_on_pr != expected
        ):
            return False
        # Invariant: no writer clears session_id on a BLOCKED_ON_USER
        # dependency_hold worker park before the registrar vets it, so
        # session_id is not None means a worker park, not an operator hold.
        if task.session_id is None:
            return False
        task.blocked_on_pr = replacement
        if replacement is None:
            task.disposition = AWAITING_OPERATOR_DISPOSITION
        save_dev_queue(store)
    return True


def _watch_collision(
    store: DevQueueStore, repo: str, pr_number: int, client: str
) -> WatchedPr | None:
    """Return the active watch on *repo*/*pr_number* owned by another client, if any.

    Emits no event and takes no lock: it reads the store the caller already
    holds. It uses the same predicate as ``register_or_adopt_watched_pr``;
    only the read-only scan is kept here so it can run inside the mutation lock.
    """
    for existing in store.watched_prs:
        if watched_pr_collides(existing, repo=repo, pr_number=pr_number, client=client):
            return existing
    return None


def _update_existing_hold(
    store: DevQueueStore,
    task: TicketTask,
    ref: PrRef,
    current: PrRef,
    via_ticket: str | None,
) -> HoldResult:
    """Move or convert a dependency hold that already exists (caller holds the lock)."""
    previous_disposition = task.disposition
    previous_reason = task.blocked_reason
    converted = task.session_id is not None
    cleared = task.session_id
    task.session_id = None
    task.blocked_on_pr = ref
    task.stale_gate_detected_at = None
    result = HoldResult(
        ticket_id=task.ticket_id,
        client=task.client,
        pr_number=ref.number,
        outcome="updated",
        previous_pr=current.number,
        previous_disposition=previous_disposition,
        previous_blocked_reason=previous_reason,
        converted_from_worker_park=converted,
        cleared_session_id=cleared,
        pr_repo=ref.repo,
        previous_pr_repo=current.repo,
        via_ticket=via_ticket,
    )
    save_dev_queue(store)
    _log.info(
        "dependency hold updated ticket=%s client=%s pr=%s previous_pr=%s converted=%s",
        task.ticket_id,
        task.client,
        ref.label(),
        current.label(),
        converted,
    )
    return result


def _hold_locked(
    ticket_id: str,
    client: str,
    ref: PrRef,
    *,
    repo: str,
    via_ticket: str | None,
    bounded: bool,
) -> HoldResult:
    """The check-then-mutate window of :func:`hold_ticket`, under the queue lock."""
    # Why: sessions_lock is outermost by ADR-0019, so a session writer cannot
    # become live between this validation and clearing the queue binding.
    with sessions_lock(bounded=bounded), _lock(bounded=bounded):
        state = load_state()
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client, warn=False)
        check_holdable(task, state)
        if is_operator_hold_on(task, ref):
            return HoldResult(
                ticket_id=task.ticket_id,
                client=task.client,
                pr_number=ref.number,
                outcome="unchanged",
                previous_pr=ref.number,
                previous_disposition=None,
                previous_blocked_reason=None,
                converted_from_worker_park=False,
                cleared_session_id=None,
                pr_repo=ref.repo,
                previous_pr_repo=ref.repo,
                via_ticket=via_ticket,
            )
        clash = _watch_collision(store, repo, ref.number, task.client)
        if clash is not None:
            msg = (
                f"{_where(ref, repo)} is already watched by client"
                f" {clash.client}; refusing to hold {task.ticket_id}"
            )
            raise CwError(msg)
        current = existing_hold_pr(task)
        if current is not None:
            return _update_existing_hold(store, task, ref, current, via_ticket)
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
        task.blocked_on_pr = ref  # after the transition, which clears it
        result = HoldResult(
            ticket_id=task.ticket_id,
            client=task.client,
            pr_number=ref.number,
            outcome="held",
            previous_pr=None,
            previous_disposition=previous_disposition,
            previous_blocked_reason=previous_reason,
            converted_from_worker_park=False,
            cleared_session_id=cleared,
            pr_repo=ref.repo,
            via_ticket=via_ticket,
        )
        save_dev_queue(store)
        _log.info(
            "dependency hold placed ticket=%s client=%s pr=%s replaced_disposition=%s"
            " replaced_blocked_reason=%s",
            task.ticket_id,
            task.client,
            ref.label(),
            previous_disposition,
            previous_reason,
        )
        return result


def hold_ticket(
    ticket_id: str,
    client: str,
    ref: PrRef,
    *,
    repo: str,
    via_ticket: str | None = None,
    bounded: bool = False,
) -> HoldResult:
    """Park a row as a dependency hold on *ref* (#2321, #3634).

    *repo* is the resolved watch repo (the client's slug for a same-repo ref,
    the ref's repo for an explicit one), used by the collision check.

    Saves the row and returns. The caller registers the ``WatchedPr``
    afterwards (outside this lock), then records ``ticket.held`` via
    :func:`record_hold_event` last: state first, then event. Raises
    :class:`CwError` on any refusal, before mutating.
    """
    return _hold_locked(
        ticket_id, client, ref, repo=repo, via_ticket=via_ticket, bounded=bounded
    )


def record_hold_event(result: HoldResult, repo: str) -> None:
    """Record the ``ticket.held`` audit event for one mutating hold (#2321).

    The durable audit trail for a same-status overwrite, which emits no
    ``task.transition``. Mutating callers append it after the row save; no
    handler wraps it, so a failed write propagates. An ``unchanged`` repeat
    records nothing.
    """
    if result.outcome == "unchanged":
        return
    record_event(
        OrchestratorEventType.TICKET_HELD,
        {**asdict(result), "repo": repo},
        correlation_id=result.ticket_id,
    )
