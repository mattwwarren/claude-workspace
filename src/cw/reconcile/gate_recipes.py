"""Daemon-side gate recipes: mechanical gate-clearing reactor (RFC 0009).

Gate recipes are the opt-in automation layer that clears an approval gate a
human would otherwise have to clear by hand, but only when a fixed, verifiable
predicate holds. Unlike the concierge reactor (``cw.reconcile.concierge``),
which *recovers* rows stuck behind dead sessions, gate recipes *advance* a live
approval gate — so they gate on their own opt-in master switch
(``OrchestratorConfig.gate_recipes_enabled``, default False) and forward their
audit event to the operator channel, since an auto-advance with no human in the
loop is attention-worthy.

**P1+P2 scope (GitHub #1065):** the ``auto_approve_clean_review`` recipe. It
auto-approves a ``review_pending_approval`` gate when the review came back
completely clean — no MUST_FIX findings, nothing deferred, health
recommendation PROCEED, and no forbidden-area touch.

**P3 scope (GitHub #1066):** the ``auto_adopt_clean_plan`` recipe. It
auto-adopts a ``plan_pending_approval`` gate when the plan-of-record carries
both signoff markers (``plan-spec-reviewed`` and ``plan-soundness-reviewed``)
appended by ``auto-dev-plan``. The per-lane ``resolve_gate_recipe_enabled``
precedence (P4, #1067) remains out of scope here.

The recipe follows the repo's detect/act split (see ``concierge.py`` for the
closest sibling): a pure ``_detect_auto_approve_review`` classification phase,
then ``_act_auto_approve_review`` which re-validates the predicate under
``dev_queue_lock()`` and mutates. Emit-before-act is a hard requirement:
:class:`OrchestratorEventType.GATE_AUTO_APPROVED` is recorded (durably, to the
append-only events inbox) *before* the approval mutation, so evidence of what
the recipe decided survives even if the subsequent write fails (mirrors the
concierge ``CONCIERGE_RECOVERED`` ordering). If the mutation itself then
raises, :class:`OrchestratorEventType.GATE_AUTO_APPROVE_FAILED` is emitted as
a durable, operator-forwarded correction — without it, ``GATE_AUTO_APPROVED``
would stand alone on the operator channel as an uncorrected false-positive
"approved" signal — and ``TicketTask.gate_recipe_failed_at`` is stamped as a
one-shot latch so the same still-failing episode doesn't re-detect and
re-emit both events every reconcile tick forever. The latch clears itself
the same way the RFC 0008 escalation latch does: unconditionally, on the
next status transition (``dev_queue.transition_task_status``).

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
    save_dev_queue,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.gh import fetch_approved_plan_comment
from cw.models import OrchestratorEventType, QueueItemStatus

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, DevQueueStore, OrchestratorConfig, TicketTask

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

# The only sentinel status the plan recipe fires on. A row whose owning
# session's last_result is not at this gate is never a candidate.
_PLAN_PENDING_APPROVAL = "plan_pending_approval"

# The two signoff markers auto-dev-plan appends to the plan-of-record body.
# _PLAN_SPEC_MARKER mirrors gh._PLAN_MARKER — a local copy avoids importing a
# private cross-module constant; keep the two in sync (see
# test_plan_spec_marker_matches_gh_marker for a drift guard).
_PLAN_SPEC_MARKER = "<!-- plan-spec-reviewed"
_PLAN_SOUNDNESS_MARKER = "<!-- plan-soundness-reviewed"

# predicate_snapshot dict keys (R3) — named once so the producer
# (_clean_plan_snapshot) and consumer (_post_auto_adopt_comment) can't drift
# via a typo'd string literal at one site only.
_SNAPSHOT_KEY_SPEC = "plan_spec_reviewed"
_SNAPSHOT_KEY_SOUNDNESS = "plan_soundness_reviewed"

_AUTO_ADOPT_COMMENT_TEMPLATE = """\
Auto-approved by gate recipe `{recipe}`.

The plan met the clean-plan predicate (both signoff markers present on the
plan-of-record) and was approved automatically (no human review) by RFC 0009
gate-recipe automation:

- plan_spec_reviewed: {plan_spec_reviewed}
- plan_soundness_reviewed: {plan_soundness_reviewed}

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


def _marker_version(body: str, *, marker: str) -> str | None:
    """Extract the ``<date> <vN>`` version string that follows *marker*.

    The caller guarantees *marker* is present in *body* (both markers are
    substring-checked before this is called). Substring split only — no regex
    or date parser (R3): the marker line is ``<!-- plan-spec-reviewed: D vN -->``,
    so we take everything between the marker and the ``-->`` close, strip the
    leading ``:`` and surrounding whitespace. Returns None (fail-closed) if the
    marker comment is never closed with ``-->`` — without this check,
    ``str.split`` silently returns the rest of *body* verbatim, which would
    leak raw plan-of-record text into the predicate_snapshot, the
    GATE_AUTO_APPROVED event payload, and the public audit comment.
    """
    rest = body.split(marker, 1)[1]
    if "-->" not in rest:
        return None
    return rest.split("-->", 1)[0].lstrip(":").strip()


