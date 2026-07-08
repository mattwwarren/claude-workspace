"""Daemon-side gate recipes: mechanical gate-clearing reactor (RFC 0009).

Gate recipes are the opt-in automation layer that clears an approval gate a
human would otherwise have to clear by hand, but only when a fixed, verifiable
predicate holds. Unlike the concierge reactor (``cw.reconcile.concierge``),
which *recovers* rows stuck behind dead sessions, gate recipes *advance* a live
approval gate — so they gate on their own opt-in master switch
(``OrchestratorConfig.gate_recipes_enabled``, default False) and forward their
audit event to the operator channel, since an auto-advance with no human in the
loop is attention-worthy.

**P1+P2 scope (this module, GitHub #1065):** the ``auto_approve_clean_review``
recipe. It auto-approves a ``review_pending_approval`` gate when the review
came back completely clean — no MUST_FIX findings, nothing deferred, health
recommendation PROCEED, and no forbidden-area touch. ``auto_adopt_clean_plan``
(P3, #1066) and the per-lane ``resolve_gate_recipe_enabled`` precedence (P4,
#1067) are out of scope; only the ``RECIPE_AUTO_ADOPT_PLAN`` constant is
defined here so both recipe keys have a single home.

The recipe follows the repo's detect/act split (see ``concierge.py`` for the
closest sibling): a pure ``_detect_auto_approve_review`` classification phase,
then ``_act_auto_approve_review`` which re-validates the predicate under
``dev_queue_lock()`` and mutates. Emit-before-act is a hard requirement:
:class:`OrchestratorEventType.GATE_AUTO_APPROVED` is recorded (durably, to the
append-only events inbox) *before* the approval mutation, so evidence of what
the recipe decided survives even if the subsequent write fails (mirrors the
concierge ``CONCIERGE_RECOVERED`` ordering).

The act phase calls the lock-free ``_approve_ticket_locked`` primitive directly
from inside its own ``dev_queue_lock()`` acquisition — never the public
``approve_ticket`` wrapper, which would self-deadlock by re-acquiring the same
flock-based lock (see #1065 and ``dev_queue._approve_ticket_locked``).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.config import load_state
from cw.dev_queue import (
    _approve_ticket_locked,
    dev_queue_lock,
    load_dev_queue,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType, QueueItemStatus

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, OrchestratorConfig, TicketTask

_log = logging.getLogger(__name__)

# Recipe name constants — the recognised gate-recipe keys. Only the review
# recipe is wired in P1+P2 (#1065); RECIPE_AUTO_ADOPT_PLAN is defined now so
# both keys have one home, but its detect/act land in P3 (#1066).
RECIPE_AUTO_APPROVE_REVIEW = "auto_approve_clean_review"
RECIPE_AUTO_ADOPT_PLAN = "auto_adopt_clean_plan"

# The only sentinel status the review recipe fires on. A row whose owning
# session's last_result is not at this gate is never a candidate.
_REVIEW_PENDING_APPROVAL = "review_pending_approval"
# The single health recommendation the clean-review predicate accepts.
_RECOMMENDATION_PROCEED = "PROCEED"

# Best-effort ticket-comment subprocess timeout (seconds). Same shape as
# executor._post_review_comment's gh call, but this helper LOGS failures
# rather than silently suppressing them (the plan's OQ2 resolution).
_COMMENT_TIMEOUT_SECONDS = 30

_AUTO_APPROVE_COMMENT_TEMPLATE = """\
Auto-approved by gate recipe `{recipe}`.

The review met the clean-review predicate and was approved automatically
(no human review) by RFC 0009 gate-recipe automation:

- must_fix_initial: {must_fix_initial}
- deferred: {deferred}
- recommendation: {recommendation}
- forbidden_touched: {forbidden_touched}

