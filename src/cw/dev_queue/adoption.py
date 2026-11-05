"""RFC 0016 adoption policy for dev-queue rows (GitHub #2943).

The shared leaf every adoption touch-point in ``cw.dev_queue`` calls into, so
the policy for an adopted human PR lives in one place:

- :func:`refuse_if_adoption_conflicts` -- the carried-by-a-live-row check
  ``add_ticket`` runs inside its ``_lock`` (pure, no I/O under the lock);
- :func:`apply_settled_pin` -- the write half of the session-end re-pin, a
  compare-and-set under a brief ``dev_queue_lock`` with no I/O; the read half
  (origin head, fetch, ancestry) runs lock-free in
  :mod:`cw.dispatch.adopted_head_gate`;
- :func:`advance_pin_after_repair` -- the pin advance after the repair
  recipe's lease push succeeded (no I/O);
- :func:`adopted_requeue_refusal` and :func:`apply_adopted_requeue` -- the
  requeue gate (pure refusal, shared by ``precheck_requeue`` and
  ``requeue_ticket``) and the operator re-pin / intake re-check applied under
  ``requeue_ticket``'s lock.

The pin (``TicketTask.adopted_head_sha``) advances automatically in exactly
two places -- the session-end settle and the repair push -- and otherwise only
through the operator's explicit ``requeue --repin-adopted-head``. Every
ancestry answer that gates an advance fails closed on both ``False`` and
``None`` (:func:`cw._git.is_ancestor`).

Import discipline: ``cw.dev_queue.crud`` and ``cw.dev_queue.requeue`` import
this module, so it must not import either of them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, NamedTuple

from cw._git import is_ancestor
from cw.dev_queue.dispositions import (
    ADOPT_PR_NOT_GREEN_DISPOSITION,
    ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
    ADOPTED_HEAD_DRIFT_DISPOSITION,
)
from cw.dev_queue.next_commands import dev_queue_command
from cw.dev_queue.storage import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import AdoptionRefusedError, RequeueStateError
from cw.gh import GH_PR_STATE_OPEN, branch_head_sha_on_origin
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    resolve_feature_branch,
)
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.models import ClientConfig, DevQueueStore, TicketTask

_log = logging.getLogger(__name__)

#: The two intake parks ``requeue`` releases only with a fresh intake re-read.
ADOPT_PR_PARK_DISPOSITIONS: frozenset[str] = frozenset(
    {ADOPT_PR_NOT_GREEN_DISPOSITION, ADOPT_PR_STATE_UNKNOWN_DISPOSITION}
)

#: The CLI flag that releases an ``adopted_head_drift`` park.
REPIN_ADOPTED_HEAD_FLAG: Final = "--repin-adopted-head"

#: A re-pin lands the row at review or earlier: the new commits are unreviewed.
_REPIN_ALLOWED_STAGES: frozenset[Stage] = frozenset(
    {Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.REVIEW}
)

#: Statuses where a row is live for the adoption conflict check (RFC 0016 R7).
_LIVE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {
        QueueItemStatus.PENDING,
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    }
)


class AdoptedPrSnapshot(NamedTuple):
    """One lock-free read of an adopted PR (built by :mod:`cw.adopt_pr`)."""

    pr_url: str
    branch: str
    head_sha: str
    is_draft: bool


def _is_live_row(task: TicketTask) -> bool:
    """True iff *task* still carries its PR for the conflict check (A7).

    A COMPLETED row counts while its PR is still OPEN, or when hydration has
    not observed it yet but a ``pr_url`` is recorded (no I/O under the lock,
    so an unhydrated row fails closed).
    """
    if task.status in _LIVE_STATUSES:
        return True
    if task.status != QueueItemStatus.COMPLETED:
        return False
    if task.pr_state is None:
        return task.pr_url is not None
    return task.pr_state.state == GH_PR_STATE_OPEN


def refuse_if_adoption_conflicts(
    store: DevQueueStore, task: TicketTask, client_cfg: ClientConfig | None
) -> None:
    """Raise :class:`AdoptionRefusedError` if adopting *task* would collide.

    Run inside ``add_ticket``'s ``_lock`` on the freshly loaded *store*; pure
    (no gh, no git). Refuses when (a) any ``(client, ticket_id)`` row already
    exists, with a message distinct from "already queued", or (b) a live row
    of the same client carries the same ``adopted_pr_url`` or resolves to the
    same branch.
    """
    branch = resolve_feature_branch(task.ticket_id, client_cfg, task)
    for existing in store.tasks:
        if existing.client != task.client:
            continue
        if existing.ticket_id == task.ticket_id:
            msg = (
                f"Cannot adopt {task.adopted_pr_url} as ticket '{task.ticket_id}':"
                f" client '{task.client}' already has a row for that ticket"
                f" (status {existing.status.value}, stage {existing.stage.value})."
                " Adoption creates a fresh review-entry row; remove or finish the"
                f" existing row first (`{dev_queue_command('show', existing)}`)."
            )
            raise AdoptionRefusedError(msg)
        if not _is_live_row(existing):
            continue
        same_pr = (
            existing.adopted_pr_url is not None
            and existing.adopted_pr_url == task.adopted_pr_url
        )
        existing_branch = resolve_feature_branch(
            existing.ticket_id, client_cfg, existing
        )
        if same_pr or existing_branch == branch:
            what = "PR" if same_pr else f"branch '{branch}'"
            msg = (
                f"Cannot adopt {task.adopted_pr_url}: its {what} is already"
                f" carried by live row '{existing.ticket_id}'"
                f" (status {existing.status.value})."
                f" Inspect it: `{dev_queue_command('show', existing)}`."
            )
            raise AdoptionRefusedError(msg)


def _find_adopted_row(
    store: DevQueueStore, client: str, ticket_id: str
) -> TicketTask | None:
    for row in store.tasks:
        if (
            row.client == client
            and row.ticket_id == ticket_id
            and row.adopted_head_sha is not None
        ):
            return row
    return None


def apply_settled_pin(
    client: str,
    ticket_id: str,
    *,
    expected_old: str,
    new_pin: str,
    observed_sha: str | None = None,
    ancestry_result: str | None = None,
    source: str = "session-end-settle",
) -> bool:
    """Compare-and-set the adopted pin and clear ``adopted_repin_due``.

    The write half of the session-end re-pin; the caller observed the origin
    head and decided ancestry with no lock held. Under a brief
    ``dev_queue_lock`` and with no I/O: when the row is adopted, its pin still
    equals *expected_old* and its flag is still set, set the pin to *new_pin*
    and clear the flag. ``new_pin == expected_old`` is the clear-only case
    (head unchanged, or a non-descendant/git error that keeps the pin).

    Returns whether the row was written. A missing row, a non-adopted row, a
    pin that moved since the snapshot or an already-clear flag is a no-op.
    The durable audit event is emitted only after the compare-and-set write
    succeeds and after the queue lock is released.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        row = _find_adopted_row(store, client, ticket_id)
        if (
            row is None
            or not row.adopted_repin_due
            or row.adopted_head_sha != expected_old
        ):
            return False
        row.adopted_head_sha = new_pin
        row.adopted_repin_due = False
        save_dev_queue(store)
    record_event(
        OrchestratorEventType.TASK_TRANSITION,
        {
            "ticket_id": ticket_id,
            "client": client,
            "transition": "adoption_pin",
            "old_sha": expected_old,
            "new_sha": new_pin,
            "observed_sha": observed_sha or new_pin,
            "ancestry_result": ancestry_result or "unknown",
            "source": source,
        },
        correlation_id=ticket_id,
    )
    return True


