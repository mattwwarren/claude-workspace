"""Sentinel stage classification and the forward stage-pointer walk.

Extracted from the flat ``dispatch/routing.py`` by #1728 — the largest single
concern in that module. Everything here answers one question ahead of the Rule
1-6 table: *where does this sentinel's ``stage_reached`` sit relative to
``task.stage``, and what does the pipeline pointer have to do about it?* The
answers are ``"refuse"`` (a late/replayed sentinel from a previous leg, the
#986/#1019 guard), ``"proceed"`` (route the table at the task's stage), or
``"parked"`` (a REVIEW gate stopped a multi-rung forward walk).

The gate predicates and park helpers the walk consults live in
``dispatch/review_gates.py`` (#1823); the routing table those verdicts feed
lives in this package's ``__init__``.

**Deferred-import discipline.** ``routing/__init__.py`` imports this module at
module top, so this module's one reach back into the package
(``_record_scope_routing_decision`` / ``_RULE_STAGE_WALK``, both of which must
stay defined in ``__init__`` — see its "Monkeypatch coupling" note) is a
*function-level* import inside ``_walk_stage_pointer_forward``. Promoting it to
module top would import a partially initialized ``cw.dispatch.routing`` and the
package would stop importing at all. Identical shape and rationale to
``review_gates.py``'s two deferred imports (#1823) and ``claim.py``'s (#1310);
covered by ``test_dispatch_package_submodules_import_without_cycle``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cw.auto_dev_result import (
    _STAGE_REACHED_CANONICAL,
    STAGE_SUCCESS_STATUSES,
)
from cw.dev_queue import _advance_task_pointer
from cw.dispatch.regress_repeat import (
    _consume_finalize_regress_repeat,
    _maybe_emit_finalize_regress_repeat_signal,
)
from cw.dispatch.review_gates import (
    _park_branch_staleness_gate,
    _park_empty_diff_gate,
    _park_finalize_hold,
    _park_scope_hint_gate,
    _park_signoff_gate,
    _should_force_hold_finalize,
    _should_gate_for_branch_staleness,
    _should_gate_for_empty_diff,
    _should_gate_for_scope_hint,
    _should_gate_for_signoff,
)
from cw.models import Stage

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        TicketTask,
    )


# Maps a sentinel's ``AutoDevResult.stage_reached`` (the closed 7-value
# StageReached literal, see cw.auto_dev_result) to the pipeline Stage it
# represents completion of. Used by ``_classify_sentinel_stage_position`` to
# guard against a late/replayed sentinel from a previous leg being routed
# against whatever stage the task's row currently holds (#986 incident, GitHub
# #1019), and to classify a legitimate later-stage self-escalation (#1149).
# GitHub #1676 narrowed the earlier-stage arm of that guard: only a
# STAGE_SUCCESS_STATUSES sentinel (the shape the #986/#1019 replay race can
# actually produce) still refuses at an earlier stage -- see
# ``_is_stage_advance_claim`` and ``_resolve_stage_walk``. A follow-up in the
# same ticket then gated the two Rule 1-6 bodies that perform their own
# additional task.stage-mutating decision beyond the table's ordinary status
# transition (Rule 5a's FINALIZE self-heal regress, Rule 1's small-tier
# auto-advance) on ``_is_earlier_stage_report`` -- neither is safe to fire on
# a sentinel that never reached task.stage, even though the table's ordinary
# transition now routes it correctly.
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


def _is_stage_advance_claim(last_result: dict[str, object] | None) -> bool:
    """True iff *last_result* claims a stage-advance (GitHub #1676).

    Only a ``STAGE_SUCCESS_STATUSES`` sentinel (``shipped``/``stage_complete``)
    can be the subject of the #986/#1019 same-session multi-observation replay
    race this guard exists for: those are the only statuses a live worker
    self-escalates on, walking ``task.stage`` forward one rung at a time via
    ``_walk_stage_pointer_forward`` while preserving ``session_id`` across
    hops, which is exactly the shape a stale/replayed observation of an
    earlier hop can be mistaken for. Every other terminal status (``blocked``,
    a pause, a scope-gated approval, ``no_op``, ...) reported at an earlier
    stage is a legitimate "could not reach the dispatched stage" outcome, not
    a replay -- ``_resolve_stage_walk`` uses this to narrow the earlier-stage
    refusal to advance-claims only.
    """
    return isinstance(last_result, dict) and last_result.get("status") in (
        STAGE_SUCCESS_STATUSES
    )


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
    - ``"earlier"``      -- the sentinel's stage precedes ``task.stage``. This
      classifier is a pure ordinal position -- it does not itself decide
      refuse/proceed. ``_resolve_stage_walk`` refuses an ``"earlier"``
      position only when the sentinel is a stage-advance claim
      (``_is_stage_advance_claim``, GitHub #1676): the late/replayed-sentinel
      shape from a previous leg (the #986 incident). Every other earlier-stage
      status is a legitimate "could not reach the dispatched stage" report and
      proceeds to the ordinary Rule 1-6 table at the task's unchanged stage.
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


def _is_earlier_stage_report(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> bool:
    """True iff the sentinel's ``stage_reached`` precedes ``task.stage`` (GitHub #1676).

    ``_resolve_stage_walk``'s narrowed refusal (``_is_stage_advance_claim``)
    already lets a non-advance-claim earlier-stage sentinel proceed to the
    ordinary Rule 1-6 table at ``task.stage`` unchanged -- that part is
    correct and covers Rule 2/Rule 4/Rule 5's main body as-is. But two Rule
    bodies perform an *additional* ``task.stage``-mutating decision beyond
    the table's ordinary status transition, and both implicitly assumed the
    sentinel reflects work actually done AT ``task.stage``: Rule 5a's
    FINALIZE self-heal regress, and Rule 1's small-tier auto-advance
    (``_route_scope_gated_approval``). An earlier-stage sentinel breaks that
    assumption -- it never reached ``task.stage``, so regressing or
    advancing ``task.stage`` on its say-so is wrong regardless of its
    ``blocker.reason`` or resolved scope tier. This predicate gates exactly
    those two sites; it does not change ``_resolve_stage_walk`` itself.
    """
    position, _, _ = _classify_sentinel_stage_position(task, last_result, clients)
    return position == "earlier"


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
    emits exactly one event. Before crossing a REVIEW rung the gates are
    checked, in order (#1617 D1/D3, extended by #1823 and #1870):

      1. The empty-diff gate (#1870): the branch has zero commits ahead of
         ``origin/<default_branch>``, so the walk stops at REVIEW and parks the
         task ``BLOCKED_ON_USER``/``empty_diff_gate``. Ahead of everything
         below -- there is nothing for the other gates to reason about.
      2. Otherwise the branch-staleness gate (#1823): the branch is behind
         ``origin/<default_branch>`` with overlapping churn; parks
         ``BLOCKED_ON_USER``/``branch_behind_main``.
      3. Otherwise the scope_hint escalation gate: an operator/queue
         ``scope_hint`` of ``"large"`` outranks both gates below and parks the
         task ``BLOCKED_ON_USER``/``approval_gate`` -- the third park-decision
         site #1617 closed (the Checkpoint-3a-headless-auto-continue bypass).
      4. Otherwise the RFC 0011 A3 proactive finalize hold (#1160): the walk
         stops at REVIEW and parks the task ``BLOCKED_ON_USER``/
         ``finalize_gate_held``.
      5. Otherwise the operator-signoff gate -- if it applies, the walk stops
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
    # Deferred, not module-top: _record_scope_routing_decision and
    # _RULE_STAGE_WALK stay in routing/__init__.py (the former calls the
    # monkeypatched record_event binding and must resolve against that module
    # object), and routing/__init__.py imports this module at module top. See
    # this module's docstring on the #1310/#1823 precedent.
    from cw.dispatch.routing import _RULE_STAGE_WALK, _record_scope_routing_decision

    original_session_id = task.session_id
    # #1717: captured once, before any hop -- mirrors original_session_id
    # above. _advance_task_pointer clears task.stage_base_ref on every hop
    # (same R6-style side effect as session_id), so the live value is already
    # gone by the time the walk reaches the REVIEW rung on the very same hop
    # that carried it there. No real dispatch claim happens mid-walk, so this
    # pre-walk snapshot is the correct "current head" proxy throughout.
    original_stage_base_ref = task.stage_base_ref
    while stages.index(task.stage) < target_idx:
        # Computed once per iteration, before any REVIEW-rung gate check
        # below, and consumed (cleared) here regardless of which gate -- if
        # any -- fires below. REVIEW is visited at most once per walk (the
        # while condition only moves forward), so this is the walk's single
        # consumption point for the FINALIZE-regress repeat marker.
        # _consume_finalize_regress_repeat itself no-ops at any non-REVIEW
        # stage.
        is_repeat = _consume_finalize_regress_repeat(task, original_stage_base_ref)
        if task.stage == Stage.REVIEW and _should_gate_for_empty_diff(task, clients):
            # Why ahead of even the staleness gate: staleness asks whether the
            # branch's content still matches main, which presumes there IS
            # content. A branch with zero commits has nothing to be stale about
            # and nothing to approve -- answering "may this ship?" at all is the
            # bug (#1870).
            _park_empty_diff_gate(task)
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
            return "parked"
        if task.stage == Stage.REVIEW and _should_gate_for_branch_staleness(
            task, clients
        ):
            # Why first: every gate below reasons about a sentinel produced
            # against this branch's tree. If the tree no longer matches
            # origin/<default> in a file-overlapping way, that sentinel's
            # verdict -- scope, health, everything -- describes a tree that is
            # not what would ship (#1823).
            _park_branch_staleness_gate(task)
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
            return "parked"
        if task.stage == Stage.REVIEW and _should_gate_for_scope_hint(
            task, last_result
        ):
            _park_scope_hint_gate(task)
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
            return "parked"
        # Not `elif` (ruff RET505: an elif after a `return` is redundant) --
        # the `return` above already makes this exclusive with the branches
        # below, unlike the landing Rule 1-6 sites (which have no early return
        # and so use a real `elif` chain instead).
        if task.stage == Stage.REVIEW and _should_force_hold_finalize(task, clients):
            _park_finalize_hold(task)
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
            return "parked"
        if task.stage == Stage.REVIEW and _should_gate_for_signoff(task, clients):
            _park_signoff_gate(task)
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
            return "parked"
        if task.stage == Stage.REVIEW:
            _record_scope_routing_decision(task, last_result, _RULE_STAGE_WALK)
            _maybe_emit_finalize_regress_repeat_signal(task, is_repeat)
        _advance_task_pointer(task, stages)
        task.session_id = original_session_id
    return "proceed"


def _resolve_stage_walk(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> Literal["refuse", "proceed", "parked"]:
    """Decide how a sentinel's stage position routes against ``task.stage`` (#1149).

    Unresolvable positions always refuse (fail-closed, the #1019/#986 guard,
    preserved). An earlier-stage position refuses only when the sentinel is a
    stage-advance claim (``_is_stage_advance_claim``, GitHub #1676) --
    narrowed from an unconditional earlier-stage refusal, since only a
    ``STAGE_SUCCESS_STATUSES`` sentinel can be the #986/#1019 same-session
    replay this guard exists for. Every other earlier-stage status (a
    pause, a scope-gated approval, ``no_op``, ``blocked``, ...) is a
    legitimate "could not reach the dispatched stage" report and proceeds to
    the ordinary Rule 1-6 table at the task's unchanged stage, same as
    same-stage and bypass. A later-stage sentinel walks ``task.stage`` forward
    to the sentinel's stage via ``_walk_stage_pointer_forward``, then proceeds
    (or parks at a REVIEW signoff gate). The walk mutates ``task.stage`` in
    place as a side effect -- the caller then applies the Rule 1-6 table at
    the now-matching stage.
    """
    position, stages, target_idx = _classify_sentinel_stage_position(
        task, last_result, clients
    )
    if position == "later" and stages is not None and target_idx is not None:
        return _walk_stage_pointer_forward(
            task, stages, target_idx, clients, last_result
        )
    if position == "unresolvable":
        return "refuse"
    if position == "earlier" and _is_stage_advance_claim(last_result):
        return "refuse"
    return "proceed"
