"""Stalled-headless-session detection and act phases for reconcile.

A stalled headless DAEMON session is one past its wall-clock budget that
produced no further Stop-hook firings. See GitHub #185, #552, ADR-0006.
"""

from __future__ import annotations

import logging
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
from cw.gh import pr_exists_for_branch
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    CompletionReason,
    LivenessBucket,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionStatus,
    Stage,
)
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _FINALIZE_BLOCKED_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _LIVE_STATUSES,
    _NEEDS_SALVAGE_REASON,
    _PHANTOM_REAP_MERGED_REASON,
    _SALVAGE_SKIP_ESCALATED_REASON,
    _SALVAGE_SKIP_REASON,
    _SILENTLY_IDLE_REASON,
    _STALLED_CAP_PARKED_REASON,
    ProposedAction,
    ReapCandidate,
    _apply_queue_mutations,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _cleanup_timed_out_worktree,
    _emit_reap_proposed,
    _is_headless,
    _parse_any_sentinel_from_transcript,
    _queue_status_for_salvaged,
    _transcript_age_seconds,
    classify_sentinel_stage_position,
    feature_branch_key,
    resolve_headless_budget,
    resolve_reap_policy,
    resolve_stalled_retry_cap,
    ticket_id_for_session,
)
from cw.reconcile.liveness import _classify_liveness_bucket
from cw.reconcile.tasks import _client_cwd, _is_dangling_client
from cw.worktree import _has_commits_beyond_base

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.models import ClientConfig, CwState, Session, TicketTask

_log = logging.getLogger(__name__)


def _resolve_finalize_blocked_condition(
    task: TicketTask | None,
    session: Session,
    wt_path: Path,
    default_branch: str,
    *,
    clients: dict[str, ClientConfig] | None = None,
    finalize_pr_by_branch: dict[str, tuple[bool | None, bool]] | None = None,
) -> tuple[bool, str | None]:
    """Return (is_finalize_blocked, branch_name_or_none).

    True when all conditions hold:
    - task.stage == Stage.FINALIZE
    - worktree has commits beyond origin/<default_branch>
    - no open PR exists for the feature branch
    - gh is available (gh-unavailable falls through to REVERT_TASK)

    clients: pre-loaded effective clients dict; loaded lazily if None.
    finalize_pr_by_branch: pre-computed pr_exists_for_branch results keyed by
      branch name, computed in reconcile()'s lockless pre-pass to avoid calling
      gh under sessions_lock (#485). Falls back to a direct call when None
      (used by tests and revert_stalled_headless_sessions).

    Returns (False, None) for any short-circuit.
    """
    if task is None or task.stage != Stage.FINALIZE:
        return False, None
    if not _has_commits_beyond_base(wt_path, default_branch):
        return False, None
    effective_clients = (
        clients if clients is not None else _deps.load_effective_clients()
    )
    branch = feature_branch_key(session.client, task.ticket_id, effective_clients)
    if finalize_pr_by_branch is not None:
        pr_result, gh_available = finalize_pr_by_branch.get(branch, (None, False))
    else:
        # Dangling client (clients.yaml populated but missing session.client) →
        # skip the unscoped gh call and fall through to REVERT_TASK, mirroring
        # this function's "any short-circuit → (False, None)" contract
        # (GitHub #1269/#1279 R7). The precomputed-map branch above is already
        # scoped upstream via core.py's _build_finalize_pr_map.
        if _is_dangling_client(session.client, effective_clients):
            return False, None
        # Why: None means caller is outside sessions_lock (e.g.
        # revert_stalled_headless_sessions). Direct gh call is safe there.
        # _reconcile_locked converts None → {} so gh never runs under the lock
        # (#816 SHOULD_FIX 1).
        cwd = _client_cwd(session.client, effective_clients)
        pr_result, gh_available = pr_exists_for_branch(branch, cwd=cwd)
    # gh absent, PR already open, or transient error — fall through to REVERT_TASK.
    # Only pr_result is False (confirmed no open PR) triggers FINALIZE_BLOCKED.
    if not gh_available or pr_result is not False:
        return False, None
    return True, branch


