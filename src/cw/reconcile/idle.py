"""Silently-idle DAEMON session detection and act phases for reconcile.

A silently idle session stalled past the watchdog budget without emitting a
terminal sentinel (e.g. the child self-backgrounded a subagent and exited
before it returned). See GitHub #105, #121, #545, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import INTERMEDIATE_ADVANCE_STATUSES, AutoDevResult
from cw.config import save_state
from cw.dev_queue import (
    _derive_disposition,
    _extract_pr_url,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.models import (
    DEFAULT_LANE,
    CompletionReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionStatus,
    Stage,
)
from cw.pr_hydrate import derive_counterparty
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _CAUSE_IDLE_STALL,
    _CAUSE_USAGE_LIMIT,
    _EXTERNAL_COUNTERPARTY_IDLE_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _LIVE_STATUSES,
    _PAUSED_STATUS_KEY,
    _PHANTOM_REAP_MERGED_REASON,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _SILENTLY_IDLE_REASON,
    ProposedAction,
    ReapCandidate,
    _apply_queue_mutations,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _awaiting_subagent,
    _cleanup_timed_out_worktree,
    _detect_post_review_clean,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    _queue_status_for_salvaged,
    _SalvageCandidate,
    _transcript_recently_active,
    classify_sentinel_stage_position,
    resolve_idle_retry_cap,
    resolve_idle_watchdog_budget,
    resolve_reap_policy,
    ticket_id_for_session,
)
from cw.reconcile.dispositions import (
    build_salvage_completion_payload,
    emit_routed_sentinel_completion,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import ClientConfig, CwState, Session, TicketTask
    from cw.pr_hydrate import Counterparty


def _revert_task_candidate(
    session: Session,
    *,
    ticket_id: str | None,
    elapsed: float,
    new_count: int,
    lane: str,
    worktree_dirty: bool = False,
) -> ReapCandidate:
    """Build a REVERT_TASK ReapCandidate.

    Shared by the new merged-first check and the pre-existing retry-cap check
    in ``_classify_idle_threshold`` -- the retry-cap site previously built this
    inline; the two now differ only in ``worktree_dirty``. See GitHub #1054.
    """
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id=ticket_id,
        elapsed_seconds=elapsed,
        worktree_dirty=worktree_dirty,
        new_observation_count=new_count,
        usage_limit_detected=_shared.detect_usage_limit(session),
        lane=lane,
        client=session.client,
    )


def _classify_idle_threshold(
    session: Session,
    *,
    task: TicketTask | None,
    ticket_id: str | None,
    config: OrchestratorConfig,
    elapsed: float,
    new_count: int,
    merged_client_ticket_ids: frozenset[tuple[str, str]] = frozenset(),
    counterparty: Counterparty = "self",
) -> ReapCandidate | None:
    """Classify the final disposition for an idle session past the confirm threshold.

    Returns a single ReapCandidate: REVERT_TASK when the ticket's PR is already
    merged (shipped ground truth, checked before any other classification),
    ESCALATE_EXTERNAL_IDLE when the session is reviewing a teammate's PR
    (RFC 0011 B1), SALVAGE_GIT when a worktree branch exists, REVERT_TASK when
    the owning task is below its retry cap, else PARK_BLOCKED_ON_USER. Returns
    None when the session is at Stage.FINALIZE with a live worktree branch --
    that disposition is owned by stalled.py's finalize-blocked path. Makes
    zero writes. See GitHub #552, ADR-0006, #1054, #1158.
    """
    lane = task.lane if task else DEFAULT_LANE
    # Merged-first check (#1054): a ticket whose PR already merged is shipped
    # ground truth regardless of stage or git state. Route it through the
    # existing merged-REVERT_TASK completion path in _act_on_idle_candidates
    # (splits REVERT_TASK candidates by ticket_id in merged_ticket_ids) rather
    # than reclassifying it as a git-salvage or park candidate.
    #
    # Why (client, ticket_id) and not a bare ticket_id: ticket_id strings are
    # not globally unique across clients (dev_queue.py's _find_ticket /
    # remove_ticket / cancel_ticket all key on the (ticket_id, client) pair,
    # and GitHub issue numbers restart per repo) -- a bare-string check here
    # would let one client's merged ticket route a *different* client's
    # same-numbered, unmerged ticket into completion. See GitHub #1054.
    merged_key = (session.client, ticket_id) if ticket_id is not None else None
    if merged_key is not None and merged_key in merged_client_ticket_ids:
        return _revert_task_candidate(
            session,
            ticket_id=ticket_id,
            elapsed=elapsed,
            new_count=new_count,
            lane=lane,
        )
    # RFC 0011 B1 (#1158): an `external`-counterparty session (reviewing a
    # teammate's PR) is exempt from silent idle-reap. Checked after the
    # merged-first ground-truth check (a shipped ticket has nothing left to
    # escalate) and before every reap/park disposition below.
    if counterparty == "external":
        return ReapCandidate(
            session_id=session.id,
            proposed_action=ProposedAction.ESCALATE_EXTERNAL_IDLE,
            ticket_id=ticket_id,
            new_observation_count=new_count,
            lane=lane,
            client=session.client,
            paused_status=_EXTERNAL_COUNTERPARTY_IDLE_REASON,
        )
    # Git-state salvage path.
    if session.worktree_path is not None:
        branch = _deps.checked_out_branch(session.worktree_path)
        if branch is not None:
            # Why: FINALIZE-stage parking is owned by stalled.py's
            # finalize-blocked path (_resolve_finalize_blocked_condition +
            # rescue_finalize_blocked_sessions), which is aware of the
            # larger finalize wall-clock budget and merged-PR ground truth.
            # idle's shorter confirm-threshold races ahead of that path; left
            # unguarded, it would also reach _detect_post_review_clean below,
            # which is structurally always False at FINALIZE (each stage
            # spawns a new session id, so STAGE_ENTERED's session_id never
            # matches this FINALIZE session). Returning None here -- before
            # that call -- is what prevents idle from false-parking a
            # FINALIZE-stage session as needs_salvage. See GitHub #1054.
            if task is not None and task.stage == Stage.FINALIZE:
                return None
            post_review_clean = _detect_post_review_clean(session)
            worktree_dirty = _shared.worktree_dirty_by_path(
                session.client, session.worktree_path
            )
            return ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.SALVAGE_GIT,
                ticket_id=ticket_id,
                branch=branch,
                worktree_path_str=str(session.worktree_path),
                post_review_clean=post_review_clean,
                worktree_dirty=worktree_dirty,
                new_observation_count=new_count,
                lane=lane,
                client=session.client,
            )
    cap = resolve_idle_retry_cap(task, config)
    worktree_dirty = _shared.worktree_dirty_by_path(
        session.client, session.worktree_path
    )
    if task is not None and task.attempts < cap:
        return _revert_task_candidate(
            session,
            ticket_id=ticket_id,
            elapsed=elapsed,
            new_count=new_count,
            lane=lane,
            worktree_dirty=worktree_dirty,
        )
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
        ticket_id=ticket_id,
        worktree_dirty=worktree_dirty,
        new_observation_count=new_count,
        lane=lane,
        client=session.client,
        paused_status=_SILENTLY_IDLE_REASON,
    )


def _idle_advance_sentinel_candidate(
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    clients: dict[str, ClientConfig],
) -> ReapCandidate | None:
    """ROUTE_EMITTED_SENTINEL candidate for a confirmed-idle stage-advance session.

    idle.py's own near-duplicate of ``stalled._stalled_advance_sentinel_candidate``
    (#1149) for the silently-idle sweep (#1283). ``salvage_terminal_result``
    deliberately excludes ``stage_complete`` (not in ``SALVAGE_TERMINAL_STATUSES``),
    so a session that finished its stage cleanly but whose ``last_result`` is
    already a non-``None``, status-free park marker — which closes idle.py's
    pre-existing ``last_result is None`` unrouted-check fast path (#578) — is
    invisible to ``_detect_idle_confirmed_candidate``'s salvage check and would
    fall through to ``_classify_idle_threshold``'s SALVAGE_GIT path, a false
    ``needs_salvage`` park on a healthy stage boundary. Harvest a same-/later-
    stage advance sentinel here so it routes forward instead. An earlier-stage or
    unresolvable-position sentinel (a stale replay, #986) returns ``None`` so
    detection falls through to the existing salvage-git / retry-cap / park chain
    unchanged. Kept a standalone near-duplicate of ``stalled.py``'s copy per
    #1149 R1 (it does not need the RESET_SALVAGE_SKIP_COUNTER dance, which is
    scoped to stalled.py's SKIP_PARKED exits); the 3-way consolidation into
    ``_shared.py`` is deferred to #1335.
    """
    if task is None:
        return None
    # FINALIZE-stage parking is owned by stalled.py's finalize-blocked path;
    # mirror _classify_idle_threshold's explicit FINALIZE-ownership boundary.
    if task.stage == Stage.FINALIZE:
        return None
    parsed = _parse_any_sentinel_from_transcript(session)
    if parsed is None:
        return None
    result, csid = parsed
    if (
        not isinstance(result, AutoDevResult)
        or result.status not in INTERMEDIATE_ADVANCE_STATUSES
    ):
        return None
    position, _stages, _target_idx = classify_sentinel_stage_position(
        task, result.model_dump(mode="json"), clients
    )
    if position not in ("same", "later"):
        return None
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
        ticket_id=ticket_id,
        routed_sentinel=result,
        salvage_csid=csid,
        elapsed_seconds=elapsed,
        lane=task.lane,
        client=session.client,
    )


def _detect_idle_confirmed_candidate(
    session: Session,
    *,
    task: TicketTask | None,
    ticket_id: str | None,
    config: OrchestratorConfig,
    elapsed: float,
    merged_client_ticket_ids: frozenset[tuple[str, str]],
    clients: dict[str, ClientConfig],
    counterparty: Counterparty = "self",
) -> ReapCandidate | None:
    """Classify a session that already passed the liveness check.

    Split out of ``_detect_idle_candidate_for_session`` to keep each
    function's return-statement count under the PLR0911 limit. See GitHub
    #1054.
    """
    # Sentinel salvage: evidence-based completion, not deferred by counter.
    salvage = _shared.salvage_terminal_result(session)
    if salvage is not None:
        result, claude_session_id = salvage
        return ReapCandidate(
            session_id=session.id,
            proposed_action=ProposedAction.SALVAGE_COMPLETION,
            ticket_id=ticket_id,
            salvage_result=result,
            salvage_csid=claude_session_id,
            elapsed_seconds=elapsed,
            lane=task.lane if task else DEFAULT_LANE,
            client=session.client,
        )
    # #1283 advance-sentinel backstop: a stage_complete sentinel is excluded from
    # SALVAGE_TERMINAL_STATUSES, so the salvage check above never sees it. Harvest
    # a same-/later-stage advance sentinel here — evidence-based, not deferred by
    # counter (same priority tier as the salvage check) — BEFORE the confirm
    # increment so a cleanly-completed stage is never routed to SALVAGE_GIT.
    routed_advance = _idle_advance_sentinel_candidate(
        session, task, ticket_id, elapsed, clients
    )
    if routed_advance is not None:
        return routed_advance
    # Confirm-before-reap: accumulate consecutive failed observations.
    new_count = session.idle_observation_count + 1
    if new_count < config.idle_confirm_observations:
        return ReapCandidate(
            session_id=session.id,
            proposed_action=ProposedAction.INCREMENT_COUNTER,
            ticket_id=ticket_id,
            new_observation_count=new_count,
            lane=task.lane if task else DEFAULT_LANE,
            client=session.client,
        )
    # Threshold reached: classify final disposition.
    return _classify_idle_threshold(
        session,
        task=task,
        ticket_id=ticket_id,
        config=config,
        elapsed=elapsed,
        new_count=new_count,
        merged_client_ticket_ids=merged_client_ticket_ids,
        counterparty=counterparty,
    )


def _detect_idle_candidate_for_session(
    session: Session,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task: TicketTask | None,
    ticket_id: str | None,
    merged_client_ticket_ids: frozenset[tuple[str, str]],
    clients: dict[str, ClientConfig],
    counterparty: Counterparty = "self",
) -> ReapCandidate | None:
    """Classify a single live DAEMON session for idle disposition, or None.

    Extracted from ``_detect_idle_candidates`` (unchanged logic, just moved)
    to keep that function's branch count under the PLR0912 limit after
    adding the None-skip for FINALIZE-stage sessions. See GitHub #1054.
    """
    elapsed = (now - session.started_at).total_seconds()
    budget = resolve_idle_watchdog_budget(task, config)
    # ROUTE_EMITTED_SENTINEL: fires before the full idle-budget check.
    # An emitted sentinel is positive evidence the worker completed; the
    # 300 s threshold (sentinel_unrouted_check_seconds) is shorter than
    # the watchdog budget to route the task before a reap fires.
    # Guard: last_result is None means signal_stop never ran — prevents
    # double-routing. Exempt from signal_only (constructive, not a reap).
    # See GitHub #578.
    unrouted_check = config.sentinel_unrouted_check_seconds
    if session.last_result is None and elapsed >= unrouted_check:
        routed = _parse_any_sentinel_from_transcript(session)
        if routed is not None:
            _routed_result, _csid = routed
            return ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
                ticket_id=ticket_id,
                routed_sentinel=_routed_result,
                salvage_csid=_csid,
                elapsed_seconds=elapsed,
                lane=task.lane if task else DEFAULT_LANE,
                client=session.client,
            )
    if elapsed < budget:
        return None
    # Liveness check: if active, check for recovery of observation counter.
    if _transcript_recently_active(session, now) or _awaiting_subagent(session, now):
        if session.idle_observation_count > 0:
            return ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.RECOVER_COUNTER,
                ticket_id=ticket_id,
                new_observation_count=0,
                lane=task.lane if task else DEFAULT_LANE,
                client=session.client,
            )
        return None
    return _detect_idle_confirmed_candidate(
        session,
        task=task,
        ticket_id=ticket_id,
        config=config,
        elapsed=elapsed,
        merged_client_ticket_ids=merged_client_ticket_ids,
        clients=clients,
        counterparty=counterparty,
    )


def _detect_idle_candidates(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask],
    merged_client_ticket_ids: frozenset[tuple[str, str]] = frozenset(),
) -> list[ReapCandidate]:
    """Pure classification phase for silently idle DAEMON sessions.

    Returns a list of ReapCandidate objects. Makes zero writes to state,
    queue, or event bus. The idle_observation_count increment is computed
    but NOT written; it is carried in new_observation_count on the candidate.
    See GitHub #552, ADR-0006.
    """
    # Load clients once for the advance-sentinel backstop's stage-position
    # resolution across all sessions in this pass (#1283), mirroring
    # stalled.py::_detect_stalled_candidates's identical load-once pattern.
    effective_clients = _deps.load_effective_clients()
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.status not in _LIVE_STATUSES:
            continue
        if _has_terminal_sentinel(session):
            continue
        if session.surface_ref is None or session.surface_ref not in native_live:
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        counterparty = derive_counterparty(task, operator_login=None)
        candidate = _detect_idle_candidate_for_session(
            session,
            now=now,
            config=config,
            task=task,
            ticket_id=ticket_id,
            merged_client_ticket_ids=merged_client_ticket_ids,
            clients=effective_clients,
            counterparty=counterparty,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


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


def _apply_idle_routed_mutations(
    session_by_id: dict[str, Session],
    routed_sentinel_candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> tuple[list[ReapCandidate], bool]:
    """Apply ROUTE_EMITTED_SENTINEL mutations for alive-idle workers (#1031).

    Mirrors ``phantom._apply_phantom_routed_mutations``: routes the emitted
    advance sentinel through the shared staged-advance authority
    (``_apply_sentinel_to_task`` -> ``apply_staged_decision``), then marks the
    session COMPLETED/NORMAL -- but only when the route was accepted.

    GitHub #1031 (extends #1019's phantom-path guard): when
    ``_apply_sentinel_to_task`` reports ``routed=False`` (a stage-mismatch
    refusal, the #986 incident), the session must NOT be completed here --
    unlike the phantom sweep, ``_detect_idle_candidates`` only builds these
    candidates when the surface is still reported alive by the daemon, so an
    unconditional completion would tear down a live surface, not just orphan
    a task row.

    Returns ``(accepted, state_mutated)``. ``accepted`` is only the candidates
    actually routed, so the caller's downstream event emission fires solely for
    those. ``state_mutated`` is True when any session state changed here --
    including a refusal-marker stamp with no accepted candidate -- so the caller
    persists the stamp even on a pure-refusal tick, whose ``accepted`` list is
    empty and would otherwise leave ``has_dispositions`` False and skip
    ``save_state`` (the marker would be lost and the candidate re-fire forever,
    GitHub #1149).
    """
    accepted: list[ReapCandidate] = []
    state_mutated = False
    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None or candidate.salvage_csid is None:
            continue
        routed = True
        if candidate.ticket_id:
            outcome = _apply_sentinel_to_task(
                candidate.ticket_id, candidate.session_id, candidate.routed_sentinel
            )
            routed = outcome.routed
        if not routed:
            # #1149: a stage-mismatch refusal (earlier-stage replay / unresolvable
            # position) leaves the task untouched. Stamp a paused_status-only
            # marker so the next tick's `session.last_result is None` unrouted-check
            # gate (_detect_idle_candidate_for_session) stops re-proposing this same
            # doomed candidate forever. No "status" key -> _has_terminal_sentinel
            # stays False and the ordinary idle-stall machinery still runs.
            session_by_id[candidate.session_id].last_result = {
                _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
            }
            state_mutated = True
            continue
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.last_result = candidate.routed_sentinel.model_dump(mode="json")
        session.claude_session_id = candidate.salvage_csid
        accepted.append(candidate)
        state_mutated = True
    return accepted, state_mutated


def _apply_idle_state_mutations(
    session_by_id: dict[str, Session],
    *,
    now: datetime,
    counter_candidates: list[ReapCandidate],
    salvage_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
) -> bool:
    """Apply in-place session-state mutations for idle dispositions.

    Returns whether any counter-only update occurred (so the caller can decide
    to save_state even when there are no dispositions). save_state itself is
    left to the caller's combined flush.
    """
    # Counter-only updates: just update the counter and possibly save_state.
    counters_changed = False
    for candidate in counter_candidates:
        session = session_by_id[candidate.session_id]
        session.idle_observation_count = candidate.new_observation_count
        counters_changed = True

    # Salvage completions.
    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None or candidate.salvage_csid is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result + csid
        _apply_salvaged_completion(
            session, candidate.salvage_result, candidate.salvage_csid, now=now
        )

    # Merged-complete: PR already shipped; mark session COMPLETED, not TIMED_OUT.
    for candidate in merged_revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.reap_reason = (
            ReapReason.USAGE_LIMIT_CUTOFF
            if candidate.usage_limit_detected
            else ReapReason.IDLE_STALL
        )

    # GH-blocked: can't verify PR status; terminate so it is not re-detected.
    for candidate in gh_blocked_revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = (
            ReapReason.USAGE_LIMIT_CUTOFF
            if candidate.usage_limit_detected
            else ReapReason.IDLE_STALL
        )

    # Recover (revert to PENDING for re-dispatch).
    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = (
            ReapReason.USAGE_LIMIT_CUTOFF
            if candidate.usage_limit_detected
            else ReapReason.IDLE_STALL
        )

    # Park: flag-only (preserves #348 — no daemon stop, session stays ACTIVE).
    for candidate in park_candidates:
        session = session_by_id[candidate.session_id]
        session.last_result = {"paused_status": _SILENTLY_IDLE_REASON}
        session.reap_reason = ReapReason.RETRY_CAP_PARKED

    return counters_changed


def _apply_idle_queue_mutations(
    revert_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
    salvage_candidates: list[ReapCandidate],
    salvaged_result_by_ticket: dict[str, AutoDevResult],
) -> tuple[list[str], list[str]]:
    """Apply dev-queue status changes for idle dispositions.

    Acquires ``dev_queue_lock`` for the read+write window; writes only when at
    least one task changed. Returns (blocked_ticket_ids, merged_completed_ids).
    """
    recovered_ids = {c.ticket_id for c in revert_candidates if c.ticket_id}
    # (client, ticket_id) pairs, not bare ticket_id -- merged_revert_candidates
    # can now include FINALIZE-stage / merged-first candidates (GitHub #1054),
    # and ticket_id strings are not globally unique across clients.
    merged_client_tids = {
        (c.client, c.ticket_id)
        for c in merged_revert_candidates
        if c.ticket_id and c.client
    }
    gh_blocked_tids = {c.ticket_id for c in gh_blocked_revert_candidates if c.ticket_id}
    park_disposition_by_tid = {
        c.ticket_id: c.paused_status for c in park_candidates if c.ticket_id
    }
    salvaged_ticket_ids_set = {c.ticket_id for c in salvage_candidates if c.ticket_id}
    blocked: list[str] = []
    merged_completed: list[str] = []
    if not (
        recovered_ids
        or merged_client_tids
        or gh_blocked_tids
        or park_disposition_by_tid
        or salvaged_ticket_ids_set
    ):
        return blocked, merged_completed
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.ticket_id in recovered_ids:
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                changed = True
            elif task.client and (task.client, task.ticket_id) in merged_client_tids:
                # Why: PR URL is not in hand here — not worth a second gh call.
                transition_task_status(
                    task, QueueItemStatus.COMPLETED, disposition="shipped"
                )
                task.session_id = None
                merged_completed.append(task.ticket_id)
                changed = True
            elif task.ticket_id in gh_blocked_tids:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=_GH_CHECK_BLOCKED_REASON,
                )
                task.session_id = None
                changed = True
            elif task.ticket_id in park_disposition_by_tid:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=park_disposition_by_tid[task.ticket_id],
                )
                blocked.append(task.ticket_id)
                changed = True
            elif task.ticket_id in salvaged_ticket_ids_set:
                result = salvaged_result_by_ticket[task.ticket_id]
                last_result = result.model_dump(mode="json")
                transition_task_status(
                    task,
                    _queue_status_for_salvaged(result),
                    disposition=_derive_disposition(result.status),
                    pr_url=_extract_pr_url(last_result),
                )
                changed = True
        if changed:
            save_dev_queue(store)
    return blocked, merged_completed


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

    counters_changed = _apply_idle_state_mutations(
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


def _emit_idle_events(
    session_by_id: dict[str, Session],
    revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    salvage_candidates: list[ReapCandidate],
    routed_sentinel_candidates: list[ReapCandidate],
    *,
    escalate_external_candidates: list[ReapCandidate],
    already_parked_ids: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Emit lifecycle events and stop/cleanup surfaces for idle dispositions.

    Mirrors the post-queue side effects of the original act phase:
    SESSION_TIMED_OUT + worktree cleanup for reverts, SESSION_NEEDS_ATTENTION
    (with push) for parks, SESSION_COMPLETED for merged/salvage/routed,
    SESSION_NEEDS_ATTENTION for gh-blocked candidates, and
    SESSION_NEEDS_ATTENTION (with push, no mutation) for external-counterparty
    escalations (RFC 0011 B1, #1158).

    ``already_parked_ids`` is the set of session_ids that already had a
    paused_status marker before this tick's mutations.  SESSION_NEEDS_ATTENTION
    and fire_push_notification are suppressed for those sessions so re-park ticks
    emit only once on transition. See GitHub #782.
    """
    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        _cleanup_timed_out_worktree(session, candidate.ticket_id)
        cause = (
            _CAUSE_USAGE_LIMIT
            if session.reap_reason is ReapReason.USAGE_LIMIT_CUTOFF
            else _CAUSE_IDLE_STALL
        )
        record_event(
            OrchestratorEventType.SESSION_TIMED_OUT,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "elapsed_seconds": candidate.elapsed_seconds,
                "cause": cause,
                "last_assistant_message_excerpt": "",
            },
        )

    for candidate in park_candidates:
        # Edge-triggered: suppress re-emission for sessions already parked in a
        # prior tick (paused_status already set). See GitHub #782.
        if candidate.session_id in already_parked_ids:
            continue
        session = session_by_id[candidate.session_id]
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _SILENTLY_IDLE_REASON,
                "breadcrumbs": "",
                "crashed": False,
            },
        )
        _deps.fire_push_notification(session.name, session.client)

    for candidate in escalate_external_candidates:
        session = session_by_id[candidate.session_id]
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _EXTERNAL_COUNTERPARTY_IDLE_REASON,
                "breadcrumbs": "",
                "crashed": False,
            },
        )
        _deps.fire_push_notification(session.name, session.client)

    # Why: no _cleanup_timed_out_worktree for merged — PR shipped, worktree
    # content is already in main; pruning it is not our responsibility.
    for candidate in merged_revert_candidates:
        session = session_by_id[candidate.session_id]
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "crashed": False,
                "salvaged": False,
                "reason": _PHANTOM_REAP_MERGED_REASON,
            },
            correlation_id=candidate.ticket_id,
        )

    for candidate in gh_blocked_revert_candidates:
        session = session_by_id[candidate.session_id]
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _GH_CHECK_BLOCKED_REASON,
                "breadcrumbs": str(session.worktree_path)
                if session.worktree_path
                else "",
                "crashed": False,
            },
            correlation_id=candidate.ticket_id,
        )

    _emit_idle_completion_events(
        session_by_id, salvage_candidates, routed_sentinel_candidates
    )


def _emit_idle_completion_events(
    session_by_id: dict[str, Session],
    salvage_candidates: list[ReapCandidate],
    routed_sentinel_candidates: list[ReapCandidate],
) -> None:
    """Emit SESSION_COMPLETED + stop surfaces for idle salvage / routed-sentinel.

    Split out of _emit_idle_events to keep its branch count under the limit;
    both loops emit a salvaged SESSION_COMPLETED and stop the daemon surface.
    """
    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result
        completed_payload = build_salvage_completion_payload(
            session,
            ticket_id=candidate.ticket_id,
            status=candidate.salvage_result.status,
        )
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)

    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None:
            continue
        session = session_by_id[candidate.session_id]
        emit_routed_sentinel_completion(
            session,
            ticket_id=candidate.ticket_id,
            status=candidate.routed_sentinel.status,
        )


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
