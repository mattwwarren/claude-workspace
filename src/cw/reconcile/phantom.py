"""Phantom-session detection and act phases for reconcile.

A phantom session is ACTIVE/IDLE in cw state but absent from the daemon
roster (its surface is dead). See GitHub #552, ADR-0006.
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
)
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _GH_CHECK_BLOCKED_REASON,
    _PAUSED_STATUS_KEY,
    _PHANTOM_REAP_MERGED_REASON,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    ProposedAction,
    ReapCandidate,
    _apply_queue_mutations,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    _queue_status_for_salvaged,
    _transcript_age_seconds,
    resolve_reap_policy,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, Session, TicketTask


# paused_status written to SESSION_NEEDS_ATTENTION when the phantom sweep's
# sentinel-stage-mismatch veto cap is exhausted on a still-LIVE already_refused
# session and the pending CRASH_COMPLETE proceeds (#1449). Defined locally (not
# in _shared.py, which is outside this ticket's file set) — mirrors the
# retry-cap park's own escalation reason. See docs/events.md.
_SENTINEL_MISMATCH_VETO_CAP_EXHAUSTED_REASON = "sentinel_mismatch_veto_cap_exhausted"


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
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.CRASH_COMPLETE,
                ticket_id=ticket_id,
                worktree_dirty=worktree_dirty,
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


def _apply_phantom_salvage_mutations(
    session_by_id: dict[str, Session],
    salvage_candidates: list[ReapCandidate],
    *,
    now: datetime,
    phantom_names: list[str],
    salvaged_ticket_ids: list[str],
    salvaged_result_by_ticket: dict[str, AutoDevResult],
    pending_events: list[dict[str, object]],
) -> None:
    """Apply SALVAGE_COMPLETION state mutations for phantom sessions.

    Mutates ``phantom_names``, ``salvaged_ticket_ids``,
    ``salvaged_result_by_ticket`` and ``pending_events`` in place to accumulate
    the salvage outcome for the caller's queue mutation and event emission.
    """
    for candidate in salvage_candidates:
        session = session_by_id[candidate.session_id]
        if candidate.salvage_result is None or candidate.salvage_csid is None:
            continue  # Invariant: SALVAGE_COMPLETION always has salvage_result + csid
        # RFC 0012 A3 (#1459): the door arbitrates first-writer-wins. A refusal
        # (another authority already recorded a terminal result) short-circuits
        # the whole completion for this candidate -- skip every accumulator
        # append so its ticket is not routed and no SESSION_COMPLETED fires.
        outcome = _apply_salvaged_completion(
            session, candidate.salvage_result, candidate.salvage_csid, now=now
        )
        if outcome.refused:
            continue
        phantom_names.append(session.name)
        if candidate.ticket_id:
            salvaged_ticket_ids.append(candidate.ticket_id)
            salvaged_result_by_ticket[candidate.ticket_id] = candidate.salvage_result
        # Why: claude_session_id is intentionally omitted here, unlike the
        # 8-field payload shape idle.py/stalled.py build via
        # cw.reconcile.dispositions.build_salvage_completion_payload (#1306) —
        # this loop's payload predates that shared helper and was not
        # widened to match it as part of this extraction.
        salvaged_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "crashed": False,
            "salvaged": True,
            "status": candidate.salvage_result.status,
        }
        if candidate.ticket_id:
            salvaged_payload["ticket_id"] = candidate.ticket_id
        pending_events.append(salvaged_payload)


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
    """
    effective_config = config if config is not None else OrchestratorConfig()
    clients = _deps.load_effective_clients()
    # Route each clean CRASH_COMPLETE candidate individually based on its lane's policy.
    # Merged-PR / gh-blocked check (GitHub #637) runs BEFORE policy routing so
    # that a confirmed-merged ticket is always completed, even under SIGNAL_ONLY.
    # Dirty phantoms always go to BLOCKED_ON_USER regardless of policy.
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
    escalate_candidates: list[ReapCandidate] = []
    for c in candidates:
        if c.proposed_action == ProposedAction.CRASH_COMPLETE and not c.worktree_dirty:
            if c.ticket_id and (
                c.ticket_id in merged_ticket_ids or c.ticket_id in gh_blocked_ticket_ids
            ):
                auto_candidates.append(c)
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
        else:
            auto_candidates.append(c)
    if signal_mutations:
        _apply_queue_mutations(
            signal_mutations,
            clear_session_id=set(),
            disposition=ReapReason.PHANTOM_SURFACE.value,
        )
    return auto_candidates, escalate_candidates


