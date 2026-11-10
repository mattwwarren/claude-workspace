"""Recipe 2: park-marker-poison clear.

A row behind a session repeatedly skipped by the salvage watchdog's
park-marker check (``consecutive_salvage_skips >= 1``) whose session is
confirmed dead is closed and requeued. See the package docstring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.dev_queue import (
    _extract_pr_url,
    _hold_aware_disposition,
    _result_blocker_reason,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.models import (
    CompletionReason,
    LivenessBucket,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _apply_salvaged_completion,
    _foreign_result_target_queue_status,
    _queue_status_for_salvaged,
    _transcript_age_seconds,
    _validate_existing_result_for_routing,
    salvage_terminal_result,
)
from cw.reconcile.concierge._common import (
    RECIPE_PARK_MARKER_POISON_CLEAR,
    ConciergeCandidate,
    _find_session_for_ticket,
    _has_park_marker,
    _is_session_dead,
    _refused_at_attempt_ceiling,
)
from cw.reconcile.liveness import _classify_liveness_bucket

if TYPE_CHECKING:
    from datetime import datetime

    from cw.auto_dev_result import AutoDevResult, BlockedResult
    from cw.models import (
        CwState,
        OrchestratorConfig,
        Session,
        TicketTask,
    )
    from cw.reconcile._shared import ScopeProbes
    from cw.result import EmitOutcome

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recipe 2: park-marker-poison clear
# ---------------------------------------------------------------------------
# _PARK_MARKER_REASONS / _has_park_marker live in ``_common``, shared with
# recipe 1's exclusion check that references them.


def _park_marker_transcript_stale_45m(
    session: Session, task: TicketTask, *, now: datetime, config: OrchestratorConfig
) -> bool:
    """True when the session's transcript classifies as STALE_45M (or is
    unlocatable — fail-toward-dead, mirroring the recipe 1 evidence gate).
    """
    age_seconds = _transcript_age_seconds(session, now)
    if age_seconds is None:
        return True
    stale_minutes = age_seconds / 60.0
    bucket = _classify_liveness_bucket(stale_minutes, stage=task.stage, config=config)
    return bucket == LivenessBucket.STALE_45M


def _detect_park_marker_poison_candidates(
    state: CwState,
    tasks: list[TicketTask],
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    roster_captured_at: datetime | None = None,
) -> list[ConciergeCandidate]:
    """Pure classification phase for recipe 2. Makes zero writes."""
    candidates: list[ConciergeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        session = _find_session_for_ticket(state, task.client, task.ticket_id)
        if session is None:
            continue
        if not _has_park_marker(session):
            continue
        if session.consecutive_salvage_skips < 1:
            continue
        if not _is_session_dead(
            session, native_live, roster_captured_at=roster_captured_at
        ):
            continue
        if not _park_marker_transcript_stale_45m(session, task, now=now, config=config):
            continue
        candidates.append(
            ConciergeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                recipe=RECIPE_PARK_MARKER_POISON_CLEAR,
                evidence={
                    "paused_status": session.last_result.get("paused_status")
                    if isinstance(session.last_result, dict)
                    else None,
                    "consecutive_salvage_skips": session.consecutive_salvage_skips,
                    "session_id": session.id,
                },
                session_id=session.id,
                # GitHub #1750/#1751: mirrors the dispatch claim path's
                # ceiling — both must read the same counter, and (since the
                # ceiling became lane-scoped) the same resolved number, or the
                # concierge would refuse a requeue the claim path would have
                # allowed.
                refused_ceiling=_refused_at_attempt_ceiling(task, config),
            )
        )
    return candidates


def _close_confirmed_dead_session(
    session_id: str,
    now: datetime,
    *,
    scope_probes: ScopeProbes | None = None,
    warned_blocks: set[str] | None = None,
) -> tuple[bool, AutoDevResult | None, EmitOutcome | None]:
    """Flip a confirmed-dead session to COMPLETED.

    Returns ``(changed, salvage_result, refusal)``.

    GitHub #1353: attempts terminal-sentinel salvage before stamping CRASHED —
    mirrors idle.py/stalled.py/phantom.py's own pre-close salvage check
    (_shared.salvage_terminal_result). salvage_result is the recovered
    AutoDevResult when the session's transcript carried a valid terminal
    sentinel (the caller routes the owning task to completion/blocked-on-user
    instead of PENDING); None means the session is genuinely unrecoverable and
    the pre-existing CRASHED stamp applies unchanged. *scope_probes* feeds the
    salvage's prefetched scope correction (#2641).

    RFC 0012 A3 (#1459): the pre-close salvage now routes through the door
    (``_apply_salvaged_completion`` -> ``emit_result_on``). On a first-writer-
    wins refusal (another authority already recorded a terminal result for this
    session) the session is left COMPLETELY untouched -- ``changed`` stays False,
    ``salvage_result`` stays None, and the door's refusal ``EmitOutcome`` is
    returned as ``refusal`` so the caller can route the task off the existing
    (foreign) result instead of blindly requeuing to PENDING (Adopted
    Assumption 1).

    Fresh load_state()/save_state() pair — safe under the sessions_lock the
    reconcile-tick caller already holds (mirrors revert_timed_out_tasks's own
    load_state()/save_state() pattern rather than re-acquiring the lock).
    """
    state = load_state()
    changed = False
    salvage_result: AutoDevResult | None = None
    refusal: EmitOutcome | None = None
    for session in state.sessions:
        if session.id != session_id:
            continue
        if session.status in _LIVE_STATUSES:
            salvage = (
                salvage_terminal_result(
                    session, scope_probes=scope_probes, warned_blocks=warned_blocks
                )
                if session.origin is SessionOrigin.DAEMON
                else None
            )
            if salvage is not None:
                result, claude_session_id = salvage
                outcome = _apply_salvaged_completion(
                    session, result, claude_session_id, now=now
                )
                if outcome.refused:
                    # Door declined: leave the session byte-identical (no
                    # changed/save), hand the refusal back for task routing.
                    refusal = outcome
                else:
                    salvage_result = result
                    changed = True
            else:
                session.status = SessionStatus.COMPLETED
                session.completed_at = now
                session.completed_reason = CompletionReason.CRASHED
                changed = True
        break
    if changed:
        save_state(state)
    return changed, salvage_result, refusal


def _route_park_marker_poison_task(
    task: TicketTask,
    salvage_result: AutoDevResult | None,
    refusal: EmitOutcome | None,
) -> None:
    """Route a poison-clear TASK to its terminal/requeue status (RFC 0012 A3).

    Extracted from ``_act_on_park_marker_poison_candidates``' loop so that
    function stays under the branch cap. Three arms:

    1. ``salvage_result`` present -> the door accepted a fresh salvage; route
       via ``_queue_status_for_salvaged`` (session_id / stage_base_ref left
       intact per the #918 rescue contract).
    2. door refused with a validating ``existing_result`` -> route off that
       foreign terminal result, special-casing ``status=="blocked"`` (both
       shapes) to BLOCKED_ON_USER so a blocked result is not mis-completed.
    3. otherwise (no salvage, unroutable/absent refusal) -> requeue PENDING and
       clear session_id / stage_base_ref (the pre-existing floor).
    """
    if salvage_result is not None:
        last_result = salvage_result.model_dump(mode="json")
        blocker_reason = _result_blocker_reason(salvage_result)
        transition_task_status(
            task,
            _queue_status_for_salvaged(salvage_result),
            disposition=_hold_aware_disposition(salvage_result.status, blocker_reason),
            pr_url=_extract_pr_url(last_result),
        )
        return

    validated_refusal: AutoDevResult | BlockedResult | None = (
        _validate_existing_result_for_routing(refusal.existing_result)
        if refusal is not None
        else None
    )
    if validated_refusal is not None:
        _log.warning(
            "concierge park_marker_poison_clear: routing door-refused "
            "existing result ticket=%s existing_source=%s",
            task.ticket_id,
            refusal.existing_source if refusal is not None else None,
        )
        dumped = validated_refusal.model_dump(mode="json")
        # Ordering (isinstance(BlockedResult) before the delegated classifier
        # call) is binding for mypy --strict narrowing -- see
        # _foreign_result_target_queue_status's docstring (#1566).
        target_status = _foreign_result_target_queue_status(validated_refusal)
        refusal_reason = _result_blocker_reason(validated_refusal)
        transition_task_status(
            task,
            target_status,
            disposition=_hold_aware_disposition(
                validated_refusal.status, refusal_reason
            ),
            pr_url=_extract_pr_url(dumped),
        )
        return

    if refusal is not None:
        _log.warning(
            "concierge park_marker_poison_clear: door-refused result "
            "unroutable ticket=%s existing_source=%s existing_shape=%r",
            task.ticket_id,
            refusal.existing_source,
            refusal.existing_result,
        )
    transition_task_status(task, QueueItemStatus.PENDING)
    task.session_id = None
    task.stage_base_ref = None


def _act_on_park_marker_poison_candidates(
    candidates: list[ConciergeCandidate],
    *,
    now: datetime,
    scope_probes: ScopeProbes | None = None,
    warned_blocks: set[str] | None = None,
) -> list[str]:
    """Act phase for recipe 2: emit, close the dead session, then requeue.

    ADR-0006: this is the recipe Q1 singled out as ADR-0006-adjacent ("poison-
    loop recovery runs spawn close = daemon stop"), so the reasoning is spelled
    out here rather than left to the module docstring alone. It stays
    non-destructive because ``_close_confirmed_dead_session`` only flips the
    status of a session the detect phase already proved dead (roster-absent +
    consecutive_salvage_skips >= 1 + transcript stale 45m+) to COMPLETED/
    CRASHED — it is bookkeeping on an already-gone session, not a live
    ``spawn close`` that stops a running daemon. No worktree is touched.
    """
    if not candidates:
        return []
    by_ticket = {c.ticket_id: c for c in candidates}
    recovered: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            candidate = by_ticket.get(task.ticket_id)
            if candidate is None:
                continue
            if task.status != QueueItemStatus.BLOCKED_ON_USER:
                continue
            if candidate.refused_ceiling:
                continue
            record_event(
                OrchestratorEventType.CONCIERGE_RECOVERED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "recipe": RECIPE_PARK_MARKER_POISON_CLEAR,
                    "evidence": candidate.evidence,
                },
                correlation_id=task.ticket_id,
            )
            salvage_result: AutoDevResult | None = None
            refusal: EmitOutcome | None = None
            if candidate.session_id is not None:
                _, salvage_result, refusal = _close_confirmed_dead_session(
                    candidate.session_id,
                    now,
                    scope_probes=scope_probes,
                    warned_blocks=warned_blocks,
                )
            # RFC 0012 A3 (#1459): route via the shared arm-picker -- a fresh
            # salvage, a door-refused-but-routable foreign result, or the
            # PENDING-requeue floor. Extracted so this loop stays under the
            # branch cap. session_id / stage_base_ref are cleared only on the
            # PENDING arm inside the helper.
            _route_park_marker_poison_task(task, salvage_result, refusal)
            recovered.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return recovered
