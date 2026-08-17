"""Act-phase session-state and dev-queue mutations for the phantom sweep.

Extracted verbatim from the historical flat ``cw.reconcile.phantom`` module
by the package split. ``save_state`` itself is left to the caller in
``core``. See GitHub #552, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.dev_queue import (
    _extract_pr_url,
    _hold_aware_disposition,
    _result_blocker_reason,
    _stamp_salvage_stage,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.models import (
    CompletionReason,
    QueueItemStatus,
    ReapReason,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile._shared import (
    _DIRTY_WORKTREE_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _PAUSED_STATUS_KEY,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _UNRESOLVED_SUBAGENT_SPAWN_REASON,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _queue_status_for_salvaged,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.auto_dev_result import AutoDevResult
    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate


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
    unresolved_spawn_ticket_ids: set[str] | None = None,
) -> None:
    """Apply dev-queue status changes for phantom dispositions.

    Mutates ``ticket_ids_to_revert`` and ``merged_completed_ids`` in place to
    surface the PENDING-reverted and merged-completed ticket IDs to the caller.
    Acquires ``dev_queue_lock``; writes only when at least one task changed.

    ``unresolved_spawn_ticket_ids`` (#1646) overrides the dirty-worktree
    disposition for rows whose worker died with a sub-agent spawn in flight.
    Both facts are true of such a row, but only one disposition slot exists;
    the unresolved spawn wins because it is the more specific and more
    actionable of the two — it names *why* the verification tail never ran,
    where "dirty" only reports that something is uncommitted.
    """
    # Deferred, not module-top: cw.dispatch's package __init__ imports
    # cw.reconcile (loop.py/gating.py/lanes.py), so a top-level import of any
    # cw.dispatch submodule here is a real circular import at package-init
    # time. Same shape as the #698 reconcile._shared -> cw.dispatch precedent
    # and tasks.py's deferred cw.dispatch.routing import. See #1750.
    from cw.dispatch.productivity import extract_claim_evidence, is_unproductive

    unresolved_spawn_ids = unresolved_spawn_ticket_ids or set()
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
                        disposition=(
                            _UNRESOLVED_SUBAGENT_SPAWN_REASON
                            if task.ticket_id in unresolved_spawn_ids
                            else _DIRTY_WORKTREE_REASON
                        ),
                    )
                else:
                    transition_task_status(task, QueueItemStatus.PENDING)
                    ticket_ids_to_revert.append(task.ticket_id)
                task.session_id = None
                changed = True
            elif task.ticket_id in merged_crash_tids:
                # Why: PR URL is not in hand here — not worth a second gh call.
                # unproductive=False hardcoded (#1750): this branch fires only
                # when the crashed session's PR is confirmed MERGED, which is
                # definitionally the most productive outcome there is. It is
                # deliberately not evidence-derived: there is no sentinel to
                # extract evidence from here (that is why the comment above
                # says the PR URL is not in hand), so the classifier would see
                # an empty payload and wrongly charge a terminal salvage.
                transition_task_status(
                    task,
                    QueueItemStatus.COMPLETED,
                    disposition="shipped",
                    unproductive=False,
                )
                _stamp_salvage_stage(task)
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
                reason = _result_blocker_reason(salvaged_result)
                transition_task_status(
                    task,
                    _queue_status_for_salvaged(salvaged_result),
                    disposition=_hold_aware_disposition(salvaged_result.status, reason),
                    pr_url=_extract_pr_url(last_result),
                    # #1750: unlike the merged-crash branch above, a genuine
                    # validated sentinel IS in hand here, so classify it rather
                    # than assuming. Reuses the `last_result` dump computed
                    # above — the same schema-owned extractor routing.py uses.
                    unproductive=is_unproductive(extract_claim_evidence(last_result)),
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
        session = session_by_id[candidate.session_id]
        routed = True
        if candidate.ticket_id:
            outcome = _apply_sentinel_to_task(
                candidate.ticket_id, session, candidate.routed_sentinel, now=now
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
            existing = session.last_result
            if isinstance(existing, dict):
                session.last_result = {
                    **existing,
                    _SENTINEL_ADVANCE_REFUSED_KEY: True,
                }
            else:
                session.last_result = {
                    _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
                }
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        session.reap_reason = ReapReason.PHANTOM_SURFACE
        session.last_result = candidate.routed_sentinel.model_dump(mode="json")
        session.claude_session_id = candidate.salvage_csid
        phantom_names.append(session.name)
        accepted.append(candidate)
    return accepted
