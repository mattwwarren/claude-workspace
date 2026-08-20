"""Act-phase lifecycle-event emission for the phantom sweep.

Extracted verbatim from the historical flat ``cw.reconcile.phantom`` module
by the package split. Runs after the caller's ``save_state`` flush. See
GitHub #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus, SessionOrigin
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _GH_CHECK_BLOCKED_REASON,
    _PHANTOM_REAP_MERGED_REASON,
)

if TYPE_CHECKING:
    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate

# paused_status written to SESSION_NEEDS_ATTENTION when the phantom sweep's
# sentinel-stage-mismatch veto cap is exhausted on a still-LIVE already_refused
# session and the pending CRASH_COMPLETE proceeds (#1449). Defined locally (not
# in _shared.py, which is outside this ticket's file set) — mirrors the
# retry-cap park's own escalation reason. See docs/events.md.
_SENTINEL_MISMATCH_VETO_CAP_EXHAUSTED_REASON = "sentinel_mismatch_veto_cap_exhausted"


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
                    "provider_overload_detected": candidate.provider_overload_detected,
                },
                correlation_id=candidate.ticket_id,
            )
    return dirty_ticket_ids


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
