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
    _PHANTOM_REAP_MERGED_REASON,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    ProposedAction,
    ReapCandidate,
    _apply_queue_mutations,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    _queue_status_for_salvaged,
    resolve_reap_policy,
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


def _detect_phantom_candidates(
    state: CwState,
    phantom_set: set[str],
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[ReapCandidate]:
    """Pure classification phase for phantom sessions.

    Returns a list of ReapCandidate objects. Makes zero writes.
    The worktree_dirty check for DAEMON sessions is performed here
    so the act phase does not need to repeat it. See GitHub #552, ADR-0006.

    task_by_ticket is used to stamp candidate.lane from the owning task's lane
    (GitHub #560). When None or the ticket has no task, lane defaults to DEFAULT_LANE.
    """
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
        already_refused = (
            isinstance(session.last_result, dict)
            and session.last_result.get("paused_status")
            == _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        )
        if session.origin is SessionOrigin.DAEMON and not already_refused:
            advance = _phantom_advance_sentinel_candidate(session, ticket_id, lane)
            if advance is not None:
                candidates.append(advance)
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
            _shared.detect_usage_limit(session)
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
        _apply_salvaged_completion(
            session, candidate.salvage_result, candidate.salvage_csid, now=now
        )
        phantom_names.append(session.name)
        if candidate.ticket_id:
            salvaged_ticket_ids.append(candidate.ticket_id)
            salvaged_result_by_ticket[candidate.ticket_id] = candidate.salvage_result
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
) -> list[ReapCandidate]:
    """Apply per-lane reap-policy routing to clean CRASH_COMPLETE candidates.

    Under ``ReapPolicy.SIGNAL_ONLY`` a clean (non-dirty) CRASH_COMPLETE candidate
    is routed to BLOCKED_ON_USER instead of passing through. Dirty phantoms and
    merged / gh-blocked tickets always pass through (#637). SALVAGE_COMPLETION
    candidates are unaffected. Returns the surviving "auto" candidates.
    """
    effective_config = config if config is not None else OrchestratorConfig()
    clients = _deps.load_effective_clients()
    # Route each clean CRASH_COMPLETE candidate individually based on its lane's policy.
    # Merged-PR / gh-blocked check (GitHub #637) runs BEFORE policy routing so
    # that a confirmed-merged ticket is always completed, even under SIGNAL_ONLY.
    # Dirty phantoms always go to BLOCKED_ON_USER regardless of policy.
    signal_mutations: dict[str, QueueItemStatus] = {}
    auto_candidates: list[ReapCandidate] = []
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
    return auto_candidates


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
            # untouched. Stamp a paused_status-only marker so the detect-phase
            # skip check in _detect_phantom_candidates stops re-offering this
            # same doomed candidate to _phantom_advance_sentinel_candidate.
            session_by_id[candidate.session_id].last_result = {
                "paused_status": _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
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
    candidates = _route_phantom_by_policy(
        candidates,
        config=config,
        merged_ticket_ids=merged_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
    )
    if not candidates:
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

    return (
        ticket_ids_to_revert,
        phantom_names,
        usage_limited,
        salvaged_ticket_ids,
        salvaged_result_by_ticket,
        merged_completed_ids,
    )