def _maybe_append_salvage_skip_reset(
    candidates: list[ReapCandidate],
    session: Session,
    ticket_id: str | None,
) -> None:
    """Append a RESET_SALVAGE_SKIP_COUNTER candidate when the latch is nonzero.

    Shared by all 5 non-SKIP_PARKED detect-phase exits in
    _detect_stalled_candidates (#974). Gated at detect time on
    session.consecutive_salvage_skips != 0 so a session whose counter is
    already 0 never grows the per-tick candidate list. A session can thus
    yield TWO ReapCandidates in one pass — its own disposition candidate plus
    this reset — which is novel for this ticket.
    """
    if session.consecutive_salvage_skips != 0:
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
                ticket_id=ticket_id,
                client=session.client,
            )
        )


def _stalled_advance_sentinel_candidate(
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    clients: dict[str, ClientConfig],
) -> ReapCandidate | None:
    """ROUTE_EMITTED_SENTINEL candidate for a wall-clock-expired stage-advance session.

    Path 1 backstop (GitHub #1149, R5). ``salvage_terminal_result`` deliberately
    excludes ``stage_complete`` (not in ``SALVAGE_TERMINAL_STATUSES``), so a
    ``stage_complete`` sentinel on a stalled session is invisible to stalled.py's
    own salvage check and the session is wall-clock-reverted despite having
    finished its stage cleanly. This mirrors ``phantom._phantom_advance_sentinel_
    candidate`` (parse any sentinel, filter to ``INTERMEDIATE_ADVANCE_STATUSES``)
    but ALSO resolves the sentinel's stage position against ``task.stage`` (R2):
    only a *same*- or *later*-stage sentinel is harvested here, where routing is
    expected to succeed (or park at a signoff gate) at apply time. An *earlier*-
    stage or unresolvable-position sentinel (a stale replay, #1019) returns
    ``None`` so detection falls through to the existing finalize-blocked /
    retry-cap / wall-clock-revert chain unchanged, and this backstop never
    intercepts stalled.py's fallback reap path for a stale sentinel.
    """
    if task is None:
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


def _append_routed_advance_candidate(
    candidates: list[ReapCandidate],
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    clients: dict[str, ClientConfig],
) -> bool:
    """Append a Path 1 backstop candidate if one applies; return whether it did.

    Mirrors the other recovery exits in ``_detect_stalled_candidates``: on a
    hit, also appends the RESET_SALVAGE_SKIP_COUNTER reset when the latch is
    nonzero (#974). Extracted to keep the detect loop under the statement cap;
    the caller ``continue``s when this returns True. See GitHub #1149.
    """
    routed_advance = _stalled_advance_sentinel_candidate(
        session, task, ticket_id, elapsed, clients
    )
    if routed_advance is None:
        return False
    _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
    candidates.append(routed_advance)
    return True


