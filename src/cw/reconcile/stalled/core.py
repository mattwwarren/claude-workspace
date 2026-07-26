"""Policy routing and act-phase orchestration for the stalled-headless sweep.

Extracted verbatim from the historical flat ``cw.reconcile.stalled`` module by
the #1484 package split. This module owns the reap-policy routing gate, the
act-phase driver that fans candidates out to ``_mutations`` / ``_events``, and
the standalone ``revert_stalled_headless_sessions`` entry point. See GitHub
#185, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import save_state
from cw.dev_queue import load_dev_queue
from cw.events import record_event
from cw.models import (
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    ProposedAction,
    _apply_queue_mutations,
    _emit_reap_proposed,
    feature_branch_key,
    resolve_reap_policy,
)
from cw.reconcile.stalled._detect import _detect_stalled_candidates
from cw.reconcile.stalled._events import _emit_stalled_events, _record_salvage_skip
from cw.reconcile.stalled._mutations import (
    _apply_finalize_blocked_queue_mutations,
    _apply_stalled_queue_mutations,
    _apply_stalled_routed_mutations,
    _apply_stalled_state_mutations,
)
from cw.reconcile.tasks import _client_cwd, _is_dangling_client

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, TicketTask
    from cw.reconcile._shared import ReapCandidate


def _route_stalled_by_policy(
    candidates: list[ReapCandidate],
    *,
    config: OrchestratorConfig | None,
    merged_ticket_ids: frozenset[str],
    gh_blocked_ticket_ids: frozenset[str],
) -> tuple[list[ReapCandidate], list[ReapCandidate]]:
    """Apply per-lane reap-policy routing to stalled REVERT_TASK candidates.

    Under ``ReapPolicy.SIGNAL_ONLY`` a REVERT_TASK candidate is routed to
    BLOCKED_ON_USER (via _apply_queue_mutations) instead of passing through.
    Merged / gh-blocked tickets always pass through (#637). Non-REVERT
    candidates are unaffected.

    Returns ``(auto_candidates, escalate_candidates)``. ``auto_candidates`` are
    the survivors that pass through to the normal act phase. ``escalate_candidates``
    are the SIGNAL_ONLY-rerouted REVERT_TASK candidates whose liveness veto was
    *cap-exhausted* (#1445): the task still routes silently to BLOCKED_ON_USER via
    the existing mutation, but they additionally drive an immediate
    session.needs_attention in :func:`_emit_stalled_events` so a still-live worker
    that has exhausted its veto budget surfaces to the operator this same tick.
    """
    effective_config = config if config is not None else OrchestratorConfig()
    clients = _deps.load_effective_clients()
    # Route each REVERT_TASK candidate individually based on its lane's policy.
    # Merged-PR / gh-blocked check (GitHub #637) runs BEFORE policy routing so
    # that a confirmed-merged ticket is always completed, even under SIGNAL_ONLY.
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
    escalate_candidates: list[ReapCandidate] = []
    for c in candidates:
        if c.proposed_action == ProposedAction.REVERT_TASK:
            if c.ticket_id and (
                c.ticket_id in merged_ticket_ids or c.ticket_id in gh_blocked_ticket_ids
            ):
                auto_candidates.append(c)
                continue
            policy = resolve_reap_policy(c, clients, effective_config)
            if policy is ReapPolicy.SIGNAL_ONLY:
                if c.ticket_id:
                    signal_mutations[c.ticket_id] = QueueItemStatus.BLOCKED_ON_USER
                # #1445: a cap-exhausted veto on a still-live session escalates
                # to the operator this tick (parity with the retry-cap park,
                # which already emits needs_attention) instead of a silent park.
                if c.veto_cap_exhausted:
                    escalate_candidates.append(c)
            else:
                auto_candidates.append(c)
        else:
            auto_candidates.append(c)
    if signal_mutations:
        _apply_queue_mutations(
            signal_mutations,
            clear_session_id=set(),
            disposition=ReapReason.WALL_CLOCK_BUDGET.value,
        )
    return auto_candidates, escalate_candidates


def _act_on_stalled_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
    merged_ticket_ids: frozenset[str] = frozenset(),
    gh_blocked_ticket_ids: frozenset[str] = frozenset(),
    branch_absent_ticket_ids: frozenset[str] = frozenset(),
    newly_proposed_ids: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """Act phase for stalled headless sessions: apply all mutations.

    Consumes ReapCandidate objects from _detect_stalled_candidates.
    Mirrors the side-effect logic in revert_stalled_headless_sessions.
    Returns (reverted_ticket_ids, merged_completed_ticket_ids).
    reverted_ticket_ids contains ticket IDs reverted to PENDING.
    merged_completed_ticket_ids contains ticket IDs completed because their
    PR was already merged (from merged_ticket_ids pre-pass; GitHub #637).

    Under ``ReapPolicy.SIGNAL_ONLY`` (default), REVERT_TASK candidates are
    routed to BLOCKED_ON_USER instead of triggering stop/remove.  Non-REVERT
    candidates (SALVAGE_*, SKIP_PARKED) are unaffected and pass through.
    Per-lane resolution: each REVERT_TASK candidate's effective policy is
    resolved individually via resolve_reap_policy (GitHub #560).

    ``newly_proposed_ids`` is the set of session_ids stamped by the preceding
    _emit_reap_proposed call.  SESSION_STAGE_TIMED_OUT_RETRIED fires only for
    sessions in that set, suppressing re-emission on every subsequent re-detect
    tick. See GitHub #782.
    """
    if not candidates:
        return [], []

    # Emit SESSION_STAGE_TIMED_OUT_RETRIED before policy routing so the event
    # fires for both auto and signal_only lanes (visibility-only; no retry cap).
    # Skips merged-PR and gh-blocked tickets — those are not genuine timeouts.
    # Edge-triggered: fires only for sessions newly proposed this tick (in
    # newly_proposed_ids). See GitHub #724 (original) and #782 (storm fix).
    _excluded_tids = merged_ticket_ids | gh_blocked_ticket_ids
    for _c in candidates:
        if _c.proposed_action is not ProposedAction.REVERT_TASK:
            continue
        if _c.ticket_id is None or _c.ticket_id in _excluded_tids:
            continue
        if _c.session_id not in newly_proposed_ids:
            continue
        record_event(
            OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED,
            {
                "ticket_id": _c.ticket_id,
                "session_id": _c.session_id,
                "stage": _c.stage,
                "client": _c.client,
                "elapsed_seconds": _c.elapsed_seconds,
                "attempts": _c.attempts,
            },
            correlation_id=_c.ticket_id,
        )

    candidates, wall_clock_veto_escalation_candidates = _route_stalled_by_policy(
        candidates,
        config=config,
        merged_ticket_ids=merged_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
    )
    if not candidates and not wall_clock_veto_escalation_candidates:
        return [], []

    # Separate by action for batch processing.
    skip_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SKIP_PARKED
    ]
    salvage_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    ]
    all_revert_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.REVERT_TASK
    ]
    park_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    ]
    finalize_blocked_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.PARK_FINALIZE_BLOCKED
    ]
    reset_salvage_skip_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.RESET_SALVAGE_SKIP_COUNTER
    ]
    park_vetoed_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.PARK_VETOED
    ]
    routed_sentinel_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    ]
    foreign_result_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.COMPLETE_FOREIGN_RESULT
    ]

    # Split REVERT_TASK candidates by world-state check results (GitHub #637).
    # merged_ticket_ids / gh_blocked_ticket_ids come from a pre-pass in
    # reconcile() that runs BEFORE sessions_lock, so no gh subprocess executes
    # here. Candidates with no ticket_id fall through to the normal revert path.
    merged_revert_candidates = [
        c
        for c in all_revert_candidates
        if c.ticket_id and c.ticket_id in merged_ticket_ids
    ]
    gh_blocked_revert_candidates = [
        c
        for c in all_revert_candidates
        if c.ticket_id and c.ticket_id in gh_blocked_ticket_ids
    ]
    revert_candidates = [
        c
        for c in all_revert_candidates
        if c not in merged_revert_candidates and c not in gh_blocked_revert_candidates
    ]

    # Why: no per-action-list emptiness guard here (there was one, historically
    # — see #974 plan review bug fix, which found it silently dropped counter
    # mutations on a skip/reset-only tick because it predated those two
    # candidate lists). The `if not candidates: return [], []` above already
    # guarantees non-emptiness, and skip/salvage/revert/park/finalize_blocked/
    # reset_salvage_skip/park_vetoed/routed_sentinel/foreign_result exhaustively
    # partition every ProposedAction that _detect_stalled_candidates emits — so
    # a second guard here can never fire and only risks silently reintroducing
    # the same bug class the next time a new ProposedAction is added and this
    # list isn't updated to match. session_by_id must be built unconditionally:
    # SKIP_PARKED/RESET_SALVAGE_SKIP_COUNTER candidates need live Session
    # objects even when they are the tick's only candidates.
    session_by_id = {s.id: s for s in state.sessions}

    effective_config = config if config is not None else OrchestratorConfig()

    # SKIP_PARKED: increment the salvage-skip latch and emit its event(s).
    for candidate in skip_candidates:
        _record_salvage_skip(session_by_id, candidate, config=effective_config)

    # PARK_VETOED: emit session.park_vetoed AND persist the incremented veto
    # count onto the session so the veto is bounded across ticks (#1445). The
    # count is the only state this action mutates; the task stays RUNNING and
    # the session stays ACTIVE/IDLE. The save_state(state) below persists it.
    for candidate in park_vetoed_candidates:
        session_by_id[
            candidate.session_id
        ].consecutive_park_vetoes = candidate.new_veto_count
        record_event(
            OrchestratorEventType.SESSION_PARK_VETOED,
            {
                "ticket_id": candidate.ticket_id,
                "client": candidate.client,
                "session_id": candidate.session_id,
                "stage": candidate.stage,
                "consecutive_vetoes": candidate.new_veto_count,
                # #1030 pattern: read the candidate's own reap_reason (now
                # branched at construction across two call sites — the
                # wall-clock revert path and the retry-cap park path, #1277)
                # instead of hardcoding a single cause. Defensive fallback:
                # reap_reason is always stamped by _liveness_veto_candidate,
                # but None is handled rather than raising.
                "reason": (
                    candidate.reap_reason.value
                    if candidate.reap_reason is not None
                    else ReapReason.WALL_CLOCK_BUDGET.value
                ),
                "stale_minutes": candidate.stale_minutes,
            },
            correlation_id=candidate.ticket_id,
        )

    # Wall-clock veto-cap escalation (#1445): persist the bumped counter here,
    # BEFORE save_state below, so the next tick reads it back as > park_veto_cap
    # and _liveness_veto_candidate's exact-cap check no longer reports
    # exhaustion. Without this the SIGNAL_ONLY escalation -- which deliberately
    # never stops the daemon or terminates the session, unlike the retry-cap
    # park -- would re-fire every tick the session stays LIVE past the cap.
    for candidate in wall_clock_veto_escalation_candidates:
        session_by_id[
            candidate.session_id
        ].consecutive_park_vetoes = candidate.new_veto_count

    # Reassign salvage_candidates to the door-accepted subset (RFC 0012 A3,
    # #1459) so a refused candidate is excluded from salvaged_result_by_ticket,
    # _apply_stalled_queue_mutations, and _emit_stalled_events below -- mirrors
    # the routed_sentinel_candidates reassignment just below.
    salvage_candidates = _apply_stalled_state_mutations(
        session_by_id,
        now=now,
        salvage_candidates=salvage_candidates,
        merged_revert_candidates=merged_revert_candidates,
        gh_blocked_revert_candidates=gh_blocked_revert_candidates,
        revert_candidates=revert_candidates,
        park_candidates=park_candidates,
        finalize_blocked_candidates=finalize_blocked_candidates,
        reset_salvage_skip_candidates=reset_salvage_skip_candidates,
        foreign_result_candidates=foreign_result_candidates,
    )

    # Path 1 backstop (#1149): route emitted advance sentinels through the shared
    # authority and complete the session — persisted by the save_state below,
    # alongside the other session-state mutations.
    routed_sentinel_candidates = _apply_stalled_routed_mutations(
        session_by_id, routed_sentinel_candidates, now=now
    )

    save_state(state)

    salvaged_result_by_ticket = {
        c.ticket_id: c.salvage_result
        for c in salvage_candidates
        if c.ticket_id and c.salvage_result
    }
    reverted, merged_completed = _apply_stalled_queue_mutations(
        revert_candidates,
        merged_revert_candidates,
        gh_blocked_revert_candidates,
        park_candidates,
        salvage_candidates,
        salvaged_result_by_ticket,
        foreign_result_candidates=foreign_result_candidates,
    )

    _apply_finalize_blocked_queue_mutations(finalize_blocked_candidates)

    _emit_stalled_events(
        session_by_id,
        revert_candidates,
        merged_revert_candidates,
        gh_blocked_revert_candidates,
        park_candidates,
        salvage_candidates,
        finalize_blocked_candidates,
        routed_sentinel_candidates,
        wall_clock_veto_escalation_candidates,
        foreign_result_candidates=foreign_result_candidates,
        branch_absent_ticket_ids=branch_absent_ticket_ids,
    )

    return reverted, merged_completed


def revert_stalled_headless_sessions(
    state: CwState,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[str]:
    """Transition stalled headless DAEMON sessions past budget to TIMED_OUT.

    Passive backstop complementing signal_stop's Stop-hook-driven check.
    signal_stop can only fire at Claude turn boundaries; a session whose agent
    stalled mid-turn (classifier denial, OOM, long subagent chain) produces no
    further Stop firings and would sit ACTIVE forever without this sweep.

    Runs unconditionally before the outage guard so a transient backend hiccup
    does not delay enforcement of the wall-clock budget. The sweep is purely
    time-based; surface liveness is irrelevant.

    Loads the dev queue once (read-only, no lock) for per-ticket budget lookups.
    The existing dev_queue_lock block for the revert step (below) still guards
    the read-write window.

    Calls save_state(state) when any sessions are transitioned — callers must
    not assume state is unchanged on return. On the phantom-handling path in
    reconcile(), save_state is called again later; this double-save is benign
    because save_state is idempotent over identical content.

    Returns the list of ticket IDs whose TicketTask was reverted to PENDING.
    Tickets whose PR is already merged complete instead of reverting (#637).
    Tickets whose gh availability check fails go to BLOCKED_ON_USER (#637).
    See GitHub issue #185, #265.
    """
    if task_by_ticket is None:
        task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_stalled_candidates(
        state, now=now, config=config, task_by_ticket=task_by_ticket
    )
    # Lockless gh pre-pass — mirrors reconcile() in core.py.
    # gh subprocess must NOT run under sessions_lock (liveness, #485); this
    # wrapper is called outside sessions_lock so the constraint is satisfied.
    # Fail-open: only (True, True) suppresses timeout. (None, True) transient
    # error falls through to TIMED_OUT; (None, False) gh-absent routes to
    # BLOCKED_ON_USER (matching core.py's established contract). See #315.
    _clients = _deps.load_effective_clients()
    _merged_tids: list[str] = []
    _gh_blocked_tids: list[str] = []
    _branch_absent_tids: list[str] = []
    _gh_available = True
    for candidate in candidates:
        if candidate.proposed_action is not ProposedAction.REVERT_TASK:
            continue
        if candidate.ticket_id is None:
            continue
        if candidate.client is None:
            continue
        if not _gh_available:
            _gh_blocked_tids.append(candidate.ticket_id)
            continue
        _branch = feature_branch_key(candidate.client, candidate.ticket_id, _clients)
        if _is_dangling_client(candidate.client, _clients):
            # Client removed/renamed from a populated clients.yaml -- route to
            # gh_blocked (BLOCKED_ON_USER downstream) rather than risk an
            # unscoped gh call (GitHub #1269/#1279 R7). Mirrors core.py's guard
            # position: this also skips the still-unwired pr_is_merged_for_ticket
            # call for a dangling client only (a deliberate narrowing).
            _gh_blocked_tids.append(candidate.ticket_id)
            continue
        _cwd = _client_cwd(candidate.client, _clients)
        # Why: pr_is_merged_for_ticket deliberately does NOT get cwd= here --
        # this function has no production callers (test-only, per GitHub #1279
        # Discoveries), so wiring its cwd threading is out of scope for this
        # ticket; _cwd is only used below by branch_exists_on_origin.
        _merged, _gh_avail = _deps.pr_is_merged_for_ticket(
            candidate.ticket_id, branch=_branch
        )
        if not _gh_avail:
            _gh_available = False
            _gh_blocked_tids.append(candidate.ticket_id)
            continue
        if _merged is None:
            continue
        if _merged:
            _merged_tids.append(candidate.ticket_id)
        else:
            # No merged PR found: consult branch presence for diagnostic annotation.
            # Fail-open: (None, *) → no tag; never blocks disposition.
            _exists, _ = _deps.branch_exists_on_origin(_branch, cwd=_cwd)
            if _exists is False:
                _branch_absent_tids.append(candidate.ticket_id)
    merged_ticket_ids = frozenset(_merged_tids)
    gh_blocked_ticket_ids = frozenset(_gh_blocked_tids)
    branch_absent_ticket_ids = frozenset(_branch_absent_tids)
    # _emit_reap_proposed stamps reap_proposed_at on newly-detected sessions and
    # returns the set of newly-stamped session_ids for the edge-trigger gate in
    # _act_on_stalled_candidates. Mirrors the core.py _reconcile_locked flow so
    # SESSION_STAGE_TIMED_OUT_RETRIED fires once per transition. See GitHub #782.
    newly_proposed_ids = _emit_reap_proposed(
        state, candidates, native_live=set(), now=now
    )
    # Discard merged_completed_ids — callers expect list[str] (reverted only).
    # merged completions surface through ReconcileReport.completed_ticket_ids
    # inside _reconcile_locked (GitHub #637).
    reverted, _merged_completed = _act_on_stalled_candidates(
        state,
        candidates,
        now=now,
        config=config,
        merged_ticket_ids=merged_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
        branch_absent_ticket_ids=branch_absent_ticket_ids,
        newly_proposed_ids=newly_proposed_ids,
    )
    return reverted
