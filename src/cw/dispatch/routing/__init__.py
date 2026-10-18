"""Sentinel interpretation and staged stage-routing for the dispatch pipeline.

Owns the B2 staged-advance decision table: it reads a session's sentinel
(``AutoDevResult``-derived ``last_result``), classifies its ``stage_reached``
against the task's current stage, walks the stage pointer forward for a
legitimate self-escalation, and applies the Rule 1-6 status routing (scope-gated
approval, pauses, stage success/advance, no-op, failure/regress, and the
conservative fallback). Also resolves the effective scope tier and the
operator-signoff policy, and accumulates per-session cost onto the task.

Package split (#1728, #2719). The historical flat ``routing.py`` was 1345 lines,
~35% over CLAUDE.md's ~1000-line ceiling. Five separable concerns now live in
submodules, re-exported below so no ``from cw.dispatch.routing import X`` call
site changed:

  - ``scope_tier``    -- effective scope-tier resolution and carried-context
    persistence (``_resolve_scope_tier`` & co.).
  - ``stage_walk``    -- ``stage_reached`` classification and the forward
    stage-pointer walk, including its REVIEW-rung gate ladder
    (``_resolve_stage_walk`` & co.).
  - ``pr_refs``       -- the #1713 blocker-``details`` PR cross-reference
    literals and extractor.
  - ``cost``          -- ``_accumulate_task_cost``.
  - ``stage_failure`` -- Rule 5 (``_route_stage_failure``,
    ``_park_must_fix_mechanically_rejected``, ``_breadcrumb_for_reason``) and
    the paused-status constants it writes (#2719).

**Monkeypatch coupling -- why this ``__init__`` is not a pure re-export shim.**
Unlike ``cw.cli``/``cw.reconcile``, whose ``__init__`` files only re-export,
this one *defines* the Rule 1, 3 and 6 decision table itself.
``tests/test_dispatch.py`` monkeypatches ``cw.dispatch.routing.record_event``,
``._stage_advance_unchecked`` and ``._should_gate_for_review_staleness`` by
dotted string path, and a function resolves its free names against the
``__dict__`` of the module it was *defined* in, not the one that calls it (see
``tests/conftest.py``'s ``capture_events`` docstring). Every function that
calls one of those names -- ``_record_scope_routing_decision``,
``_stage_advance_unchecked``, ``_route_scope_gated_approval``,
``_route_stage_success``, ``_route_staged_decision``,
``apply_staged_decision`` -- must therefore stay defined in the module object
bound to the exact dotted path ``cw.dispatch.routing``. Moving any of them into
a submodule would leave the patches pointing at a module the real calls never
consult: green tests observing nothing. That constraint, not line-count
aesthetics, chose the seams above. Rule 5 is the one exception (#2719, the only
way under the ceiling): its free names (``record_event``, ``_stage_regress``,
``resolve_task_worktree``, ``merge_in_progress``) resolve against
``stage_failure``, so its tests patch ``cw.dispatch.routing.stage_failure.<name>``.
``tests/test_dispatch_routing_package.py`` pins both halves as executable
assertions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    PAUSED_FOR_USER_INPUT_STATUSES,
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_SMALL,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
)
from cw.dev_queue import (
    APPROVAL_GATE_REASON,
    _advance_task_pointer,
    _extract_pr_url,
    _hold_aware_disposition,
    transition_task_status,
)
from cw.dispatch.impl_gates import (
    _park_unconcluded_finalize_regress_merge_gate,
    _should_gate_for_unconcluded_finalize_regress_merge,
)
from cw.dispatch.productivity import extract_claim_evidence, is_unproductive
from cw.dispatch.regress_repeat import (
    _consume_finalize_regress_repeat,
    _maybe_emit_finalize_regress_repeat_signal,
)

# The REVIEW-scoped gate table (#1823 extraction). Imported at module top in
# this direction only: review_gates reaches back into this package for
# _resolve_scope_tier / _APPROVAL_GATE_REASON via function-level deferred
# imports, so promoting either of those to a module-top import there recreates
# a real cycle. Same shape as gating/ <-> claim/ since #1310, and as
# routing/stage_walk.py's own deferred reach back into this module (#1728),
# guarded by test_dispatch_package_submodules_import_without_cycle.
from cw.dispatch.review_gates import (
    _gate_recipe_will_release,
    _park_branch_staleness_gate,
    _park_empty_diff_gate,
    _park_finalize_hold,
    _park_review_health_gate,
    _park_review_staleness_gate,
    _park_scope_hint_gate,
    _park_signoff_gate,
    _should_force_hold_finalize,
    _should_gate_for_branch_staleness,
    _should_gate_for_empty_diff,
    _should_gate_for_review_health,
    _should_gate_for_review_staleness,
    _should_gate_for_scope_hint,
    _should_gate_for_signoff,
)
from cw.dispatch.routing.cost import _accumulate_task_cost
from cw.dispatch.routing.pr_refs import (
    _BLOCKING_PR_NUMBER_RE,
    _extract_blocked_on_pr,
)
from cw.dispatch.routing.scope_tier import (
    _extract_scope_tier,
    _persist_carried_context,
    _resolve_scope_tier,
)
from cw.dispatch.routing.stage_failure import (
    _AWAITING_OPERATOR_REASON,
    BREADCRUMB_ELIGIBLE_PAUSED_STATUSES,
    _route_stage_failure,
)
from cw.dispatch.routing.stage_walk import (
    _STAGE_REACHED_TO_STAGE,
    _classify_sentinel_stage_position,
    _is_earlier_stage_report,
    _is_stage_advance_claim,
    _resolve_stage_walk,
    _StagePosition,
    _walk_stage_pointer_forward,
)
from cw.events import record_event
from cw.executor import resolve_pipeline_stages
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.queue_rows import (
    _AUTOMERGE_NOT_ARMED_REASON,
    _PRIOR_PIPELINE_PR_OPEN_REASON,
)

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        TicketTask,
    )

# Re-exported submodule surface. Listed here (rather than left as bare
# imports) because several names are consumed only by *other* modules --
# dispatch/__init__.py's facade, dispatch/loop.py, the historic
# ``cw.dispatch.routing.<name>`` import paths -- and would otherwise read as
# unused. Mirrors
# dispatch/__init__.py's own __all__ convention.
__all__ = [
    "BREADCRUMB_ELIGIBLE_PAUSED_STATUSES",
    "_AUTOMERGE_NOT_ARMED_REASON",
    "_AWAITING_OPERATOR_REASON",
    "_BLOCKING_PR_NUMBER_RE",
    "_PRIOR_PIPELINE_PR_OPEN_REASON",
    "_STAGE_REACHED_TO_STAGE",
    "_StagePosition",
    "_accumulate_task_cost",
    "_classify_sentinel_stage_position",
    "_extract_blocked_on_pr",
    "_extract_scope_tier",
    "_is_earlier_stage_report",
    "_is_stage_advance_claim",
    "_persist_carried_context",
    "_resolve_scope_tier",
    "_resolve_stage_walk",
    "_walk_stage_pointer_forward",
    "apply_staged_decision",
]

_log = logging.getLogger("cw.dispatch")


# ``rule`` values stamped onto the #1617 ``dispatch.scope_routing_decision``
# audit event by each of this package's park-decision sites, plus the
# gate-release site in ``dev_queue.approval`` (imported there, see that
# module's function-level deferred-import convention for the
# dispatch<->dev_queue cycle break). Named constants so a typo at any of the
# ~9 call sites cannot silently break the audit trail's site-attribution.
# ``_RULE_STAGE_WALK`` is consumed by ``routing/stage_walk.py`` through its
# deferred import back into this module.
_RULE_SCOPE_GATED_APPROVAL = "Rule 1"
_RULE_STAGE_SUCCESS = "Rule 3"
_RULE_STAGE_WALK = "stage_walk"
_RULE_GATE_RELEASE = "gate_release"


# paused_status written to SESSION_NEEDS_ATTENTION when a session parks at plan
# stage (ambiguities_pending_resolution / premises_pending_verification).
_PLAN_PARKED_REASON = "plan_parked"


# paused_status written to SESSION_NEEDS_ATTENTION when Rule 1 parks a
# non-small-tier scope-gated approval status (plan_pending_approval /
# review_pending_approval) to BLOCKED_ON_USER. Deliberately distinct from
# _PLAN_PARKED_REASON -- that constant is scoped to the v4
# ambiguities/premises statuses, an unrelated park reason -- per the
# operator's R1 resolution. See GitHub #1302.
_APPROVAL_GATE_REASON = APPROVAL_GATE_REASON


# Disposition stamped by _stage_advance_unchecked when the task's client is
# absent from the effective clients dict — a config error, not a
# transient/recoverable state. Deliberately excluded from both
# concierge._FALSE_PARK_ELIGIBLE_DISPOSITIONS and
# escalation._ELIGIBLE_DISPOSITIONS. See GitHub #976.
_UNKNOWN_CLIENT_REASON = "unknown_client"


# Disposition stamped by _stage_advance_unchecked when task.stage is not in
# the client's configured pipeline stages — a config error, not a
# transient/recoverable state. Same exclusion as _UNKNOWN_CLIENT_REASON. See
# GitHub #976.
_INVALID_STAGE_REASON = "invalid_stage_config"


# paused_status written to SESSION_NEEDS_ATTENTION when Rule 1 parks an
# earlier-stage scope-gated approval sentinel instead of auto-advancing it
# (GitHub #1676 follow-up). Deliberately distinct from _APPROVAL_GATE_REASON
# -- that reason means "tier resolved to large, waiting on an operator";
# this one means "the sentinel never reached task.stage, so its tier verdict
# cannot be trusted to auto-advance regardless of tier."
_EARLIER_STAGE_REPORT_REASON = "earlier_stage_report"


def _record_scope_routing_decision(
    task: TicketTask, last_result: dict[str, object] | None, rule: str
) -> None:
    """Emit the #1617 scope-routing audit event after a park-decision site runs.

    Reads ``task.disposition``/``task.scope_hint`` AFTER the site's mutation
    (or lack of one), so ``disposition`` reflects what actually happened, not
    just what was evaluated. Called unconditionally on every routing pass at
    each of the three ``routing`` park-decision sites (Rule 1, Rule 3, and
    the stage-walk's REVIEW rung, the last of which lives in
    ``routing/stage_walk.py`` and reaches this function through a deferred
    import) -- this is why the event is excluded from
    ``_DEFAULT_OPERATOR_EVENT_TYPES`` (``orchestrator_config/operator_forward.py``): an
    audit/diagnostic trail, not an operator alert, at far higher volume than
    any currently-forwarded member.
    """
    record_event(
        OrchestratorEventType.SCOPE_ROUTING_DECISION,
        {
            "ticket_id": task.ticket_id,
            "client": task.client,
            "scope_hint": task.scope_hint,
            "sentinel_tier": _extract_scope_tier(last_result),
            "resolved_tier": _resolve_scope_tier(last_result, task),
            "rule": rule,
            "disposition": task.disposition,
        },
        correlation_id=task.ticket_id,
    )


def _stage_advance_unchecked(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    *,
    disposition: str | None = None,
    pr_url: str | None = None,
) -> None:
    """Advance task to next pipeline stage, or mark COMPLETED at terminal stage.

    Assert-free: no status precondition. The live consume/reconcile paths reach
    this only through ``apply_staged_decision`` (which asserts RUNNING first);
    the #918 late-sentinel rescue path reaches it through
    ``_route_staged_decision`` for a BLOCKED_ON_USER (idle-parked) task, so the
    RUNNING assert cannot live here. Mirrors ``_advance_task_pointer``'s
    assert-free contract in dev_queue.py.
    """
    client_cfg = clients.get(task.client)
    if client_cfg is None:
        _log.warning(
            "dispatch: advance: client %r not found for task %r -- parking as BLOCKED",
            task.client,
            task.ticket_id,
        )
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": _UNKNOWN_CLIENT_REASON,
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=_UNKNOWN_CLIENT_REASON
        )
        return
    stages = resolve_pipeline_stages(task, client_cfg)
    if task.stage not in stages:
        _log.warning(
            "dispatch: advance: stage %r not in pipeline for task %r",
            task.stage,
            task.ticket_id,
        )
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": _INVALID_STAGE_REASON,
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=_INVALID_STAGE_REASON
        )
        return
    if task.stage == stages[-1]:
        # unproductive=False (#1750): reaching the terminal stage IS the
        # pipeline succeeding. The non-terminal branch below needs no kwarg —
        # _advance_task_pointer hardcodes it at the shared chokepoint.
        transition_task_status(
            task,
            QueueItemStatus.COMPLETED,
            disposition=disposition,
            pr_url=pr_url,
            unproductive=False,
        )
    else:
        _advance_task_pointer(task, stages)


def apply_staged_decision(
    task: TicketTask,
    status: str | None,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> bool:
    """Apply the B2 staged advance decision to a RUNNING task.

    Thin RUNNING-asserting wrapper over ``_route_staged_decision`` (the shared
    assert-free routing core). The consume path (_apply_events_to_store) and the
    RUNNING arm of reconcile's emitted-sentinel router (_apply_sentinel_to_task)
    both call this; the #918 late-sentinel rescue path calls
    ``_route_staged_decision`` directly for a parked task. Precondition:
    task.status is RUNNING. Mutates task in place. Returns ``_route_staged_
    decision``'s bool: whether the sentinel was routed (``False`` iff refused
    by the stage-mismatch guard, #1019).
    """
    if task.status != QueueItemStatus.RUNNING:
        msg = f"apply_staged_decision: expected RUNNING, got {task.status!r}"
        raise AssertionError(msg)
    return _route_staged_decision(task, status, last_result, clients)


def _route_scope_gated_approval(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    last_result: dict[str, object] | None,
    disposition: str | None,
    pr_url: str | None,
    *,
    claim_unproductive: bool,
) -> None:
    """Rule 1 body: scope-gated approval -- small tier auto-advances, large blocks.

    Small tier additionally checks two REVIEW-scoped gates before advancing:
    first the RFC 0011 A3 proactive finalize hold (#1160), then the
    operator-signoff gate. Both are checked ONLY at Stage.REVIEW -- they are
    ship checkpoints (REVIEW->FINALIZE), not per-stage checkpoints, so a
    small-tier `plan_pending_approval` at Stage.PLAN must advance unattended
    exactly as it did before #990/#1160. Mirrors ``_route_stage_success``'s
    identical REVIEW-scoping.

    The non-small (large) arm returns *before* either gate is reached: a large
    ticket already parks BLOCKED_ON_USER at the approval gate, so it keeps the
    status-derived disposition rather than being restamped ``finalize_gate_held``
    -- the hold changes nothing about where that ticket stops. Tier resolution
    is escalate-only -- see ``_resolve_scope_tier`` docstring (#696, #926).
    Extracted from ``_route_staged_decision`` to keep that function under the
    PLR0912 branch ceiling.

    The large-tier park pages the operator (``SESSION_NEEDS_ATTENTION``) only
    when no gate recipe will release it (``_gate_recipe_will_release``). A
    ticket's size alone is not a reason to page anyone; the recipes in
    ``cw.reconcile.gate_recipes`` clear such a park on the next reconcile
    tick. The row still parks either way, because the recipes act only on
    parked rows. An earlier-stage report always pages.

    Every call emits the #1617 scope-routing audit event
    (``_record_scope_routing_decision``) after the decision is made, whichever
    of the four arms below actually ran.

    An earlier-stage report (GitHub #1676 follow-up, ``_is_earlier_stage_
    report``) also always parks, regardless of resolved tier: the sentinel
    never reached ``task.stage``, so its scope block cannot be trusted to
    auto-advance the pipeline past stage work that was never done. Distinct
    ``paused_status`` (``_EARLIER_STAGE_REPORT_REASON``) from the large-tier
    park (``_APPROVAL_GATE_REASON``) so the two causes stay diagnosable.

    Ahead of all of that -- ahead of tier resolution itself -- runs the #1702
    review-health gate, REVIEW-scoped for the reason given on
    ``_should_gate_for_review_health``. It returns immediately on a park, so a
    degraded-health row never reaches the tier logic at any tier. Placing it
    first changes the ``disposition`` (never the terminal status) for the
    large-tier + degraded-health combination: a quality signal outranks an
    authorization-workflow signal, mirroring the existing finalize_hold-
    outranks-signoff precedent below.

    Ahead of even that runs the #1870 empty-diff gate. This is the site the
    incident it closes actually reached: a large-tier row whose branch held
    zero commits parked here under the ordinary ``approval_gate`` disposition,
    presenting an empty diff to a human as a routine scope decision.
    """
    # #1717: computed once, before any REVIEW-scoped gate check below (mirrors
    # _walk_stage_pointer_forward's identical placement). No-ops at any
    # non-REVIEW stage internally -- see _consume_finalize_regress_repeat.
    # No hop has run yet in this function, so task.stage_base_ref is still
    # the live, un-cleared claim-time value here.
    is_repeat = _consume_finalize_regress_repeat(task, task.stage_base_ref)
    if task.stage == Stage.REVIEW and _should_gate_for_empty_diff(task, clients):
        # Why first of all: this is the site the #1870 incident reached -- a
        # zero-commit branch parked here asking an operator to approve an empty
        # diff as if it were an ordinary large-tier decision. Returns
        # immediately, so an empty branch never reaches tier resolution.
        _park_empty_diff_gate(task)
        _record_scope_routing_decision(task, last_result, _RULE_SCOPE_GATED_APPROVAL)
        _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
        return
    if task.stage == Stage.REVIEW and _should_gate_for_branch_staleness(task, clients):
        # Why ahead of even the review-health gate: a stale tree undermines
        # every downstream signal, including the health recommendation itself
        # -- the review that produced it reviewed a tree that is not what would
        # ship. Returns immediately, so a stale row never reaches tier
        # resolution at any tier (#1823).
        _park_branch_staleness_gate(task)
        _record_scope_routing_decision(task, last_result, _RULE_SCOPE_GATED_APPROVAL)
        _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
        return
    if task.stage == Stage.REVIEW and _should_gate_for_review_staleness(
        task, last_result, clients
    ):
        # Why between the two: branch-staleness is about the tree itself being
        # out of date, which a rebase fixes; this is about the *review* being
        # out of date relative to that tree, which only a re-review fixes. And
        # why still ahead of review-health: a health recommendation derived
        # from artifacts that never saw HEAD describes something other than
        # what would ship, so it cannot be trusted either way (#2123).
        _park_review_staleness_gate(task)
        _record_scope_routing_decision(task, last_result, _RULE_SCOPE_GATED_APPROVAL)
        _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
        return
    if task.stage == Stage.REVIEW and _should_gate_for_review_health(last_result):
        _park_review_health_gate(task)
        _record_scope_routing_decision(task, last_result, _RULE_SCOPE_GATED_APPROVAL)
        _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
        return
    tier = _resolve_scope_tier(last_result, task)
    earlier_stage_report = _is_earlier_stage_report(task, last_result, clients)
    if tier != SCOPE_TIER_SMALL or earlier_stage_report:
        if earlier_stage_report or not _gate_recipe_will_release(
            task, last_result, clients
        ):
            record_event(
                OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                {
                    "session_id": task.session_id or "",
                    "session_name": "",
                    "client": task.client,
                    "ticket_id": task.ticket_id,
                    "claude_session_id": None,
                    "paused_status": (
                        _EARLIER_STAGE_REPORT_REASON
                        if earlier_stage_report
                        else _APPROVAL_GATE_REASON
                    ),
                    "breadcrumbs": "",
                    "crashed": False,
                    "lane": task.lane,
                },
                correlation_id=task.ticket_id,
            )
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=disposition,
            # #1750: a scope-gated park is a legitimate stop, but whether the
            # claim that reached it did any work is an evidence question --
            # reuse the caller's hoisted classification rather than
            # recomputing it here (both must agree on the same claim).
            unproductive=claim_unproductive,
        )
        _record_scope_routing_decision(task, last_result, _RULE_SCOPE_GATED_APPROVAL)
        _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
        return
    if task.stage == Stage.REVIEW and _should_force_hold_finalize(task, clients):
        # Why first: the A3 force hold is an operator's explicit "do not ship
        # this unattended" and outranks both the small-tier auto-advance AND
        # the signoff gate below -- a signoff park is an authorization slot a
        # second `approve` clears, whereas this park is the stop itself
        # (RFC 0011 A3, #1160). Chained as if/elif so an armed force hold never
        # double-parks the row through the signoff branch too.
        _park_finalize_hold(task)
    elif task.stage == Stage.REVIEW and _should_gate_for_signoff(task, clients):
        # Why: the operator-signoff gate takes precedence over the small-tier
        # auto-advance -- the ticket parks for an explicit operator approval
        # before continuing the pipeline, rather than advancing unattended
        # (RFC 0007 Phase 3, #990). REVIEW-scoped for the same reason as
        # _route_stage_success: signoff is the ship checkpoint only.
        _park_signoff_gate(task)
    else:
        _stage_advance_unchecked(task, clients, disposition=disposition, pr_url=pr_url)
    _record_scope_routing_decision(task, last_result, _RULE_SCOPE_GATED_APPROVAL)
    _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)


def _route_stage_success(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    disposition: str | None,
    pr_url: str | None,
    last_result: dict[str, object] | None,
) -> None:
    """Rule 3 body: shipped/stage_complete -- advance or complete.

    Six REVIEW-scoped gates run ahead of the advance, in order: the #1870
    empty-diff gate, then the #1823 branch-staleness gate, then the #1702
    review-health gate, then the scope_hint escalation gate, then the RFC 0011
    A3 proactive finalize hold (#1160), then the operator-signoff gate (#1617
    D1 fixed the order of the last three). The first two are ordered by how
    fundamentally they invalidate everything below: an empty branch makes every
    later question vacuous, and a stale one makes every later answer describe a
    tree that is not what would ship. The review-health gate follows because it
    is the only remaining one that says the *work itself* is not vouched for;
    the other three are all authorization/scope workflow. Below it, the
    scope_hint gate wins outright over the remaining two -- an operator/queue
    ``scope_hint`` of ``"large"`` means "gate this," full stop -- and the hold
    in turn wins outright over signoff when both of those are armed -- see
    ``_route_scope_gated_approval``.

    One IMPL-scoped gate runs ahead of those six: the #2421 unconcluded
    FINALIZE-regress merge gate (``dispatch/impl_gates.py``), which refuses an
    IMPL->REVIEW advance while a FINALIZE-regress round trip's merge is still
    unconcluded. It is stage-exclusive with the REVIEW rungs.

    Why REVIEW-scoped: STAGE_SUCCESS_STATUSES fires at every pipeline stage as
    the ordinary staged-advance signal (each of HARDEN/PLAN/IMPL/REVIEW's
    "stage_complete", plus terminal "shipped"); gating every one of those
    would pause the ticket at every stage. Signoff (and the scope_hint gate)
    are the ship checkpoint only -- the REVIEW->FINALIZE transition -- so
    those gates apply only when task.stage is REVIEW. This relies on an
    unenforced producer contract that only REVIEW's advance represents "ready
    to ship"; dispatch does not otherwise verify it (RFC 0007 Phase 3, #990).
    Similarly, the sentinel's own ``scope.tier`` (and ``last_result`` in
    general) is self-reported by the agent that produced the work and is not
    independently verified by dispatch -- only ``task.scope_hint``, an
    operator/queue-set field, is trusted to escalate a gate; see
    ``_resolve_scope_tier``'s escalate-only precedence (#1617). Extracted from
    ``_route_staged_decision`` to keep that function under the PLR0912
    branch ceiling.

    Every call emits the #1617 scope-routing audit event
    (``_record_scope_routing_decision``) after the decision is made.
    """
    # #1717: computed once, before any REVIEW-scoped gate check below (mirrors
    # _route_scope_gated_approval's identical placement). No hop has run yet
    # in this function, so task.stage_base_ref is still the live value.
    is_repeat = _consume_finalize_regress_repeat(task, task.stage_base_ref)
    if task.stage == Stage.IMPL and _should_gate_for_unconcluded_finalize_regress_merge(
        task, clients
    ):
        # Stage-exclusive with every REVIEW rung below, so its position only
        # matters for readability: a FINALIZE-regress round trip must not
        # leave IMPL with its merge unconcluded (#2421).
        _park_unconcluded_finalize_regress_merge_gate(task)
    elif task.stage == Stage.REVIEW and _should_gate_for_empty_diff(task, clients):
        # Why first: every gate below reasons about a branch that has content --
        # whether it is current, whether it was reviewed, whether it is big
        # enough to need approval. A branch with zero commits ahead of
        # origin/<default> answers all three vacuously (#1870).
        _park_empty_diff_gate(task)
    elif task.stage == Stage.REVIEW and _should_gate_for_branch_staleness(
        task, clients
    ):
        # Why here: the gates below ask "may this ship?" and "is there
        # anything shippable here?" -- both about a sentinel produced against
        # this branch's tree. A tree that is behind origin/<default> with
        # overlapping churn makes those answers describe something other than
        # what would actually ship, so it is answered first (#1823).
        _park_branch_staleness_gate(task)
    elif task.stage == Stage.REVIEW and _should_gate_for_review_staleness(
        task, last_result, clients
    ):
        # Why here: the gates below all reason about a review verdict -- is it
        # vouched for, is it big enough to need approval. This one asks whether
        # the verdict describes the tree that would actually ship. If it does
        # not, every answer below is about the wrong tree (#2123). Chained as
        # if/elif so a stale row never double-parks through a second branch.
        _park_review_staleness_gate(task)
    elif task.stage == Stage.REVIEW and _should_gate_for_review_health(last_result):
        # Why here: every gate below is an authorization or scope-workflow
        # question ("may this ship?"); this one is a quality question ("is
        # there anything shippable here?"). A review that self-reported
        # EXIT_FOR_HUMAN_REVIEW has not vouched for its own coverage, so
        # answering the authorization question yet is premature (#1702).
        # Chained as if/elif so a degraded row never double-parks through a
        # second branch.
        _park_review_health_gate(task)
    elif task.stage == Stage.REVIEW and _should_gate_for_scope_hint(task, last_result):
        _park_scope_hint_gate(task)
    elif task.stage == Stage.REVIEW and _should_force_hold_finalize(task, clients):
        _park_finalize_hold(task)
    elif task.stage == Stage.REVIEW and _should_gate_for_signoff(task, clients):
        _park_signoff_gate(task)
    else:
        _stage_advance_unchecked(task, clients, disposition=disposition, pr_url=pr_url)
    _record_scope_routing_decision(task, last_result, _RULE_STAGE_SUCCESS)
    _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)


def _route_staged_decision(
    task: TicketTask,
    status: str | None,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> bool:
    """Shared assert-free core of the B2 staged advance decision table.

    The single advance authority shared by the consume path
    (_apply_events_to_store, via ``apply_staged_decision``) and reconcile's
    emitted-sentinel router (_apply_sentinel_to_task), so staged dispatch
    advances regardless of which path observes the completion first (#698) and
    a late-sentinel rescue lands in exactly the state its live counterpart would
    (#918). No status precondition — callers gate as needed. Mutates in place.

    First classifies the sentinel's ``stage_reached`` against ``task.stage`` by
    pipeline position (``_resolve_stage_walk``, GitHub #1149, extending #1019).
    An unresolvable sentinel always refuses; an *earlier*-stage sentinel
    refuses only when it is a stage-advance claim -- a late/replayed sentinel
    from a previous leg, the #986 incident (``_is_stage_advance_claim``,
    GitHub #1676). A refusal is a true no-op -- no status transition, no
    ``save_dev_queue`` by callers that gate on the return value -- and
    ``SENTINEL_STAGE_MISMATCH`` is emitted for observability. Every other
    earlier-stage sentinel (a pause, a scope-gated approval, ``no_op``,
    ``blocked``, ...) is a legitimate "could not reach the dispatched stage"
    report and proceeds to the ordinary Rule 1-6 table at the task's unchanged
    stage -- except Rule 5a's FINALIZE self-heal regress and Rule 1's
    small-tier auto-advance, which additionally gate on
    ``_is_earlier_stage_report`` (GitHub #1676 follow-up) and park instead,
    since both perform a further ``task.stage`` mutation the table's ordinary
    transition does not, one that assumes the sentinel reflects work done AT
    ``task.stage``. A *later*-stage sentinel (a legitimate self-escalation the row lags
    behind) walks ``task.stage`` forward one rung at a time to the sentinel's
    stage, then the Rule 1-6 table applies at the now-matching stage; if a
    REVIEW gate (the A3 finalize hold, #1160, or the signoff gate) intervenes
    the walk parks the task and returns without applying the table.

    Same-stage and no-``stage_reached`` sentinels route through Rule 1-6 exactly
    as before. Returns ``False`` on refusal, ``True`` for every routed path.
    """
    walk_outcome = _resolve_stage_walk(task, last_result, clients)
    if walk_outcome == "refuse":
        stage_reached = (
            last_result.get("stage_reached") if isinstance(last_result, dict) else None
        )
        record_event(
            OrchestratorEventType.SENTINEL_STAGE_MISMATCH,
            {
                "ticket_id": task.ticket_id,
                "client": task.client,
                "session_id": task.session_id,
                "expected_stage": task.stage,
                "sentinel_stage_reached": stage_reached,
            },
            correlation_id=task.ticket_id,
        )
        return False
    _persist_carried_context(task, last_result)
    if walk_outcome == "parked":
        # A later-stage sentinel that stopped at a REVIEW gate: the task is
        # already parked (BLOCKED_ON_USER/finalize_gate_held for the A3 force
        # hold, else AWAITING_OPERATOR_SIGNOFF) by the walk. Do not apply
        # the Rule 1-6 status table (the sentinel's status was never observed at
        # this stage). Routed, so callers persist the parked state (#1149).
        return True
    pr_url = _extract_pr_url(last_result)
    # Hoisted above the disposition computation (#1254): Rule 5's branch already
    # read blocker/blocker_reason for the regress check and the breadcrumbs, and
    # _hold_aware_disposition needs the same reason *before* the rule table runs.
    # One extraction, reused by both.
    blocker = last_result.get("blocker") if isinstance(last_result, dict) else None
    blocker_reason = blocker.get("reason") if isinstance(blocker, dict) else None
    disposition = _hold_aware_disposition(status, blocker_reason)
    # GitHub #1750: hoisted once alongside blocker/blocker_reason above and
    # reused by every same-stage rule below, so all of them classify the same
    # claim identically. Only the same-stage, no-advance branches consult it —
    # advancing and regressing branches are already covered by the hardcodes
    # at lifecycle.py's _advance_task_pointer/_stage_regress chokepoints.
    claim_unproductive = is_unproductive(extract_claim_evidence(last_result))
    if status in SCOPE_GATED_APPROVAL_STATUSES:
        # Rule 1: scope-gated approval; small tier auto-advances, large blocks.
        # Must fire before Rule 2 (SCOPE_GATED ⊂ PAUSED_FOR_USER_INPUT).
        _route_scope_gated_approval(
            task,
            clients,
            last_result,
            disposition,
            pr_url,
            claim_unproductive=claim_unproductive,
        )
    elif status in PAUSED_FOR_USER_INPUT_STATUSES:
        # Rule 2: pure pause (v4 statuses: ambiguities_pending_resolution,
        # premises_pending_verification). Scope-gated statuses caught by Rule 1.
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": _PLAN_PARKED_REASON,
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=disposition,
            unproductive=claim_unproductive,  # #1750 Rule 2
        )
    elif status in STAGE_SUCCESS_STATUSES:
        # Rule 3: shipped -- advance or complete (REVIEW-scoped signoff gate;
        # see _route_stage_success docstring).
        _route_stage_success(task, clients, disposition, pr_url, last_result)
    elif status == "merge_pending":
        # Rule 3b: PR created but awaiting CI/merge gate (#899). Not a failure
        # — preserve pr_url so operator can monitor/merge. Do not re-dispatch.
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": "merge_pending",
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=disposition,
            pr_url=pr_url,
            unproductive=claim_unproductive,  # #1750 Rule 3b
        )
    elif status == "no_op":
        # Rule 4: pre-flight already satisfied -- terminal
        # regardless of remaining stages.
        # unproductive=False hardcoded (#1750): "the work was already done" is
        # a successful terminal outcome, and its payload legitimately carries
        # no commits — evidence-deriving it would charge a correct no-op.
        transition_task_status(
            task,
            QueueItemStatus.COMPLETED,
            disposition="no_op",
            unproductive=False,
        )
    elif status in STAGE_FAILURE_STATUSES:
        # Rule 5: blocked/merge_gate_blocked/scope_exceeded/forbidden_area.
        # Body extracted to _route_stage_failure (#1948) to keep this
        # function's branch count under CLAUDE.md's PLR0912 ceiling -- see
        # that function's docstring for the full rule-table rationale.
        _route_stage_failure(
            task,
            clients,
            status,
            last_result,
            blocker,
            blocker_reason,
            disposition,
            claim_unproductive,
        )
    else:
        # Rule 6: None/not dict/missing status -- conservative fallback
        # Why: unparseable sentinel must never silently advance/complete
        # (B2 correctness requirement). Changes pre-B2 behavior which
        # fell through to COMPLETED.
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": "blocked",
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition="abandoned",
            unproductive=claim_unproductive,  # #1750 Rule 6
        )
    return True