See event `GATE_AUTO_APPROVED` for the full audit trail.
"""


@dataclass(frozen=True)
class GateRecipeCandidate:
    """Classification result from a gate recipe's detect phase.

    Same shape as :class:`cw.reconcile.concierge.ConciergeCandidate` plus a
    ``lane`` field: the ``GATE_AUTO_APPROVED`` event payload carries the row's
    lane, so the candidate captures it at detect time. ``evidence`` carries the
    ``predicate_snapshot`` — the exact four field values that licensed the fire
    (``must_fix_initial``, ``deferred``, ``recommendation``,
    ``forbidden_touched``), read off ``session.last_result``.

    Why ``evidence`` is unlike :class:`ConciergeCandidate`'s: the concierge act
    phase reads ``candidate.evidence`` straight into its event payload, but
    this module's act phase (:func:`_act_auto_approve_review`) deliberately
    re-derives a fresh snapshot instead of trusting this detect-time one — the
    predicate is re-checked under ``dev_queue_lock()`` to close the
    detect-to-act race (a concurrent human approve or new sentinel can
    invalidate it), so acting on a stale ``evidence`` value here would defeat
    that guard. The field is kept for detect-phase introspection/tests only.
    """

    ticket_id: str
    client: str
    lane: str
    recipe: str
    evidence: dict[str, object]
    session_id: str


def _clean_review_snapshot(last_result: object) -> dict[str, object] | None:
    """Extract the clean-review predicate snapshot, or None if not fireable.

    Returns None (fail-closed) unless *last_result* is a dict at the
    ``review_pending_approval`` gate whose ``review``/``health``/``scope``
    sections are all present dicts. The returned snapshot holds the four
    predicate field values verbatim; whether the predicate *holds* is a
    separate check (:func:`_predicate_holds`) so detect and act share both the
    extraction and the decision.
    """
    if not isinstance(last_result, dict):
        return None
    if last_result.get("status") != _REVIEW_PENDING_APPROVAL:
        return None
    review = last_result.get("review")
    health = last_result.get("health")
    scope = last_result.get("scope")
    if not (
        isinstance(review, dict)
        and isinstance(health, dict)
        and isinstance(scope, dict)
    ):
        return None
    return {
        "must_fix_initial": review.get("must_fix_initial"),
        "deferred": review.get("deferred", 0),
        "recommendation": health.get("recommendation"),
        "forbidden_touched": scope.get("forbidden_touched"),
    }


def _predicate_holds(snapshot: dict[str, object]) -> bool:
    """True iff the four-field clean-review predicate is satisfied.

    Every field is compared against its clean value; a missing/None field
    (e.g. a malformed producer payload) fails the comparison and blocks the
    fire — the predicate is fail-closed.
    """
    return (
        snapshot["must_fix_initial"] == 0
        and snapshot["deferred"] == 0
        and snapshot["recommendation"] == _RECOMMENDATION_PROCEED
        and snapshot["forbidden_touched"] is False
    )


def _detect_auto_approve_review(
    state: CwState, tasks: list[TicketTask]
) -> list[GateRecipeCandidate]:
    """Pure classification phase for auto_approve_clean_review. Zero writes.

    A candidate is produced for every BLOCKED_ON_USER row whose owning session
    (resolved via ``task.session_id``, the same lookup ``approve_ticket``
    performs) sits at the ``review_pending_approval`` gate with a clean-review
    ``last_result``.
    """
    candidates: list[GateRecipeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        if task.session_id is None:
            continue
        session = state.find_by_name_or_id(task.session_id)
        if session is None:
            continue
        snapshot = _clean_review_snapshot(session.last_result)
        if snapshot is None or not _predicate_holds(snapshot):
            continue
        candidates.append(
            GateRecipeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                lane=task.lane,
                recipe=RECIPE_AUTO_APPROVE_REVIEW,
                evidence=snapshot,
                session_id=task.session_id,
            )
        )
    return candidates


def _post_auto_approve_comment(ticket_id: str, snapshot: dict[str, object]) -> None:
    """Post the auto-approve audit comment to the ticket (best-effort, logged).

    A distinct helper from ``executor._post_review_comment``: that one swallows
    failures with zero logging, whereas the ticket's OQ2 resolution requires a
    comment-write failure to be logged (the event remains the source-of-truth
    audit trail — a failed comment never undoes the approve).
    """
    body = _AUTO_APPROVE_COMMENT_TEMPLATE.format(
        recipe=RECIPE_AUTO_APPROVE_REVIEW,
        must_fix_initial=snapshot["must_fix_initial"],
        deferred=snapshot["deferred"],
        recommendation=snapshot["recommendation"],
        forbidden_touched=snapshot["forbidden_touched"],
    )
    try:
        result = subprocess.run(
            ["gh", "issue", "comment", ticket_id, "--body", body],
            capture_output=True,
            timeout=_COMMENT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("gate_recipe_comment_failed ticket=%s: %s", ticket_id, exc)
        return
    if result.returncode != 0:
        _log.warning(
            "gate_recipe_comment_failed ticket=%s rc=%s: %s",
            ticket_id,
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )


def _act_auto_approve_review(
    candidates: list[GateRecipeCandidate], *, now: datetime
) -> list[str]:
    """Act phase: re-validate under lock, emit, then approve via the primitive.

    For each candidate the row + session are re-loaded fresh under
    ``dev_queue_lock()`` and the four-field predicate re-checked — a concurrent
    human approve, re-dispatch, or new sentinel between detect and act can have
    invalidated it (the re-check race). Only a still-valid candidate fires:
    :class:`OrchestratorEventType.GATE_AUTO_APPROVED` is emitted BEFORE the
    mutation, then the lock-free :func:`_approve_ticket_locked` advances the
    gate exactly as a human ``approve_ticket`` call would. Event payload
    sources come from the re-loaded row/session, never the (possibly stale)
    detect-time candidate. The audit comment is posted after the lock releases,
    best-effort — a comment-write failure never undoes the approve.
    """
    if not candidates:
        return []
    # Keyed on (ticket_id, client): ticket_id alone is a per-repo GitHub issue
    # number, not globally unique across this multi-tenant system's clients —
    # keying on ticket_id alone would let two different clients' candidates
    # that happen to share a ticket_id collide and silently drop one.
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    approved: list[str] = []
    comment_jobs: list[tuple[str, dict[str, object]]] = []
    with dev_queue_lock():
        # Loaded once: dev_queue_lock() is the exclusive writer lock for this
        # file, so no concurrent process can change it mid-loop, and every
        # mutation this loop performs goes through _approve_ticket_locked's
        # own internal load/save round-trip rather than this snapshot.
        store = load_dev_queue()
        for candidate in by_key.values():
            state = load_state()
            task = next(
                (
                    t
                    for t in store.tasks
                    if t.ticket_id == candidate.ticket_id
                    and t.client == candidate.client
                ),
                None,
            )
            if task is None or task.status != QueueItemStatus.BLOCKED_ON_USER:
                continue
            if task.session_id is None:
                continue
            session = state.find_by_name_or_id(task.session_id)
            if session is None:
                continue
            snapshot = _clean_review_snapshot(session.last_result)
            if snapshot is None or not _predicate_holds(snapshot):
                continue
            record_event(
                OrchestratorEventType.GATE_AUTO_APPROVED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "lane": task.lane,
                    "session_id": session.id,
                    "recipe": RECIPE_AUTO_APPROVE_REVIEW,
                    "predicate_snapshot": snapshot,
                    "approved_at": now.isoformat(),
                },
                correlation_id=task.ticket_id,
            )
            try:
                _approve_ticket_locked(task.ticket_id, task.client)
            except CwError:
                # The GATE_AUTO_APPROVED event above is already durable, but
                # the mutation didn't land (e.g. a duplicate row resolved to a
                # different task, or the client's pipeline config changed
                # between detect and here). Log and skip rather than let this
                # escape uncaught: an uncaught raise here would abort the rest
                # of this reconcile tick (including run_escalation_sweep and
                # every other still-valid candidate) and, via callers that
                # don't wrap reconcile() in a broad except (e.g. cw status),
                # surface as a crash to unrelated CLI commands.
                _log.warning(
                    "gate_recipe_approve_failed ticket=%s client=%s",
                    task.ticket_id,
                    task.client,
                    exc_info=True,
                )
                continue
            approved.append(task.ticket_id)
            comment_jobs.append((task.ticket_id, snapshot))
    for ticket_id, snapshot in comment_jobs:
        _post_auto_approve_comment(ticket_id, snapshot)
    return approved


def run_gate_recipes(*, now: datetime, config: OrchestratorConfig) -> list[str]:
    """Run all enabled gate recipes for one reconcile tick.

    No-op (returns ``[]`` immediately) unless ``config.gate_recipes_enabled``
    is True. Loads fresh state/dev-queue snapshots itself rather than accepting
    them from the caller — by the time reconcile's ``_reconcile_locked`` reaches
    this wiring point several prior sweeps have already mutated and saved both
    files, so a caller-supplied snapshot would be stale (mirrors
    ``run_concierge_recoveries``). Safe to call while the caller already holds
    ``sessions_lock`` — this function only acquires ``dev_queue_lock`` per act
    phase, never ``sessions_lock`` itself.

    Returns the list of ticket IDs auto-approved this tick.
    """
    if not config.gate_recipes_enabled:
        return []

    state = load_state()
    tasks = load_dev_queue().tasks

    candidates = _detect_auto_approve_review(state, tasks)
    return _act_auto_approve_review(candidates, now=now)
