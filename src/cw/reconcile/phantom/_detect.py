"""Detect-phase classification for the phantom sweep.

Pure classification helpers, extracted verbatim from the historical flat
``cw.reconcile.phantom`` module by the package split. Every function here
is read-only: zero writes to state, queue, or event bus. See GitHub #552,
ADR-0006.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.auto_dev_result import INTERMEDIATE_ADVANCE_STATUSES, AutoDevResult
from cw.config import get_client
from cw.exceptions import CwError
from cw.models import DEFAULT_LANE, OrchestratorConfig, SessionOrigin
from cw.reconcile import _shared
from cw.reconcile._shared import (
    _PAUSED_STATUS_KEY,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    ProposedAction,
    ReapCandidate,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    _transcript_age_seconds,
    ticket_id_for_session,
)
from cw.result import reconstruct_staged_sentinel

_log = logging.getLogger(__name__)

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
    enabled: bool = True,
) -> tuple[ReapCandidate | None, bool, float | None]:
    """Return ``(veto_candidate_or_None, cap_exhausted, stale_seconds)`` for an
    already_refused phantom (#1281, bounded by #1449, cap-only since #2405
    for clients with the rollout enabled).

    Guards the already_refused latch's fall-through to CRASH_COMPLETE in
    _detect_phantom_candidates: a session whose most recent tick refused a
    stage-mismatched sentinel (#1149) must not be crash-completed on the
    first tick after the refusal -- the #1281 incident killed a session 56
    seconds before its valid sentinel landed.

    GitHub #2405 (ADR-0014 audit): for an opted-in client, the veto is gated
    solely by the evidence-based attempt cap
    ``config.sentinel_mismatch_veto_cap`` against
    ``session.consecutive_sentinel_mismatch_vetoes``. Transcript age is still
    read, but only as a diagnostic (``stale_seconds`` / ``stale_minutes``); it
    never decides veto vs. fall-through, and an unlocatable transcript no
    longer short-circuits to CRASH_COMPLETE. The disabled rollout preserves
    the pre-#2405 age-gated fallback and logs the cap-only shadow decision.
    The three return shapes below describe the opted-in path:

    - ``(candidate, False, stale_seconds)`` — under the cap: veto, with the
      candidate's ``new_veto_count`` set to
      ``consecutive_sentinel_mismatch_vetoes + 1``.
    - ``(None, True, stale_seconds)`` — the count is *exactly* at the cap this
      tick (the first tick cap-exhaustion is observed): fall through and
      escalate. The caller threads ``stale_seconds`` from this exact tuple into
      the CRASH_COMPLETE fallthrough's ``stale_minutes`` instead of re-reading
      the transcript (#1449 fix cycle 2: avoids both a duplicate filesystem
      read and a TOCTOU window between the decision and the value reported in
      the escalation payload).
    - ``(None, False, None)`` — *already* past the cap (``> cap``, not ``==``):
      a session that already escalated (its counter bumped to ``cap + 1`` by
      the act phase) reads back ``> cap`` here — edge-triggering the
      escalation rather than re-firing it every tick. ``stale_seconds`` is
      deliberately dropped in this sub-case (#1449 fix cycle 3): only the tick
      that fires the cap-exhaustion escalation may populate ``stale_minutes``
      on the resulting candidate.

    ``stale_seconds`` is None in the first two shapes too when the transcript
    cannot be located.

    See GitHub #1281, #1449, #2405 and ADR-0014.
    """
    stale_seconds = _transcript_age_seconds(session, now)
    if not enabled:
        _log.warning(
            "sentinel.stage_mismatch_veto_shadowed: client=%s session=%s "
            "count=%s cap=%s stale_seconds=%s cap_only_would_veto=%s; "
            "sentinel_mismatch_veto_enabled is false",
            session.client,
            session.id,
            session.consecutive_sentinel_mismatch_vetoes,
            config.sentinel_mismatch_veto_cap,
            stale_seconds,
            session.consecutive_sentinel_mismatch_vetoes
            < config.sentinel_mismatch_veto_cap,
        )
        if (
            stale_seconds is None
            or stale_seconds >= _shared.TRANSCRIPT_LIVENESS_WINDOW_SECONDS
        ):
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
            stale_minutes=(stale_seconds / 60.0 if stale_seconds is not None else None),
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

    now is used for the already_refused veto's diagnostic transcript staleness
    (#1281, #2405) — see _sentinel_mismatch_veto_candidate.

    config bounds that veto (#1449): its ``sentinel_mismatch_veto_cap`` caps how
    many consecutive vetoes a single already_refused session may collect
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
        ticket_id = ticket_id_for_session(session.name)
        task = _task_by_ticket.get(ticket_id) if ticket_id else None
        lane = task.lane if task else DEFAULT_LANE
        # #1149: a session already marked refused (an earlier-stage replay /
        # unresolvable position stamped by _apply_phantom_routed_mutations on a
        # prior tick) must not be re-offered to any router. Hoisted above the
        # staged-sentinel branch by #1762: the refusal stamp is merged INTO
        # last_result, so it stays terminal-shaped and would otherwise be
        # reconstructed and re-refused on every tick, forever. Unlike idle.py,
        # phantom.py's detect phase has no `last_result is None` precondition,
        # so the apply-phase stamp alone would be inert here.
        already_refused = isinstance(session.last_result, dict) and (
            session.last_result.get(_PAUSED_STATUS_KEY)
            == _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
            or session.last_result.get(_SENTINEL_ADVANCE_REFUSED_KEY) is True
        )
        # Issue #536 as narrowed by #1762: a session that already pushed a
        # terminal result (last_result carries a "status") is authoritative and
        # must never be re-salvaged or re-crashed *over* -- but #536 enforced
        # that with a bare `continue`, which also denied it any completion path.
        # session.status is flipped to COMPLETED only by the Stop hook, which a
        # crashed daemon never reaches, so the owning row stayed RUNNING on
        # every tick forever, reap_policy never even consulted. Route the staged
        # result through the same shared authority the #716 stage-advance path
        # uses instead. A last_result that reconstructs into neither arm of the
        # AutoDevResult/BlockedResult union carries nothing to route, so it
        # falls through to the salvage/advance/crash pipeline below rather than
        # being ignored forever.
        #
        # Deliberately NOT DAEMON-gated, unlike the salvage and stage-advance
        # branches below: this is constructive completion off the session's own
        # recorded evidence, and it carries no origin-specific hazard --
        # ticket_id_for_session only resolves for auto-dev/<id> names, so a USER
        # session produces a ticket-less candidate and _apply_phantom_routed_
        # mutations never touches the queue for it. Gating would only mean
        # marking a session that demonstrably emitted `shipped` as CRASHED.
        if _has_terminal_sentinel(session) and not already_refused:
            staged = reconstruct_staged_sentinel(session.last_result)
            if staged is not None:
                candidates.append(
                    ReapCandidate(
                        session_id=session.id,
                        proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
                        ticket_id=ticket_id,
                        routed_sentinel=staged,
                        # May legitimately be None: unlike the transcript-parsing
                        # producers, this one reads session state, so there is no
                        # csid to derive. _resolve_routed_sentinel tolerates it.
                        salvage_csid=session.claude_session_id,
                        lane=lane,
                        client=session.client,
                        worktree_path=session.worktree_path,
                    )
                )
                continue
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
        # #1449: stamped True on the CRASH_COMPLETE fall-through below when the
        # veto declined on the tick the cap was reached (as opposed to an
        # already-escalated session past the cap). Reset per-iteration.
        veto_cap_exhausted = False
        # #1449: the diagnostic transcript staleness _sentinel_mismatch_veto_
        # candidate read on the cap-exhaustion tick, threaded through so the
        # CRASH_COMPLETE fallthrough's stale_minutes reports that exact value --
        # never re-read (fix cycle 2: avoids both a duplicate filesystem read
        # and a TOCTOU window). Non-None only when veto_cap_exhausted is True
        # this tick (fix cycle 3: the helper enforces this), and None even then
        # when the transcript is unlocatable (#2405: staleness is diagnostic).
        veto_stale_seconds: float | None = None
        if session.origin is SessionOrigin.DAEMON and not already_refused:
            advance = _phantom_advance_sentinel_candidate(session, ticket_id, lane)
            if advance is not None:
                candidates.append(advance)
                continue
        elif session.origin is SessionOrigin.DAEMON and already_refused:
            # GitHub #1281: this session was already refused on a prior tick
            # (#1149's already_refused latch above) -- without this check it
            # falls straight into the CRASH_COMPLETE construction below on the
            # very next tick (the #1281 incident: a valid AUTO_DEV_RESULT landed
            # 56s after the refusal that burned the task's final attempt). Veto
            # the crash until the attempt cap is spent (#1449, cap-only since
            # #2405 for opted-in clients -- see _sentinel_mismatch_veto_candidate).
            try:
                veto_enabled = get_client(session.client).sentinel_mismatch_veto_enabled
            except CwError:
                veto_enabled = False
            veto, veto_cap_exhausted, veto_stale_seconds = (
                _sentinel_mismatch_veto_candidate(
                    session,
                    ticket_id,
                    lane,
                    now=now,
                    config=effective_config,
                    enabled=veto_enabled,
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
        worktree_dirty_reason = (
            _shared.worktree_dirty_reason_by_path(session.client, session.worktree_path)
            if session.origin is SessionOrigin.DAEMON
            else None
        )
        worktree_dirty = worktree_dirty_reason is not None
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
        # #1923: scan for the provider-overload (API 529) signature so the
        # operator can see the phantom was likely caused by an upstream
        # outage rather than a code bug. DAEMON-only for the same reason as
        # worktree_dirty/usage_limit_detected above. Diagnostics-only per
        # A1/R1 -- see ReapCandidate.provider_overload_detected's doc comment.
        provider_overload_detected = (
            _shared.detect_provider_overload(session)
            if session.origin is SessionOrigin.DAEMON
            else False
        )
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.CRASH_COMPLETE,
                ticket_id=ticket_id,
                worktree_dirty=worktree_dirty,
                worktree_dirty_reason=worktree_dirty_reason,
                unresolved_subagent_spawn=unresolved_subagent_spawn,
                usage_limit_detected=usage_limit_detected,
                provider_overload_detected=provider_overload_detected,
                lane=lane,
                client=session.client,
                worktree_path=session.worktree_path,
                # #1449: when the sentinel-mismatch veto declined because the cap
                # was reached this tick (any transcript state, #2405), route
                # this crash to an immediate operator escalation under SIGNAL_ONLY (see
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