def _detect_stalled_candidates(
    state: CwState,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask],
    finalize_pr_by_branch: dict[str, tuple[bool | None, bool]] | None = None,
) -> list[ReapCandidate]:
    """Pure classification phase for stalled headless DAEMON sessions.

    Returns a list of ReapCandidate objects. Makes zero writes to state,
    queue, or event bus. See GitHub #552, ADR-0006.
    """
    # Load clients once for finalize-blocked branch-key resolution across all
    # sessions in this pass, rather than per-session inside the loop.
    effective_clients = _deps.load_effective_clients()
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        # Only live, headless DAEMON sessions are eligible for the stalled sweep.
        if (
            session.status not in _LIVE_STATUSES
            or session.origin is not SessionOrigin.DAEMON
            or not _is_headless(session)
        ):
            continue
        # Park-marker check: sessions already parked by the idle watchdog.
        # Detect returns SKIP_PARKED candidate; act emits the skip event.
        if isinstance(session.last_result, dict) and session.last_result.get(
            "paused_status"
        ) in (_SILENTLY_IDLE_REASON, _NEEDS_SALVAGE_REASON):
            actual_paused_status = session.last_result.get("paused_status")
            ticket_id = ticket_id_for_session(session.name)
            # Stamp lane for SKIP_PARKED too so act phase has a consistent candidate.
            skip_task = task_by_ticket.get(ticket_id) if ticket_id else None
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SKIP_PARKED,
                    ticket_id=ticket_id,
                    paused_status=str(actual_paused_status)
                    if actual_paused_status
                    else None,
                    lane=skip_task.lane if skip_task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        budget = resolve_headless_budget(task, session, config)
        elapsed = (now - session.started_at).total_seconds()
        if elapsed < budget:
            # Why: a session that recovers (elapsed back under budget after a
            # prior SKIP_PARKED streak isn't possible via this branch alone,
            # but a session can carry a nonzero latch from an earlier tick and
            # then clear the park-marker condition above; this is one of the
            # 5 non-SKIP_PARKED detect-phase exits that must reset the latch.
            # A session can therefore yield a RESET_SALVAGE_SKIP_COUNTER
            # candidate here with no disposition candidate alongside it (#974).
            _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
            continue
        # Try terminal-sentinel salvage before declaring timeout.
        salvage = _shared.salvage_terminal_result(session)
        if salvage is not None:
            result, claude_session_id = salvage
            # Why: recovery via salvage — this session yields TWO candidates
            # this pass (its own SALVAGE_COMPLETION disposition plus a
            # RESET_SALVAGE_SKIP_COUNTER reset), novel for this ticket (#974).
            _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SALVAGE_COMPLETION,
                    ticket_id=ticket_id,
                    salvage_result=result,
                    salvage_csid=claude_session_id,
                    elapsed_seconds=elapsed,
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        # Path 1 backstop (#1149): a stage_complete advance sentinel is excluded
        # from SALVAGE_TERMINAL_STATUSES, so the salvage check above never sees
        # it. Harvest a same-/later-stage advance sentinel here (routing it via
        # the shared authority) BEFORE any finalize-blocked / cap / wall-clock
        # revert candidate is constructed, so a cleanly-completed stage is not
        # reverted. Earlier-stage / unresolvable sentinels fall through unchanged.
        if _append_routed_advance_candidate(
            candidates, session, task, ticket_id, elapsed, effective_clients
        ):
            continue
        # Finalize-blocked check: branch pushed but finalize failed before opening
        # a PR (GitHub #812). Must run BEFORE the retry-cap check so the task
        # is parked (preserving the worktree) rather than capped-and-abandoned.
        if session.worktree_path is not None and task is not None:
            fb_client_cfg = effective_clients.get(session.client)
            if fb_client_cfg is None:
                _log.warning(
                    "finalize_blocked: unknown client %r for session %s — skipping",
                    session.client,
                    session.id,
                )
            else:
                fb_blocked, fb_branch = _resolve_finalize_blocked_condition(
                    task,
                    session,
                    session.worktree_path,
                    fb_client_cfg.default_branch,
                    clients=effective_clients,
                    finalize_pr_by_branch=finalize_pr_by_branch,
                )
                if fb_blocked and fb_branch is not None:
                    # Why: recovery via finalize-park — a session can yield
                    # both its PARK_FINALIZE_BLOCKED disposition and a
                    # RESET_SALVAGE_SKIP_COUNTER candidate in the same pass
                    # (#974).
                    _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
                    candidates.append(
                        ReapCandidate(
                            session_id=session.id,
                            proposed_action=ProposedAction.PARK_FINALIZE_BLOCKED,
                            ticket_id=ticket_id,
                            elapsed_seconds=elapsed,
                            reap_reason=ReapReason.FINALIZE_BLOCKED,
                            lane=task.lane,
                            client=session.client,
                            stage=task.stage,
                            worktree_path=session.worktree_path,
                            branch=fb_branch,
                        )
                    )
                    continue
        cap = resolve_stalled_retry_cap(task, config)
        # Why: task.attempts is the shared counter for both this per-tier stalled cap
        # and the global attempt ceiling (#786). Do not add a parallel counter.
        if task is not None and task.attempts >= cap:
            # #1277: check the liveness veto FIRST — a session that is
            # demonstrably still making progress (fresh transcript) must not
            # be parked just because it also happens to be at the retry cap.
            # Previously the veto was only ever consulted from the ordinary
            # wall-clock revert path below, which this branch always
            # short-circuits via `continue`, making the veto structurally
            # unreachable for cap-exceeded sessions. Stamping
            # STALLED_RETRY_CAP_PARKED (rather than WALL_CLOCK_BUDGET) keeps
            # the emitted session.park_vetoed event's reason attributable to
            # the branch that actually produced it.
            cap_veto = _liveness_veto_candidate(
                session,
                task,
                ticket_id,
                elapsed,
                now=now,
                config=config,
                reap_reason=ReapReason.STALLED_RETRY_CAP_PARKED,
            )
            if cap_veto is not None:
                _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
                candidates.append(cap_veto)
                continue
            # Why: recovery via retry-cap park — a session can yield both its
            # PARK_BLOCKED_ON_USER disposition and a RESET_SALVAGE_SKIP_COUNTER
            # candidate in the same pass (#974).
            _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
            # Why: this branch never previously computed usage_limit_detected —
            # unlike the revert path below, which mirrors idle.py's existing
            # usage-limit precedent, this cap-park usage-limit branch is new
            # logic (GitHub #1030).
            cap_usage_limit_detected = _shared.detect_usage_limit(session)
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
                    ticket_id=ticket_id,
                    elapsed_seconds=elapsed,
                    reap_reason=(
                        ReapReason.USAGE_LIMIT_CUTOFF
                        if cap_usage_limit_detected
                        else ReapReason.STALLED_RETRY_CAP_PARKED
                    ),
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                    stage=task.stage if task else DEFAULT_STAGE,
                    attempts=task.attempts if task else 0,
                    paused_status=_STALLED_CAP_PARKED_REASON,
                    usage_limit_detected=cap_usage_limit_detected,
                )
            )
            continue
        # Why: recovery via ordinary wall-clock revert — a session can yield
        # both its REVERT_TASK disposition and a RESET_SALVAGE_SKIP_COUNTER
        # candidate in the same pass (#974). This is the 5th and final
        # non-SKIP_PARKED detect-phase exit (loop falls through, no continue).
        # The liveness veto (#976, #1277) is also checked from inside the
        # cap-exceeded branch above (with a different reap_reason) — this
        # call below only runs when that branch was not taken at all (task
        # is below the retry cap), not as a second chance for a cap-exceeded
        # session that already declined the veto.
        _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
        candidates.append(
            _resolve_wall_clock_candidate(
                session, task, ticket_id, elapsed, now=now, config=config
            )
        )
    return candidates


