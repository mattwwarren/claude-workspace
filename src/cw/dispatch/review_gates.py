"""The REVIEW-scoped gate table for the dispatch staged-decision router.

Every gate here answers one question about a ticket sitting at the
REVIEW->FINALIZE ship checkpoint: *may this advance unattended, or must an
operator look at it first?* Each is a ``_should_gate_for_*`` predicate paired
with a ``_park_*`` helper that emits ``SESSION_NEEDS_ATTENTION`` and stamps the
terminal status/disposition. The call-site wiring -- which gate runs first at
which of the three park-decision sites -- lives in ``dispatch/routing.py``, not
here; this module owns the gates themselves, not the routing table.

Five gates, in the order ``routing.py`` evaluates them:

  1. ``_should_gate_for_branch_staleness`` (#1823) -- the branch is behind
     ``origin/<default_branch>`` and the intervening main commits overlap it.
  2. ``_should_gate_for_review_health`` (#1702) -- the review self-reported
     ``EXIT_FOR_HUMAN_REVIEW``.
  3. ``_should_gate_for_scope_hint`` (#1617) -- an operator/queue
     ``scope_hint`` of ``"large"``.
  4. ``_should_force_hold_finalize`` (RFC 0011 A3, #1160) -- a proactive
     operator stop before an unattended finalize.
  5. ``_should_gate_for_signoff`` (RFC 0007 Phase 3, #990) -- an explicit
     operator signature slot.

Extracted from ``dispatch/routing.py`` by #1823 (it was 1482 lines, ~48% over
CLAUDE.md's ~1000-line ceiling; a fuller split remains #1728's job). Every name
here is re-exported through ``dispatch/__init__.py``'s existing facade, exactly
as ``routing.py``'s own surface is, so no ``from cw.dispatch import X`` call
site changed.

**Deferred-import discipline.** ``routing.py`` imports this module at module
top. Two gates need state that stays in ``routing.py``
(``_resolve_scope_tier``, ``_APPROVAL_GATE_REASON``) and reach back for it via
*function-level* imports. Promoting either to a module-top import recreates a
genuine circular import and ``cw.dispatch`` stops importing at all. This is the
same shape ``gating.py``/``claim.py`` have carried since #1310, guarded by the
same test (``test_dispatch_package_submodules_import_without_cycle``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import SCOPE_TIER_LARGE
from cw.config import load_effective_config
from cw.dev_queue import (
    BRANCH_STALENESS_GATE_DISPOSITION,
    FINALIZE_GATE_HELD_DISPOSITION,
    REVIEW_HEALTH_GATE_DISPOSITION,
    SIGNOFF_GATE_DISPOSITION,
    transition_task_status,
)
from cw.dispatch.branch_freshness import has_overlapping_branch_staleness
from cw.events import record_event
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
)

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        OrchestratorConfig,
        TicketTask,
    )


# paused_status written to SESSION_NEEDS_ATTENTION when the RFC 0011 A3
# proactive finalize hold parks a ticket at the REVIEW->FINALIZE checkpoint.
# Deliberately distinct from dev_queue.lifecycle.FINALIZE_GATE_HELD_DISPOSITION
# ("finalize_gate_held") -- that constant classifies TicketTask.disposition,
# this one is a paused_status string. Different namespaces, same event. Follows
# the _APPROVAL_GATE_REASON / _UNKNOWN_CLIENT_REASON convention in routing.py.
# See GitHub #1160.
_FINALIZE_HOLD_REASON = "finalize_hold"


# paused_status written to SESSION_NEEDS_ATTENTION when the RFC 0007
# Phase 3 operator-signoff gate parks a ticket at the REVIEW->FINALIZE
# ship checkpoint (GitHub #990, #1552). Shares its literal string value
# with dev_queue.lifecycle.SIGNOFF_GATE_DISPOSITION (task.disposition) by
# deliberate choice on that ticket -- unlike the _FINALIZE_HOLD_REASON /
# FINALIZE_GATE_HELD_DISPOSITION pair, which uses distinct strings across
# the two namespaces. See GitHub #1552.
_SIGNOFF_GATE_REASON = "signoff_gate"


# paused_status written to SESSION_NEEDS_ATTENTION when the #1702 review-health
# gate parks a REVIEW-stage ticket whose sentinel reported
# health.recommendation == "EXIT_FOR_HUMAN_REVIEW". Shares its literal string
# value with dev_queue.lifecycle.REVIEW_HEALTH_GATE_DISPOSITION
# (task.disposition) by deliberate choice, following the _SIGNOFF_GATE_REASON /
# SIGNOFF_GATE_DISPOSITION precedent above rather than the _FINALIZE_HOLD_REASON
# / FINALIZE_GATE_HELD_DISPOSITION one -- still two constants in two namespaces,
# do not collapse them.
_REVIEW_HEALTH_GATE_REASON = "review_health_gate"


# paused_status written to SESSION_NEEDS_ATTENTION when the #1823 branch-
# staleness gate parks a REVIEW-stage ticket whose branch is behind
# origin/<default_branch> with overlapping intervening churn. Shares its
# literal string value with dev_queue.lifecycle.
# BRANCH_STALENESS_GATE_DISPOSITION (task.disposition) on the same
# _REVIEW_HEALTH_GATE_REASON precedent above -- still two constants in two
# namespaces, do not collapse them.
_BRANCH_STALENESS_REASON = "branch_behind_main"


# The single Health.recommendation value that means "the producer does not
# vouch for this work". Pinned as a constant rather than an inline literal
# because it is compared against a raw sentinel dict (post-model_dump), where
# the schema's Literal type gives no compile-time protection against a typo.
# See cw.auto_dev_result.schema.Health.recommendation.
_DEGRADED_HEALTH_RECOMMENDATION = "EXIT_FOR_HUMAN_REVIEW"


def _resolve_health_recommendation(last_result: dict[str, object] | None) -> str | None:
    """Pull ``health.recommendation`` off a raw sentinel dict (#1702).

    Same defensive isinstance-guard shape as ``routing._extract_scope_tier``: a
    missing, null, or non-dict ``health`` block resolves to ``None`` rather
    than raising. ``health`` is required by the ``AutoDevResult`` schema, so a
    schema-valid sentinel always carries it -- but this function is also
    reachable from paths that never validated (Rule 6's non-dict fallback
    parks ``abandoned`` before either gated function runs, so it cannot reach
    here today; the guard is belt-and-suspenders against a future caller).
    """
    health_val = last_result.get("health") if last_result is not None else None
    if not isinstance(health_val, dict):
        return None
    recommendation = health_val.get("recommendation")
    return recommendation if isinstance(recommendation, str) else None


def _should_gate_for_review_health(last_result: dict[str, object] | None) -> bool:
    """True iff *last_result* reports degraded review health (#1702).

    ``_derive_health`` (codex review) computes an accurate
    ``Health.recommendation`` from real reviewer participation, but before
    #1702 nothing downstream read it -- ``_route_stage_success`` and
    ``_route_scope_gated_approval`` advanced purely off ``status``, so a review
    that self-reported "I could not vouch for this" still shipped unattended.

    Callers MUST scope this to ``task.stage == Stage.REVIEW``. It is
    deliberately NOT stage-agnostic: ``local_runner.synthesize_git_result``
    hardcodes ``EXIT_FOR_HUMAN_REVIEW`` on its only success path (#1580) as an
    honest "I have no reviewer, so I cannot claim this is vetted" default, not
    a derived review signal. Gating on that at IMPL stage would misread the
    default as a real degraded-review verdict and permanently park every
    LOCAL-backend IMPL completion, silently disabling the documented unattended
    ``IMPL -> REVIEW`` auto-advance (config/CONFIG_REFERENCE.md).

    Anything other than the degraded literal -- ``"PROCEED"``, a missing or
    malformed ``health`` block, a non-str value -- is "not degraded", so this
    is a pure boolean gate with no effect on any pre-existing routing path.
    """
    recommendation = _resolve_health_recommendation(last_result)
    return recommendation == _DEGRADED_HEALTH_RECOMMENDATION


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
    # Deferred, not module-top: _resolve_scope_tier stays in routing.py (it is
    # also called directly by _route_scope_gated_approval and
    # _record_scope_routing_decision), and routing.py imports this module at
    # module top. See this module's docstring on the #1310 precedent.
    from cw.dispatch.routing import _resolve_scope_tier

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


def _should_gate_for_branch_staleness(
    task: TicketTask, clients: dict[str, ClientConfig]
) -> bool:
    """True iff *task*'s branch is stale against ``origin/<default_branch>`` (#1823).

    "Stale" is the narrow, file-overlap-scoped sense (the ticket's option B):
    behind main AND the intervening main commits touch at least one file the
    branch itself touches. A branch that has merely fallen behind, with
    disjoint churn, is NOT gated -- see
    ``branch_freshness.has_overlapping_branch_staleness``.

    Callers MUST scope this to ``task.stage == Stage.REVIEW``, mirroring all
    four sibling gates: staleness only invalidates a *review* verdict, and
    gating earlier stages would park every ticket whose branch drifted mid-IMPL
    rather than letting it finish and be re-reviewed.

    Fails open on an unresolvable client -- without a ``ClientConfig`` there is
    no authoritative ``default_branch``, and guessing ``"main"`` would park
    rows on any client that does not use it. Two-arg predicate shape matching
    ``_should_gate_for_signoff``/``_should_force_hold_finalize`` so every gate
    check reads identically at each call site.
    """
    client_cfg = clients.get(task.client)
    if client_cfg is None:
        return False
    return has_overlapping_branch_staleness(
        task.worktree_path, client_cfg.default_branch
    )


def _park_finalize_hold(task: TicketTask) -> None:
    """Park *task* BLOCKED_ON_USER for an A3 force-hold (RFC 0011 A3, #1160).

    Shared by all three REVIEW-scoped gate sites in ``routing.py`` so neither
    the attention payload nor the status/disposition pairing can drift between
    them; the surrounding control flow (``return "parked"`` vs falling through
    an ``if``/``elif``/``else``) stays at each call site because it differs per
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

    Shared by all four signoff-park sites -- three in ``routing.py``'s staged
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
    # the field-for-field mirroring that ticket was explicitly scoped to do.
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
    ``"review_pending_approval"``), an established, test-covered behavior.
    """
    # Deferred, not module-top: _APPROVAL_GATE_REASON stays in routing.py (it
    # is also used directly by _route_scope_gated_approval's own non-small-tier
    # park), and routing.py imports this module at module top. See this
    # module's docstring on the #1310 precedent.
    from cw.dispatch.routing import _APPROVAL_GATE_REASON

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


def _park_review_health_gate(task: TicketTask) -> None:
    """Park *task* BLOCKED_ON_USER for degraded review health (#1702).

    Shared by ``_route_stage_success`` (Rule 3) and
    ``_route_scope_gated_approval`` (Rule 1) so neither the attention payload
    nor the status/disposition pairing can drift between the two sites.
    Field-for-field mirror of ``_park_scope_hint_gate`` above, including its
    emit-before-transition ordering (see ``_park_signoff_gate``'s note on why
    that ordering is kept uniform across every ``_park_*`` helper here).

    BLOCKED_ON_USER, not AWAITING_OPERATOR_SIGNOFF: the signoff status means
    "waiting for an operator to authorize a shippable result", and this result
    is not yet shippable -- there is nothing to authorize until review is
    re-run. Accordingly ``cw dev-queue approve`` fails closed on a row parked
    this way (``approval.py``'s gate-release condition matches neither the
    scope-gated statuses nor the approval-gate disposition); the intended
    recovery is ``cw dev-queue requeue``/``drain``.

    ``breadcrumbs`` is hardcoded ``""`` here, deliberately (#1729) -- see the
    module-level comment above ``routing.BREADCRUMB_ELIGIBLE_PAUSED_STATUSES``.
    #1775 reaffirms that choice specifically for a degraded reviewer's stated
    reason: it has no data path into this function's scope (``task`` only,
    no ``ReviewVerdict`` in hand) and does not need one, because
    ``render_verdict_comment`` has already posted it to the ticket by the
    time this park runs -- mirroring how ``routing._park_must_fix_mechanically_
    rejected`` documents its own deliberate divergence from this same
    convention.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _REVIEW_HEALTH_GATE_REASON,
            "breadcrumbs": "",
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=REVIEW_HEALTH_GATE_DISPOSITION,
    )


def _park_branch_staleness_gate(task: TicketTask) -> None:
    """Park *task* BLOCKED_ON_USER for a stale ticket branch (#1823).

    Shared by all three REVIEW-scoped park-decision sites in ``routing.py``.
    Field-for-field mirror of ``_park_review_health_gate`` above, including its
    emit-before-transition ordering.

    BLOCKED_ON_USER with a disposition distinct from the incoming sentinel
    status is the load-bearing part of this gate. Both release paths key off
    something the staleness check never touches -- ``cw dev-queue approve``
    reads ``session.last_result.status`` (still ``review_pending_approval``
    here) and the RFC 0009 ``auto_approve_clean_review`` recipe reads the same
    sentinel's five clean-review fields -- so without a divergent
    ``task.disposition`` for those two to exclude on, a staleness park would be
    released by either one and ship the stale tree anyway. See
    ``dev_queue.approval._not_at_approval_gate`` and
    ``reconcile.gate_recipes._detect_auto_approve_review``.

    ``breadcrumbs`` is hardcoded ``""``, following the same #1729 convention
    every gate-class park here uses; the recovery is ``cw dev-queue
    requeue``/``drain`` after rebasing the branch.
    """
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": task.session_id or "",
            "session_name": "",
            "client": task.client,
            "ticket_id": task.ticket_id,
            "claude_session_id": None,
            "paused_status": _BRANCH_STALENESS_REASON,
            "breadcrumbs": "",
            "crashed": False,
            "lane": task.lane,
        },
        correlation_id=task.ticket_id,
    )
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=BRANCH_STALENESS_GATE_DISPOSITION,
    )
