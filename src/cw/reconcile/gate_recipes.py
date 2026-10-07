"""Daemon-side gate recipes: mechanical gate-clearing reactor (RFC 0009).

Gate recipes are the automation layer that clears an approval gate a human
would otherwise have to clear by hand, but only when a fixed, verifiable
predicate holds. Unlike the concierge reactor (``cw.reconcile.concierge``),
which *recovers* rows stuck behind dead sessions, gate recipes *advance* a live
approval gate — so they sit behind their own master switch
(``OrchestratorConfig.gate_recipes_enabled``, default True) and forward their
audit event to the operator channel as a record of what was released.

**P1+P2 scope (GitHub #1065):** the ``auto_approve_clean_review`` recipe. It
auto-approves a ``review_pending_approval`` gate when the review's health
recommendation is PROCEED, no forbidden area was touched, and at least one
reviewer agent actually ran (``agents_run > 0``).

**P3 scope (GitHub #1066):** the ``auto_adopt_clean_plan`` recipe. It
auto-adopts a ``plan_pending_approval`` gate.

**Size alone never pages the operator.** Both recipes are on by default and
release a Large gate unless a predicate in :mod:`cw.reconcile.gate_predicates`
(which documents the full policy) names a reason a person is needed. The plan
recipe does not require the two signoff markers — a Large park runs its review
stations in advisory mode and never writes them — so an unreviewed plan is
released through ``_approve_ticket_locked``'s #968 same-stage requeue: the
row-path approval stamped there lets the re-dispatched plan stage pass
Checkpoint 1 and run Plan Quality Review (the ambiguity scan already ran in
the round that parked), which can still park the plan. A reviewed plan advances
to IMPL directly, as before. The plan-of-record read (``gh``/``git``) runs in
a lockless pre-pass, never under ``sessions_lock`` (#2545; see
:mod:`cw.reconcile.gate_plan_probes`).

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

**Invariant (GitHub #1199):** cw never grants a GitHub pull-request review
approval — no ``gh pr review --approve``, no GraphQL mutation that adds a
pull-request review with an approving event, and no REST reviews-endpoint
call with an approving event exists anywhere in ``src/``, and this
module's own ``auto_approve_clean_review`` recipe does not touch GitHub
review state at all — it advances only cw's internal dev-queue gate via
``_approve_ticket_locked``. See ADR-0012 and
``tests/test_review_approval_guard.py`` for the exact call shapes this
invariant covers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from cw.config import load_effective_clients, load_state
from cw.dev_queue import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
    BRANCH_STALENESS_GATE_DISPOSITION,
    REVIEW_STALENESS_GATE_DISPOSITION,
    _approve_ticket_locked,
    _local_plan_body,
    _marker_version,
    _newest_by_created_at,
    _plan_body_signoff_ok,
    _tracker_allows_github_fetch,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.gh import fetch_approved_plan_comment, post_issue_comment
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.queue_rows import resolve_hold_finalize
from cw.reconcile.gate_plan_probes import PlanProbeUnavailableError, lookup_plan_probe
from cw.reconcile.gate_predicates import (
    _PLAN_PENDING_APPROVAL,
    _REVIEW_PENDING_APPROVAL,
    _SNAPSHOT_KEY_FILES,
    _SNAPSHOT_KEY_FINGERPRINT,
    _SNAPSHOT_KEY_FORBIDDEN,
    _SNAPSHOT_KEY_LINES,
    _SNAPSHOT_KEY_REVIEWED,
    _SNAPSHOT_KEY_SOUNDNESS,
    _SNAPSHOT_KEY_SPEC,
    _SNAPSHOT_KEY_TIER,
    _clean_review_snapshot,
    _plan_gate_snapshot,
    _plan_predicate_holds,
    _predicate_holds,
    _row_eligible,
)
from cw.reconcile.gate_recipe_comments import (
    AUTO_ADOPT_COMMENT_TEMPLATE,
    AUTO_APPROVE_COMMENT_TEMPLATE,
    defer_gate_recipe_comment_jobs,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorConfig,
        Session,
        TicketTask,
    )
    from cw.reconcile.deferred import DeferredReconcileJobs
    from cw.reconcile.gate_plan_probes import PlanBodySource, PlanProbes

_log = logging.getLogger(__name__)
_PLAN_PREFETCH_MISS_LOG = (
    "gate recipe: no usable plan prefetch for %s/%s; skipping this tick (%s)"
)

# Recipe name constants — the recognised gate-recipe keys. Only the review
# recipe is wired in P1+P2 (#1065); RECIPE_AUTO_ADOPT_PLAN is defined now so
# both keys have one home, but its detect/act land in P3 (#1066).
# NOTE (#1199): "auto_approve" here means cw's internal dispatch gate only —
# never a GitHub PR review approval. See the module docstring and ADR-0012.
RECIPE_AUTO_APPROVE_REVIEW = "auto_approve_clean_review"
RECIPE_AUTO_ADOPT_PLAN = "auto_adopt_clean_plan"

# RFC 0009 P4 (#1067) — tier-3 hardcoded fallback for the per-lane resolver.
# Both recipes default ON: the operator is paged for a product or scope
# question, never for a ticket's size alone (see the module docstring). A lane
# or ticket opts back into manual gating with ``gate_recipes: {<recipe>:
# false}``; ``gate_recipes_enabled: false`` turns both off globally. NOT a
# config field — it is the floor the ticket/lane tiers fall through to.
_DEFAULT_GATE_RECIPE_ENABLED: dict[str, bool] = {
    RECIPE_AUTO_APPROVE_REVIEW: True,
    RECIPE_AUTO_ADOPT_PLAN: True,
}


def resolve_gate_recipe_enabled(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    recipe_name: str,
) -> bool:
    """Return whether *recipe_name* is enabled for *task*, per RFC 0009 P4.

    3-tier precedence, highest first (mirrors resolve_signoff's shape and
    resolve_concierge_recipe_enabled's per-recipe .get fallback):

    1. ``task.gate_recipes`` — ticket-level override wins when it names the
       recipe.
    2. ``LaneConfig.gate_recipes`` on the task's lane — the per-lane map.
    3. ``_DEFAULT_GATE_RECIPE_ENABLED`` — the hardcoded floor (both on).

    Robust to a missing client (absent from *clients*) or a missing lane
    (absent from the client's ``effective_lanes``): either falls straight
    through to the default with no exception.
    """
    if task.gate_recipes is not None and recipe_name in task.gate_recipes:
        return task.gate_recipes[recipe_name]
    client_cfg = clients.get(task.client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if (
                lane_cfg.name == task.lane
                and lane_cfg.gate_recipes is not None
                and recipe_name in lane_cfg.gate_recipes
            ):
                return lane_cfg.gate_recipes[recipe_name]
    # .get(..., False): a recipe_name outside _DEFAULT_GATE_RECIPE_ENABLED
    # (i.e. not one of the two RECIPE_* constants) falls through to the safe
    # default instead of raising KeyError, matching this function's documented
    # no-exception robustness guarantee for every other unresolved input.
    return _DEFAULT_GATE_RECIPE_ENABLED.get(recipe_name, False)


def _recipe_gate_open(
    config: OrchestratorConfig,
    task: TicketTask,
    clients: dict[str, ClientConfig],
    recipe_name: str,
) -> bool:
    """Return whether *recipe_name* may fire for *task* right now.

    Composes the master switch with the per-lane/per-ticket resolution so
    both ``_detect_*`` functions share one gating check instead of drifting
    copies. Why: redundant with ``run_gate_recipes``'s top-level short-circuit
    on ``config.gate_recipes_enabled`` — needed here too so a caller invoking
    ``_detect_*`` directly (unit tests) still gets correct gating.
    """
    return config.gate_recipes_enabled and resolve_gate_recipe_enabled(
        task, clients, recipe_name
    )


# The two signoff markers auto-dev-plan appends to the plan-of-record body.
# Canonical definition now lives in cw.dev_queue.lifecycle (#1567) — imported
# above rather than redefined here. _PLAN_SPEC_MARKER still mirrors
# gh._PLAN_MARKER, a genuinely separate definition in a different module;
# test_plan_spec_marker_matches_gh_marker continues to guard that drift.


@dataclass(frozen=True)
class GateRecipeCandidate:
    """Classification result from a gate recipe's detect phase.

    Same shape as :class:`cw.reconcile.concierge.ConciergeCandidate` plus a
    ``lane`` field: the ``GATE_AUTO_APPROVED`` event payload carries the row's
    lane, so the candidate captures it at detect time. ``evidence`` carries the
    ``predicate_snapshot`` — the exact five field values that licensed the fire
    (``must_fix_initial``, ``deferred``, ``recommendation``,
    ``forbidden_touched``, ``agents_run``), read off ``session.last_result``.

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


def _finalize_hold_armed(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> bool:
    """True iff an RFC 0011 A3 proactive finalize hold is armed for *task*.

    The review recipe's automatic approve always declines on a held row
    (``_approve_ticket_locked`` returns ``finalize_held``), so such a row is
    never "released": routing must page for it, and detect must not pick it up
    every tick only to emit another ``GATE_AUTO_APPROVE_HELD``. The policy
    resolver lives in the ``cw.queue_rows`` leaf (#2613), so it is imported at
    module scope rather than reached through ``cw.dispatch``.
    """
    return resolve_hold_finalize(task, clients, config) is not None


def gate_recipe_will_release(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> bool:
    """Whether a gate recipe will release *task*'s pending approval gate.

    Called by dispatch routing (Rule 1) at park time, so it does not page the
    operator about a gate the next reconcile tick clears without them. Pure
    over the sentinel and the row: it shares each recipe's predicate and
    enablement check but makes no tracker call, because whether the plan was
    already reviewed changes how the plan recipe releases the gate, not
    whether it does. A recipe whose act phase later fails emits the
    operator-forwarded ``GATE_AUTO_APPROVE_FAILED``, so a suppressed page is
    never a silent park.
    """
    if not isinstance(last_result, dict):
        return False
    status = last_result.get("status")
    if status == _REVIEW_PENDING_APPROVAL:
        review_snapshot = _clean_review_snapshot(last_result)
        return (
            review_snapshot is not None
            and _row_eligible(task, Stage.REVIEW)
            and _predicate_holds(review_snapshot)
            and _recipe_gate_open(config, task, clients, RECIPE_AUTO_APPROVE_REVIEW)
            and not _finalize_hold_armed(task, clients, config)
        )
    if status == _PLAN_PENDING_APPROVAL:
        plan_snapshot = _plan_gate_snapshot(last_result)
        return (
            plan_snapshot is not None
            and _row_eligible(task, Stage.PLAN)
            and _plan_predicate_holds(plan_snapshot, task)
            and _recipe_gate_open(config, task, clients, RECIPE_AUTO_ADOPT_PLAN)
        )
    return False


def _plan_of_record_body(
    task: TicketTask, client_cfg: ClientConfig | None
) -> str | None:
    """Return the plan-of-record body, tracker-first with a `.cw/plan.md` fallback.

    Why tracker-first (opposite of local_runner.build_task_message's
    .cw-first order): that function fills a local cache for Stage-2 task
    prompts; this gate checks *current* approval freshness, for which the
    tracker is the authoritative, freshest source. The GitHub-only tracker
    read is skipped when ``_tracker_allows_github_fetch`` says the client's
    tracker is positively non-GitHub (a ``gh`` call against a Linear id can
    only fail, and every reconcile tick would pay for it, #1906). Falls back
    to the worktree's ``.cw/plan.md`` via ``_local_plan_body`` -- which
    resolves the real branch-derived worktree for dispatch-driven rows whose
    ``worktree_path`` is never stamped -- only when the tracker read returns
    None. Every miss or read failure degrades to None rather than
    propagating: an unhandled exception here would abort the entire
    reconcile tick, including the unrelated auto_approve_clean_review recipe
    processed in the same run_gate_recipes() call. Runs gh/git, so it is
    called only from the lockless :func:`capture_plan_probes` (#2545).
    """
    if _tracker_allows_github_fetch(client_cfg):
        body = fetch_approved_plan_comment(task.ticket_id)
        if body is not None:
            return body
    return _local_plan_body(task, client_cfg)


def _adoptable_plan_snapshot(
    session: Session, task: TicketTask
) -> tuple[dict[str, object], str] | None:
    """Pure: the plan-gate snapshot and its fingerprint, or None if not fireable."""
    snapshot = _plan_gate_snapshot(session.last_result)
    if snapshot is None or not _plan_predicate_holds(snapshot, task):
        return None
    fingerprint = snapshot[_SNAPSHOT_KEY_FINGERPRINT]
    return (snapshot, fingerprint) if isinstance(fingerprint, str) else None


def _clean_plan_snapshot(
    snapshot: dict[str, object], body: str | None
) -> dict[str, object]:
    """Fold the plan-of-record *body*'s signoff reading into *snapshot*.

    The plan-of-record read decides only *how* the gate is released, recorded
    as ``plan_reviewed``: a plan whose body carries BOTH signoff markers
    advances to IMPL, and any other plan goes back to PLAN through the #968
    same-stage requeue for Plan Quality Review. Both markers are read from
    the SAME body (R2 — there is exactly one ``body`` variable in scope, so
    same-source is structural); a union across tracker + `.cw/plan.md` is
    impossible by construction. The raw plan body is never placed in the
    snapshot, the event payload, or the audit comment.
    """
    reviewed = body is not None and _plan_body_signoff_ok(body)
    snapshot[_SNAPSHOT_KEY_REVIEWED] = reviewed
    snapshot[_SNAPSHOT_KEY_SPEC] = (
        _marker_version(body, marker=_PLAN_SPEC_MARKER)
        if reviewed and body is not None
        else None
    )
    snapshot[_SNAPSHOT_KEY_SOUNDNESS] = (
        _marker_version(body, marker=_PLAN_SOUNDNESS_MARKER)
        if reviewed and body is not None
        else None
    )
    return snapshot


def _detect_auto_approve_review(
    state: CwState,
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
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

    A row parked by the #1823 branch-staleness gate or the #2123
    review-staleness gate is excluded outright, however clean its sentinel
    reads — see the inline notes at those guards.
    """
    candidates: list[GateRecipeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        if task.gate_recipe_failed_at is not None:
            continue
        # #1823: the five-field clean-review predicate below is derived
        # entirely from session.last_result, which the branch-staleness gate
        # never mutates -- it diverges only task.disposition. So a
        # staleness-parked row satisfies every other condition here and would
        # be auto-approved on any lane with this recipe enabled, silently
        # shipping a tree that no longer matches origin/<default_branch>.
        # Excluded on disposition, mirroring dev_queue.approval's own
        # fail-closed override for the manual `approve` path.
        if task.disposition == BRANCH_STALENESS_GATE_DISPOSITION:
            continue
        # #2123: the same exclusion, and the load-bearing one for this ticket.
        # A stale-artifact row satisfies all five clean-review fields BECAUSE
        # those numbers describe the tree the reviewers actually saw -- a
        # different tree from the one that would ship. Auto-approving it is
        # precisely the incident: clean numbers, wrong tree, nobody looking.
        if task.disposition == REVIEW_STALENESS_GATE_DISPOSITION:
            continue
        if not _row_eligible(task, Stage.REVIEW):
            continue
        if _finalize_hold_armed(task, clients, config):
            continue
        if task.session_id is None:
            continue
        session = state.find_by_name_or_id(task.session_id)
        if session is None:
            continue
        snapshot = _clean_review_snapshot(session.last_result)
        if snapshot is None or not _predicate_holds(snapshot):
            continue
        if not _recipe_gate_open(config, task, clients, RECIPE_AUTO_APPROVE_REVIEW):
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
    state: CwState,
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    body_source: PlanBodySource,
) -> list[GateRecipeCandidate]:
    """Read-only classification phase for auto_adopt_clean_plan. Zero writes.

    Mirrors :func:`_detect_auto_approve_review`'s guard chain (BLOCKED_ON_USER,
    ``gate_recipe_failed_at`` latch None, resolvable session) but swaps the
    clean-review snapshot for a clean-plan snapshot: the row's owning session
    must sit at the ``plan_pending_approval`` gate and
    :func:`_plan_predicate_holds` must pass, and the recipe must be enabled
    for the row. Only then is the plan-of-record body (it decides only whether
    the plan is already reviewed) asked of *body_source*, which has no default
    (#2545): a live capture in :func:`capture_plan_probes`, a subprocess-free
    lookup in :func:`run_gate_recipes`. A lookup with no usable body skips the
    candidate for this tick, never read live in-lock nor acted on off a clock.
    """
    candidates: list[GateRecipeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        if task.gate_recipe_failed_at is not None:
            continue
        if not _row_eligible(task, Stage.PLAN):
            continue
        if task.session_id is None:
            continue
        session = state.find_by_name_or_id(task.session_id)
        if session is None:
            continue
        adoptable = _adoptable_plan_snapshot(session, task)
        if adoptable is None:
            continue
        if not _recipe_gate_open(config, task, clients, RECIPE_AUTO_ADOPT_PLAN):
            continue
        snapshot, fingerprint = adoptable
        try:
            body = body_source(task, fingerprint)
        except PlanProbeUnavailableError as err:
            _log.warning(_PLAN_PREFETCH_MISS_LOG, task.client, task.ticket_id, err)
            continue
        candidates.append(
            GateRecipeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                lane=task.lane,
                recipe=RECIPE_AUTO_ADOPT_PLAN,
                evidence=_clean_plan_snapshot(snapshot, body),
                session_id=task.session_id,
            )
        )
    return candidates


def capture_plan_probes(
    state: CwState,
    tasks: list[TicketTask],
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    probes: PlanProbes,
) -> None:
    """Lockless pre-pass: read every plan candidate's plan-of-record (#2545).

    Re-runs the plan detect with *probes*' capture as the body source, so the
    live read happens only for a row the detect would act on, by construction.
    The candidates are discarded; *probes* keeps the bodies. Runs gh/git:
    never call it with ``sessions_lock`` held.
    """
    live = partial(
        probes.capture, read=lambda t: _plan_of_record_body(t, clients.get(t.client))
    )
    _detect_auto_adopt_plan(
        state, tasks, clients=clients, config=config, body_source=live
    )


def _post_auto_approve_comment(
    ticket_id: str, snapshot: dict[str, object], *, cwd: Path | None = None
) -> None:
    """Post the auto-approve audit comment to the ticket (best-effort, logged).

    A distinct helper from ``codex_background._post_review_comment``: that one
    swallows failures with zero logging, whereas the ticket's OQ2 resolution
    requires a comment-write failure to be logged (the event remains the
    source-of-truth audit trail — a failed comment never undoes the approve).

    *cwd* scopes the gh call to the client's repo (GitHub #1269/#1279).
    """
    body = AUTO_APPROVE_COMMENT_TEMPLATE.format(
        recipe=RECIPE_AUTO_APPROVE_REVIEW,
        must_fix_initial=snapshot["must_fix_initial"],
        deferred=snapshot["deferred"],
        recommendation=snapshot["recommendation"],
        forbidden_touched=snapshot["forbidden_touched"],
        agents_run=snapshot["agents_run"],
    )
    result = post_issue_comment(ticket_id, body, cwd=cwd)
    if result is None:
        _log.warning("gate_recipe_comment_failed ticket=%s: gh call failed", ticket_id)
        return
    if result.returncode != 0:
        _log.warning(
            "gate_recipe_comment_failed ticket=%s rc=%s: %s",
            ticket_id,
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )


def _post_auto_adopt_comment(
    ticket_id: str, snapshot: dict[str, object], *, cwd: Path | None = None
) -> None:
    """Post the auto-adopt audit comment to the ticket (best-effort, logged).

    Mirrors :func:`_post_auto_approve_comment` exactly (same ``gh issue
    comment`` subprocess call, same best-effort log-on-failure behavior),
    formatting the plan template with the predicate snapshot's fields.

    *cwd* scopes the gh call to the client's repo (GitHub #1269/#1279).
    """
    # Explicit named args, not **snapshot: this writes to a public,
    # append-only GitHub comment, so the fields exposed there must stay
    # grep-able and reviewable at this call site. Unpacking the full
    # snapshot dict would auto-expose any future key added to
    # _clean_plan_snapshot with no code change here to acknowledge the new
    # public disclosure, and would raise TypeError if a future key ever
    # collided with the `recipe=` kwarg.
    body = AUTO_ADOPT_COMMENT_TEMPLATE.format(
        recipe=RECIPE_AUTO_ADOPT_PLAN,
        tier=snapshot.get(_SNAPSHOT_KEY_TIER),
        files=snapshot.get(_SNAPSHOT_KEY_FILES),
        lines_estimate=snapshot.get(_SNAPSHOT_KEY_LINES),
        forbidden_touched=snapshot.get(_SNAPSHOT_KEY_FORBIDDEN),
        plan_draft_fingerprint=snapshot.get(_SNAPSHOT_KEY_FINGERPRINT),
        plan_reviewed=snapshot.get(_SNAPSHOT_KEY_REVIEWED),
    )
    result = post_issue_comment(ticket_id, body, cwd=cwd)
    if result is None:
        _log.warning("gate_recipe_comment_failed ticket=%s: gh call failed", ticket_id)
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
    return _newest_by_created_at(matches)


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


def _handle_gate_recipe_approve_failure(
    task: TicketTask,
    session: Session,
    recipe: str,
    exc: CwError,
    *,
    now: datetime,
) -> None:
    """Log, emit a durable GATE_AUTO_APPROVE_FAILED correction, and stamp the
    one-shot failure latch (GitHub #1065/#1570).

    Shared body of both act phases' ``except CwError`` blocks. The mutation
    is caught and swallowed here, rather than left to propagate, because an
    uncaught raise would abort the rest of this reconcile tick (including
    ``run_escalation_sweep`` and every other still-valid candidate) and, via
    callers that don't wrap ``reconcile()`` in a broad except (e.g. ``cw
    status``), surface as a crash to unrelated CLI commands.

    The GATE_AUTO_APPROVED event already recorded before the mutation is
    durable, but without this correction it would stand alone on the
    operator channel as an uncorrected false-positive "approved" signal (a
    log line alone isn't queryable via the event stream). Stamping the
    one-shot failure latch then keeps a persisting failure from re-detecting
    and re-emitting both events every reconcile tick forever. Caller keeps
    its own ``continue``; this helper performs no control-flow.
    """
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
            "recipe": recipe,
            "error": str(exc),
        },
        correlation_id=task.ticket_id,
    )
    _stamp_gate_recipe_failure(task.ticket_id, task.client, now=now)


def _act_auto_approve_review(
    candidates: list[GateRecipeCandidate],
    *,
    now: datetime,
    clients: dict[str, ClientConfig] | None = None,
    deferred: DeferredReconcileJobs,
) -> list[str]:
    """Act phase: re-validate under lock, emit, then approve via the primitive.

    For each candidate the row + session are re-loaded fresh under
    ``dev_queue_lock()`` and the five-field predicate re-checked — a concurrent
    human approve, re-dispatch, or new sentinel between detect and act can have
    invalidated it (the re-check race). Only a still-valid candidate fires:
    :class:`OrchestratorEventType.GATE_AUTO_APPROVED` is emitted BEFORE the
    mutation, then the lock-free :func:`_approve_ticket_locked` advances the
    gate exactly as a human ``approve_ticket`` call would. Event payload
    sources come from the re-loaded row/session, never the (possibly stale)
    detect-time candidate. The audit comment is queued on *deferred*, the
    caller's post-lock sink, and posted after ``reconcile()`` releases
    ``sessions_lock`` (#1232), best-effort — a comment-write failure never
    undoes the approve.

    A third outcome exists alongside "approved" and "raised" (RFC 0011 A3,
    #1160): the row may carry an armed proactive finalize hold, in which case
    :func:`_approve_ticket_locked` — called here WITHOUT ``operator_initiated``,
    i.e. as the automatic caller it is — declines to mutate and returns
    ``finalize_held=True``. That is not a failure, so it does not stamp the
    ``gate_recipe_failed_at`` latch; but ``GATE_AUTO_APPROVED`` is already
    durable by then, so a ``GATE_AUTO_APPROVE_HELD`` correction is emitted and
    the ticket is neither reported approved nor commented on.

    Only a hold armed inside the detect→act race reaches this branch: detect
    skips a row whose hold is already armed (:func:`_finalize_hold_armed`).
    """
    if not candidates:
        return []
    # Keyed on (ticket_id, client): ticket_id alone is a per-repo GitHub issue
    # number, not globally unique across this multi-tenant system's clients —
    # keying on ticket_id alone would let two different clients' candidates
    # that happen to share a ticket_id collide and silently drop one.
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    approved: list[str] = []
    # (ticket_id, client, snapshot): client name is carried across the
    # dev_queue_lock boundary so the R7 dangling check runs against this
    # tick's `clients` snapshot when the comment is queued rather than the
    # detect-time candidate, matching how every other field here is
    # re-validated rather than taken from detect-time state (GitHub #1279).
    comment_jobs: list[tuple[str, str, dict[str, object]]] = []
    with dev_queue_lock():
        # Loaded once: dev_queue_lock() is the exclusive writer lock for this
        # file, so no concurrent process can change it mid-loop, and every
        # mutation this loop performs goes through _approve_ticket_locked's
        # own internal load/save round-trip rather than this snapshot.
        store = load_dev_queue()
        for candidate in by_key.values():
            state = load_state()
            task = _find_blocked_task(store, candidate.ticket_id, candidate.client)
            if task is None or not _row_eligible(task, Stage.REVIEW):
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
                # RFC 0009 / #1083: pin the mutation to THIS validated row's
                # identity so _approve_ticket_locked cannot re-resolve to a
                # newer AWAITING_OPERATOR_SIGNOFF duplicate and clear a signoff
                # gate this recipe never checked.
                result = _approve_ticket_locked(
                    task.ticket_id, task.client, resolved_task=task
                )
            except CwError as exc:
                _handle_gate_recipe_approve_failure(
                    task, session, RECIPE_AUTO_APPROVE_REVIEW, exc, now=now
                )
                continue
            if result["finalize_held"]:
                # RFC 0011 A3 (#1160): the row's proactive finalize hold
                # declined this automatic approve. Nothing was mutated and
                # nothing is broken, so no failure latch is stamped -- but the
                # already-durable GATE_AUTO_APPROVED needs a correction, or it
                # stands alone on the operator channel as a false "approved".
                _log.info(
                    "gate_recipe_approve_held ticket=%s client=%s",
                    task.ticket_id,
                    task.client,
                )
                record_event(
                    OrchestratorEventType.GATE_AUTO_APPROVE_HELD,
                    {
                        "ticket_id": task.ticket_id,
                        "client": task.client,
                        "lane": task.lane,
                        "session_id": session.id,
                        "recipe": RECIPE_AUTO_APPROVE_REVIEW,
                    },
                    correlation_id=task.ticket_id,
                )
                continue
            approved.append(task.ticket_id)
            comment_jobs.append((task.ticket_id, task.client, snapshot))
    defer_gate_recipe_comment_jobs(
        comment_jobs,
        clients,
        _post_auto_approve_comment,
        recipe=RECIPE_AUTO_APPROVE_REVIEW,
        deferred=deferred,
    )
    return approved


def _act_auto_adopt_plan(
    candidates: list[GateRecipeCandidate],
    *,
    now: datetime,
    clients: dict[str, ClientConfig] | None = None,
    deferred: DeferredReconcileJobs,
) -> list[str]:
    """Act phase for auto_adopt_clean_plan: in-memory re-check, emit, approve.

    Mirrors :func:`_act_auto_approve_review` with one deliberate divergence
    (R5): the re-check under ``dev_queue_lock()`` reads ONLY already-loaded
    in-memory state — the row is still BLOCKED_ON_USER and eligible, its
    session still resolves, and ``session.last_result`` still passes the pure
    :func:`_plan_gate_snapshot`/:func:`_plan_predicate_holds` pair. It does
    NOT re-run :func:`_plan_of_record_body`: the plan-of-record read is a
    ~30s ``gh`` subprocess that never runs under ``sessions_lock`` (#2545).
    The signoff markers are append-only per comment, but the newest trusted
    comment can change between capture and act; that window is bounded by
    the 120 s probe age plus the lock wait, and a later change is picked up
    on the next tick. ``candidate.evidence`` (the detect-time
    snapshot) is reused directly as the event's ``predicate_snapshot`` and
    the audit-comment source, and its ``plan_reviewed`` value selects the
    release path: advance to IMPL, or the #968 requeue back to PLAN. As
    there, the audit comment is queued on *deferred* and posted after the
    lock releases (#1232).
    """
    if not candidates:
        return []
    by_key = {(c.ticket_id, c.client): c for c in candidates}
    approved: list[str] = []
    # (ticket_id, client, snapshot): see _act_auto_approve_review for why the
    # client name is deferred across the lock boundary (GitHub #1279 R7).
    comment_jobs: list[tuple[str, str, dict[str, object]]] = []
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
            if task is None or not _row_eligible(task, Stage.PLAN):
                continue
            if task.session_id is None:
                continue
            session = state.find_by_name_or_id(task.session_id)
            if session is None:
                continue
            # In-memory re-check only (R5): no plan-of-record re-fetch, which
            # would run gh under sessions_lock (#2545). See the docstring for
            # the capture-to-act window this accepts.
            gate_snapshot = _plan_gate_snapshot(session.last_result)
            if gate_snapshot is None or not _plan_predicate_holds(gate_snapshot, task):
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
                # RFC 0009 / #1083: pin the mutation to THIS validated row's
                # identity so _approve_ticket_locked cannot re-resolve to a
                # newer AWAITING_OPERATOR_SIGNOFF duplicate and clear a signoff
                # gate this recipe never checked. plan_reviewed is always an
                # explicit bool (#968), never None, which documents the
                # no-refetch contract: the lockless capture already read the
                # plan-of-record (see capture_plan_probes), so the act phase must not
                # trigger a second live _plan_is_reviewed() fetch. True
                # advances a reviewed plan to IMPL; False sends an unreviewed
                # one back to PLAN for Plan Quality Review.
                _approve_ticket_locked(
                    task.ticket_id,
                    task.client,
                    resolved_task=task,
                    plan_reviewed=snapshot.get(_SNAPSHOT_KEY_REVIEWED) is True,
                )
            except CwError as exc:
                _handle_gate_recipe_approve_failure(
                    task, session, RECIPE_AUTO_ADOPT_PLAN, exc, now=now
                )
                continue
            approved.append(task.ticket_id)
            comment_jobs.append((task.ticket_id, task.client, snapshot))
    defer_gate_recipe_comment_jobs(
        comment_jobs,
        clients,
        _post_auto_adopt_comment,
        recipe=RECIPE_AUTO_ADOPT_PLAN,
        deferred=deferred,
    )
    return approved


def run_gate_recipes(
    *,
    now: datetime,
    config: OrchestratorConfig,
    deferred: DeferredReconcileJobs,
    plan_probes: PlanProbes | None,
) -> list[str]:
    """Run all enabled gate recipes for one reconcile tick.

    No-op (returns ``[]`` immediately) unless ``config.gate_recipes_enabled``
    is True. Loads fresh state/dev-queue snapshots itself rather than accepting
    them from the caller — by the time reconcile's ``_reconcile_locked`` reaches
    this wiring point several prior sweeps have already mutated and saved both
    files, so a caller-supplied snapshot would be stale (mirrors
    ``run_concierge_recoveries``). Safe to call while the caller already holds
    ``sessions_lock`` — this function only acquires ``dev_queue_lock`` per act
    phase, never ``sessions_lock`` itself. It runs no subprocess under that
    lock: each released ticket's audit comment is queued on *deferred*, the
    caller's post-lock sink, and posted after the lock releases (#1232), and
    plan bodies come from *plan_probes*, captured before the lock (#2545).
    It has no default, so no caller falls back to a live read by omission;
    ``None`` skips every plan candidate this tick (review is unaffected).

    Returns the list of ticket IDs auto-approved this tick.
    """
    if not config.gate_recipes_enabled:
        return []

    state = load_state()
    tasks = load_dev_queue().tasks
    # Per-lane enablement (RFC 0009 P4) is resolved against effective clients —
    # load_effective_clients so lane pause/override state is honoured, matching
    # where the scheduler makes per-lane dispatch decisions.
    clients = load_effective_clients()

    # Review-then-plan order (R6), matching the constant declaration order. A
    # task sits at exactly one gate at a time, so review-approved rows are
    # never plan candidates; plan act re-loads under its own lock.
    approved = _act_auto_approve_review(
        _detect_auto_approve_review(state, tasks, clients=clients, config=config),
        now=now,
        clients=clients,
        deferred=deferred,
    )
    approved += _act_auto_adopt_plan(
        _detect_auto_adopt_plan(
            state,
            tasks,
            clients=clients,
            config=config,
            body_source=lookup_plan_probe(plan_probes),
        ),
        now=now,
        clients=clients,
        deferred=deferred,
    )
    return approved