def _plan_of_record_body(task: TicketTask) -> str | None:
    """Return the plan-of-record body, tracker-first with a `.cw/plan.md` fallback.

    Why tracker-first (opposite of local_runner.build_task_message's
    .cw-first order): that function fills a local cache for Stage-2 task
    prompts; this gate checks *current* approval freshness, for which the
    tracker is the authoritative, freshest source. Falls back to the
    worktree's ``.cw/plan.md`` only when the tracker read returns None. A row
    with no materialized worktree has no fallback (returns None rather than
    raising on ``Path(None)``).
    """
    body = fetch_approved_plan_comment(task.ticket_id)
    if body is not None:
        return body
    if task.worktree_path is None:
        return None
    plan_path = task.worktree_path / ".cw" / "plan.md"
    if not plan_path.exists():
        return None
    try:
        return plan_path.read_text(encoding="utf-8")
    except OSError:
        # Read failure between .exists() and read_text() (deleted,
        # permission error, etc.) degrades to "no plan body" rather than
        # propagating — an unhandled exception here would abort the entire
        # reconcile tick, including the unrelated auto_approve_clean_review
        # recipe processed in the same run_gate_recipes() call.
        return None


def _clean_plan_snapshot(
    last_result: object, task: TicketTask
) -> dict[str, object] | None:
    """Extract the clean-plan predicate snapshot, or None if not fireable.

    Returns None (fail-closed) unless *last_result* is a dict at the
    ``plan_pending_approval`` gate AND the plan-of-record body carries BOTH
    signoff markers. Both markers are read from the SAME body (R2 — there is
    exactly one ``body`` variable in scope, so same-source is structural, not
    a separate check); a union across tracker + `.cw/plan.md` is impossible by
    construction. The returned snapshot holds only the two marker-version
    strings — the raw plan body is never placed in the snapshot, the event
    payload, or the audit comment.
    """
    if not isinstance(last_result, dict):
        return None
    if last_result.get("status") != _PLAN_PENDING_APPROVAL:
        return None
    body = _plan_of_record_body(task)
    if body is None:
        return None
    if _PLAN_SPEC_MARKER not in body or _PLAN_SOUNDNESS_MARKER not in body:
        return None
    spec_version = _marker_version(body, marker=_PLAN_SPEC_MARKER)
    soundness_version = _marker_version(body, marker=_PLAN_SOUNDNESS_MARKER)
    if spec_version is None or soundness_version is None:
        return None
    return {
        _SNAPSHOT_KEY_SPEC: spec_version,
        _SNAPSHOT_KEY_SOUNDNESS: soundness_version,
    }


