"""RFC 0016 adoption policy for dev-queue rows (GitHub #2943).

The shared leaf every adoption touch-point in ``cw.dev_queue`` calls into, so
the policy for an adopted human PR lives in one place:

- :func:`refuse_if_adoption_conflicts` -- the carried-by-a-live-row check
  ``add_ticket`` runs inside its ``_lock`` (pure, no I/O under the lock);
  :func:`is_live_row` is the same liveness predicate, also read by the
  finalize Step 4a sibling inventory via ``cw dev-queue adopted-prs`` (#2949);
- :func:`apply_settled_pin` -- the write half of the session-end re-pin, a
  compare-and-set under a brief ``dev_queue_lock`` with no I/O; the read half
  (origin head, fetch, ancestry) runs lock-free in
  :mod:`cw.dispatch.adopted_head_gate`;
- :func:`advance_pin_after_repair` -- the pin advance after the repair
  recipe's lease push succeeded (no I/O);
- :func:`adopted_requeue_refusal` and :func:`apply_adopted_requeue` -- the
  requeue gate (pure refusal, shared by ``precheck_requeue`` and
  ``requeue_ticket``) and the operator re-pin / intake re-check applied under
  ``requeue_ticket``'s lock;
- :func:`classify_ancestry` and :func:`record_recheck_pin` -- the intake
  re-check's ancestry answer and its ``adoption_pin`` audit event (#2970),
  emitted by the CLI after the lock is released;
- :func:`branch_charset_error` -- the row branch charset applied to a PR's
  ref names before they become a row field or a git argument (#2944).

The pin (``TicketTask.adopted_head_sha``) advances automatically in exactly
two places -- the session-end settle and the repair push -- and otherwise only
through the operator's explicit ``requeue --repin-adopted-head``. Every
ancestry answer that gates an advance fails closed on both ``False`` and
``None`` (:func:`cw._git.is_ancestor`). The intake re-check re-pin gates
nothing on ancestry; it records the answer instead.

The ``adoption_pin`` event's ``ancestry_result`` carries two vocabularies:
the settle gate's ``same`` / ``descendant`` / ``not_descendant_or_unknown``
and the re-check's ``same`` / ``descendant`` / ``not_descendant`` /
``unknown`` (the ``ANCESTRY_*`` constants). An event consumer must handle both.

Import discipline: ``cw.dev_queue.crud`` and ``cw.dev_queue.requeue`` import
this module, so it must not import either of them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, NamedTuple

from pydantic import ValidationError

from cw._git import is_ancestor
from cw.dev_queue.dispositions import (
    ADOPT_PR_STATE_UNKNOWN_DISPOSITION,
    ADOPTED_HEAD_DRIFT_DISPOSITION,
    LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION,
)
from cw.dev_queue.next_commands import dev_queue_command
from cw.dev_queue.storage import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import AdoptionRefusedError, RequeueStateError
from cw.gh import _GH_PR_STATE_OPEN, branch_head_sha_on_origin
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
    resolve_feature_branch,
)
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, DevQueueStore

_log = logging.getLogger(__name__)

#: The intake parks ``requeue`` releases only with a fresh intake re-read:
#: ``adopt_pr_state_unknown``, the one park intake still stamps, and the
#: read-only legacy ``adopt_pr_not_green`` a v1.87.0 queue may still hold
#: (#2944 routes a non-green PR to impl instead of parking it).
ADOPT_PR_PARK_DISPOSITIONS: frozenset[str] = frozenset(
    {ADOPT_PR_STATE_UNKNOWN_DISPOSITION, LEGACY_ADOPT_PR_NOT_GREEN_DISPOSITION}
)

_CHARSET_PROBE_TICKET: Final = "charset-probe"


def branch_charset_error(branch: str) -> str | None:
    """The row's branch validator's message for *branch*, or ``None``.

    The one definition of "a ref name safe to persist on a row and pass to
    git": adoption refuses a ``headRefName`` that fails it, and the repair
    feedback synthesizer (``cw.pr_feedback``) checks both ref names with it.
    """
    try:
        TicketTask(
            ticket_id=_CHARSET_PROBE_TICKET, client=_CHARSET_PROBE_TICKET, branch=branch
        )
    except ValidationError as exc:
        return "; ".join(str(err["msg"]) for err in exc.errors())
    return None


#: The CLI flag that releases an ``adopted_head_drift`` park.
REPIN_ADOPTED_HEAD_FLAG: Final = "--repin-adopted-head"

#: ``TASK_TRANSITION`` payload discriminator for an automatic pin advance, and
#: the two ``source`` tags that say which path advanced it.
ADOPTION_PIN_TRANSITION: Final = "adoption_pin"
PIN_SOURCE_SESSION_END_SETTLE: Final = "session-end-settle"
PIN_SOURCE_REPAIR: Final = "repair"
PIN_SOURCE_REQUEUE_RECHECK: Final = "requeue-recheck"
ADOPTED_ANCESTRY_RESULT_KEY: Final = "adopted_ancestry_result"

#: The intake re-check's ancestry answer (#2970): the pin versus the fresh head.
#: ``unknown`` is ``apply_settled_pin``'s default; ``not_descendant`` is the
#: audit's own value (the settle gate merges it into
#: ``not_descendant_or_unknown`` because it gates the pin).
ANCESTRY_SAME: Final = "same"
ANCESTRY_DESCENDANT: Final = "descendant"
ANCESTRY_NOT_DESCENDANT: Final = "not_descendant"
ANCESTRY_UNKNOWN: Final = "unknown"

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


def is_live_row(task: TicketTask) -> bool:
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
    return task.pr_state.state == _GH_PR_STATE_OPEN


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
        if not is_live_row(existing):
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


def _compare_and_set_pin(
    client: str,
    ticket_id: str,
    *,
    expected_old: str,
    new_pin: str,
    require_repin_due: bool,
) -> bool:
    """Move an adopted row's pin from *expected_old* to *new_pin* and clear the flag.

    The one compare-and-set both automatic pin advances share. Under a brief
    ``dev_queue_lock`` and with no I/O: when the row is adopted and its pin
    still equals *expected_old* (and, with *require_repin_due*, its
    ``adopted_repin_due`` flag is still set), write *new_pin*, clear the flag
    and save. Returns whether the row was written; a missing row, a
    non-adopted row, a pin that moved since the caller's snapshot or a clear
    flag (when required) is a no-op.

    Ordering trade-off (operator decision, #2943 review round 12): the row is
    saved and the lock released BEFORE the caller emits the audit event
    (:func:`_emit_pin_transition`). The pin on the row is the authority; a
    crash between the save and the emit loses only the audit event. Emitting
    inside the lock, or before the mutation, was rejected: it puts an event
    write under ``dev_queue_lock`` and can leave an event for a pin that was
    never persisted. Review rounds have flagged both orderings in turn; do
    not flip it again.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        row = _find_adopted_row(store, client, ticket_id)
        if (
            row is None
            or row.adopted_head_sha != expected_old
            or (require_repin_due and not row.adopted_repin_due)
        ):
            return False
        row.adopted_head_sha = new_pin
        row.adopted_repin_due = False
        save_dev_queue(store)
    return True


def _emit_pin_transition(
    client: str,
    ticket_id: str,
    *,
    old_sha: str,
    new_sha: str,
    source: str,
    **detail: str | None,
) -> None:
    """Emit the ``TASK_TRANSITION`` audit event for one automatic pin advance.

    Called only after :func:`_compare_and_set_pin` wrote the row and released
    the queue lock. *detail* carries the per-source payload keys.
    """
    record_event(
        OrchestratorEventType.TASK_TRANSITION,
        {
            "ticket_id": ticket_id,
            "client": client,
            "transition": ADOPTION_PIN_TRANSITION,
            "old_sha": old_sha,
            "new_sha": new_sha,
            "source": source,
            **detail,
        },
        correlation_id=ticket_id,
    )


def apply_settled_pin(
    client: str,
    ticket_id: str,
    *,
    expected_old: str,
    new_pin: str,
    observed_sha: str | None = None,
    ancestry_result: str | None = None,
    source: str = PIN_SOURCE_SESSION_END_SETTLE,
) -> bool:
    """Compare-and-set the adopted pin and clear ``adopted_repin_due``.

    The write half of the session-end re-pin; the caller observed the origin
    head and decided ancestry with no lock held. The row is written only when
    it is adopted, its pin still equals *expected_old* and its flag is still
    set. ``new_pin == expected_old`` is the clear-only case (head unchanged,
    or a non-descendant/git error that keeps the pin).

    Returns whether the row was written. The audit event follows the save,
    after the queue lock is released (see :func:`_compare_and_set_pin`).
    """
    if not _compare_and_set_pin(
        client,
        ticket_id,
        expected_old=expected_old,
        new_pin=new_pin,
        require_repin_due=True,
    ):
        return False
    _emit_pin_transition(
        client,
        ticket_id,
        old_sha=expected_old,
        new_sha=new_pin,
        source=source,
        observed_sha=observed_sha or new_pin,
        ancestry_result=ancestry_result or "unknown",
    )
    return True


def classify_ancestry(repo: Path, old: str, new: str) -> str:
    """Four-valued ancestry of *old* versus *new* for the re-check audit.

    ``same`` when the SHAs are equal (no git call); otherwise
    :func:`cw._git.is_ancestor` maps ``True`` to ``descendant``, ``False`` to
    ``not_descendant`` and ``None`` (an object absent locally, a git error) to
    ``unknown``. Policy-free: the caller decides what each answer permits.
    """
    if old == new:
        return ANCESTRY_SAME
    answer = is_ancestor(repo, old, new)
    if answer is None:
        return ANCESTRY_UNKNOWN
    return ANCESTRY_DESCENDANT if answer else ANCESTRY_NOT_DESCENDANT


def record_recheck_pin(
    client: str,
    ticket_id: str,
    *,
    old_sha: str,
    new_sha: str,
    ancestry_result: str,
) -> None:
    """Emit the ``adoption_pin`` audit event for an intake re-check re-pin (#2970).

    Same payload shape as :func:`apply_settled_pin`'s, with source
    ``requeue-recheck``. Called by the CLI echo layer after ``requeue_ticket``
    saved the row and released the queue lock; ``requeue_ticket`` itself emits
    nothing (see :func:`_compare_and_set_pin` for the ordering trade-off).
    """
    _emit_pin_transition(
        client,
        ticket_id,
        old_sha=old_sha,
        new_sha=new_sha,
        source=PIN_SOURCE_REQUEUE_RECHECK,
        observed_sha=new_sha,
        ancestry_result=ancestry_result,
    )


def advance_pin_after_repair(
    client: str,
    ticket_id: str,
    *,
    expected_sha: str,
    new_sha: str,
    repair_job: str | None = None,
    session_id: str | None = None,
    reason: str = "repair_lease_push",
) -> bool:
    """Advance the adopted pin after the repair recipe's lease push (R5).

    The lease push replaced ``expected_sha`` with ``new_sha`` on origin, so a
    row pinned at ``expected_sha`` moves to ``new_sha``; the push is cw's own,
    so ``adopted_repin_due`` clears too (it need not be set). No I/O under the
    lock. A missing row, a non-adopted row or a mismatched pin is a no-op.
    Returns whether the row was written; the audit event follows the save
    (see :func:`_compare_and_set_pin`).
    """
    if not _compare_and_set_pin(
        client,
        ticket_id,
        expected_old=expected_sha,
        new_pin=new_sha,
        require_repin_due=False,
    ):
        return False
    _emit_pin_transition(
        client,
        ticket_id,
        old_sha=expected_sha,
        new_sha=new_sha,
        source=PIN_SOURCE_REPAIR,
        repair_job=repair_job or f"{client}/{ticket_id}",
        session_id=session_id,
        reason=reason,
    )
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
    if (repin or recheck is not None) and _repin_target_stage(
        task, stage_override
    ) not in _REPIN_ALLOWED_STAGES:
        return (
            "A re-pin lands the row at review or earlier (the new commits are"
            " unreviewed); pass `--stage review`."
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
    task: TicketTask, client_cfg: ClientConfig, recheck: AdoptedPrSnapshot
) -> dict[str, str | bool | int]:
    """Re-pin an ``adopt_pr_*`` park to the fresh intake head and record ancestry.

    Unlike the explicit ``--repin-adopted-head`` path this never refuses on a
    non-descendant head: the pin of an intake park has never been reviewed, so
    a rewritten head loses nothing. The ancestry answer is returned as
    :data:`ADOPTED_ANCESTRY_RESULT_KEY` (``unknown`` when the objects could not be
    fetched), logged here, and audited by the CLI after the lock is released.

    Invariant: an ``adopt_pr_*`` park is stamped only by
    :func:`cw.adopt_pr.build_adopted_task` at intake, on a row that never ran a
    session (``ever_spawned=False``), so there is no review round, ledger or
    approval for a rewritten head to invalidate. If that ever stops being
    true, three backstops still apply: a same-stage REVIEW requeue clears the
    approval and ``must_fix_override``
    (``lifecycle._reset_for_same_stage_requeue``), a regress clears the
    approval (``lifecycle._stage_regress``), and the convergence ledger resets
    itself on a rewritten history (``reconcile_ledger_for_init``). #2944
    (conflicted PRs entering at impl once repair routing lands, RFC 0016 D-5)
    is the change that must revisit this docstring and the invariant tests in
    ``tests/test_dev_queue_adopt_requeue.py::TestAdoptParkInvariant``.
    """
    if recheck.pr_url != task.adopted_pr_url or recheck.branch != task.branch:
        msg = (
            f"Refusing the intake re-check for ticket '{task.ticket_id}': it read"
            f" {recheck.pr_url} on branch '{recheck.branch}', but the row adopted"
            f" {task.adopted_pr_url} on branch '{task.branch}'."
        )
        raise RequeueStateError(msg)
    old = task.adopted_head_sha or ""
    ancestry = classify_ancestry(_git_dir(client_cfg), old, recheck.head_sha)
    task.adopted_head_sha = recheck.head_sha
    task.adopted_repin_due = False
    log = (
        _log.info if ancestry in {ANCESTRY_SAME, ANCESTRY_DESCENDANT} else _log.warning
    )
    log(
        "adopted_repinned_on_recheck ticket=%s client=%s old=%s new=%s ancestry=%s",
        task.ticket_id,
        task.client,
        old,
        recheck.head_sha,
        ancestry,
    )
    return {
        "adopted_repinned": True,
        "adopted_head_sha_old": old,
        "adopted_head_sha": recheck.head_sha,
        ADOPTED_ANCESTRY_RESULT_KEY: ancestry,
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
    ``adopt_pr_*`` park with *recheck* re-pins to the fresh intake head and
    reports how it relates to the old pin (:func:`_repin_to_recheck`).
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
        return _repin_to_recheck(task, client_cfg, recheck)
    return {}
