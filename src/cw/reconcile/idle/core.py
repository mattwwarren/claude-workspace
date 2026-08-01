"""Policy routing and act-phase orchestration for the silently-idle sweep.

Extracted verbatim from the historical flat ``cw.reconcile.idle`` module by
the package split. This module owns the reap-policy routing gate, the act-
phase driver that fans candidates out to ``_mutations`` / ``_events``, and
the standalone ``flag_silently_idle_daemon_sessions`` entry point. See
GitHub #105, #121, #545, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import save_state
from cw.dev_queue import load_dev_queue
from cw.models import (
    OrchestratorConfig,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _SILENTLY_IDLE_REASON,
    ProposedAction,
    _apply_queue_mutations,
    resolve_reap_policy,
)
from cw.reconcile.idle._detect import _detect_idle_candidates
from cw.reconcile.idle._events import _emit_idle_events
from cw.reconcile.idle._mutations import (
    _apply_idle_queue_mutations,
    _apply_idle_routed_mutations,
    _apply_idle_state_mutations,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, TicketTask
    from cw.reconcile._shared import ReapCandidate, _SalvageCandidate


def _route_idle_by_policy(
    candidates: list[ReapCandidate],
    *,
    config: OrchestratorConfig | None,
    merged_client_ticket_ids: frozenset[tuple[str, str]],
    gh_blocked_ticket_ids: frozenset[str],
) -> list[ReapCandidate]:
    """Apply per-lane reap-policy routing to idle REVERT_TASK candidates.

    Under ``ReapPolicy.SIGNAL_ONLY`` a REVERT_TASK candidate is routed to
    BLOCKED_ON_USER (via _apply_queue_mutations) instead of passing through.
    Merged / gh-blocked tickets always pass through (#637). Non-REVERT
    candidates are unaffected. Returns the surviving "auto" candidates.
    """
    effective_config = config if config is not None else OrchestratorConfig()
    clients = _deps.load_effective_clients()
    # Route each REVERT_TASK candidate individually based on its lane's policy.
    # Merged-PR / gh-blocked check (GitHub #637) runs BEFORE policy routing so
    # that a confirmed-merged ticket is always completed, even under SIGNAL_ONLY.
    # Why (client, ticket_id): keyed by merged_client_ticket_ids, not a bare
    # ticket_id, so one client's merged ticket cannot bypass SIGNAL_ONLY for a
    # different client's same-numbered, unmerged candidate. See GitHub #1054.
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
    for c in candidates:
        if c.proposed_action == ProposedAction.REVERT_TASK:
            merged_key = (c.client, c.ticket_id) if c.client and c.ticket_id else None
            if c.ticket_id and (
                (merged_key is not None and merged_key in merged_client_ticket_ids)
                or c.ticket_id in gh_blocked_ticket_ids
            ):
                auto_candidates.append(c)
                continue
            policy = resolve_reap_policy(c, clients, effective_config)
            if policy is ReapPolicy.SIGNAL_ONLY:
                if c.ticket_id:
                    signal_mutations[c.ticket_id] = QueueItemStatus.BLOCKED_ON_USER
            else:
                auto_candidates.append(c)
        else:
            auto_candidates.append(c)
    if signal_mutations:
        _apply_queue_mutations(
            signal_mutations,
            clear_session_id=set(),
            disposition=ReapReason.IDLE_STALL.value,
        )
    return auto_candidates


def _act_on_idle_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
    merged_client_ticket_ids: frozenset[tuple[str, str]] = frozenset(),
    gh_blocked_ticket_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str], list[_SalvageCandidate]]:
    """Act phase for silently idle sessions: apply all mutations.

    Consumes ReapCandidate objects from _detect_idle_candidates.
    Returns (blocked_ticket_ids, merged_completed_ticket_ids, salvage_git_candidates).
    merged_completed_ticket_ids contains ticket IDs completed because their
    PR was already merged (from merged_client_ticket_ids pre-pass; GitHub #637,
    #1054). Keyed by (client, ticket_id) rather than a bare ticket_id -- see
    GitHub #1054: ticket_id strings are not globally unique across clients, and
    this act phase (unlike the sibling stalled/phantom sweeps, out of scope for
    #1054) now also completes FINALIZE-stage / merged-first candidates that
    idle's classify phase previously never routed here at all.

    Under ``ReapPolicy.SIGNAL_ONLY`` (default), REVERT_TASK candidates are
    routed to BLOCKED_ON_USER instead of triggering stop/remove.  Non-REVERT
    candidates (SALVAGE_*, INCREMENT_COUNTER, RECOVER_COUNTER,
    PARK_BLOCKED_ON_USER) are unaffected and pass through.
    Per-lane resolution: each REVERT_TASK candidate's effective policy is
    resolved individually via resolve_reap_policy (GitHub #560).
    """
    if not candidates:
        return [], [], []

    candidates = _route_idle_by_policy(
        candidates,
        config=config,
        merged_client_ticket_ids=merged_client_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
    )
    if not candidates:
        return [], [], []

    session_by_id = {s.id: s for s in state.sessions}

    counter_candidates = [
        c
        for c in candidates
        if c.proposed_action
        in (
            ProposedAction.INCREMENT_COUNTER,
            ProposedAction.RECOVER_COUNTER,
            # SALVAGE_GIT reaches the threshold — persist new_observation_count so
            # a process restart between ticks does not replay the observation as fresh.
            ProposedAction.SALVAGE_GIT,
        )
    ]
    salvage_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    ]
    all_revert_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.REVERT_TASK
    ]

    # Split REVERT_TASK candidates by world-state check results (GitHub #637).
    # merged_client_ticket_ids / gh_blocked_ticket_ids come from a pre-pass in
    # reconcile() that runs BEFORE sessions_lock, so no gh subprocess executes
    # here. Candidates with no ticket_id fall through to the normal revert path.
    # Why (client, ticket_id): a merged-first candidate from idle's classify
    # phase can now reach this split for a FINALIZE-stage / git-branch session
    # (previously unreachable -- such sessions always short-circuited to
    # SALVAGE_GIT). A bare ticket_id match here would let one client's merged
    # ticket mark a different client's same-numbered RUNNING task COMPLETED.
    # See GitHub #1054.
    merged_revert_candidates = [
        c
        for c in all_revert_candidates
        if c.ticket_id
        and c.client
        and (c.client, c.ticket_id) in merged_client_ticket_ids
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
    park_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.PARK_BLOCKED_ON_USER
    ]
    escalate_external_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.ESCALATE_EXTERNAL_IDLE
    ]
    salvage_git_candidates_list = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_GIT
    ]
    routed_sentinel_candidates = [
        c
        for c in candidates
        if c.proposed_action == ProposedAction.ROUTE_EMITTED_SENTINEL
    ]

    # Apply ROUTE_EMITTED_SENTINEL mutations up front so a stage-mismatch
    # refusal (#1031) is filtered out before it can influence has_dispositions
    # or downstream event emission -- a refused candidate must not complete or
    # tear down a session the daemon still reports alive.
    routed_sentinel_candidates, routed_state_mutated = _apply_idle_routed_mutations(
        session_by_id, routed_sentinel_candidates, now=now
    )

    # Snapshot which park candidates already have a paused_status marker BEFORE
    # mutations run. Used by _emit_idle_events to suppress re-emission of
    # SESSION_NEEDS_ATTENTION and fire_push_notification on re-park ticks.
    # Keys off current last_result["paused_status"] (not a permanent flag) per
    # the authoritative pre-flight resolution. See GitHub #782.
    already_parked_ids = {
        c.session_id
        for c in park_candidates
        if (sess := session_by_id.get(c.session_id)) is not None
        and isinstance(sess.last_result, dict)
        and sess.last_result.get("paused_status") == _SILENTLY_IDLE_REASON
    }

    # Reassign salvage_candidates to the door-accepted subset (RFC 0012 A3,
    # #1459) so the refused ones are excluded from has_dispositions,
    # salvaged_result_by_ticket, _apply_idle_queue_mutations, and
    # _emit_idle_events below -- mirrors the routed_sentinel_candidates
    # reassignment one function above.
    counters_changed, salvage_candidates = _apply_idle_state_mutations(
        session_by_id,
        now=now,
        counter_candidates=counter_candidates,
        salvage_candidates=salvage_candidates,
        merged_revert_candidates=merged_revert_candidates,
        gh_blocked_revert_candidates=gh_blocked_revert_candidates,
        revert_candidates=revert_candidates,
        park_candidates=park_candidates,
    )

    has_dispositions = bool(
        salvage_candidates
        or revert_candidates
        or merged_revert_candidates
        or gh_blocked_revert_candidates
        or park_candidates
        or escalate_external_candidates
        or salvage_git_candidates_list
        or routed_sentinel_candidates
    )

    if counters_changed or has_dispositions or routed_state_mutated:
        save_state(state)

    if not has_dispositions:
        return [], [], []

    salvaged_result_by_ticket = {
        c.ticket_id: c.salvage_result
        for c in salvage_candidates
        if c.ticket_id and c.salvage_result
    }
    blocked, merged_completed = _apply_idle_queue_mutations(
        revert_candidates,
        merged_revert_candidates,
        gh_blocked_revert_candidates,
        park_candidates,
        salvage_candidates,
        salvaged_result_by_ticket,
    )

    _emit_idle_events(
        session_by_id,
        revert_candidates,
        park_candidates,
        merged_revert_candidates,
        gh_blocked_revert_candidates,
        salvage_candidates,
        routed_sentinel_candidates,
        escalate_external_candidates=escalate_external_candidates,
        already_parked_ids=already_parked_ids,
    )

    salvage_git: list[_SalvageCandidate] = [
        (
            c.session_id,
            c.ticket_id,
            c.branch,
            c.worktree_path_str,
            c.post_review_clean,
        )
        for c in salvage_git_candidates_list
        if c.branch is not None and c.worktree_path_str is not None
    ]

    return blocked, merged_completed, salvage_git


def flag_silently_idle_daemon_sessions(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> tuple[list[str], list[_SalvageCandidate]]:
    """Flag DAEMON RUNNING sessions idle past the watchdog budget with no sentinel.

    These are sessions that stalled without emitting a terminal signal — typically
    because the child process self-backgrounded a subagent and exited before
    the subagent returned (GitHub #105, #121). They appear ACTIVE/IDLE in cw
    state while producing no output.

    Only targets sessions whose ``surface_ref`` is currently in *native_live*
    (the daemon still has them). Sessions with a dead surface ref are handled
    by the phantom sweep → PENDING for retry.

    Confirm-before-reap (#545): a session must fail the liveness check on
    ``config.idle_confirm_observations`` consecutive watchdog ticks before it
    is dispositioned. ``session.idle_observation_count`` is incremented each
    tick a session fails; it is reset to 0 on recovery. This prevents a single
    quiet poll from killing a healthy DAEMON worker.

    Returns a tuple of:
    - list of ticket IDs whose queue task was set to BLOCKED_ON_USER
    - list of git-state salvage candidates for the post-lock pass:
      (session_id, ticket_id, branch, worktree_path_str, post_review_clean)
    """
    if task_by_ticket is None:
        task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live=native_live,
        config=config,
        task_by_ticket=task_by_ticket,
    )
    blocked, _merged_completed, salvage_git = _act_on_idle_candidates(
        state, candidates, now=now, config=config
    )
    return blocked, salvage_git