def advance_pin_after_repair(
    client: str, ticket_id: str, *, expected_sha: str, new_sha: str
) -> bool:
    """Advance the adopted pin after the repair recipe's lease push (R5).

    The lease push replaced ``expected_sha`` with ``new_sha`` on origin, so a
    row pinned at ``expected_sha`` moves to ``new_sha``; the push is cw's own,
    so ``adopted_repin_due`` clears too. Takes ``dev_queue_lock`` briefly and
    does no I/O. A missing row, a non-adopted row or a mismatched pin is a
    no-op. Returns whether the row was written.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        row = _find_adopted_row(store, client, ticket_id)
        if row is None or row.adopted_head_sha != expected_sha:
            return False
        row.adopted_head_sha = new_sha
        row.adopted_repin_due = False
        save_dev_queue(store)
    _log.info(
        "adopted_pin_advanced_after_repair ticket=%s client=%s old=%s new=%s",
        ticket_id,
        client,
        expected_sha,
        new_sha,
    )
    return True


def _repin_target_stage(task: TicketTask, stage_override: str | None) -> Stage:
    return Stage(stage_override) if stage_override is not None else task.stage


def adopted_requeue_refusal(
    task: TicketTask,
    stage_override: str | None,
    *,
    repin: bool,
    recheck: AdoptedPrSnapshot | None,
) -> str | None:
    """Return why ``requeue`` must refuse *task*, or ``None`` (pure, no I/O).

    Shared by ``precheck_requeue`` and ``requeue_ticket`` so the prediction
    cannot drift. An ``adopted_head_drift`` park releases only with *repin*
    (the CLI's ``--repin-adopted-head``); an ``adopt_pr_*`` park only with a
    fresh intake *recheck*. Auto callers pass neither, so they can never
    silently absorb a foreign push (R10). Either keyword on any other row is
    refused, and a re-pin targeting a stage past review is refused.
    """
    disposition = task.disposition
    if disposition == ADOPTED_HEAD_DRIFT_DISPOSITION and not repin:
        return (
            f"Ticket '{task.ticket_id}' is parked {disposition}: its adopted"
            " PR's origin head moved off the pinned head outside a cw session."
            " Review the new commits, then re-pin explicitly:"
            f" `{dev_queue_command('requeue', task, REPIN_ADOPTED_HEAD_FLAG)}`."
        )
    if repin and disposition != ADOPTED_HEAD_DRIFT_DISPOSITION:
        return (
            f"{REPIN_ADOPTED_HEAD_FLAG} applies only to an"
            f" {ADOPTED_HEAD_DRIFT_DISPOSITION} park; ticket '{task.ticket_id}'"
            f" has disposition {disposition!r}."
        )
    if repin and _repin_target_stage(task, stage_override) not in (
        _REPIN_ALLOWED_STAGES
    ):
        return (
            f"{REPIN_ADOPTED_HEAD_FLAG} lands the row at review or earlier (the"
            " new commits are unreviewed); pass `--stage review`."
        )
    if disposition in ADOPT_PR_PARK_DISPOSITIONS and recheck is None:
        return (
            f"Ticket '{task.ticket_id}' is parked {disposition}: only the"
            " operator's requeue, which re-reads the PR first, can release it:"
            f" `{dev_queue_command('requeue', task)}`."
        )
    if recheck is not None and disposition not in ADOPT_PR_PARK_DISPOSITIONS:
        return (
            f"An adoption intake re-check applies only to an adopt_pr_* park;"
            f" ticket '{task.ticket_id}' has disposition {disposition!r}."
        )
    return None


def _repin_to_origin_head(
    task: TicketTask, client_cfg: ClientConfig
) -> dict[str, str | bool | int]:
    old = task.adopted_head_sha
    if old is None:
        msg = f"Ticket '{task.ticket_id}' carries no adopted pin to re-pin."
        raise RequeueStateError(msg)
    branch = resolve_feature_branch(task.ticket_id, client_cfg, task)
    repo = _git_dir(client_cfg)
    head, _gh_available = branch_head_sha_on_origin(branch, cwd=repo)
    if head is None:
        msg = (
            f"Cannot re-pin ticket '{task.ticket_id}': the head of"
            f" origin/{branch} is unreadable (gh unavailable or transient)."
            f" Retry: `{dev_queue_command('requeue', task, REPIN_ADOPTED_HEAD_FLAG)}`."
        )
        raise RequeueStateError(msg)
    if is_ancestor(repo, old, head) is not True:
        _log.warning(
            "adopted_repin_declined ticket=%s client=%s pinned=%s observed=%s",
            task.ticket_id,
            task.client,
            old,
            head,
        )
        msg = (
            f"Refusing to re-pin ticket '{task.ticket_id}': origin/{branch} head"
            f" {head} does not descend from the pinned head {old} (a rewrite or"
            " force-push), or ancestry could not be decided (fetch the branch"
            " and retry). A non-descendant head is never absorbed."
        )
        raise RequeueStateError(msg)
    task.adopted_head_sha = head
    task.adopted_repin_due = False
    _log.info(
        "adopted_repinned ticket=%s client=%s old=%s new=%s",
        task.ticket_id,
        task.client,
        old,
        head,
    )
    return {
        "adopted_repinned": True,
        "adopted_head_sha_old": old,
        "adopted_head_sha": head,
    }


def _repin_to_recheck(
    task: TicketTask, recheck: AdoptedPrSnapshot
) -> dict[str, str | bool | int]:
    if recheck.pr_url != task.adopted_pr_url or recheck.branch != task.branch:
        msg = (
            f"Refusing the intake re-check for ticket '{task.ticket_id}': it read"
            f" {recheck.pr_url} on branch '{recheck.branch}', but the row adopted"
            f" {task.adopted_pr_url} on branch '{task.branch}'."
        )
        raise RequeueStateError(msg)
    old = task.adopted_head_sha or ""
    task.adopted_head_sha = recheck.head_sha
    task.adopted_repin_due = False
    _log.info(
        "adopted_repinned_on_recheck ticket=%s client=%s old=%s new=%s",
        task.ticket_id,
        task.client,
        old,
        recheck.head_sha,
    )
    return {
        "adopted_repinned": True,
        "adopted_head_sha_old": old,
        "adopted_head_sha": recheck.head_sha,
    }


def apply_adopted_requeue(
    task: TicketTask,
    client_cfg: ClientConfig,
    stage_override: str | None,
    *,
    repin: bool,
    recheck: AdoptedPrSnapshot | None,
) -> dict[str, str | bool | int]:
    """Gate and apply the adoption half of ``requeue_ticket`` under its lock.

    Raises :class:`RequeueStateError` for :func:`adopted_requeue_refusal`'s
    refusals. A drift park with *repin* reads the origin head and re-pins only
    when the old pin is an ancestor of it (``False`` and ``None`` both refuse;
    the ``_approve_scope_drift_locked`` under-lock origin-read precedent). An
    ``adopt_pr_*`` park with *recheck* re-pins to the fresh intake head.
    Returns the result keys to merge -- empty unless a re-pin happened.
    """
    refusal = adopted_requeue_refusal(
        task, stage_override, repin=repin, recheck=recheck
    )
    if refusal is not None:
        raise RequeueStateError(refusal)
    if repin:
        return _repin_to_origin_head(task, client_cfg)
    if recheck is not None:
        return _repin_to_recheck(task, recheck)
    return {}