def _liveness_veto_candidate(
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    *,
    now: datetime,
    config: OrchestratorConfig,
    reap_reason: ReapReason,
) -> ReapCandidate | None:
    """Return a PARK_VETOED candidate when the session is still LIVE, else None.

    Computes fresh transcript-mtime staleness via :func:`_transcript_age_seconds`
    and classifies it through the same per-stage-floor liveness ladder the
    observability sweep (``cw.reconcile.liveness``) uses. When the freshly-
    classified bucket is :attr:`LivenessBucket.LIVE`, the caller's pending
    park is vetoed entirely — no disposition, no queue mutation — because the
    session is demonstrably still making progress despite the wall-clock
    budget (or retry cap) having been exceeded. Fail-toward-park: a session
    whose transcript cannot be located (``_transcript_age_seconds`` returns
    ``None``) returns ``None`` so the caller falls through to its normal
    park/revert candidate.

    Shared by two call sites (GitHub #1277): the ordinary wall-clock revert
    path (``reap_reason=WALL_CLOCK_BUDGET``) and the stalled-retry-cap park
    branch (``reap_reason=STALLED_RETRY_CAP_PARKED``). See GitHub #976.
    """
    stage = task.stage if task is not None else DEFAULT_STAGE
    stale_seconds = _transcript_age_seconds(session, now)
    if stale_seconds is None:
        return None
    stale_minutes = stale_seconds / 60.0
    bucket = _classify_liveness_bucket(stale_minutes, stage=stage, config=config)
    if bucket is not LivenessBucket.LIVE:
        return None
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.PARK_VETOED,
        ticket_id=ticket_id,
        elapsed_seconds=elapsed,
        reap_reason=reap_reason,
        lane=task.lane if task else DEFAULT_LANE,
        client=session.client,
        stage=stage,
        attempts=task.attempts if task else 0,
        stale_minutes=stale_minutes,
    )


