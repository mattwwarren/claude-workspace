"""Policy routing and act-phase orchestration for the phantom sweep.

Extracted verbatim from the historical flat ``cw.reconcile.phantom`` module
by the package split. This module owns the reap-policy routing gate and the
``_act_on_phantom_candidates`` driver that fans candidates out to
``_mutations`` / ``_events``. See GitHub #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import save_state
from cw.events import record_event
from cw.models import (
    CompletionReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionStatus,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _UNRESOLVED_SUBAGENT_SPAWN_REASON,
    ProposedAction,
    _apply_queue_mutations,
    resolve_reap_policy,
)
from cw.reconcile.phantom._detect import _split_crash_candidates
from cw.reconcile.phantom._events import (
    _emit_phantom_routed_events,
    _emit_phantom_terminal_events,
    _emit_sentinel_mismatch_veto_escalation_events,
)
from cw.reconcile.phantom._mutations import (
    _apply_phantom_queue_mutations,
    _apply_phantom_routed_mutations,
    _apply_phantom_salvage_mutations,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.auto_dev_result import AutoDevResult
    from cw.models import CwState, Session
    from cw.reconcile._shared import ReapCandidate


def _route_phantom_by_policy(
    candidates: list[ReapCandidate],
    *,
    config: OrchestratorConfig | None,
    merged_ticket_ids: frozenset[str],
    gh_blocked_ticket_ids: frozenset[str],
) -> tuple[list[ReapCandidate], list[ReapCandidate]]:
    """Apply per-lane reap-policy routing to clean CRASH_COMPLETE candidates.

    Under ``ReapPolicy.SIGNAL_ONLY`` a clean (non-dirty) CRASH_COMPLETE candidate
    is routed to BLOCKED_ON_USER instead of passing through. Dirty phantoms and
    merged / gh-blocked tickets always pass through (#637). SALVAGE_COMPLETION
    candidates are unaffected.

    Returns ``(auto_candidates, escalate_candidates)``. ``auto_candidates`` are
    the survivors that pass through to the normal act phase. ``escalate_candidates``
    are the SIGNAL_ONLY-rerouted CRASH_COMPLETE candidates whose sentinel-mismatch
    veto was *cap-exhausted* (#1449): the task still routes silently to
    BLOCKED_ON_USER via the existing mutation, but they additionally drive an
    immediate session.needs_attention in :func:`_act_on_phantom_candidates` so a
    still-live already_refused worker that has exhausted its veto budget surfaces
    to the operator this same tick (mirrors stalled.py's wall-clock escalation).

    GitHub #1646 adds one **unconditional** reroute ahead of the policy lookup:
    a clean crash whose worktree carries an unresolved subagent-spawn stamp goes
    to BLOCKED_ON_USER regardless of the lane's configured ``reap_policy``, and
    under its own disposition. ``reap_policy: auto`` is opt-in per lane and this
    crash class is rare, but silently retrying a session that has committed work
    behind a verification tail that never ran compounds the exact failure the
    stamp exists to catch — so this one class is a deliberate, documented
    exception to that policy (see docs/dispatch-runbook.md §7).

    The override requires a ``ticket_id``: there is no dev-queue row to park a
    ticket-less DAEMON session (an ad-hoc ``cw start`` ``idea``/``debt``
    worker) under, so such a candidate falls through to the ordinary
    policy-resolution branch below instead — the same place it landed before
    #1646 existed. A version of this override that unconditionally intercepted
    every unresolved-spawn candidate, ticket-less or not, would silently drop
    the ticket-less ones from every downstream list (they satisfy none of
    ``unresolved_spawn_mutations``, ``escalate_candidates``, or
    ``auto_candidates``) and leave the session live forever, re-detected as
    phantom on every future tick — a regression on ``reap_policy: auto`` lanes
    caught during review (#1646).
    """
    effective_config = config if config is not None else OrchestratorConfig()
    clients = _deps.load_effective_clients()
    # Route each clean CRASH_COMPLETE candidate individually based on its lane's policy.
    # Merged-PR / gh-blocked check (GitHub #637) runs BEFORE policy routing so
    # that a confirmed-merged ticket is always completed, even under SIGNAL_ONLY.
    # Dirty phantoms always go to BLOCKED_ON_USER regardless of policy.
    signal_mutations: dict[str, QueueItemStatus] = {}
    unresolved_spawn_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
    escalate_candidates: list[ReapCandidate] = []
    for c in candidates:
        if c.proposed_action != ProposedAction.CRASH_COMPLETE or c.worktree_dirty:
            auto_candidates.append(c)
            continue
        if c.ticket_id and (
            c.ticket_id in merged_ticket_ids or c.ticket_id in gh_blocked_ticket_ids
        ):
            auto_candidates.append(c)
            continue
        # #1646: unconditional override -- deliberately BEFORE resolve_reap_policy,
        # not after. Routing it afterwards would leave the AUTO branch below one
        # refactor away from reclaiming the candidate and silently reverting it
        # to PENDING, which is the exact retry this class must never get.
        # Gated on ticket_id: a ticket-less candidate has no dev-queue row to
        # park, so it falls through to ordinary policy resolution below instead
        # of being dropped from every list (see the docstring's ticket_id note).
        if c.unresolved_subagent_spawn and c.ticket_id:
            unresolved_spawn_mutations[c.ticket_id] = QueueItemStatus.BLOCKED_ON_USER
            if c.veto_cap_exhausted:
                escalate_candidates.append(c)
            continue
        policy = resolve_reap_policy(c, clients, effective_config)
        if policy is ReapPolicy.SIGNAL_ONLY:
            if c.ticket_id:
                signal_mutations[c.ticket_id] = QueueItemStatus.BLOCKED_ON_USER
            # #1449: a cap-exhausted veto on a still-live already_refused
            # session escalates to the operator this tick (parity with the
            # retry-cap park) instead of a silent BLOCKED_ON_USER park.
            if c.veto_cap_exhausted:
                escalate_candidates.append(c)
        else:
            auto_candidates.append(c)
    # Two calls, not one: _apply_queue_mutations stamps a single disposition
    # across its whole batch, and this tick's batch is no longer homogeneous.
    if signal_mutations:
        _apply_queue_mutations(
            signal_mutations,
            clear_session_id=set(),
            disposition=ReapReason.PHANTOM_SURFACE.value,
        )
    if unresolved_spawn_mutations:
        _apply_queue_mutations(
            unresolved_spawn_mutations,
            clear_session_id=set(),
            disposition=_UNRESOLVED_SUBAGENT_SPAWN_REASON,
        )
    return auto_candidates, escalate_candidates


def _mark_phantom_sessions_terminal(
    session_by_id: dict[str, Session],
    crash_candidates: list[ReapCandidate],
    merged_crash_candidates: list[ReapCandidate],
    gh_blocked_crash_candidates: list[ReapCandidate],
    *,
    now: datetime,
    phantom_names: list[str],
    pending_events: list[dict[str, object]],
) -> None:
    """Flip every phantom crash session to a terminal state.

    Extracted from :func:`_act_on_phantom_candidates` (which had grown past the
    statement ceiling) with behaviour unchanged. All three populations mark
    COMPLETED with ``reap_reason=PHANTOM_SURFACE`` and land in *phantom_names*
    so the caller stops their surfaces; they differ only in completion reason
    and whether a ``SESSION_COMPLETED`` payload is queued:

    - *merged_crash_candidates* — the PR already shipped, so NORMAL, not
      CRASHED. Still phantom (absent from the daemon roster), so the caller
      still needs the name for queue cleanup.
    - *crash_candidates* — the ordinary crash; queues a completion payload.
    - *gh_blocked_crash_candidates* — the PR state could not be verified. Marked
      COMPLETED purely so they leave ``_LIVE_STATUSES`` and are not re-detected
      next tick.
    """
    for candidate in merged_crash_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.NORMAL
        session.completed_at = now
        session.reap_reason = ReapReason.PHANTOM_SURFACE
        phantom_names.append(session.name)

    for candidate in crash_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        session.reap_reason = ReapReason.PHANTOM_SURFACE
        phantom_names.append(session.name)
        crash_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "crashed": True,
        }
        if candidate.ticket_id:
            crash_payload["ticket_id"] = candidate.ticket_id
        pending_events.append(crash_payload)

    for candidate in gh_blocked_crash_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        session.reap_reason = ReapReason.PHANTOM_SURFACE
        phantom_names.append(session.name)


def _act_on_phantom_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
    merged_ticket_ids: frozenset[str] = frozenset(),
    gh_blocked_ticket_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str], bool, list[str], dict[str, AutoDevResult], list[str]]:
    """Act phase for phantom sessions: apply all mutations.

    Returns (ticket_ids_to_revert, phantom_names, usage_limited,
             salvaged_ticket_ids, salvaged_result_by_ticket, merged_completed_ids).
    ticket_ids_to_revert contains only PENDING-routed tickets (not dirty/blocked).
    merged_completed_ids contains ticket IDs completed because their PR was already
    merged (from merged_ticket_ids pre-pass; GitHub #637).

    Under ``ReapPolicy.SIGNAL_ONLY`` (default), CRASH_COMPLETE candidates
    (non-dirty only) are routed to BLOCKED_ON_USER instead of triggering
    stop/remove.  Dirty-worktree CRASH_COMPLETE already routes to
    BLOCKED_ON_USER in both policies — the gate only affects clean phantoms.
    SALVAGE_COMPLETION candidates pass through unaffected.
    Per-lane resolution: each clean CRASH_COMPLETE candidate's effective policy
    is resolved individually via resolve_reap_policy (GitHub #560).
    """
    if not candidates:
        return [], [], False, [], {}, []

    # Compute before policy routing: signal-only candidates are dropped from the
    # auto-reap list but still carry their usage_limit_detected flag (#804).
    usage_limited = any(c.usage_limit_detected for c in candidates)
    candidates, escalate_candidates = _route_phantom_by_policy(
        candidates,
        config=config,
        merged_ticket_ids=merged_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
    )
    if not candidates and not escalate_candidates:
        return [], [], usage_limited, [], {}, []

    session_by_id = {s.id: s for s in state.sessions}

    salvage_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    ]
    routed_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    ]
    veto_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.SENTINEL_STAGE_MISMATCH_VETOED
    ]
    crash_candidates, merged_crash_candidates, gh_blocked_crash_candidates = (
        _split_crash_candidates(candidates, merged_ticket_ids, gh_blocked_ticket_ids)
    )

    phantom_names: list[str] = []
    # ticket_ids to revert (only PENDING-routed, excludes dirty/BLOCKED_ON_USER)
    ticket_ids_to_revert: list[str] = []
    merged_completed_ids: list[str] = []
    salvaged_ticket_ids: list[str] = []
    salvaged_result_by_ticket: dict[str, AutoDevResult] = {}
    pending_events: list[dict[str, object]] = []

    # SENTINEL_STAGE_MISMATCH_VETOED: emit session.sentinel_stage_mismatch_vetoed
    # AND persist the incremented veto count onto the session so the veto is
    # bounded across ticks (#1449, was zero-mutation under #1281). The count is
    # the only state this action mutates; the task stays RUNNING and the session
    # stays ACTIVE/IDLE. The save_state(state) below persists it. Mirrors
    # stalled.py's PARK_VETOED loop (#976, #1445).
    for candidate in veto_candidates:
        session_by_id[
            candidate.session_id
        ].consecutive_sentinel_mismatch_vetoes = candidate.new_veto_count
        record_event(
            OrchestratorEventType.SESSION_SENTINEL_STAGE_MISMATCH_VETOED,
            {
                "ticket_id": candidate.ticket_id,
                "client": candidate.client,
                "session_id": candidate.session_id,
                "stale_minutes": candidate.stale_minutes,
                "new_veto_count": candidate.new_veto_count,
            },
            correlation_id=candidate.ticket_id,
        )

    # Sentinel-mismatch veto-cap escalation (#1449): persist the bumped counter
    # here, BEFORE save_state below, so the next tick reads it back as
    # > sentinel_mismatch_veto_cap and _sentinel_mismatch_veto_candidate's
    # exact-cap check no longer reports exhaustion. Without this the SIGNAL_ONLY
    # escalation -- which deliberately never stops the daemon or terminates the
    # session -- would re-fire every tick the session stays LIVE past the cap.
    for candidate in escalate_candidates:
        session_by_id[
            candidate.session_id
        ].consecutive_sentinel_mismatch_vetoes = candidate.new_veto_count

    _apply_phantom_salvage_mutations(
        session_by_id,
        salvage_candidates,
        now=now,
        phantom_names=phantom_names,
        salvaged_ticket_ids=salvaged_ticket_ids,
        salvaged_result_by_ticket=salvaged_result_by_ticket,
        pending_events=pending_events,
    )

    # Route emitted stage-advance sentinels (stage_complete) from exited workers
    # through the staged-advance authority instead of reverting them (#716). The
    # queue mutation happens inside _apply_sentinel_to_task (its own lock), so the
    # routed tickets are intentionally excluded from _apply_phantom_queue_mutations.
    routed_candidates = _apply_phantom_routed_mutations(
        session_by_id,
        routed_candidates,
        now=now,
        phantom_names=phantom_names,
    )

    _mark_phantom_sessions_terminal(
        session_by_id,
        crash_candidates,
        merged_crash_candidates,
        gh_blocked_crash_candidates,
        now=now,
        phantom_names=phantom_names,
        pending_events=pending_events,
    )

    # Write-ordering: queue first (task → PENDING), session second
    # (session → COMPLETED) — mirrors the canonical ordering in
    # unblock_ticket() (dev_queue.py).  If a crash occurs between the two
    # writes, the session stays ACTIVE/IDLE; the next reconcile() tick
    # re-detects it as a phantom and the queue mutation is a no-op (task
    # already PENDING, not matched by the RUNNING guard).  #867
    # Origin filter omitted: _classify_phantom_candidates sets worktree_dirty=False
    # for all non-DAEMON sessions, so non-DAEMON entries never enter this set.
    dirty_ticket_ids = {
        c.ticket_id
        for c in crash_candidates
        if c.ticket_id is not None and c.worktree_dirty
    }
    # #1646: a dirty crash never reaches _route_phantom_by_policy's override
    # (that branch only handles clean crashes), so the distinct disposition has
    # to be selected inside the dirty branch of the queue mutation instead.
    unresolved_spawn_ticket_ids = {
        c.ticket_id
        for c in crash_candidates
        if c.ticket_id is not None and c.unresolved_subagent_spawn
    }

    # Queue mutations — written before session state (safe-fail direction).
    _apply_phantom_queue_mutations(
        session_by_id,
        crash_candidates,
        merged_crash_candidates,
        gh_blocked_crash_candidates,
        salvaged_ticket_ids,
        salvaged_result_by_ticket,
        dirty_ticket_ids,
        ticket_ids_to_revert,
        merged_completed_ids,
        unresolved_spawn_ticket_ids,
    )

    save_state(state)

    for payload in pending_events:
        record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    # Return value pre-computed above as dirty_ticket_ids; call for side
    # effects only (surface stops and event emission).
    _emit_phantom_terminal_events(
        session_by_id,
        crash_candidates,
        merged_crash_candidates,
        gh_blocked_crash_candidates,
    )
    _emit_phantom_routed_events(session_by_id, routed_candidates)
    _emit_sentinel_mismatch_veto_escalation_events(session_by_id, escalate_candidates)

    return (
        ticket_ids_to_revert,
        phantom_names,
        usage_limited,
        salvaged_ticket_ids,
        salvaged_result_by_ticket,
        merged_completed_ids,
    )
