"""Detect-phase classification for the silently-idle sweep.

Pure classification helpers, extracted verbatim from the historical flat
``cw.reconcile.idle`` module by the package split. Every function here is
read-only: zero writes to state, queue, or event bus. See GitHub #105,
#121, #545, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import INTERMEDIATE_ADVANCE_STATUSES, AutoDevResult
from cw.models import DEFAULT_LANE, SessionOrigin, Stage
from cw.pr_hydrate import derive_counterparty
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _EXTERNAL_COUNTERPARTY_IDLE_REASON,
    _LIVE_STATUSES,
    _SILENTLY_IDLE_REASON,
    ProposedAction,
    ReapCandidate,
    _awaiting_subagent,
    _detect_post_review_clean,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    _transcript_recently_active,
    classify_sentinel_stage_position,
    resolve_idle_retry_cap,
    resolve_idle_watchdog_budget,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import ClientConfig, CwState, OrchestratorConfig, Session, TicketTask
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
        usage_limit_detected=_shared.usage_limit_is_recent(
            _shared.detect_usage_limit(session),
            window_seconds=_shared.USAGE_LIMIT_BACKOFF_WINDOW_SECONDS,
        ),
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