def _emit_phantom_terminal_events(
    session_by_id: dict[str, Session],
    crash_candidates: list[ReapCandidate],
    merged_crash_candidates: list[ReapCandidate],
    gh_blocked_crash_candidates: list[ReapCandidate],
) -> set[str]:
    """Emit terminal lifecycle events for phantom dispositions (post-save_state).

    Stops surfaces and emits SESSION_COMPLETED for merged phantoms,
    SESSION_NEEDS_ATTENTION for gh-blocked phantoms, and SESSION_PHANTOM_REVERTED
    for DAEMON-origin crashes. Returns the set of dirty-worktree ticket IDs;
    the return value is pre-computed by the caller (dirty_ticket_ids) and the
    return is retained for signature compatibility — see #867.
    """
    # SESSION_COMPLETED for merged phantoms (PR already shipped, not CRASHED).
    for candidate in merged_crash_candidates:
        session = session_by_id[candidate.session_id]
        if session.surface_ref is not None:
            _deps.get_native_daemon_client().stop(session.surface_ref)
        merged_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "crashed": False,
            "salvaged": False,
            "reason": _PHANTOM_REAP_MERGED_REASON,
        }
        if candidate.ticket_id:
            merged_payload["ticket_id"] = candidate.ticket_id
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            merged_payload,
            correlation_id=candidate.ticket_id,
        )

    # SESSION_NEEDS_ATTENTION for gh-blocked phantoms.
    for candidate in gh_blocked_crash_candidates:
        session = session_by_id[candidate.session_id]
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _GH_CHECK_BLOCKED_REASON,
                "breadcrumbs": str(candidate.worktree_path)
                if candidate.worktree_path
                else "",
                "crashed": False,
                "lane": candidate.lane,
            },
            correlation_id=candidate.ticket_id,
        )

    # Emit SESSION_PHANTOM_REVERTED for DAEMON-origin CRASH_COMPLETE candidates.
    dirty_ticket_ids: set[str] = set()
    for candidate in crash_candidates:
        if (
            candidate.ticket_id
            and session_by_id[candidate.session_id].origin is SessionOrigin.DAEMON
        ):
            wt_path_str: str | None = (
                str(candidate.worktree_path) if candidate.worktree_path else None
            )
            if candidate.worktree_dirty:
                dirty_ticket_ids.add(candidate.ticket_id)
            queue_status = (
                QueueItemStatus.BLOCKED_ON_USER
                if candidate.worktree_dirty
                else QueueItemStatus.PENDING
            )
            record_event(
                OrchestratorEventType.SESSION_PHANTOM_REVERTED,
                {
                    "session_id": candidate.session_id,
                    "ticket_id": candidate.ticket_id,
                    "client": candidate.client,
                    "worktree_dirty": candidate.worktree_dirty,
                    "worktree_path": wt_path_str,
                    "queue_status": queue_status,
                },
                correlation_id=candidate.ticket_id,
            )
    return dirty_ticket_ids


