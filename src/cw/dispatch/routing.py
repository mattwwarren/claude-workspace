"""Sentinel interpretation and staged stage-routing for the dispatch pipeline.

Owns the B2 staged-advance decision table: it reads a session's sentinel
(``AutoDevResult``-derived ``last_result``), classifies its ``stage_reached``
against the task's current stage, walks the stage pointer forward for a
legitimate self-escalation, and applies the Rule 1-6 status routing (scope-gated
approval, pauses, stage success/advance, no-op, failure/regress, and the
conservative fallback). Also resolves the effective scope tier and the
operator-signoff policy, and accumulates per-session cost onto the task.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import (
    _STAGE_REACHED_CANONICAL,
    FINALIZE_REGRESS_BLOCKER_REASONS,
    FINALIZE_REGRESS_CAP,
    OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
    PAUSED_FOR_USER_INPUT_STATUSES,
    PLAN_SOURCE_NONE,
    SCOPE_GATED_APPROVAL_STATUSES,
    SCOPE_TIER_LARGE,
    SCOPE_TIER_SMALL,
    STAGE_FAILURE_STATUSES,
    STAGE_SUCCESS_STATUSES,
)
from cw.config import (
    load_effective_config,
    load_state,
)
from cw.dev_queue import (
    FINALIZE_GATE_HELD_DISPOSITION,
    SIGNOFF_GATE_DISPOSITION,
    _advance_task_pointer,
    _extract_pr_url,
    _hold_aware_disposition,
    _stage_regress,
    transition_task_status,
)
from cw.events import record_event
from cw.models import (
    ClientConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        OrchestratorConfig,
        TicketTask,
    )

_log = logging.getLogger("cw.dispatch")


# ``rule`` values stamped onto the #1617 ``dispatch.scope_routing_decision``
# audit event by each of this module's park-decision sites, plus the
# gate-release site in ``dev_queue.approval`` (imported there, see that
# module's function-level deferred-import convention for the
# dispatch<->dev_queue cycle break). Named constants so a typo at any of the
# ~9 call sites cannot silently break the audit trail's site-attribution.
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


# paused_status written to SESSION_NEEDS_ATTENTION when the RFC 0011 A3
# proactive finalize hold parks a ticket at the REVIEW->FINALIZE checkpoint.
# Deliberately distinct from dev_queue.lifecycle.FINALIZE_GATE_HELD_DISPOSITION
# ("finalize_gate_held") -- that constant classifies TicketTask.disposition,
# this one is a paused_status string. Different namespaces, same event. Follows
# the _APPROVAL_GATE_REASON / _UNKNOWN_CLIENT_REASON convention above. See
# GitHub #1160.
_FINALIZE_HOLD_REASON = "finalize_hold"


# paused_status written to SESSION_NEEDS_ATTENTION when the RFC 0007
# Phase 3 operator-signoff gate parks a ticket at the REVIEW->FINALIZE
# ship checkpoint (GitHub #990, #1552). Shares its literal string value
# with dev_queue.lifecycle.SIGNOFF_GATE_DISPOSITION (task.disposition) by
# deliberate choice on this ticket -- unlike the _FINALIZE_HOLD_REASON /
# FINALIZE_GATE_HELD_DISPOSITION pair, which uses distinct strings across
# the two namespaces. See GitHub #1552.
_SIGNOFF_GATE_REASON = "signoff_gate"


# Paused-status values that carry a non-empty breadcrumbs string derived
# verbatim from blocker.reason in Rule 5's SESSION_NEEDS_ATTENTION payload
# below (GitHub #1511) -- every STAGE_FAILURE_STATUSES member for which the
# schema allows a non-null blocker (schema.py's #777 exception:
# "blocked"/"merge_gate_blocked" only -- scope_exceeded/forbidden_area never
# carry one, by design), plus the _AWAITING_OPERATOR_REASON substitute the
# ternary below writes when that blocker's reason is in
# OPERATOR_UNAVAILABLE_BLOCKER_REASONS. Named + anchored so
# .claude/skills/orchestrate-sprint/scripts/attention_monitor.sh's
# hand-transcribed Python set (which runs outside src/cw and cannot import
# this constant) has one file:line to keep in sync against. See #1597.
BREADCRUMB_ELIGIBLE_PAUSED_STATUSES: frozenset[str] = (
    STAGE_FAILURE_STATUSES - {"scope_exceeded", "forbidden_area"}
) | {_AWAITING_OPERATOR_REASON}


def _accumulate_task_cost(task: TicketTask, session_id: str | None) -> None:
    """Add the session's cost_usd to task.total_cost_usd, if available.

    Reads cost via two-source fallback:
      1. session.cost_usd (populated by signal_stop — normal headless path)
      2. session.last_result.get('cost_usd') (populated by the RFC 0012 door —
         the harvest-authority write path used when signal_stop did not run)

    When both sources are absent, total_cost_usd is left unchanged.
    Called inside dev_queue_lock so the mutation is covered by the same
    save_dev_queue call that persists the COMPLETED status.
    """
    if session_id is None:
        return
    state = load_state()
    session = next((s for s in state.sessions if s.id == session_id), None)
    if session is None:
        return
    cost: float | None = session.cost_usd
    if cost is None and isinstance(session.last_result, dict):
        raw_cost = session.last_result.get("cost_usd")
        if isinstance(raw_cost, (int, float)):
            cost = float(raw_cost)
    if cost is not None:
        task.total_cost_usd = (task.total_cost_usd or 0.0) + cost


def _extract_scope_tier(last_result: dict[str, object] | None) -> str | None:
    """Pull ``scope.tier`` off a raw sentinel dict, tolerating a missing/non-dict
    ``scope`` key. Shared by ``_persist_carried_context`` and
    ``_resolve_scope_tier`` so the two never drift on how they read the field.
    """
    scope_val = last_result.get("scope") if last_result is not None else None
    return scope_val.get("tier") if isinstance(scope_val, dict) else None


def _persist_carried_context(
    task: TicketTask, last_result: dict[str, object] | None
) -> None:
    """Stamp carried-through context (plan_source, computed scope tier) onto the
    task from a stage-matched sentinel, so a rescue respawn's fresh claim->spawn
    re-materializes it via cw-context.json (#1050). Null/pre-impl values and a
    stray plan_source=PLAN_SOURCE_NONE never clobber an already-set value.
    """
    if not isinstance(last_result, dict):
        return
    plan_source = last_result.get("plan_source")
    if isinstance(plan_source, str) and plan_source not in ("", PLAN_SOURCE_NONE):
        task.plan_source = plan_source
    tier = _extract_scope_tier(last_result)
    if isinstance(tier, str) and tier:
        task.computed_scope_tier = tier


def _resolve_scope_tier(
    last_result: dict[str, object] | None, task: TicketTask
) -> str | None:
    """Resolve the effective scope tier for a scope-gated advance decision.

    Precedence (escalate-only, #314, #696, #926):
      0. If either ``task.scope_hint`` or the sentinel's ``scope.tier`` is
         ``"large"``, the result is ``"large"`` -- an operator hint can only
         ADD the approval gate, never remove it, and a large sentinel tier is
         never de-escalated by a smaller hint.
      1. Otherwise, ``last_result.scope.tier`` -- the plan sentinel's own
         classification.
      2. Otherwise, ``task.scope_hint`` -- operator/queue hint, used when the
         sentinel omits the tier.

    Why: a real PLAN-stage sentinel can legitimately carry ``scope.tier=null``
    (``lines_actual`` is unknown pre-impl), so a raw read blocked small tickets
    that should flow PLAN->IMPL unattended (#663 dogfood). Returns ``None`` when
    neither source supplies a tier -- the caller then blocks conservatively.
    """
    tier = _extract_scope_tier(last_result)
    # Step 0 of the precedence above. Only the exact string "large" escalates;
    # unexpected hint values (e.g. "medium") are treated as not-large.
    if SCOPE_TIER_LARGE in (task.scope_hint, tier):
        return SCOPE_TIER_LARGE
    if isinstance(tier, str):
        return tier
    return task.scope_hint


def _should_gate_for_scope_hint(
    task: TicketTask, last_result: dict[str, object] | None
) -> bool:
    """True iff the resolved scope tier requires parking at the REVIEW boundary
    (#1617).

    Mirrors ``_should_gate_for_signoff``/``_should_force_hold_finalize``'s
    two-arg predicate shape so all three REVIEW-scoped gate checks read
    identically at each call site. Deliberately checks for exactly
    ``SCOPE_TIER_LARGE``, NOT Rule 1's conservative "not small" semantics
    (``_resolve_scope_tier(...) != SCOPE_TIER_SMALL``, which also catches an
    unresolved/``None`` tier): ``STAGE_SUCCESS_STATUSES`` sentinels
    (``stage_complete``/``shipped``) routinely omit ``scope.tier`` entirely --
    it is only meaningful on a scope-gated-approval sentinel -- so treating
    "unknown" as "gate" here would park nearly every ordinary REVIEW->FINALIZE
    advance. Reuses ``_resolve_scope_tier`` so this predicate and Rule 1's
    escalate-only precedence never drift.
    """
    return _resolve_scope_tier(last_result, task) == SCOPE_TIER_LARGE


def resolve_signoff(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> Literal["operator"] | None:
    """Resolve the effective operator-signoff policy for *task* (RFC 0007 Phase 3).

    Precedence (highest to lowest), mirroring ``resolve_reap_policy``
    (reconcile/_shared.py) but with a 3rd tier for the per-ticket override:
      1. ``TicketTask.signoff`` -- per-ticket override (``cw dev-queue add
         --signoff operator``).
      2. ``LaneConfig.signoff`` in the task's client config.
      3. ``OrchestratorConfig.default_signoff`` -- global default; ``"none"``
         resolves to ``None`` (no gate).

    A task whose client is absent from *clients*, or whose lane name is not
    declared in that client's lanes, falls through to the global default --
    keeps behaviour identical to the pre-#990 read for any task that predates
    lane stamping. See GitHub #990.
    """
    if task.signoff is not None:
        return task.signoff
    client_cfg = clients.get(task.client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.name == task.lane and lane_cfg.signoff is not None:
                return lane_cfg.signoff
    return config.default_signoff if config.default_signoff != "none" else None


def _should_gate_for_signoff(
    task: TicketTask, clients: dict[str, ClientConfig]
) -> bool:
    """True iff *task* requires an explicit operator signoff before advancing.

    Lazily loads ``OrchestratorConfig`` itself -- the single call site for that
    load -- so ``_route_staged_decision``/``apply_staged_decision`` (and
    ``approve_ticket`` in dev_queue.py, its other caller) keep their existing
    signatures unchanged (#990). Mirrors the ad hoc ``load_effective_config()``
    calls already made elsewhere in ``run_dispatch_loop``.
    """
    config = load_effective_config()
    return resolve_signoff(task, clients, config) is not None


def resolve_hold_finalize(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> Literal["manual"] | None:
    """Resolve the effective proactive finalize-hold policy for *task*.

    Precedence (highest to lowest), mirroring ``resolve_signoff`` above:
      1. ``TicketTask.hold_finalize`` -- per-ticket override (``cw dev-queue
         add --hold-finalize``).
      2. ``LaneConfig.finalize_gate`` in the task's client config.
      3. ``OrchestratorConfig.default_finalize_gate`` -- global default;
         ``"auto"`` resolves to ``None`` (no hold).

    A task whose client is absent from *clients*, or whose lane name is not
    declared in that client's lanes, falls through to the global default --
    identical fall-through semantics to ``resolve_signoff``. See GitHub #1160
    (RFC 0011 A3).
    """
    if task.hold_finalize is not None:
        return task.hold_finalize
    client_cfg = clients.get(task.client)
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.name == task.lane and lane_cfg.finalize_gate is not None:
                return lane_cfg.finalize_gate
    default = config.default_finalize_gate
    return default if default != "auto" else None


def _should_force_hold_finalize(
    task: TicketTask, clients: dict[str, ClientConfig]
) -> bool:
    """True iff *task* must stop before an unattended finalize (RFC 0011 A3).

    Lazily loads ``OrchestratorConfig`` itself -- the single call site for that
    load -- so the three REVIEW-scoped gate sites and ``_approve_ticket_locked``
    keep their existing signatures unchanged. Exact sibling of
    ``_should_gate_for_signoff`` above, including the two-arg shape. See GitHub
    #1160.
    """
    config = load_effective_config()
    return resolve_hold_finalize(task, clients, config) is not None


def _park_finalize_hold(task: TicketTask) -> None:
    """Park *task* BLOCKED_ON_USER for an A3 force-hold (RFC 0011 A3, #1160).

    Shared by all three REVIEW-scoped gate sites so neither the attention
    payload nor the status/disposition pairing can drift between them; the
    surrounding control flow (``return "parked"`` vs falling through an
    ``if``/``elif``/``else``) stays at each call site because it differs per
    site.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _FINALIZE_HOLD_REASON,
            "breadcrumbs": "",
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=FINALIZE_GATE_HELD_DISPOSITION,
    )


def _park_signoff_gate(task: TicketTask) -> None:
    """Park *task* AWAITING_OPERATOR_SIGNOFF for the RFC 0007 Phase 3 ship
    checkpoint (REVIEW->FINALIZE, #990, #1552).

    Shared by all four signoff-park sites -- three in this module's staged
    -decision routing table, plus dev_queue.approval's `approve` CLI path
    -- so neither the attention payload nor the status/disposition pairing
    can drift between them (mirrors _park_finalize_hold above). Before
    #1552 none of the four sites emitted SESSION_NEEDS_ATTENTION at park
    time; the gap was only latency, not permanent silence -- the durable
    escalation sweep (cw.reconcile.escalation) still fires OPERATOR_
    ESCALATION once, ESCALATION_PARK_MINUTES later, for any park this
    helper does not reach (e.g. one already parked before this shipped).
    """
    # Why: emits before transition_task_status, matching _park_finalize_hold's
    # existing order above -- a #1552 review pass flagged the resulting crash
    # window (event durably logged before the caller's save_dev_queue()
    # persists the status change) but this ordering is an established,
    # unchanged pattern across every _park_* helper in this module, not new
    # risk introduced here; diverging just for this one helper would break
    # the field-for-field mirroring this ticket was explicitly scoped to do.
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _SIGNOFF_GATE_REASON,
            "breadcrumbs": "",
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    transition_task_status(
        task,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        disposition=SIGNOFF_GATE_DISPOSITION,
    )


def _park_scope_hint_gate(task: TicketTask) -> None:
    """Park *task* BLOCKED_ON_USER for a ``scope_hint=="large"`` escalation at
    the REVIEW->FINALIZE boundary (#1617).

    Shared by ``_route_stage_success`` and ``_walk_stage_pointer_forward`` --
    the two REVIEW-scoped park-decision sites that (unlike
    ``_route_scope_gated_approval``, Rule 1) do not naturally carry a
    status-derived disposition to stamp, since their incoming sentinel status
    (``stage_complete``/``shipped``) is not itself an approval-gated status.
    Reuses ``_APPROVAL_GATE_REASON`` as both the ``SESSION_NEEDS_ATTENTION``
    ``paused_status`` and the task ``disposition``, mirroring
    ``_park_finalize_hold``/``_park_signoff_gate``'s fixed-disposition-constant
    shape. Deliberately NOT used by ``_route_scope_gated_approval`` itself:
    that site's existing disposition is the incoming sentinel status (e.g.
    ``"review_pending_approval"``), an established, test-covered behavior this
    ticket does not change.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _APPROVAL_GATE_REASON,
            "breadcrumbs": "",
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    transition_task_status(
        task, QueueItemStatus.BLOCKED_ON_USER, disposition=_APPROVAL_GATE_REASON
    )


def _record_scope_routing_decision(
    task: TicketTask, last_result: dict[str, object] | None, rule: str
) -> None:
    """Emit the #1617 scope-routing audit event after a park-decision site runs.

    Reads ``task.disposition``/``task.scope_hint`` AFTER the site's mutation
    (or lack of one), so ``disposition`` reflects what actually happened, not
    just what was evaluated. Called unconditionally on every routing pass at
    each of the three ``routing.py`` park-decision sites (Rule 1, Rule 3, and
    the stage-walk's REVIEW rung) -- this is why the event is excluded from
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
        transition_task_status(
            task, QueueItemStatus.COMPLETED, disposition=disposition, pr_url=pr_url
        )
    else:
        _advance_task_pointer(task, stages)


# Maps a sentinel's ``AutoDevResult.stage_reached`` (the closed 7-value
# StageReached literal, see cw.auto_dev_result) to the pipeline Stage it
# represents completion of. Used by ``_classify_sentinel_stage_position`` to
# guard against a late/replayed sentinel from a previous leg being routed
# against whatever stage the task's row currently holds (#986 incident, GitHub
# #1019), and to classify a legitimate later-stage self-escalation (#1149).
#
# Stage.HARDEN is deliberately absent: it has no legitimate stage_reached
# counterpart (RFC 0005 A1, dormant stage) -- every one of the 7 canonical
# values below maps to PLAN/IMPL/REVIEW/FINALIZE, never HARDEN, so any
# sentinel arriving against a HARDEN-stage task always mismatches by
# construction.
_STAGE_REACHED_TO_STAGE: dict[str, Stage] = {
    "stage1_pre_flight": Stage.PLAN,
    "stage1_plan": Stage.PLAN,
    "stage2_impl": Stage.IMPL,
    "stage3_review": Stage.REVIEW,
    "stage4a_merge_gate": Stage.FINALIZE,
    "stage4b_pr_create": Stage.FINALIZE,
    "stage5_post_create": Stage.FINALIZE,
}


# Fail fast at import time if this mapping's keys ever drift from the
# canonical StageReached value set (e.g. a new stage added to the Literal in
# auto_dev_result.py without a matching entry here) -- a silent gap here
# degrades to a permanent stage-mismatch refusal with no test signal.
if set(_STAGE_REACHED_TO_STAGE) != _STAGE_REACHED_CANONICAL:
    _drift_msg = (
        "_STAGE_REACHED_TO_STAGE keys drifted from cw.auto_dev_result."
        "_STAGE_REACHED_CANONICAL -- update both together"
    )
    raise AssertionError(_drift_msg)


_StagePosition = Literal["bypass", "earlier", "same", "later", "unresolvable"]


def _classify_sentinel_stage_position(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> tuple[_StagePosition, list[Stage] | None, int | None]:
    """Classify the sentinel's mapped stage relative to ``task.stage`` (#1149).

    Returns ``(position, stages, target_idx)``. ``stages`` and ``target_idx``
    are populated only for the ``"later"`` case (the pipeline stage list and
    the walk's destination index); all other positions return ``(pos, None,
    None)``.

    - ``"bypass"``       -- no ``stage_reached`` to check (e.g. a
      ``BlockedResult``-derived payload). Routing proceeds exactly as before
      #1019.
    - ``"earlier"``      -- the sentinel's stage precedes ``task.stage``: a
      late/replayed sentinel from a previous leg (the #986 incident). Refuse.
    - ``"same"``         -- exact match. Routes normally via the Rule 1-6 table,
      exactly as the pre-#1149 equality guard did.
    - ``"later"``        -- the sentinel's stage follows ``task.stage``: a
      legitimate self-escalation the row has not yet caught up to. Walk forward.
    - ``"unresolvable"`` -- a non-str ``stage_reached``, an unmapped value, an
      unknown client, or a stage absent from the client's pipeline. Fail-closed
      refuse, matching the pre-#1149 equality check's behavior for these cases.

    Position is computed via ``pipeline.stages.index`` (R2) -- never the
    ``Stage`` StrEnum's own ordering, which is alphabetical and unrelated to
    pipeline order. See ``_STAGE_REACHED_TO_STAGE`` for the
    HARDEN-always-mismatches rationale.
    """
    stage_reached = (
        last_result.get("stage_reached") if isinstance(last_result, dict) else None
    )
    if stage_reached is None:
        return "bypass", None, None
    mapped = (
        _STAGE_REACHED_TO_STAGE.get(stage_reached)
        if isinstance(stage_reached, str)
        else None
    )
    if mapped is None:
        return "unresolvable", None, None
    if mapped == task.stage:
        # Exact match routes regardless of client/pipeline resolvability --
        # preserves the pre-#1149 equality guard, which never consulted clients.
        return "same", None, None
    # A non-matching stage needs the pipeline order to decide earlier vs later.
    client_cfg = clients.get(task.client)
    stages = client_cfg.pipeline.stages if client_cfg is not None else None
    if stages is None or task.stage not in stages or mapped not in stages:
        return "unresolvable", None, None
    sentinel_idx = stages.index(mapped)
    if sentinel_idx < stages.index(task.stage):
        return "earlier", None, None
    return "later", stages, sentinel_idx


def _walk_stage_pointer_forward(
    task: TicketTask,
    stages: list[Stage],
    target_idx: int,
    clients: dict[str, ClientConfig],
    last_result: dict[str, object] | None,
) -> Literal["proceed", "parked"]:
    """Walk ``task.stage`` forward to ``stages[target_idx]``, one rung at a time.

    Each rung advances through ``_advance_task_pointer`` (the shared
    ``TASK_STAGE_CHANGED`` chokepoint, dev_queue.py), so every real stage move
    emits exactly one event. Before crossing a REVIEW rung three gates are
    checked, in order (#1617 D1/D3):

      1. The scope_hint escalation gate: an operator/queue ``scope_hint`` of
         ``"large"`` outranks both gates below and parks the task
         ``BLOCKED_ON_USER``/``approval_gate`` -- the third park-decision site
         this ticket closes (the Checkpoint-3a-headless-auto-continue bypass).
      2. Otherwise the RFC 0011 A3 proactive finalize hold (#1160): the walk
         stops at REVIEW and parks the task ``BLOCKED_ON_USER``/
         ``finalize_gate_held``.
      3. Otherwise the operator-signoff gate -- if it applies, the walk stops
         at REVIEW and parks the task ``AWAITING_OPERATOR_SIGNOFF`` (signoff is
         the ship checkpoint, REVIEW->FINALIZE; RFC 0007 Phase 3, #990).

    Every REVIEW-rung evaluation -- gated or not -- emits the #1617
    scope-routing audit event (``_record_scope_routing_decision``) so a
    bypass is diagnosable after the fact.

    ``_advance_task_pointer`` unconditionally clears ``task.session_id`` on
    every hop ("R6: clear session_id on advance"). That is correct for a genuine
    single-hop advance, but a multi-hop walk must not blank the id before the
    landing Rule 1-6 body reads it for its ``SESSION_NEEDS_ATTENTION`` event --
    so the real id is captured once and restored after each hop. The landing
    Rule's own genuine advance (Rule 3) still clears it, exactly as pre-#1149
    single-hop behavior. See GitHub #1149 (plan-review MUST_FIX #1).
    """
    original_session_id = task.session_id
    while stages.index(task.stage) < target_idx:
        if task.stage == Stage.REVIEW and _should_gate_for_scope_hint(
            task, last_result
        ):
            _park_scope_hint_gate(task)
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            return "parked"
        # Not `elif` (ruff RET505: an elif after a `return` is redundant) --
        # the `return` above already makes this exclusive with the branches
        # below, unlike the landing Rule 1-6 sites (which have no early return
        # and so use a real `elif` chain instead).
        if task.stage == Stage.REVIEW and _should_force_hold_finalize(task, clients):
            _park_finalize_hold(task)
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            return "parked"
        if task.stage == Stage.REVIEW and _should_gate_for_signoff(task, clients):
            _park_signoff_gate(task)
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            return "parked"
        if task.stage == Stage.REVIEW:
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
        _advance_task_pointer(task, stages)
        task.session_id = original_session_id
    return "proceed"


def _resolve_stage_walk(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> Literal["refuse", "proceed", "parked"]:
    """Decide how a sentinel's stage position routes against ``task.stage`` (#1149).

    Earlier-stage replays and unresolvable positions refuse (fail-closed, the
    #1019/#986 guard, preserved); same-stage and bypass proceed to the ordinary
    Rule 1-6 table (unchanged); a later-stage sentinel walks ``task.stage``
    forward to the sentinel's stage via ``_walk_stage_pointer_forward``, then
    proceeds (or parks at a REVIEW signoff gate). The walk mutates ``task.stage``
    in place as a side effect -- the caller then applies the Rule 1-6 table at
    the now-matching stage.
    """
    position, stages, target_idx = _classify_sentinel_stage_position(
        task, last_result, clients
    )
    if position == "later" and stages is not None and target_idx is not None:
        return _walk_stage_pointer_forward(
            task, stages, target_idx, clients, last_result
        )
    if position in ("earlier", "unresolvable"):
        return "refuse"
    return "proceed"


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
    """
    tier = _resolve_scope_tier(last_result, task)
    if tier != SCOPE_TIER_SMALL:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": task.session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": _APPROVAL_GATE_REASON,
                "breadcrumbs": "",
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
        transition_task_status(
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=disposition
        )
        _record_scope_routing_decision(task, last_result, _RULE_SCOPE_GATED_APPROVAL)
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


def _route_stage_success(
    task: TicketTask,
    clients: dict[str, ClientConfig],
    disposition: str | None,
    pr_url: str | None,
    last_result: dict[str, object] | None,
) -> None:
    """Rule 3 body: shipped/stage_complete -- advance or complete.

    Three REVIEW-scoped gates run ahead of the advance, in order (#1617 D1):
    the scope_hint escalation gate, then the RFC 0011 A3 proactive finalize
    hold (#1160), then the operator-signoff gate. The scope_hint gate wins
    outright over both -- an operator/queue ``scope_hint`` of ``"large"``
    means "gate this," full stop -- and the hold in turn wins outright over
    signoff when both of those are armed -- see ``_route_scope_gated_approval``.

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
    if task.stage == Stage.REVIEW and _should_gate_for_scope_hint(task, last_result):
        _park_scope_hint_gate(task)
    elif task.stage == Stage.REVIEW and _should_force_hold_finalize(task, clients):
        _park_finalize_hold(task)
    elif task.stage == Stage.REVIEW and _should_gate_for_signoff(task, clients):
        _park_signoff_gate(task)
    else:
        _stage_advance_unchecked(task, clients, disposition=disposition, pr_url=pr_url)
    _record_scope_routing_decision(task, last_result, _RULE_STAGE_SUCCESS)


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
    An *earlier*-stage or unresolvable sentinel (a late/replayed sentinel from a
    previous leg, the #986 incident) is refused: a true no-op -- no status
    transition, no ``save_dev_queue`` by callers that gate on the return value
    -- and ``SENTINEL_STAGE_MISMATCH`` is emitted for observability. A *later*-
    stage sentinel (a legitimate self-escalation the row lags behind) walks
    ``task.stage`` forward one rung at a time to the sentinel's stage, then the
    Rule 1-6 table applies at the now-matching stage; if a REVIEW gate (the A3
    finalize hold, #1160, or the signoff gate) intervenes the walk parks the
    task and returns without applying the table.
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
    if status in SCOPE_GATED_APPROVAL_STATUSES:
        # Rule 1: scope-gated approval; small tier auto-advances, large blocks.
        # Must fire before Rule 2 (SCOPE_GATED ⊂ PAUSED_FOR_USER_INPUT).
        _route_scope_gated_approval(task, clients, last_result, disposition, pr_url)
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
            task, QueueItemStatus.BLOCKED_ON_USER, disposition=disposition
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
        )
    elif status == "no_op":
        # Rule 4: pre-flight already satisfied -- terminal
        # regardless of remaining stages
        transition_task_status(task, QueueItemStatus.COMPLETED, disposition="no_op")
    elif status in STAGE_FAILURE_STATUSES:
        # Rule 5: blocked/merge_gate_blocked/scope_exceeded/forbidden_area
        # Sub-rule 5a: "blocked" at FINALIZE with a regress-eligible blocker
        # reason and attempts below cap → regress to IMPL for self-heal (#770).
        # scope_exceeded/forbidden_area have no blocker field (validator enforces
        # this) so they always fall through to BLOCKED_ON_USER. merge_gate_blocked
        # MAY carry one (schema.py's #777 exception), but 5a is gated on
        # status=="blocked" so it still cannot regress. blocker/blocker_reason are
        # read once above, before the rule table (#1254).
        if (
            status == "blocked"
            and task.stage == Stage.FINALIZE
            and blocker_reason in FINALIZE_REGRESS_BLOCKER_REASONS
            and task.regress_attempts < FINALIZE_REGRESS_CAP
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
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=disposition,
            blocked_reason=blocker_reason,
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
            task, QueueItemStatus.BLOCKED_ON_USER, disposition="abandoned"
        )
    return True
