"""Act-phase event emission and surface teardown for the stalled-headless sweep.

Extracted verbatim from the historical flat ``cw.reconcile.stalled`` module by
the #1484 package split. These helpers emit lifecycle events, stop daemon
surfaces, and clean up worktrees for the dispositions the detect phase
classified. See GitHub #185, #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import (
    OrchestratorEventType,
    ReapReason,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _FINALIZE_BLOCKED_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _PHANTOM_REAP_MERGED_REASON,
    _SALVAGE_SKIP_ESCALATED_REASON,
    _SALVAGE_SKIP_REASON,
    _STALLED_CAP_PARKED_REASON,
    _cleanup_timed_out_worktree,
)
from cw.reconcile.dispositions import (
    build_salvage_completion_payload,
    emit_routed_sentinel_completion,
)

if TYPE_CHECKING:
    from cw.models import OrchestratorConfig, Session
    from cw.reconcile._shared import ReapCandidate


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
                "lane": candidate.lane,
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
        emit_routed_sentinel_completion(
            session,
            ticket_id=candidate.ticket_id,
            status=candidate.routed_sentinel.status,
        )


def _emit_stalled_foreign_result_events(
    session_by_id: dict[str, Session],
    foreign_result_candidates: list[ReapCandidate],
) -> None:
    """Emit salvaged SESSION_COMPLETED + stop surface for foreign-result completions.

    #1470. Mirrors ``_emit_stalled_routed_events`` and stalled.py's own salvage
    loop exactly. Extracted so ``_emit_stalled_events`` stays under the branch cap.
    """
    for candidate in foreign_result_candidates:
        if candidate.routed_sentinel is None:
            continue  # Invariant: COMPLETE_FOREIGN_RESULT always has routed_sentinel
        session = session_by_id[candidate.session_id]
        completed_payload = build_salvage_completion_payload(
            session,
            ticket_id=candidate.ticket_id,
            status=candidate.routed_sentinel.status,
        )
        record_event(OrchestratorEventType.SESSION_COMPLETED, completed_payload)
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
    wall_clock_veto_escalation_candidates: list[ReapCandidate],
    foreign_result_candidates: list[ReapCandidate],
    *,
    branch_absent_ticket_ids: frozenset[str] = frozenset(),
) -> None:
    """Emit lifecycle events and stop/cleanup surfaces for stalled dispositions.

    Mirrors the post-queue side effects of the original act phase: SESSION_TIMED_OUT
    + worktree cleanup for reverts, SESSION_COMPLETED for merged/salvage, and
    SESSION_NEEDS_ATTENTION for gh-blocked and finalize-blocked candidates.
    ROUTE_EMITTED_SENTINEL candidates (Path 1 backstop, #1149) emit a salvaged
    SESSION_COMPLETED and stop the surface, mirroring the salvage loop.
    ``wall_clock_veto_escalation_candidates`` (#1445) are SIGNAL_ONLY-rerouted
    REVERT_TASK candidates whose liveness veto was cap-exhausted: they emit an
    immediate SESSION_NEEDS_ATTENTION + push notification (parity with the
    retry-cap park) but — preserving SIGNAL_ONLY's non-destructive contract —
    do NOT stop the daemon or remove the worktree; the task already routed
    silently to BLOCKED_ON_USER via _route_stalled_by_policy's mutation.
    ``foreign_result_candidates`` (#1470) emit a salvaged SESSION_COMPLETED and
    stop the surface, mirroring the salvage_candidates loop exactly.
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
                "lane": candidate.lane,
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
            _build_needs_attention_park_payload(
                session, candidate, paused_status=_STALLED_CAP_PARKED_REASON
            ),
            correlation_id=candidate.ticket_id,
        )
        _deps.fire_push_notification(session.name, session.client)

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

    _emit_wall_clock_veto_escalation_events(
        session_by_id, wall_clock_veto_escalation_candidates
    )
    _emit_stalled_routed_events(session_by_id, routed_sentinel_candidates)
    _emit_finalize_blocked_events(session_by_id, finalize_blocked_candidates)
    _emit_stalled_foreign_result_events(session_by_id, foreign_result_candidates)


def _build_needs_attention_park_payload(
    session: Session,
    candidate: ReapCandidate,
    *,
    paused_status: str,
) -> dict[str, object]:
    """Shared SESSION_NEEDS_ATTENTION payload for the two "parity" park-notice
    sites (#1445): the retry-cap park_candidates loop and the wall-clock
    veto-escalation loop below. Extracted so the two sites cannot silently
    drift apart on payload shape — the exact failure mode this ticket is about.

    #1625: the stalled_retry_cap_parked disposition additionally carries
    crashed/regress_attempts/spawn_error_count so a consumer does not have to
    cross-reference the task record by hand. Scoped strictly to that
    disposition (identified by ``paused_status``) so the wall-clock
    veto-escalation call site — which shares this payload builder but is not a
    retry-cap park — does not pick up fields that don't apply to it.
    """
    payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "client": session.client,
        "ticket_id": candidate.ticket_id,
        "claude_session_id": session.claude_session_id,
        "paused_status": paused_status,
        "breadcrumbs": str(session.worktree_path) if session.worktree_path else "",
        "crashed": False,
        "stage": str(candidate.stage),
        "attempts": candidate.attempts,
        "lane": candidate.lane,
    }
    if paused_status == _STALLED_CAP_PARKED_REASON:
        payload["regress_attempts"] = candidate.regress_attempts
        payload["spawn_error_count"] = candidate.spawn_error_count
    return payload


def _emit_wall_clock_veto_escalation_events(
    session_by_id: dict[str, Session],
    candidates: list[ReapCandidate],
) -> None:
    """Emit SESSION_NEEDS_ATTENTION + push for cap-exhausted wall-clock vetoes (#1445).

    Reuses the park_candidates payload shape
    (:func:`_build_needs_attention_park_payload`) but with
    ``paused_status=WALL_CLOCK_BUDGET`` and WITHOUT ``daemon.stop()`` /
    worktree cleanup — the task already routed silently to BLOCKED_ON_USER via
    :func:`_route_stalled_by_policy`'s SIGNAL_ONLY mutation; only the operator
    notification is added here (parity with the retry-cap park's own
    needs_attention emission). Extracted so ``_emit_stalled_events`` stays under
    the branch cap, mirroring ``_emit_finalize_blocked_events``. Edge-triggered:
    the act phase already persisted each candidate's post-cap counter bump
    before this runs (see ``_act_on_stalled_candidates``), so a still-LIVE
    session that already escalated will not produce a new candidate here on a
    later tick — see ``_liveness_veto_candidate``'s docstring.
    """
    for candidate in candidates:
        session = session_by_id[candidate.session_id]
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            _build_needs_attention_park_payload(
                session, candidate, paused_status=ReapReason.WALL_CLOCK_BUDGET.value
            ),
            correlation_id=candidate.ticket_id,
        )
        _deps.fire_push_notification(session.name, session.client)


def _record_salvage_skip(
    session_by_id: dict[str, Session],
    candidate: ReapCandidate,
    *,
    config: OrchestratorConfig,
) -> None:
    """Increment a session's salvage-skip latch and emit its events (#974, #1283).

    Emits SESSION_SALVAGE_SKIPPED only on the 0->1 transition of
    ``consecutive_salvage_skips`` (the first skip of a fresh parked episode),
    edge-triggered per #1283 to stop the every-tick event storm -- matching this
    codebase's established edge-trigger convention (``_emit_reap_proposed``'s
    ``reap_proposed_at`` dedup and idle.py's ``already_parked_ids`` suppression,
    both #782). Additionally emits session.needs_attention exactly once, when the
    incremented count hits config.salvage_skip_attention_threshold (latch: no
    re-fire while still at the threshold on subsequent ticks, since detect only
    re-appends a SKIP_PARKED candidate — the count keeps climbing past the
    threshold on later ticks, but the emit below only fires on exact equality).
    """
    session = session_by_id[candidate.session_id]
    was_first = session.consecutive_salvage_skips == 0
    session.consecutive_salvage_skips += 1
    if was_first:
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
                "lane": candidate.lane,
            },
            correlation_id=candidate.ticket_id,
        )