def _apply_phantom_queue_mutations(
    session_by_id: dict[str, Session],
    crash_candidates: list[ReapCandidate],
    merged_crash_candidates: list[ReapCandidate],
    gh_blocked_crash_candidates: list[ReapCandidate],
    salvaged_ticket_ids: list[str],
    salvaged_result_by_ticket: dict[str, AutoDevResult],
    dirty_ticket_ids: set[str],
    ticket_ids_to_revert: list[str],
    merged_completed_ids: list[str],
) -> None:
    """Apply dev-queue status changes for phantom dispositions.

    Mutates ``ticket_ids_to_revert`` and ``merged_completed_ids`` in place to
    surface the PENDING-reverted and merged-completed ticket IDs to the caller.
    Acquires ``dev_queue_lock``; writes only when at least one task changed.
    """
    daemon_ticket_ids_to_revert = [
        c.ticket_id
        for c in crash_candidates
        if c.ticket_id and session_by_id[c.session_id].origin is SessionOrigin.DAEMON
    ]
    revert_set = set(daemon_ticket_ids_to_revert)
    merged_crash_tids = {c.ticket_id for c in merged_crash_candidates if c.ticket_id}
    gh_blocked_crash_tids = {
        c.ticket_id for c in gh_blocked_crash_candidates if c.ticket_id
    }
    salvaged_set = set(salvaged_ticket_ids)
    if not (revert_set or merged_crash_tids or gh_blocked_crash_tids or salvaged_set):
        return
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.ticket_id in revert_set:
                if task.ticket_id in dirty_ticket_ids:
                    transition_task_status(
                        task,
                        QueueItemStatus.BLOCKED_ON_USER,
                        disposition="dirty_worktree",
                    )
                else:
                    transition_task_status(task, QueueItemStatus.PENDING)
                    ticket_ids_to_revert.append(task.ticket_id)
                task.session_id = None
                changed = True
            elif task.ticket_id in merged_crash_tids:
                # Why: PR URL is not in hand here — not worth a second gh call.
                transition_task_status(
                    task, QueueItemStatus.COMPLETED, disposition="shipped"
                )
                task.session_id = None
                merged_completed_ids.append(task.ticket_id)
                changed = True
            elif task.ticket_id in gh_blocked_crash_tids:
                transition_task_status(
                    task,
                    QueueItemStatus.BLOCKED_ON_USER,
                    disposition=_GH_CHECK_BLOCKED_REASON,
                )
                task.session_id = None
                changed = True
            elif task.ticket_id in salvaged_set:
                salvaged_result = salvaged_result_by_ticket[task.ticket_id]
                last_result = salvaged_result.model_dump(mode="json")
                transition_task_status(
                    task,
                    _queue_status_for_salvaged(salvaged_result),
                    disposition=_derive_disposition(salvaged_result.status),
                    pr_url=_extract_pr_url(last_result),
                )
                changed = True
        if changed:
            save_dev_queue(store)


def _apply_phantom_routed_mutations(
    session_by_id: dict[str, Session],
    routed_candidates: list[ReapCandidate],
    *,
    now: datetime,
    phantom_names: list[str],
) -> list[ReapCandidate]:
    """Apply ROUTE_EMITTED_SENTINEL mutations for exited stage-advance workers (#716).

    Routes the emitted advance sentinel through the shared staged-advance
    authority (``_apply_sentinel_to_task`` → ``apply_staged_decision``) so the
    task advances to the next stage, then marks the session COMPLETED/NORMAL —
    mirroring the alive-session path in ``idle.py``. ``_apply_sentinel_to_task``
    acquires its own ``dev_queue_lock``; session state is flushed by the caller's
    ``save_state``. Appends each routed session to ``phantom_names`` so the caller
    stops the surface and emits its completion event.

    GitHub #1019: when ``_apply_sentinel_to_task`` reports ``routed=False`` (a
    stage-mismatch refusal, the #986 incident), the session must NOT be
    completed or torn down — the task row was left untouched, so orphaning the
    session here would strand a live/reapable surface with no owning task.
    Returns only the candidates that were actually routed, so the caller's
    ``_emit_phantom_routed_events`` (SESSION_COMPLETED) fires solely for those.
    """
    accepted: list[ReapCandidate] = []
    for candidate in routed_candidates:
        if candidate.routed_sentinel is None or candidate.salvage_csid is None:
            continue  # Invariant: ROUTE_EMITTED_SENTINEL has routed_sentinel + csid
        routed = True
        if candidate.ticket_id:
            outcome = _apply_sentinel_to_task(
                candidate.ticket_id, candidate.session_id, candidate.routed_sentinel
            )
            routed = outcome.routed
        if not routed:
            # #1149: mirror idle.py's refusal stamp — a stage-mismatch refusal
            # (earlier-stage replay / unresolvable position) leaves the task
            # untouched. Stamp a marker so the detect-phase skip check in
            # _detect_phantom_candidates stops re-offering this same doomed
            # candidate to _phantom_advance_sentinel_candidate.
            #
            # Unlike idle.py (whose detect phase only builds a candidate when
            # last_result is already None, so an unconditional overwrite can
            # never clobber anything), phantom.py's detect phase has no such
            # precondition -- a session already legitimately parked by another
            # sweep (idle.py's _SILENTLY_IDLE_REASON, salvage.py's
            # _NEEDS_SALVAGE_REASON) can reach here with last_result already
            # set. Overwriting it wholesale would destroy that marker and
            # defeat stalled.py's SKIP_PARKED check, which reads
            # last_result.get("paused_status") for exactly those two reasons --
            # silently un-parking a session another sweep correctly parked.
            # But merely skipping the stamp in that case (rather than merging
            # it in) would re-open the very refusal-loop this stamp exists to
            # close for that overlap: already_refused would never become True,
            # so the doomed candidate re-offers forever. So: start from a
            # pre-existing dict and merge the refusal flag in under its own
            # key (never touching the caller's own paused_status value);
            # only a None last_result gets the original single-key stamp.
            existing = session_by_id[candidate.session_id].last_result
            if isinstance(existing, dict):
                session_by_id[candidate.session_id].last_result = {
                    **existing,
                    _SENTINEL_ADVANCE_REFUSED_KEY: True,
                }
            else:
                session_by_id[candidate.session_id].last_result = {
                    _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
                }
            continue
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.reap_reason = ReapReason.PHANTOM_SURFACE
        session.last_result = candidate.routed_sentinel.model_dump(mode="json")
        session.claude_session_id = candidate.salvage_csid
        phantom_names.append(session.name)
        accepted.append(candidate)
    return accepted


