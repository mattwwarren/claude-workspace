"""Act-phase lifecycle-event emission for the silently-idle sweep.

Extracted verbatim from the historical flat ``cw.reconcile.idle`` module by
the package split. Runs after the caller's ``save_state`` flush. See GitHub
#105, #121, #545, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType, ReapReason
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _CAUSE_IDLE_STALL,
    _CAUSE_USAGE_LIMIT,
    _EXTERNAL_COUNTERPARTY_IDLE_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _PHANTOM_REAP_MERGED_REASON,
    _SILENTLY_IDLE_REASON,
    _cleanup_timed_out_worktree,
)
from cw.reconcile.dispositions import (
    build_salvage_completion_payload,
    emit_routed_sentinel_completion,
)

if TYPE_CHECKING:
    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate


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
                "lane": candidate.lane,
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
                "lane": candidate.lane,
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
                "lane": candidate.lane,
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
