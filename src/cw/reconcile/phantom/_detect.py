"""Detect-phase classification for the phantom sweep.

Pure classification helpers, extracted verbatim from the historical flat
``cw.reconcile.phantom`` module by the package split. Every function here
is read-only: zero writes to state, queue, or event bus. See GitHub #552,
ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.auto_dev_result import INTERMEDIATE_ADVANCE_STATUSES, AutoDevResult
from cw.models import DEFAULT_LANE, OrchestratorConfig, SessionOrigin
from cw.reconcile import _shared
from cw.reconcile._shared import (
    _PAUSED_STATUS_KEY,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    ProposedAction,
    ReapCandidate,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    _transcript_age_seconds,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, Session, TicketTask


def _phantom_advance_sentinel_candidate(
    session: Session,
    ticket_id: str | None,
    lane: str,
) -> ReapCandidate | None:
    """Return a ROUTE_EMITTED_SENTINEL candidate for an exited stage-advance worker.

    The staged engine spawns a fresh worker per stage; a worker that finishes its
    stage emits ``stage_complete`` and exits, so its surface leaves the daemon
    roster (it becomes a phantom). ``stage_complete`` is not in
    ``SALVAGE_TERMINAL_STATUSES`` so terminal salvage skips it — without this it
    would be reverted as a crash and the next dispatch would re-run the SAME stage
    (the ~21-26 min/stage timeout tax, #716). Routing it here advances the stage
    via the shared authority, mirroring the alive-session ROUTE_EMITTED_SENTINEL
    path in ``idle.py``. Returns ``None`` when the transcript has no parseable
    sentinel or the status is not a non-terminal advance.
    """
    parsed = _parse_any_sentinel_from_transcript(session)
    if parsed is None:
        return None
    result, csid = parsed
    if (
        not isinstance(result, AutoDevResult)
        or result.status not in INTERMEDIATE_ADVANCE_STATUSES
    ):
        return None
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
        ticket_id=ticket_id,
        routed_sentinel=result,
        salvage_csid=csid,
        lane=lane,
        client=session.client,
        worktree_path=session.worktree_path,
    )


def _sentinel_mismatch_veto_candidate(
    session: Session,
    ticket_id: str | None,
    lane: str,
    *,
    now: datetime,
    config: OrchestratorConfig,
) -> tuple[ReapCandidate | None, bool, float | None]:
    """Return ``(veto_candidate_or_None, cap_exhausted, stale_seconds)`` for an
    already_refused phantom (#1281, bounded by #1449).

    Guards the already_refused latch's fall-through to CRASH_COMPLETE in
    _detect_phantom_candidates: a session whose most recent tick refused a
    stage-mismatched sentinel (#1149) must not be crash-completed while
    its transcript is still advancing -- the #1281 incident killed a
    session 56 seconds before its valid sentinel landed. Mirrors
    stalled.py's _liveness_veto_candidate (#976, #1445) architecture,
    but checks the plain _transcript_age_seconds <
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS window instead of the per-stage
    liveness-bucket ladder.

    The veto is bounded (#1449): once ``session.consecutive_sentinel_mismatch_vetoes``
    reaches ``config.sentinel_mismatch_veto_cap`` the pending crash proceeds. The
    three return shapes mirror ``_liveness_veto_candidate`` exactly:

    - ``(candidate, False, stale_seconds)`` — LIVE and under the cap: veto, with
      the candidate's ``new_veto_count`` set to
      ``consecutive_sentinel_mismatch_vetoes + 1``.
    - ``(None, True, stale_seconds)`` — LIVE and the count is *exactly* at the cap
      this tick (the first tick cap-exhaustion is observed): fall through and
      escalate. The caller threads ``stale_seconds`` from this exact tuple — the
      value the cap decision was made on — into the CRASH_COMPLETE fallthrough's
      ``stale_minutes`` instead of re-reading the transcript (#1449 fix cycle 2:
      avoids both a duplicate filesystem read and a TOCTOU window between the
      decision and the value reported in the escalation payload).
    - ``(None, False, None)`` — either not LIVE / transcript unlocatable
      (fail-toward-crash: an ordinary crash, NOT a cap-fire), OR LIVE but
      *already* past the cap (``> cap``, not ``==``): a still-LIVE session that
      already escalated (its counter bumped to ``cap + 1`` by the act phase)
      reads back ``> cap`` here — edge-triggering the escalation rather than
      re-firing it every tick the session stays LIVE. ``stale_seconds`` is
      deliberately dropped (not threaded) in this sub-case too (#1449 fix cycle
      3): only the tick that actually fires the cap-exhaustion escalation may
      populate ``stale_minutes`` on the resulting candidate — a still-LIVE
      already-escalated session must never carry a populated ``stale_minutes``
      on a ``veto_cap_exhausted=False`` candidate, mirroring the ``cap_exhausted``
      boolean's own two-source-of-truth requirement above it.

    See GitHub #1281, #1449.
    """
    stale_seconds = _transcript_age_seconds(session, now)
    if stale_seconds is None or stale_seconds >= TRANSCRIPT_LIVENESS_WINDOW_SECONDS:
        return None, False, None
    cap = config.sentinel_mismatch_veto_cap
    if session.consecutive_sentinel_mismatch_vetoes >= cap:
        cap_exhausted = session.consecutive_sentinel_mismatch_vetoes == cap
        return None, cap_exhausted, stale_seconds if cap_exhausted else None
    return (
        ReapCandidate(
            session_id=session.id,
            proposed_action=ProposedAction.SENTINEL_STAGE_MISMATCH_VETOED,
            ticket_id=ticket_id,
            lane=lane,
            client=session.client,
            worktree_path=session.worktree_path,
            stale_minutes=stale_seconds / 60.0,
            new_veto_count=session.consecutive_sentinel_mismatch_vetoes + 1,
        ),
        False,
        stale_seconds,
    )


def _detect_phantom_candidates(
    state: CwState,
    phantom_set: set[str],
    task_by_ticket: dict[str, TicketTask] | None = None,
    *,
    now: datetime,
    config: OrchestratorConfig | None = None,
) -> list[ReapCandidate]:
    """Pure classification phase for phantom sessions.

    Returns a list of ReapCandidate objects. Makes zero writes.
    The worktree_dirty check for DAEMON sessions is performed here
    so the act phase does not need to repeat it. See GitHub #552, ADR-0006.

    task_by_ticket is used to stamp candidate.lane from the owning task's lane
    (GitHub #560). When None or the ticket has no task, lane defaults to DEFAULT_LANE.

    now is used by the already_refused liveness veto (#1281) — see
    _sentinel_mismatch_veto_candidate.

    config bounds that veto (#1449): its ``sentinel_mismatch_veto_cap`` caps how
    many consecutive LIVE vetoes a single already_refused session may collect
    before the pending CRASH_COMPLETE proceeds. Defaults to a fresh
    OrchestratorConfig() (cap=2) — a pure read, no I/O, preserving detect-phase
    purity — so all existing callers keep today's behavior.
    """
    effective_config = config if config is not None else OrchestratorConfig()
    _task_by_ticket = task_by_ticket or {}
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.id not in phantom_set:
            continue
        # Issue #536: a session that already pushed a terminal result via
        # ``cw result emit`` (last_result carries a "status") is authoritative —
        # never re-salvage or re-crash over it. Mirrors idle.py:151. Left ACTIVE
        # with no inline completion path (R11(a), accepted non-blocking risk for
        # Phase 1): consume_completed_sessions is event-driven off
        # SESSION_COMPLETED, which a crashed session never emits, so this is a
        # genuine gap in automated recovery, not a covered case — operator
        # resolution is required until a compensating signal exists.
        if _has_terminal_sentinel(session):
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = _task_by_ticket.get(ticket_id) if ticket_id else None
        lane = task.lane if task else DEFAULT_LANE
        # Try sentinel salvage before declaring crashed (DAEMON only).
        salvage = (
            _shared.salvage_terminal_result(session)
            if session.origin is SessionOrigin.DAEMON
            else None
        )
        if salvage is not None:
            result, claude_session_id = salvage
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SALVAGE_COMPLETION,
                    ticket_id=ticket_id,
                    salvage_result=result,
                    salvage_csid=claude_session_id,
                    lane=lane,
                    client=session.client,
                    worktree_path=session.worktree_path,
                )
            )
            continue
        # Non-terminal advance sentinel (stage_complete): the worker finished a
        # stage and exited. Route it to advance the stage instead of reverting it
        # as a crash (DAEMON only; USER sessions have no staged task). See #716.
        # #1149: skip a session already marked refused (an earlier-stage replay /
        # unresolvable position stamped by _apply_phantom_routed_mutations on a
        # prior tick) so the same doomed candidate is not re-offered forever;
        # it falls through to the ordinary CRASH_COMPLETE construction below.
        # Unlike idle.py, phantom.py's detect phase has no `last_result is None`
        # precondition, so the apply-phase stamp alone would be inert here.
        already_refused = isinstance(session.last_result, dict) and (
            session.last_result.get(_PAUSED_STATUS_KEY)
            == _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
            or session.last_result.get(_SENTINEL_ADVANCE_REFUSED_KEY) is True
        )
        # #1449: stamped True on the CRASH_COMPLETE fall-through below when the
        # veto declined *because the cap was reached* (as opposed to the
        # transcript being genuinely stale). Reset per-iteration.
        veto_cap_exhausted = False
        # #1449: the transcript staleness _sentinel_mismatch_veto_candidate used
        # to make the cap-exhaustion call, threaded through so the CRASH_COMPLETE
        # fallthrough's stale_minutes reports the exact value the decision was
        # made on -- never re-read (fix cycle 2: avoids both a duplicate
        # filesystem read and a TOCTOU window between the decision and the value
        # reported in the escalation payload). Non-None if-and-only-if
        # veto_cap_exhausted is True this tick (fix cycle 3: the helper itself
        # enforces this pairing -- see _sentinel_mismatch_veto_candidate).
        veto_stale_seconds: float | None = None
        if session.origin is SessionOrigin.DAEMON and not already_refused:
            advance = _phantom_advance_sentinel_candidate(session, ticket_id, lane)
            if advance is not None:
                candidates.append(advance)
                continue
        elif session.origin is SessionOrigin.DAEMON and already_refused:
            # GitHub #1281: this session was already refused on a prior tick
            # (#1149's already_refused latch above) -- without this check it
            # falls straight into the CRASH_COMPLETE construction below with
            # zero liveness check, even when the transcript is still actively
            # advancing (the #1281 incident: a valid AUTO_DEV_RESULT landed 56s
            # after the refusal that burned the task's final attempt). Veto the
            # crash while the transcript is fresh -- see
            # _sentinel_mismatch_veto_candidate.
            veto, veto_cap_exhausted, veto_stale_seconds = (
                _sentinel_mismatch_veto_candidate(
                    session, ticket_id, lane, now=now, config=effective_config
                )
            )
            if veto is not None:
                candidates.append(veto)
                continue
        # Dirty-check for DAEMON sessions only; USER sessions have no worktree.
        # Why: this check runs inside sessions_lock before the queue mutation, but
        # the orphaned claude --bg process may still be alive and could write to the
        # worktree between here and the BLOCKED_ON_USER routing in
        # _act_on_phantom_candidates (TOCTOU). Accepted tradeoff: block > clobber —
        # narrow the window, accept the race. See _act_on_phantom_candidates.
        worktree_dirty = (
            _shared.worktree_dirty_by_path(session.client, session.worktree_path)
            if session.origin is SessionOrigin.DAEMON
            else False
        )
        # Scan for usage-limit text in the transcript so the dispatch loop can
        # engage its backoff when a phantom was killed by a rate limit, not a
        # code bug (#804). Only meaningful for DAEMON sessions (USER sessions
        # have no auto-dev transcript path).
        usage_limit_detected = (
            _shared.usage_limit_is_recent(
                _shared.detect_usage_limit(session),
                window_seconds=_shared.USAGE_LIMIT_BACKOFF_WINDOW_SECONDS,
            )
            if session.origin is SessionOrigin.DAEMON
            else False
        )
        # #1646: did this worker die with a sub-agent spawn still in flight?
        # DAEMON-only for the same reason as worktree_dirty above — a USER
        # session has no cw-managed worktree to have left a stamp in. Reads the
        # same worktree_path, fail-open to False on any missing evidence.
        unresolved_subagent_spawn = (
            _shared.read_unresolved_subagent_spawn(session.worktree_path)
            if session.origin is SessionOrigin.DAEMON
            else False
        )
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.CRASH_COMPLETE,
                ticket_id=ticket_id,
                worktree_dirty=worktree_dirty,
                unresolved_subagent_spawn=unresolved_subagent_spawn,
                usage_limit_detected=usage_limit_detected,
                lane=lane,
                client=session.client,
                worktree_path=session.worktree_path,
                # #1449: when the sentinel-mismatch veto declined because the cap
                # was reached on a still-LIVE session, route this crash to an
                # immediate operator escalation under SIGNAL_ONLY (see
                # _route_phantom_by_policy) and stamp the post-escalation counter
                # value (cap + 1) so the act phase persists it before the veto is
                # re-checked next tick — edge-triggering the escalation.
                veto_cap_exhausted=veto_cap_exhausted,
                new_veto_count=(
                    effective_config.sentinel_mismatch_veto_cap + 1
                    if veto_cap_exhausted
                    else 0
                ),
                stale_minutes=(
                    veto_stale_seconds / 60.0
                    if veto_stale_seconds is not None
                    else None
                ),
            )
        )
    return candidates


def _split_crash_candidates(
    candidates: list[ReapCandidate],
    merged_ticket_ids: frozenset[str],
    gh_blocked_ticket_ids: frozenset[str],
) -> tuple[list[ReapCandidate], list[ReapCandidate], list[ReapCandidate]]:
    """Partition CRASH_COMPLETE candidates by world-state check results (#637).

    Returns (crash_candidates, merged_crash_candidates, gh_blocked_crash_candidates).
    merged_ticket_ids / gh_blocked_ticket_ids come from a pre-pass in reconcile()
    that runs BEFORE sessions_lock, so no gh subprocess executes here. Candidates
    with no ticket_id fall through to the normal crash path.
    """
    all_crash_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.CRASH_COMPLETE
    ]
    merged_crash_candidates = [
        c
        for c in all_crash_candidates
        if c.ticket_id and c.ticket_id in merged_ticket_ids
    ]
    gh_blocked_crash_candidates = [
        c
        for c in all_crash_candidates
        if c.ticket_id and c.ticket_id in gh_blocked_ticket_ids
    ]
    crash_candidates = [
        c
        for c in all_crash_candidates
        if c not in merged_crash_candidates and c not in gh_blocked_crash_candidates
    ]
    return crash_candidates, merged_crash_candidates, gh_blocked_crash_candidates
