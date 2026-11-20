"""``cw dev-queue prerequisite``: the locked cores (GitHub #3791).

Pure and locked: no gh call, no git call and no subprocess under the queue
lock (ADR-0019). The CLI verb (``cw.cli.dev_queue.prerequisite``) verifies the
PR with one gh call before :func:`declare_prerequisite`; :func:`remove_prerequisite`
needs no gh call at all. Only the dev-queue lock is taken: neither core reads a
session or changes a session binding, so ``sessions_lock`` is not needed.

A declaration never changes the row's status. The claim gate
(``cw.dispatch.claim.merge_prerequisite``) applies it at the next claim. A
removal releases or restamps a sessionless ``dependency_hold`` parked on the
removed PR, so the row does not wait on a prerequisite that no longer exists.
An operator ``hold`` on the same PR looks the same as a gate park and is
released the same way (documented in the dispatch runbook).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.hold import _watch_collision, parse_github_pull_url
from cw.dev_queue.lifecycle import transition_task_status
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.exceptions import CwError
from cw.models import QueueItemStatus
from cw.models.enums import DEPENDENCY_HOLD_DISPOSITION, TERMINAL_QUEUE_STATUSES
from cw.models.merge_prerequisite import MergePrerequisite, unsatisfied

if TYPE_CHECKING:
    from cw.models import DevQueueStore, TicketTask
    from cw.models.pr_ref import PrRef

_log = logging.getLogger(__name__)

# A bound on one row's list: a typo loop cannot grow the row without limit.
MAX_MERGE_PREREQUISITES: Final = 8

PrerequisiteOutcome = Literal["declared", "unchanged", "removed"]


@dataclass(frozen=True)
class PrerequisiteResult:
    """Outcome of one declare or remove, echoed by the CLI verb.

    ``released`` is True when a removal returned a parked row to PENDING;
    ``restamped_to`` is the PR number a removal moved the park to, else None.
    """

    ticket_id: str
    client: str
    pr_number: int
    outcome: PrerequisiteOutcome
    released: bool
    restamped_to: int | None


def _result(
    task: TicketTask,
    ref: PrRef,
    outcome: PrerequisiteOutcome,
    *,
    released: bool = False,
    restamped_to: int | None = None,
) -> PrerequisiteResult:
    return PrerequisiteResult(
        ticket_id=task.ticket_id,
        client=task.client,
        pr_number=ref.number,
        outcome=outcome,
        released=released,
        restamped_to=restamped_to,
    )


def check_declarable(task: TicketTask) -> None:
    """Raise :class:`CwError` unless *task* may take a merge prerequisite.

    Only a terminal row is refused. PENDING, RUNNING, BLOCKED_ON_USER and
    AWAITING_OPERATOR_SIGNOFF are all allowed, because declaring never changes
    the status: the gate applies at the next claim. A row with a PR or a live
    session is allowed too (the reason this is not ``check_holdable``).
    """
    if task.status in TERMINAL_QUEUE_STATUSES:
        msg = (
            f"ticket {task.ticket_id} in client {task.client} is"
            f" {task.status.value}; a merge prerequisite applies only to a row"
            " that can still dispatch"
        )
        raise CwError(msg)


def has_prerequisite(task: TicketTask, ref: PrRef) -> bool:
    """True iff *task* already lists *ref* (satisfied or not)."""
    key = ref.key()
    return any(entry.pr.key() == key for entry in task.merge_prerequisites)


def _is_own_pr(task: TicketTask, ref: PrRef, repo: str) -> bool:
    """True iff *ref* in *repo* is the row's own ``pr_url`` or ``adopted_pr_url``."""
    for url in (task.pr_url, task.adopted_pr_url):
        parsed = None if url is None else parse_github_pull_url(url)
        if (
            parsed is not None
            and parsed[0].lower() == repo.lower()
            and parsed[1] == ref.number
        ):
            return True
    return False


