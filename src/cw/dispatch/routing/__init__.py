"""Sentinel interpretation and staged stage-routing for the dispatch pipeline.

Owns the B2 staged-advance decision table: it reads a session's sentinel
(``AutoDevResult``-derived ``last_result``), classifies its ``stage_reached``
against the task's current stage, walks the stage pointer forward for a
legitimate self-escalation, and applies the Rule 1-6 status routing (scope-gated
approval, pauses, stage success/advance, no-op, failure/regress, and the
conservative fallback). Also resolves the effective scope tier and the
operator-signoff policy, and accumulates per-session cost onto the task.

Package split (#1728). The historical flat ``routing.py`` was 1345 lines, ~35%
over CLAUDE.md's ~1000-line ceiling. Four separable concerns now live in
submodules, re-exported below so no ``from cw.dispatch.routing import X`` call
site changed:

  - ``scope_tier``  -- effective scope-tier resolution and carried-context
    persistence (``_resolve_scope_tier`` & co.).
  - ``stage_walk``  -- ``stage_reached`` classification and the forward
    stage-pointer walk, including its REVIEW-rung gate ladder
    (``_resolve_stage_walk`` & co.); the largest of the four.
  - ``pr_refs``     -- the #1713 blocker-``details`` PR cross-reference
    literals and extractor.
  - ``cost``        -- ``_accumulate_task_cost``.

**Monkeypatch coupling — why this ``__init__`` is not a pure re-export shim.**
Unlike ``cw.cli``/``cw.reconcile``, whose ``__init__`` files only re-export,
this one *defines* the Rule 1-6 decision table itself. ``tests/test_dispatch.py``
monkeypatches ``cw.dispatch.routing.record_event``, ``._stage_regress`` and
``._stage_advance_unchecked`` by dotted string path at 10 call sites, and a
function resolves its free names against the ``__dict__`` of the module it was
*defined* in, not the one that calls it (see ``tests/conftest.py``'s
``capture_events`` docstring). Every function that calls one of those three
names -- ``_park_must_fix_mechanically_rejected``,
``_record_scope_routing_decision``, ``_stage_advance_unchecked``,
``_route_scope_gated_approval``, ``_route_stage_success``,
``_route_staged_decision``, ``apply_staged_decision`` -- must therefore stay
defined in the module object bound to the exact dotted path
``cw.dispatch.routing``. Moving any of them into a submodule would leave the
patches pointing at a module the real calls never consult: green tests
observing nothing. That constraint, not line-count aesthetics, chose the four
seams above. ``tests/test_dispatch_routing_package.py`` pins it as an
executable assertion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    FINALIZE_REGRESS_BLOCKER_REASONS,
    FINALIZE_REGRESS_CAP,
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    PAUSED_FOR_USER_INPUT_STATUSES,
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_SMALL,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
    STALE_DISPATCH_BLOCKER_REASON,
)
from cw.codex_review import CODEX_MUST_FIX_MECHANICALLY_REJECTED
from cw.dev_queue import (
    REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
    _advance_task_pointer,
    _extract_pr_url,
    _extract_pr_url_or_info,
    _hold_aware_disposition,
    _stage_regress,
    transition_task_status,
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
# a real cycle. Same shape as gating.py <-> claim.py since #1310, and as
# routing/stage_walk.py's own deferred reach back into this module (#1728),
# guarded by test_dispatch_package_submodules_import_without_cycle.
from cw.dispatch.review_gates import (
    _park_branch_staleness_gate,
    _park_empty_diff_gate,
    _park_finalize_hold,
    _park_review_health_gate,
    _park_scope_hint_gate,
    _park_signoff_gate,
    _should_force_hold_finalize,
    _should_gate_for_branch_staleness,
    _should_gate_for_empty_diff,
    _should_gate_for_review_health,
    _should_gate_for_scope_hint,
    _should_gate_for_signoff,
)
from cw.dispatch.routing.cost import _accumulate_task_cost
from cw.dispatch.routing.pr_refs import (
    _AUTOMERGE_NOT_ARMED_REASON,
    _BLOCKING_PR_NUMBER_RE,
    _PRIOR_PIPELINE_PR_OPEN_REASON,
    _extract_blocked_on_pr,
)
from cw.dispatch.routing.scope_tier import (
    _extract_scope_tier,
    _persist_carried_context,
    _resolve_scope_tier,
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
from cw.unavailability import FAMILY_PROVIDER_OVERLOAD
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        TicketTask,
    )

# Re-exported submodule surface. Listed here (rather than left as bare
# imports) because several names are consumed only by *other* modules --
# dispatch/__init__.py's facade, dispatch/loop.py, reconcile/tasks.py's
# deferred imports -- and would otherwise read as unused. Mirrors
# dispatch/__init__.py's own __all__ convention.
__all__ = [
    "_AUTOMERGE_NOT_ARMED_REASON",
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
_APPROVAL_GATE_REASON = "approval_gate"


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


# paused_status written to SESSION_NEEDS_ATTENTION when Rule 5's blocked
# status carries a blocker reason in OPERATOR_UNAVAILABLE_BLOCKER_REASONS
# (RFC 0011 A1). Deliberately distinct from QueueItemStatus.
# AWAITING_OPERATOR_SIGNOFF -- that enum member is a REVIEW-stage signoff
# gate; this constant is a paused_status string for an operator/dependency
# unavailability, an unrelated concept that happens to share the "awaiting
# operator" phrase. See GitHub #1155.
_AWAITING_OPERATOR_REASON = "awaiting_operator_availability"


# paused_status written to SESSION_NEEDS_ATTENTION when Rule 1 parks an
# earlier-stage scope-gated approval sentinel instead of auto-advancing it
# (GitHub #1676 follow-up). Deliberately distinct from _APPROVAL_GATE_REASON
# -- that reason means "tier resolved to large, waiting on an operator";
# this one means "the sentinel never reached task.stage, so its tier verdict
# cannot be trusted to auto-advance regardless of tier."
_EARLIER_STAGE_REPORT_REASON = "earlier_stage_report"


# paused_status written to SESSION_NEEDS_ATTENTION when the #1714 gate parks a
# ticket whose blocked sentinel reports that review's only MUST_FIX finding(s)
# were mechanically rejected before adjudication. Shares its literal string
# value with dev_queue.lifecycle.REVIEW_MUST_FIX_MECHANICALLY_REJECTED_
# DISPOSITION (task.disposition) on the same _SIGNOFF_GATE_REASON /
# _REVIEW_HEALTH_GATE_REASON precedent above -- still two constants in two
# namespaces, do not collapse them. Distinct in turn from
# codex_review.CODEX_MUST_FIX_MECHANICALLY_REJECTED, which is the *blocker
# reason* the sentinel carries; that one is imported, not re-derived.
_MUST_FIX_MECHANICALLY_REJECTED_REASON = "codex_must_fix_mechanically_rejected"


# Paused-status values that carry a non-empty breadcrumbs string derived
# verbatim from blocker.reason in Rule 5's SESSION_NEEDS_ATTENTION payload
# below (GitHub #1511) -- every STAGE_FAILURE_STATUSES member for which the
# schema allows a non-null blocker (schema.py's #777 exception:
# "blocked"/"merge_gate_blocked" only -- scope_exceeded/forbidden_area never
# carry one, by design), plus two substitutes: _AWAITING_OPERATOR_REASON,
# which the ternary below writes when that blocker's reason is in
# OPERATOR_UNAVAILABLE_BLOCKER_REASONS, and (#1729)
# _MUST_FIX_MECHANICALLY_REJECTED_REASON, stamped by
# _park_must_fix_mechanically_rejected above -- the one gate-class park whose
# breadcrumbs is genuinely populated from blocker.reason rather than a
# hardcoded "" literal. Named + anchored so
# .claude/skills/orchestrate-sprint/scripts/attention_monitor.sh's
# hand-transcribed Python set (which runs outside src/cw and cannot import
# this constant) has one file to keep in sync against. See #1597.
#
# IMPORTANT: this constant has no runtime reader anywhere in src/cw -- it is
# the canonical *declaration* consumed only by attention_monitor.sh (an
# out-of-repo hand-copy) and by the pinning test below. Adding a paused_status
# here does NOT by itself cause a breadcrumb to be emitted for it: the
# producing _park_* helper must independently stamp non-empty breadcrumbs
# content at its own call site. Every gate-class park other than
# _park_must_fix_mechanically_rejected (review_gates.py's _park_finalize_hold,
# _park_signoff_gate, _park_scope_hint_gate, _park_review_health_gate and
# _park_branch_staleness_gate, plus the _stage_advance_unchecked config-error
# paths here) hardcodes breadcrumbs="" and is deliberately excluded from this
# set -- membership for one of those would be cosmetic, not a fix (#1729).
#
# #1775 reaffirms this for _park_review_health_gate specifically: a degraded
# reviewer's stated reason (ReviewerRunRecord.detail, threaded through
# cw.review_findings.consolidate_verdict and rendered by
# cw.codex_review._verdict._render_degraded_roles_note) lives entirely inside
# the review-executor process and is never written onto the
# session.last_result sentinel this dispatch-loop process reads -- there is no
# existing data path from that detail into _park_review_health_gate's
# breadcrumbs/needs_attention payload. Threading one in was considered and
# declined this round: render_verdict_comment already posts the degraded-role
# note to the ticket on every completed review pass, including the
# stage_complete path that reaches this park (codex_background.py posts it
# whenever verdict is not None), so the reason is already a durable,
# operator-visible artifact before the park happens. Adding a second copy via
# breadcrumbs would duplicate that, not fix a silence. See #1607/#1754.
BREADCRUMB_ELIGIBLE_PAUSED_STATUSES: frozenset[str] = (
    STAGE_FAILURE_STATUSES - {"scope_exceeded", "forbidden_area"}
) | {_AWAITING_OPERATOR_REASON, _MUST_FIX_MECHANICALLY_REJECTED_REASON}


def _park_must_fix_mechanically_rejected(task: TicketTask) -> None:
    """Park *task* BLOCKED_ON_USER for a mechanically-rejected MUST_FIX (#1714).

    Rule 5's sole reason-keyed override: every other blocker_reason at the
    STAGE_FAILURE_STATUSES branch falls through to the generic
    ``_hold_aware_disposition(status, blocker_reason)`` stamp, which for a
    blocked status yields the verbatim string ``"blocked"``. This reason is
    neither an operator-unavailability hold nor an ordinary block -- the review
    ran and found something, but the finding was mechanically dropped before
    adjudication, which is a quality signal that must stay distinguishable from
    both.

    Field-for-field mirror of ``_park_review_health_gate`` (review_gates.py)
    with one deliberate difference: ``breadcrumbs`` carries the real
    ``blocker.reason`` here, because -- unlike the health gate -- this park
    genuinely originates from a populated blocker dict. That matches Rule 5's
    own generic breadcrumbs semantics further down this module.

    BLOCKED_ON_USER, not a hold disposition:
    ``REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION`` is deliberately
    excluded from ``HOLD_DISPOSITIONS`` -- it clears by re-running review with
    the finding adjudicated, not by an operator saying "proceed anyway",
    mirroring ``REVIEW_HEALTH_GATE_DISPOSITION``'s identical reasoning. Stamping
    it directly here (rather than teaching ``_hold_aware_disposition`` about the
    reason) is what guarantees it can never resolve to a hold.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _MUST_FIX_MECHANICALLY_REJECTED_REASON,
            "breadcrumbs": CODEX_MUST_FIX_MECHANICALLY_REJECTED,
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION,
        blocked_reason=CODEX_MUST_FIX_MECHANICALLY_REJECTED,
        # GitHub #1750 R2: hardcoded, NOT evidence-derived. This park means the
        # reviewer emitted MUST_FIX findings that failed mechanical validation
        # — a reviewer malfunction the ticket cannot be blamed for. The payload
        # here carries no commits or countable findings, so evidence-deriving
        # it would charge the ticket for someone else's malfunction.
        unproductive=False,
    )


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
    ``_DEFAULT_OPERATOR_EVENT_TYPES`` (``orchestrator_config.py``): an
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
    pipeline = client_cfg.pipeline
    stages = pipeline.stages
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
    if task.stage == Stage.REVIEW and _should_gate_for_review_health(last_result):
        _park_review_health_gate(task)
        _record_scope_routing_decision(task, last_result, _RULE_SCOPE_GATED_APPROVAL)
        _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
        return
    tier = _resolve_scope_tier(last_result, task)
    earlier_stage_report = _is_earlier_stage_report(task, last_result, clients)
    if tier != SCOPE_TIER_SMALL or earlier_stage_report:
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
    if task.stage == Stage.REVIEW and _should_gate_for_empty_diff(task, clients):
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
        # Rule 5: blocked/merge_gate_blocked/scope_exceeded/forbidden_area
        # Sub-rule 5a: "blocked" at FINALIZE with a regress-eligible blocker
        # reason and attempts below cap → regress to IMPL for self-heal (#770).
        # scope_exceeded/forbidden_area have no blocker field (validator enforces
        # this) so they always fall through to BLOCKED_ON_USER. merge_gate_blocked
        # MAY carry one (schema.py's #777 exception), but 5a is gated on
        # status=="blocked" so it still cannot regress. blocker/blocker_reason are
        # read once above, before the rule table (#1254). Also excludes an
        # earlier-stage report (GitHub #1676 follow-up, _is_earlier_stage_report):
        # "agent_block" reported at, say, stage1_plan never actually failed at
        # FINALIZE, so self-healing off it would mask the sentinel's real,
        # earlier failure behind a fresh, doomed-to-repeat IMPL dispatch.
        if (
            status == "blocked"
            and blocker_reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED
        ):
            # #1714: dedicated override -- see
            # _park_must_fix_mechanically_rejected's docstring for why this
            # reason cannot use the generic _hold_aware_disposition stamp
            # computed above. The reason is never a member of
            # FINALIZE_REGRESS_BLOCKER_REASONS ({"agent_block"}), so there is
            # no ordering conflict with 5a below, but the branch is placed
            # first and returns immediately so that fact does not have to hold
            # forever.
            _park_must_fix_mechanically_rejected(task)
            return True
        if (
            status == "blocked"
            and task.stage == Stage.FINALIZE
            and blocker_reason in FINALIZE_REGRESS_BLOCKER_REASONS
            and task.regress_attempts < FINALIZE_REGRESS_CAP
            and not _is_earlier_stage_report(task, last_result, clients)
        ):
            _log.info(
                "dispatch: finalize gate blocked (%r) — regressing %r to IMPL"
                " (regress attempt %d/%d)",
                blocker_reason,
                task.ticket_id,
                task.regress_attempts + 1,
                FINALIZE_REGRESS_CAP,
            )
            _stage_regress(task, Stage.IMPL)
            record_event(
                OrchestratorEventType.TICKET_REQUEUED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "from_stage": Stage.FINALIZE,
                    "to_stage": Stage.IMPL,
                    "reason": "finalize_regress",
                    "blocker_reason": blocker_reason,
                    "regress_attempt": task.regress_attempts,
                },
            )
            return True
        if status == "blocked" and blocker_reason == FAMILY_PROVIDER_OVERLOAD:
            # #1948: worker-declared provider overload (API 529) is transient and
            # provider-side; same-stage retry, NOT a _stage_regress -- task.stage
            # is untouched (no pipeline boundary crossed), so none of
            # _stage_regress's re-entry machinery (regress_attempts,
            # regressed_into_stage, pending_operator_comment,
            # finalize_regress_branch_head) applies. Narrow reason-keyed --
            # blocker.retry_eligible is never read (operator's #1923-round-3
            # resolution, carried forward: a generic read would silently start
            # auto-reverting local_main_diverged_from_origin/operator_unavailable,
            # which OPERATOR_UNAVAILABLE_BLOCKER_REASONS deliberately excludes).
            # Bound: the global attempt ceiling (unproductive_attempts /
            # claim.py's resolve_attempt_ceiling), deliberately NOT
            # regress_attempts/FINALIZE_REGRESS_CAP -- that cap exists because
            # _stage_regress already double-bounds against it (lifecycle.py:757);
            # this branch performs no stage regress, so nothing else bounds it.
            # unproductive=True hardcoded (not claim_unproductive): mirrors Rule
            # 4/5's no_op/stale_dispatch hardcodes -- a provider outage killing
            # the RUNNING exit is unproductive by definition regardless of
            # whatever partial evidence landed before the API died.
            transition_task_status(task, QueueItemStatus.PENDING, unproductive=True)
            task.session_id = None
            task.stage_base_ref = None
            record_event(
                OrchestratorEventType.TICKET_REQUEUED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "from_stage": task.stage,
                    "to_stage": task.stage,
                    "reason": "provider_overload_retry",
                    "blocker_reason": blocker_reason,
                },
            )
            return True
        breadcrumbs = str(blocker_reason) if blocker_reason is not None else ""
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": (
                    _AWAITING_OPERATOR_REASON
                    if blocker_reason in OPERATOR_UNAVAILABLE_BLOCKER_REASONS
                    else status
                ),
                "breadcrumbs": breadcrumbs,
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        # GitHub #1713 Variant A: automerge_not_armed parks a real, already-
        # created PR, but the schema forbids `pr` from being non-null on a
        # `blocked` status -- the producer carries the PR's identity in the
        # unmodeled `pr_info` object instead (see
        # _extract_pr_url_or_info's docstring). Without this, task.pr_url
        # never gets stamped and cw.pr_hydrate._is_candidate permanently
        # skips the row -- it is never polled, so it can never observe its
        # own PR merging.
        gate_pr_url = (
            _extract_pr_url_or_info(last_result)
            if blocker_reason == _AUTOMERGE_NOT_ARMED_REASON
            else None
        )
        # GitHub #1862: a stale_dispatch report legitimately carries zero
        # commits and zero findings -- the session correctly refused to
        # duplicate work already sitting in an open PR. Under the generic
        # evidence-based computation that reads as unproductive, so repeated
        # honest refusals would charge the ceiling and eventually re-park the
        # row at attempt_cap_blocked, burying the specific signal this status
        # exists to surface. Hardcoded False for the same reason Rule 4
        # hardcodes it for no_op: a correct, evidence-producing terminal
        # outcome, not a crashloop.
        rule5_unproductive = False if status == "stale_dispatch" else claim_unproductive
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=disposition,
            pr_url=gate_pr_url,
            blocked_reason=blocker_reason,
            unproductive=rule5_unproductive,  # #1750 Rule 5
        )
        # GitHub #1713 Variant B: prior_pipeline_pr_open blocks this ticket
        # behind a DIFFERENT ticket's open PR -- this row has no PR of its
        # own yet, so pr_url/pr_hydrate can't help. Stamp the blocking PR's
        # bare number (regex-extracted from blocker.details, the only place
        # the producer emits it -- see routing/pr_refs.py's
        # _extract_blocked_on_pr) directly on task AFTER transition_task_status
        # returns: that call's unconditional latch-clear (mirrors
        # escalation_parked_at) zeroes blocked_on_pr as part of every
        # transition, so stamping before the call would be immediately wiped.
        #
        # GitHub #1902 (fast-follow to #1862): a stale_dispatch sentinel's
        # pr_already_open blocker.details names the blocking PR in the
        # identical "PR #<N>" free-text shape, so the same
        # _extract_blocked_on_pr regex applies unchanged -- second producer,
        # not a new parser. NOTE: unlike prior_pipeline_pr_open (whose
        # blocking PR belongs to a DIFFERENT ticket that already completed
        # and so carries its pr_url on some other store row),
        # stale_dispatch's blocking PR is this ticket's OWN earlier,
        # un-harvested-sentinel dispatch, discovered via a live
        # `gh pr list --head <branch>` query that never writes a pr_url onto
        # any TicketTask row. Stamping blocked_on_pr here is therefore
        # currently production-unreachable release groundwork for this
        # disposition -- release_stale_gated_tasks's Variant B
        # cross-reference scan (reconcile/tasks.py) has no row to match it
        # against until #1927 (an independent PR-state source) lands.
        if blocker_reason in (
            _PRIOR_PIPELINE_PR_OPEN_REASON,
            STALE_DISPATCH_BLOCKER_REASON,
        ):
            details = blocker.get("details") if isinstance(blocker, dict) else None
            task.blocked_on_pr = _extract_blocked_on_pr(details)
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