def _detect_auto_approve_review(
    state: CwState, tasks: list[TicketTask]
) -> list[GateRecipeCandidate]:
    """Pure classification phase for auto_approve_clean_review. Zero writes.

    A candidate is produced for every BLOCKED_ON_USER row whose owning session
    (resolved via ``task.session_id``, the same lookup ``approve_ticket``
    performs) sits at the ``review_pending_approval`` gate with a clean-review
    ``last_result``. A row with a non-None ``gate_recipe_failed_at`` latch is
    excluded — a prior act-phase failure for this exact episode already fired
    a correcting ``GATE_AUTO_APPROVE_FAILED`` event, and re-detecting it every
    tick would re-emit both events forever for a condition that hasn't
    changed. The latch clears itself the moment anything about the episode
    does change (``transition_task_status`` unconditionally clears it on
    every status transition, including a same-status re-park by a fresh
    review session).
    """
    candidates: list[GateRecipeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        if task.gate_recipe_failed_at is not None:
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


def _detect_auto_adopt_plan(
    state: CwState, tasks: list[TicketTask]
) -> list[GateRecipeCandidate]:
    """Read-only classification phase for auto_adopt_clean_plan. Zero writes.

    Mirrors :func:`_detect_auto_approve_review`'s guard chain (BLOCKED_ON_USER,
    ``gate_recipe_failed_at`` latch None, resolvable session) but swaps the
    clean-review snapshot for a clean-plan snapshot: the row's owning session
    must sit at the ``plan_pending_approval`` gate and the plan-of-record must
    carry both signoff markers. The plan-of-record read (tracker-first) happens
    here at detect time — unlocked — never under the act-phase lock (R5). Not
    a pure function (it makes a ``gh`` subprocess call and may read a file),
    but it performs no writes/mutations.
    """
    candidates: list[GateRecipeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        if task.gate_recipe_failed_at is not None:
            continue
        if task.session_id is None:
            continue
        session = state.find_by_name_or_id(task.session_id)
        if session is None:
            continue
        snapshot = _clean_plan_snapshot(session.last_result, task)
        if snapshot is None:
            continue
        candidates.append(
            GateRecipeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                lane=task.lane,
                recipe=RECIPE_AUTO_ADOPT_PLAN,
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


def _post_auto_adopt_comment(ticket_id: str, snapshot: dict[str, object]) -> None:
    """Post the auto-adopt audit comment to the ticket (best-effort, logged).

    Mirrors :func:`_post_auto_approve_comment` exactly (same ``gh issue
    comment`` subprocess call, same best-effort log-on-failure behavior),
    formatting the plan template with the two marker-version strings.
    """
    body = _AUTO_ADOPT_COMMENT_TEMPLATE.format(
        recipe=RECIPE_AUTO_ADOPT_PLAN,
        plan_spec_reviewed=snapshot[_SNAPSHOT_KEY_SPEC],
        plan_soundness_reviewed=snapshot[_SNAPSHOT_KEY_SOUNDNESS],
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


def _find_blocked_task(
    store: DevQueueStore, ticket_id: str, client: str
) -> TicketTask | None:
    """Resolve the (ticket_id, client) row this recipe acts on.

    Mirrors :func:`dev_queue._find_ticket`'s tie-break for the BLOCKED_ON_USER
    tier only (newest ``created_at`` wins) — this recipe never needs the
    PENDING/RUNNING/terminal tiers, since it exclusively operates on
    BLOCKED_ON_USER rows. A hand-rolled ``next()`` with no tie-break (the
    original shape of both call sites below) would, on the same duplicate-row
    condition ``_find_ticket`` itself guards against, risk resolving a
    *different* physical row than the one ``_approve_ticket_locked``
    (internally, via the real ``_find_ticket``) just acted on — silently
    latching or re-validating the wrong row. Returns ``None`` (rather than
    ``_find_ticket``'s raise) since every caller here treats a missing row as
    a silent skip, not an error.

    Deliberately NOT full parity with ``_find_ticket``: a duplicate row in a
    *live* status (PENDING/RUNNING) for the same key is out of scope here —
    ``_find_ticket``'s own live-tier precedence would resolve that case
    inside ``_approve_ticket_locked`` instead, which then rejects it with
    ``ApproveGateError`` (status not approvable). That failure is caught by
    this module's own ``except CwError`` and turned into a
    ``GATE_AUTO_APPROVE_FAILED`` correction + latch — fails safe, not silent.
    """
    matches = [
        t
        for t in store.tasks
        if t.ticket_id == ticket_id
        and t.client == client
        and t.status == QueueItemStatus.BLOCKED_ON_USER
    ]
    if not matches:
        return None
    return max(matches, key=lambda t: t.created_at)


def _stamp_gate_recipe_failure(ticket_id: str, client: str, *, now: datetime) -> None:
    """Persist the one-shot failure latch (GitHub #1065).

    A fresh load/save round-trip, independent of the caller's outer ``store``
    snapshot: the caller (:func:`_act_auto_approve_review`) holds a
    pre-loop-hoisted snapshot that other candidates in the same loop may have
    already made stale via their own successful (separately-persisted)
    approve, so writing through that stale snapshot here would silently
    revert those. Caller MUST already hold ``dev_queue_lock()``, so this
    load-then-save is race-free against any other writer.
    """
    store = load_dev_queue()
    task = _find_blocked_task(store, ticket_id, client)
    if task is None:
        return
    task.gate_recipe_failed_at = now
    save_dev_queue(store)


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
            task = _find_blocked_task(store, candidate.ticket_id, candidate.client)
            if task is None:
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
            except CwError as exc:
                # The GATE_AUTO_APPROVED event above is already durable, but
                # the mutation didn't land (e.g. a duplicate row resolved to a
                # different task, or the client's pipeline config changed
                # between detect and here). Skip rather than let this escape
                # uncaught: an uncaught raise here would abort the rest of
                # this reconcile tick (including run_escalation_sweep and
                # every other still-valid candidate) and, via callers that
                # don't wrap reconcile() in a broad except (e.g. cw status),
                # surface as a crash to unrelated CLI commands. Also emit a
                # durable, operator-forwarded correction: without it,
                # GATE_AUTO_APPROVED would stand alone on the operator
                # channel as an uncorrected false-positive "approved" signal
                # (a log line alone isn't queryable via the event stream).
                # Then stamp the one-shot failure latch so a persisting
                # failure doesn't re-detect and re-emit both events every
                # reconcile tick forever.
                _log.warning(
                    "gate_recipe_approve_failed ticket=%s client=%s",
                    task.ticket_id,
                    task.client,
                    exc_info=True,
                )
                record_event(
                    OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
                    {
                        "ticket_id": task.ticket_id,
                        "client": task.client,
                        "lane": task.lane,
                        "session_id": session.id,
                        "recipe": RECIPE_AUTO_APPROVE_REVIEW,
                        "error": str(exc),
                    },
                    correlation_id=task.ticket_id,
                )
                _stamp_gate_recipe_failure(task.ticket_id, task.client, now=now)
                continue
            approved.append(task.ticket_id)
            comment_jobs.append((task.ticket_id, snapshot))
    for ticket_id, snapshot in comment_jobs:
        _post_auto_approve_comment(ticket_id, snapshot)
    return approved


def _act_auto_adopt_plan(
    candidates: list[GateRecipeCandidate], *, now: datetime
) -> list[str]:
    """Act phase for auto_adopt_clean_plan: in-memory re-check, emit, approve.

    Mirrors :func:`_act_auto_approve_review` with one deliberate divergence
    (R5): the re-check under ``dev_queue_lock()`` reads ONLY already-loaded
    in-memory state — the row is still BLOCKED_ON_USER, its session still
    resolves, and ``session.last_result`` is still at the
    ``plan_pending_approval`` gate. It does NOT re-run
    :func:`_plan_of_record_body`/:func:`_clean_plan_snapshot`: the plan-of-
    record read is a ~30s ``gh`` subprocess, and the signoff markers are
    append-only, so cleanliness established at detect cannot regress between
    detect and act. Only task/session state can. ``candidate.evidence`` (the
    detect-time snapshot) is reused directly as the event's
    ``predicate_snapshot`` and the audit-comment source.
    """
    if not candidates:
        return []
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    approved: list[str] = []
    comment_jobs: list[tuple[str, dict[str, object]]] = []
    with dev_queue_lock():
        # Both loaded once, unlike the sibling _act_auto_approve_review (which
        # reloads state per candidate): that function re-derives a fresh
        # predicate snapshot from session.last_result on every iteration, so a
        # stale state read would matter. This function's R5 recheck only
        # reads already-loaded task/session fields and never re-derives the
        # predicate, so a per-candidate reload buys no correctness benefit
        # while extending the exclusive dev_queue_lock() hold time.
        store = load_dev_queue()
        state = load_state()
        for candidate in by_key.values():
            task = _find_blocked_task(store, candidate.ticket_id, candidate.client)
            if task is None:
                continue
            if task.session_id is None:
                continue
            session = state.find_by_name_or_id(task.session_id)
            if session is None:
                continue
            # In-memory re-check only (R5): no plan-of-record re-fetch. The
            # markers are append-only, so only task/session state can have
            # changed since detect.
            last_result = session.last_result
            if (
                not isinstance(last_result, dict)
                or last_result.get("status") != _PLAN_PENDING_APPROVAL
            ):
                continue
            snapshot = candidate.evidence
            record_event(
                OrchestratorEventType.GATE_AUTO_APPROVED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "lane": task.lane,
                    "session_id": session.id,
                    "recipe": RECIPE_AUTO_ADOPT_PLAN,
                    "predicate_snapshot": snapshot,
                    "approved_at": now.isoformat(),
                },
                correlation_id=task.ticket_id,
            )
            try:
                _approve_ticket_locked(task.ticket_id, task.client)
            except CwError as exc:
                _log.warning(
                    "gate_recipe_approve_failed ticket=%s client=%s",
                    task.ticket_id,
                    task.client,
                    exc_info=True,
                )
                record_event(
                    OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
                    {
                        "ticket_id": task.ticket_id,
                        "client": task.client,
                        "lane": task.lane,
                        "session_id": session.id,
                        "recipe": RECIPE_AUTO_ADOPT_PLAN,
                        "error": str(exc),
                    },
                    correlation_id=task.ticket_id,
                )
                _stamp_gate_recipe_failure(task.ticket_id, task.client, now=now)
                continue
            approved.append(task.ticket_id)
            comment_jobs.append((task.ticket_id, snapshot))
    for ticket_id, snapshot in comment_jobs:
        _post_auto_adopt_comment(ticket_id, snapshot)
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

    # Review-then-plan order (R6), matching the constant declaration order. A
    # task sits at exactly one gate at a time, so review-approved rows are
    # never plan candidates; plan act re-loads under its own lock.
    approved = _act_auto_approve_review(
        _detect_auto_approve_review(state, tasks), now=now
    )
    approved += _act_auto_adopt_plan(_detect_auto_adopt_plan(state, tasks), now=now)
    return approved