def _resolve_wall_clock_candidate(
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    *,
    now: datetime,
    config: OrchestratorConfig,
) -> ReapCandidate:
    """Return PARK_VETOED when the session is still LIVE, else REVERT_TASK.

    Delegates the liveness check to :func:`_liveness_veto_candidate` with
    ``reap_reason=ReapReason.WALL_CLOCK_BUDGET``. See GitHub #976, #1277.
    """
    veto = _liveness_veto_candidate(
        session,
        task,
        ticket_id,
        elapsed,
        now=now,
        config=config,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
    )
    if veto is not None:
        return veto
    stage = task.stage if task is not None else DEFAULT_STAGE
    # #1030: branch reap_reason here (not after) so the SESSION_REAP_PROPOSED
    # audit event (emitted from the candidate before the apply phase runs)
    # reports the correct cause — matching the cap-park branch's pattern below.
    revert_usage_limit_detected = _shared.detect_usage_limit(session)
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id=ticket_id,
        elapsed_seconds=elapsed,
        reap_reason=(
            ReapReason.USAGE_LIMIT_CUTOFF
            if revert_usage_limit_detected
            else ReapReason.WALL_CLOCK_BUDGET
        ),
        lane=task.lane if task else DEFAULT_LANE,
        client=session.client,
        stage=stage,
        attempts=task.attempts if task else 0,
        usage_limit_detected=revert_usage_limit_detected,
    )