def _declare_in_row(
    store: DevQueueStore, task: TicketTask, ref: PrRef, repo: str, actor: str
) -> PrerequisiteResult:
    """The check-then-append window of :func:`declare_prerequisite` (lock held)."""
    check_declarable(task)
    if has_prerequisite(task, ref):
        return _result(task, ref, "unchanged")
    if len(task.merge_prerequisites) >= MAX_MERGE_PREREQUISITES:
        msg = (
            f"ticket {task.ticket_id} already has {MAX_MERGE_PREREQUISITES}"
            " merge prerequisites; remove one first (--remove)"
        )
        raise CwError(msg)
    if _is_own_pr(task, ref, repo):
        msg = (
            f"PR #{ref.number} is ticket {task.ticket_id}'s own PR;"
            " a row cannot wait on its own merge"
        )
        raise CwError(msg)
    clash = _watch_collision(store, repo, ref.number, task.client)
    if clash is not None:
        msg = (
            f"PR #{ref.number} in {repo} is already watched by client"
            f" {clash.client}; refusing to record a prerequisite for"
            f" {task.ticket_id}"
        )
        raise CwError(msg)
    task.merge_prerequisites.append(
        MergePrerequisite(pr=ref, repo=repo, actor=actor, declared_at=datetime.now(UTC))
    )
    save_dev_queue(store)
    _log.info(
        "merge prerequisite declared ticket=%s client=%s pr=%s repo=%s actor=%s",
        task.ticket_id,
        task.client,
        ref.label(),
        repo,
        actor,
    )
    return _result(task, ref, "declared")


def declare_prerequisite(
    ticket_id: str,
    client: str,
    ref: PrRef,
    *,
    repo: str,
    actor: str,
    bounded: bool = False,
) -> PrerequisiteResult:
    """Append same-repo *ref* (verified in *repo*) to the row's prerequisites.

    Idempotent: a PR already listed returns ``unchanged`` and writes nothing.
    Raises :class:`CwError` on any refusal, before mutating.
    """
    with _lock(bounded=bounded):
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client, warn=False)
        return _declare_in_row(store, task, ref, repo, actor)


def _parked_on(task: TicketTask, ref: PrRef) -> bool:
    """True iff *task* is a sessionless dependency hold on *ref*."""
    return (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.disposition == DEPENDENCY_HOLD_DISPOSITION
        and task.session_id is None
        and task.blocked_on_pr is not None
        and task.blocked_on_pr.key() == ref.key()
    )


def _release_or_restamp(task: TicketTask, ref: PrRef) -> PrerequisiteResult:
    """Release the park, or move it to the next unsatisfied entry (lock held)."""
    remaining = unsatisfied(task.merge_prerequisites)
    if not remaining:
        transition_task_status(task, QueueItemStatus.PENDING)
        task.session_id = None
        return _result(task, ref, "removed", released=True)
    nxt = remaining[0].pr
    task.blocked_on_pr = nxt
    task.stale_gate_detected_at = None
    return _result(task, ref, "removed", restamped_to=nxt.number)


def _remove_in_row(
    store: DevQueueStore, task: TicketTask, ref: PrRef
) -> PrerequisiteResult:
    """The find-then-remove window of :func:`remove_prerequisite` (lock held)."""
    key = ref.key()
    entry = next((e for e in task.merge_prerequisites if e.pr.key() == key), None)
    if entry is None:
        msg = (
            f"ticket {task.ticket_id} in client {task.client} has no merge"
            f" prerequisite on PR {ref.label()}"
        )
        raise CwError(msg)
    task.merge_prerequisites.remove(entry)
    result = _result(task, ref, "removed")
    if entry.satisfied_at is None and _parked_on(task, ref):
        result = _release_or_restamp(task, ref)
    save_dev_queue(store)
    _log.info(
        "merge prerequisite removed ticket=%s client=%s pr=%s released=%s"
        " restamped_to=%s",
        task.ticket_id,
        task.client,
        ref.label(),
        result.released,
        result.restamped_to,
    )
    return result


def remove_prerequisite(
    ticket_id: str, client: str, ref: PrRef, *, bounded: bool = False
) -> PrerequisiteResult:
    """Remove *ref* from the row's prerequisites; release or restamp its park.

    When the row is a sessionless ``dependency_hold`` on *ref*, removing the
    last unsatisfied entry returns it to PENDING, and removing one of several
    moves ``blocked_on_pr`` to the next unsatisfied entry. Raises
    :class:`CwError` when the row lists no such entry.
    """
    with _lock(bounded=bounded):
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client, warn=False)
        return _remove_in_row(store, task, ref)
