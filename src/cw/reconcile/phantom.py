"""Phantom-session detection and act phases for reconcile.

A phantom session is ACTIVE/IDLE in cw state but absent from the daemon
roster (its surface is dead). See GitHub #552, ADR-0006. Also hosts
``_emit_reap_proposed`` (the propose-before-act hook for all clusters).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.config import save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
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
    ProposedAction,
    ReapCandidate,
    _apply_queue_mutations,
    _apply_salvaged_completion,
    _locate_session_transcript,
    _queue_status_for_salvaged,
    resolve_reap_policy,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from cw.auto_dev_result import AutoDevResult
    from cw.models import CwState, TicketTask


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
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.CRASH_COMPLETE,
                ticket_id=ticket_id,
                worktree_dirty=worktree_dirty,
                lane=lane,
                client=session.client,
                worktree_path=session.worktree_path,
            )
        )
    return candidates


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
        _apply_queue_mutations(signal_mutations, clear_session_id=set())
    candidates = auto_candidates
    if not candidates:
        return [], [], False, [], {}, []

    session_by_id = {s.id: s for s in state.sessions}

    all_crash_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.CRASH_COMPLETE
    ]
    salvage_candidates = [
        c for c in candidates if c.proposed_action == ProposedAction.SALVAGE_COMPLETION
    ]

    # Split CRASH_COMPLETE candidates by world-state check results (GitHub #637).
    # merged_ticket_ids / gh_blocked_ticket_ids come from a pre-pass in
    # reconcile() that runs BEFORE sessions_lock, so no gh subprocess executes
    # here. Candidates with no ticket_id fall through to the normal crash path.
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

    phantom_names: list[str] = []
    # ticket_ids to revert (only PENDING-routed, excludes dirty/BLOCKED_ON_USER)
    ticket_ids_to_revert: list[str] = []
    merged_completed_ids: list[str] = []
    salvaged_ticket_ids: list[str] = []
    salvaged_result_by_ticket: dict[str, AutoDevResult] = {}
    pending_events: list[dict[str, object]] = []

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

    save_state(state)

    for payload in pending_events:
        record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

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

    # Queue mutations.
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
    if revert_set or merged_crash_tids or gh_blocked_crash_tids or salvaged_set:
        with dev_queue_lock():
            store = load_dev_queue()
            changed = False
            for task in store.tasks:
                if task.status != QueueItemStatus.RUNNING:
                    continue
                if task.ticket_id in revert_set:
                    if task.ticket_id in dirty_ticket_ids:
                        task.status = QueueItemStatus.BLOCKED_ON_USER
                    else:
                        task.status = QueueItemStatus.PENDING
                        ticket_ids_to_revert.append(task.ticket_id)
                    task.session_id = None
                    changed = True
                elif task.ticket_id in merged_crash_tids:
                    task.status = QueueItemStatus.COMPLETED
                    task.session_id = None
                    merged_completed_ids.append(task.ticket_id)
                    changed = True
                elif task.ticket_id in gh_blocked_crash_tids:
                    task.status = QueueItemStatus.BLOCKED_ON_USER
                    task.session_id = None
                    changed = True
                elif task.ticket_id in salvaged_set:
                    salvaged_result = salvaged_result_by_ticket[task.ticket_id]
                    task.status = _queue_status_for_salvaged(salvaged_result)
                    changed = True
            if changed:
                save_dev_queue(store)

    return (
        ticket_ids_to_revert,
        phantom_names,
        False,
        salvaged_ticket_ids,
        salvaged_result_by_ticket,
        merged_completed_ids,
    )


_REAP_PROPOSED_ACTIONS: frozenset[ProposedAction] = frozenset(
    {
        ProposedAction.REVERT_TASK,
        ProposedAction.CRASH_COMPLETE,
        ProposedAction.PARK_BLOCKED_ON_USER,
    }
)


def _emit_reap_proposed(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    native_live: set[str],
    now: datetime | None = None,
) -> None:
    """Emit SESSION_REAP_PROPOSED for reap-shaped candidates before act phase.

    Called from _reconcile_locked after each _detect_* and before the
    corresponding _act_on_*. Satisfies ADR-0006 invariant 3 (propose before act).

    Only emits for REVERT_TASK, CRASH_COMPLETE, PARK_BLOCKED_ON_USER candidates.
    Dedup: sessions with reap_proposed_at already set are skipped.

    save_state is safe under sessions_lock — it is a raw file write, not a
    reentrant lock acquisition. See existing _act_on_stalled_candidates,
    _act_on_idle_candidates.
    """
    _now = now or datetime.now(UTC)
    session_by_id = {s.id: s for s in state.sessions}
    any_stamped = False

    for candidate in candidates:
        if candidate.proposed_action not in _REAP_PROPOSED_ACTIONS:
            continue
        session = session_by_id.get(candidate.session_id)
        if session is None or session.reap_proposed_at is not None:
            continue

        # Compute in_roster
        in_roster = (
            session.surface_ref is not None and session.surface_ref in native_live
        )

        # Compute transcript_age_seconds (best-effort, nullable)
        transcript_age_seconds: float | None = None
        transcript_path = _locate_session_transcript(session)
        if transcript_path is not None and transcript_path.exists():
            with contextlib.suppress(OSError):
                mtime = transcript_path.stat().st_mtime
                transcript_age_seconds = _now.timestamp() - mtime

        payload = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "lane": candidate.lane,
            "proposed_action": candidate.proposed_action.value,
            "reason": candidate.reap_reason.value if candidate.reap_reason else None,
            "evidence": {
                "elapsed_seconds": candidate.elapsed_seconds,
                "in_roster": in_roster,
                "transcript_age_seconds": transcript_age_seconds,
            },
        }
        # Stamp before record_event: dedup guard fires on retry if write fails.
        session.reap_proposed_at = _now
        any_stamped = True
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            payload,
            correlation_id=candidate.ticket_id or candidate.session_id,
        )

    if any_stamped:
        save_state(state)