def _emit_phantom_routed_events(
    session_by_id: dict[str, Session],
    routed_candidates: list[ReapCandidate],
) -> None:
    """Emit SESSION_COMPLETED + stop surface for routed advance sentinels (#716).

    Mirrors ``idle._emit_idle_completion_events``' routed-sentinel loop: a
    salvaged (constructive) completion, not a crash. Runs after ``save_state``.
    """
    for candidate in routed_candidates:
        if candidate.routed_sentinel is None:
            continue
        session = session_by_id[candidate.session_id]
        # Why: phantom's stop-before-emit order is a deliberate inversion of
        # idle/stalled's emit-then-stop order (see
        # cw.reconcile.dispositions.emit_routed_sentinel_completion, #1306) —
        # a phantom's surface is already dead (absent from the daemon
        # roster), so there is no live surface for a late emit to race.
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
                "salvaged": True,
                "status": candidate.routed_sentinel.status,
            },
            correlation_id=candidate.ticket_id,
        )


def _emit_sentinel_mismatch_veto_escalation_events(
    session_by_id: dict[str, Session],
    escalate_candidates: list[ReapCandidate],
) -> None:
    """Emit SESSION_NEEDS_ATTENTION + push for cap-exhausted sentinel-mismatch
    vetoes (#1449).

    Reuses phantom.py's own gh-blocked SESSION_NEEDS_ATTENTION payload shape (see
    _emit_phantom_terminal_events) with ``paused_status``
    =_SENTINEL_MISMATCH_VETO_CAP_EXHAUSTED_REASON plus ``stale_minutes`` /
    ``new_veto_count`` — the task already routed silently to BLOCKED_ON_USER via
    :func:`_route_phantom_by_policy`'s SIGNAL_ONLY mutation, so only the operator
    notification is added here (parity with the retry-cap park's needs_attention
    emission; no daemon-stop / worktree removal). Edge-triggered: the act phase
    already persisted each candidate's post-cap counter bump before this runs, so
    a still-LIVE session that already escalated will not produce a new escalate
    candidate on a later tick — see ``_sentinel_mismatch_veto_candidate``.
    """
    for candidate in escalate_candidates:
        session = session_by_id[candidate.session_id]
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": candidate.ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _SENTINEL_MISMATCH_VETO_CAP_EXHAUSTED_REASON,
                "breadcrumbs": str(candidate.worktree_path)
                if candidate.worktree_path
                else "",
                "crashed": False,
                "lane": candidate.lane,
                "stale_minutes": candidate.stale_minutes,
                "new_veto_count": candidate.new_veto_count,
            },
            correlation_id=candidate.ticket_id,
        )
        _deps.fire_push_notification(session.name, session.client)


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

    # Merged-complete: PR already shipped; mark COMPLETED + NORMAL, not CRASHED.
    # Still appended to phantom_names — these sessions ARE phantom (absent from
    # daemon roster), and callers need their names for queue cleanup below.
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

    # GH-blocked phantoms: can't verify PR; mark COMPLETED+CRASHED so they
    # leave _LIVE_STATUSES (via status=COMPLETED) and aren't re-detected.
    for candidate in gh_blocked_crash_candidates:
        session = session_by_id[candidate.session_id]
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        session.reap_reason = ReapReason.PHANTOM_SURFACE
        phantom_names.append(session.name)

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