def _route_stalled_by_policy(
    candidates: list[ReapCandidate],
    *,
    config: OrchestratorConfig | None,
    merged_ticket_ids: frozenset[str],
    gh_blocked_ticket_ids: frozenset[str],
) -> list[ReapCandidate]:
    """Apply per-lane reap-policy routing to stalled REVERT_TASK candidates.

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
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
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
    return auto_candidates


def _apply_stalled_state_mutations(
    session_by_id: dict[str, Session],
    *,
    now: datetime,
    salvage_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
    finalize_blocked_candidates: list[ReapCandidate],
    reset_salvage_skip_candidates: list[ReapCandidate],
) -> None:
    """Apply in-place session-state mutations for stalled dispositions.

    Mirrors the pattern from _apply_idle_state_mutations; save_state is left
    to the caller's combined flush.
    """
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
        session.reap_reason = ReapReason.WALL_CLOCK_BUDGET
    # GH-blocked: can't verify PR status; terminate so not re-detected as stalled.
    for candidate in gh_blocked_revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = ReapReason.WALL_CLOCK_BUDGET
    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        # #1030: read the candidate's own reap_reason (branched at construction
        # in _resolve_wall_clock_candidate) instead of re-deriving it here from
        # usage_limit_detected — keeps this in sync with the SESSION_REAP_PROPOSED
        # audit event, which reads candidate.reap_reason before this apply phase
        # runs. Same pattern as the cap-park loop below.
        session.reap_reason = candidate.reap_reason
    # Cap exceeded: terminate and park BLOCKED_ON_USER (not re-queued to PENDING).
    for candidate in park_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        # #1030: read the candidate's own reap_reason (branched at detect time,
        # including the new USAGE_LIMIT_CUTOFF case) instead of re-hardcoding
        # the pre-#1030 STALLED_RETRY_CAP_PARKED constant — the detect-phase
        # branch above would otherwise be inert.
        session.reap_reason = candidate.reap_reason
    # Finalize-blocked: work complete, PR not opened. Preserve worktree for rescue.
    # Write branch into last_result so rescue_finalize_blocked_sessions can find it.
    for candidate in finalize_blocked_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        session.reap_reason = ReapReason.FINALIZE_BLOCKED
        session.last_result = {
            "paused_status": _FINALIZE_BLOCKED_REASON,
            "branch": candidate.branch,
        }
    # Recovery: zero the salvage-skip latch (#974). Reset-eligibility was
    # already gated at detect time (session.consecutive_salvage_skips != 0),
    # so every candidate here is a real transition back to 0.
    for candidate in reset_salvage_skip_candidates:
        session_by_id[candidate.session_id].consecutive_salvage_skips = 0


def _apply_stalled_routed_mutations(
    session_by_id: dict[str, Session],
    routed_sentinel_candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> list[ReapCandidate]:
    """Apply ROUTE_EMITTED_SENTINEL mutations for wall-clock-expired advance workers.

    Path 1 backstop act phase (GitHub #1149). A direct structural mirror of
    ``idle._apply_idle_routed_mutations``: route the emitted advance sentinel
    through the shared staged-advance authority (``_apply_sentinel_to_task`` ->
    ``apply_staged_decision``), then mark the session COMPLETED/NORMAL only when
    the route was accepted. On a stage-mismatch refusal (``routed=False``) the
    session is NOT completed -- the candidate is dropped from the accepted list
    so the session falls through to the ordinary cap / wall-clock-revert path on
    the next tick rather than being torn down on a refusal. (Unlike idle.py, no
    paused_status marker is stamped: stalled.py's detect phase only builds a
    candidate for a same-/later-stage position, which routes successfully, so
    there is no earlier-stage refusal loop to break here.)

    ``_apply_sentinel_to_task`` acquires its own ``dev_queue_lock``; session
    state is flushed by the caller's ``save_state``. ``session_by_id`` is built
    from ``state.sessions`` by reference, so these mutations persist in the
    caller's existing ``save_state(state)`` call.
    """
    accepted: list[ReapCandidate] = []
    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None or candidate.salvage_csid is None:
            continue  # Invariant: ROUTE_EMITTED_SENTINEL has routed_sentinel + csid
        routed = True
        if candidate.ticket_id:
            outcome = _apply_sentinel_to_task(
                candidate.ticket_id, candidate.session_id, candidate.routed_sentinel
            )
            routed = outcome.routed
        if not routed:
            continue
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.last_result = candidate.routed_sentinel.model_dump(mode="json")
        session.claude_session_id = candidate.salvage_csid
        accepted.append(candidate)
    return accepted


def _apply_stalled_queue_mutations(
    revert_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
    salvage_candidates: list[ReapCandidate],
    salvaged_result_by_ticket: dict[str, AutoDevResult],
) -> tuple[list[str], list[str]]:
    """Apply dev-queue status changes for stalled-session dispositions.

    Acquires ``dev_queue_lock`` for the read+write window; writes only when at
    least one task changed. Returns (reverted_ticket_ids, merged_completed_ids).
    """
    timed_out_ticket_ids = {c.ticket_id for c in revert_candidates if c.ticket_id}
    merged_tids = {c.ticket_id for c in merged_revert_candidates if c.ticket_id}
    gh_blocked_tids = {c.ticket_id for c in gh_blocked_revert_candidates if c.ticket_id}
    park_disposition_by_tid = {
        c.ticket_id: c.paused_status for c in park_candidates if c.ticket_id
    }
    salvaged_ticket_ids_set = {c.ticket_id for c in salvage_candidates if c.ticket_id}
    reverted: list[str] = []
    merged_completed: list[str] = []
    if not (
        timed_out_ticket_ids
        or merged_tids
        or gh_blocked_tids
        or park_disposition_by_tid
        or salvaged_ticket_ids_set
    ):
        return reverted, merged_completed
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.ticket_id in timed_out_ticket_ids:
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                reverted.append(task.ticket_id)
                changed = True
            elif task.ticket_id in merged_tids:
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
                task.session_id = None
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
    return reverted, merged_completed


def _apply_finalize_blocked_queue_mutations(
    candidates: list[ReapCandidate],
) -> None:
    """Route RUNNING tasks for finalize-blocked sessions to BLOCKED_ON_USER.

    Separate from _apply_stalled_queue_mutations because finalize-blocked tasks
    are RUNNING at detection time (not yet reverted to PENDING by a prior tick).
    Under dev_queue_lock; no-op when the candidate set is empty. See GitHub #812.
    """
    ticket_ids = {c.ticket_id for c in candidates if c.ticket_id}
    if not ticket_ids:
        return
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.ticket_id not in ticket_ids:
                continue
            if task.status != QueueItemStatus.RUNNING:
                continue
            transition_task_status(
                task,
                QueueItemStatus.BLOCKED_ON_USER,
                disposition=_FINALIZE_BLOCKED_REASON,
            )
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)


def _emit_finalize_blocked_events(
    session_by_id: dict[str, Session],
    candidates: list[ReapCandidate],
) -> None:
    """Emit events for finalize-blocked sessions: stop daemon, SESSION_NEEDS_ATTENTION.

    Worktree is NOT cleaned up — rescue_finalize_blocked_sessions opens the PR.
    """
    for candidate in candidates:
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
                "paused_status": _FINALIZE_BLOCKED_REASON,
                "breadcrumbs": candidate.branch or "",
                "crashed": False,
            },
            correlation_id=candidate.ticket_id,
        )
        _deps.fire_push_notification(session.name, session.client)


def _branch_state_for_ticket(
    ticket_id: str | None,
    absent: frozenset[str],
) -> str | None:
    """Return branch_state tag for SESSION_TIMED_OUT payload, or None if omitted."""
    if ticket_id is not None and ticket_id in absent:
        return "absent_no_merged_pr"
    return None


def _emit_stalled_routed_events(
    session_by_id: dict[str, Session],
    routed_sentinel_candidates: list[ReapCandidate],
) -> None:
    """Emit salvaged SESSION_COMPLETED + stop surface for routed advance sentinels.

    Path 1 backstop (#1149). Mirrors ``idle._emit_idle_completion_events``'
    routed-sentinel loop and stalled.py's own salvage loop. Extracted so
    ``_emit_stalled_events`` stays under the branch cap.
    """
    for candidate in routed_sentinel_candidates:
        if candidate.routed_sentinel is None:
            continue  # Invariant: ROUTE_EMITTED_SENTINEL has routed_sentinel
        session = session_by_id[candidate.session_id]
        routed_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": candidate.routed_sentinel.status,
        }
        record_event(OrchestratorEventType.SESSION_COMPLETED, routed_payload)
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)


def _emit_stalled_events(
    session_by_id: dict[str, Session],
    revert_candidates: list[ReapCandidate],
    merged_revert_candidates: list[ReapCandidate],
    gh_blocked_revert_candidates: list[ReapCandidate],
    park_candidates: list[ReapCandidate],
    salvage_candidates: list[ReapCandidate],
    finalize_blocked_candidates: list[ReapCandidate],
    routed_sentinel_candidates: list[ReapCandidate],
    *,
    branch_absent_ticket_ids: frozenset[str] = frozenset(),
) -> None:
    """Emit lifecycle events and stop/cleanup surfaces for stalled dispositions.

    Mirrors the post-queue side effects of the original act phase: SESSION_TIMED_OUT
    + worktree cleanup for reverts, SESSION_COMPLETED for merged/salvage, and
    SESSION_NEEDS_ATTENTION for gh-blocked and finalize-blocked candidates.
    ROUTE_EMITTED_SENTINEL candidates (Path 1 backstop, #1149) emit a salvaged
    SESSION_COMPLETED and stop the surface, mirroring the salvage loop.
    """
    for candidate in revert_candidates:
        session = session_by_id[candidate.session_id]
        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "elapsed_seconds": candidate.elapsed_seconds,
            "last_assistant_message_excerpt": "",
        }
        branch_state = _branch_state_for_ticket(
            candidate.ticket_id, branch_absent_ticket_ids
        )
        if branch_state is not None:
            payload["branch_state"] = branch_state
        record_event(OrchestratorEventType.SESSION_TIMED_OUT, payload)
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        _cleanup_timed_out_worktree(session, candidate.ticket_id)

    # Why: no _cleanup_timed_out_worktree for merged — the PR shipped, so the
    # worktree content is already in main; pruning it is not our responsibility.
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

    for candidate in park_candidates:
        session = session_by_id[candidate.session_id]
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        _cleanup_timed_out_worktree(session, candidate.ticket_id)
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _STALLED_CAP_PARKED_REASON,
                "breadcrumbs": str(session.worktree_path)
                if session.worktree_path
                else "",
                "crashed": False,
                "stage": str(candidate.stage),
                "attempts": candidate.attempts,
            },
            correlation_id=candidate.ticket_id,
        )
        _deps.fire_push_notification(session.name, session.client)

    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result
        completed_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "claude_session_id": session.claude_session_id,
            "crashed": False,
            "salvaged": True,
            "status": candidate.salvage_result.status,
        }
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)

    _emit_stalled_routed_events(session_by_id, routed_sentinel_candidates)
    _emit_finalize_blocked_events(session_by_id, finalize_blocked_candidates)


def _record_salvage_skip(
    session_by_id: dict[str, Session],
    candidate: ReapCandidate,
    *,
    config: OrchestratorConfig,
) -> None:
    """Increment a session's salvage-skip latch and emit its events (#974).

    Always emits SESSION_SALVAGE_SKIPPED (unchanged payload). Additionally
    emits session.needs_attention exactly once, when the incremented count
    hits config.salvage_skip_attention_threshold (latch: no re-fire while
    still at the threshold on subsequent ticks, since detect only re-appends
    a SKIP_PARKED candidate — the count keeps climbing past the threshold on
    later ticks, but the emit below only fires on exact equality).
    """
    session = session_by_id[candidate.session_id]
    session.consecutive_salvage_skips += 1
    record_event(
        OrchestratorEventType.SESSION_SALVAGE_SKIPPED,
        {
            "session_id": candidate.session_id,
            "ticket_id": candidate.ticket_id,
            "reason": _SALVAGE_SKIP_REASON,
            "paused_status": candidate.paused_status,
        },
        correlation_id=candidate.ticket_id,
    )
    if session.consecutive_salvage_skips == config.salvage_skip_attention_threshold:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _SALVAGE_SKIP_ESCALATED_REASON,
                "breadcrumbs": (
                    f"{session.consecutive_salvage_skips} consecutive "
                    f"salvage-skips; last reason: {_SALVAGE_SKIP_REASON}"
                ),
                "crashed": False,
            },
            correlation_id=candidate.ticket_id,
        )


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

    candidates = _route_stalled_by_policy(
        candidates,
        config=config,
        merged_ticket_ids=merged_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
    )
    if not candidates:
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
    # reset_salvage_skip/park_vetoed/routed_sentinel exhaustively partition every
    # ProposedAction that _detect_stalled_candidates emits — so a second guard here
    # can never fire and only risks silently reintroducing the same bug class
    # the next time a new ProposedAction is added and this list isn't updated
    # to match. session_by_id must be built unconditionally: SKIP_PARKED/
    # RESET_SALVAGE_SKIP_COUNTER candidates need live Session objects even
    # when they are the tick's only candidates.
    session_by_id = {s.id: s for s in state.sessions}

    effective_config = config if config is not None else OrchestratorConfig()

    # SKIP_PARKED: increment the salvage-skip latch and emit its event(s).
    for candidate in skip_candidates:
        _record_salvage_skip(session_by_id, candidate, config=effective_config)

    # PARK_VETOED: side-effect-only — emit session.park_vetoed, mutate nothing
    # (#976). No state/queue mutation is added for this action anywhere below.
    for candidate in park_vetoed_candidates:
        record_event(
            OrchestratorEventType.SESSION_PARK_VETOED,
            {
                "ticket_id": candidate.ticket_id,
                "client": candidate.client,
                "session_id": candidate.session_id,
                "stage": candidate.stage,
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

    _apply_stalled_state_mutations(
        session_by_id,
        now=now,
        salvage_candidates=salvage_candidates,
        merged_revert_candidates=merged_revert_candidates,
        gh_blocked_revert_candidates=gh_blocked_revert_candidates,
        revert_candidates=revert_candidates,
        park_candidates=park_candidates,
        finalize_blocked_candidates=finalize_blocked_candidates,
        reset_salvage_skip_candidates=reset_salvage_skip_candidates,
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
