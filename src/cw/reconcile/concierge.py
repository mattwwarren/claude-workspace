"""Daemon-side gate concierge: mechanical recovery reactor (RFC 0008 capstone).

Three non-destructive recovery recipes for TicketTask rows that got stuck
behind evidence-confirmed-dead sessions:

1. **Wall-clock false-park requeue** (:func:`_detect_false_park_candidates` /
   :func:`_act_on_false_park_candidates`) — a row parked
   ``stalled_retry_cap_parked`` (or with no disposition at all) whose owning
   session is confirmed dead (absent from the daemon roster, transcript
   flat) is requeued to PENDING at its current stage.
2. **Park-marker-poison clear** (:func:`_detect_park_marker_poison_candidates`
   / :func:`_act_on_park_marker_poison_candidates`) — a row behind a session
   repeatedly skipped by the salvage watchdog's park-marker check
   (``consecutive_salvage_skips >= 1``) whose session is confirmed dead is
   closed and requeued.
3. **Cancelled-row dead-end normalization**
   (:func:`_detect_cancelled_row_candidates` /
   :func:`_act_on_cancelled_row_candidates`) — a CANCELLED row whose worktree
   still has committed work ahead of its base branch is restored to PENDING
   so that work is not silently lost.

None of these recipes stop a daemon surface that is still alive, remove a
worktree, or delete any row — they only flip a status/reset a stage, and only
when the evidence gate says the owning session is already dead. This keeps
them non-destructive under ADR-0006: the *destructive* half of ADR-0006's
concern (stopping a live daemon, deleting a worktree) never applies here,
because every recipe's precondition is "the session is already gone" — the
recipe is undoing an over-cautious park, not authorizing a new kill. All 3
gate on ``OrchestratorConfig.concierge_enabled`` (opt-in, default False, per
Q1) and are individually toggleable via
:func:`resolve_concierge_recipe_enabled` (Q7).

Each recipe follows the repo's detect/act split (see
``reconcile/tasks.py``'s ``park_terminal_sibling_tasks`` for the closest
sibling template): a pure ``_detect_*_candidates`` classification phase, then
an ``_act_on_*_candidates`` phase that re-validates under lock and mutates.
Emit-before-act is a hard requirement per recipe:
:class:`OrchestratorEventType.CONCIERGE_RECOVERED` is recorded (durably, to
the append-only events inbox) before the corresponding task/session
mutation, so evidence of what the concierge *decided* survives even if the
subsequent write fails. See GitHub #1015.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cw.config import get_client, load_state, save_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import (
    CompletionReason,
    LivenessBucket,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
)
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _NEEDS_SALVAGE_REASON,
    _SILENTLY_IDLE_REASON,
    _STALLED_CAP_PARKED_REASON,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    _transcript_age_seconds,
    _transcript_recently_active,
    ticket_id_for_session,
)
from cw.reconcile.liveness import _classify_liveness_bucket
from cw.worktree import _has_commits_beyond_base

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, OrchestratorConfig, Session, TicketTask

# Recipe name constants — the recognised keys of
# OrchestratorConfig.concierge_recoveries (Q7).
RECIPE_FALSE_PARK_REQUEUE = "false_park_requeue"
RECIPE_PARK_MARKER_POISON_CLEAR = "park_marker_poison_clear"
RECIPE_CANCELLED_ROW_RESTORE = "cancelled_row_restore"

# All 3 recipes default to enabled; concierge_recoveries is a sparse override
# dict merged onto this default (NOT a full-replace), so setting one key in
# config never silently disables the other two (Q7).
DEFAULT_CONCIERGE_RECOVERIES: dict[str, bool] = {
    RECIPE_FALSE_PARK_REQUEUE: True,
    RECIPE_PARK_MARKER_POISON_CLEAR: True,
    RECIPE_CANCELLED_ROW_RESTORE: True,
}


def resolve_concierge_recipe_enabled(
    config: OrchestratorConfig, recipe_name: str
) -> bool:
    """Return whether *recipe_name* is enabled, merging config onto the defaults.

    An operator-supplied ``concierge_recoveries`` dict only ever *overrides*
    individual keys; a recipe absent from it keeps its
    ``DEFAULT_CONCIERGE_RECOVERIES`` value (Q7).
    """
    return config.concierge_recoveries.get(
        recipe_name, DEFAULT_CONCIERGE_RECOVERIES[recipe_name]
    )


@dataclass(frozen=True)
class ConciergeCandidate:
    """Classification result from a concierge recipe's detect phase.

    ``refused_ceiling`` is True when the row's ``attempts`` is already at or
    past ``global_attempt_ceiling`` — the act phase leaves the row parked
    rather than requeuing it (A1/A2): a ceiling-refused
    ``stalled_retry_cap_parked`` row is itself part of the escalation-eligible
    set (see ``cw.reconcile.escalation``), so refusing here is not silent.
    """

    ticket_id: str
    client: str
    recipe: str
    evidence: dict[str, object] = field(default_factory=dict)
    session_id: str | None = None
    refused_ceiling: bool = False


def _find_session_for_ticket(
    state: CwState, client: str, ticket_id: str
) -> Session | None:
    """Reverse-lookup the newest session for (client, ticket_id).

    Both stalled-park paths (retry-cap park, gh-blocked park) clear
    ``task.session_id`` on parking, so recipe 1/2 cannot dereference
    ``task.session_id`` directly and must instead scan ``state.sessions`` by
    name (mirroring ``ticket_id_for_session``'s forward direction in
    reverse). Picks the most-recently-started match so a stale historical
    session for the same ticket id never shadows the live one.
    """
    matches = [
        s
        for s in state.sessions
        if s.client == client and ticket_id_for_session(s.name) == ticket_id
    ]
    if not matches:
        return None
    return max(matches, key=lambda s: s.started_at)


def _is_session_dead(session: Session | None, native_live: set[str]) -> bool:
    """Mirror doctor._is_dead_session_task's fail-toward-dead stance.

    True when there's no session at all, or its ``surface_ref`` is unset or
    absent from the live daemon roster. Missing evidence counts as dead — the
    concierge recipes only act when they can't prove the session is still
    alive.
    """
    if session is None:
        return True
    if session.surface_ref is None:
        return True
    return session.surface_ref not in native_live


def _transcript_is_flat(
    session: Session | None, now: datetime, *, window_seconds: int
) -> bool:
    """True when the session shows no recent transcript activity.

    Also true (fail-toward-dead) when there is no session to check at all —
    missing evidence is never grounds for refusing to recover a row whose
    other evidence (disposition, roster absence) already points to dead.
    """
    if session is None:
        return True
    return not _transcript_recently_active(session, now, window_seconds=window_seconds)


# ---------------------------------------------------------------------------
# Recipe 1: wall-clock false-park requeue
# ---------------------------------------------------------------------------

# Dispositions recipe 1 targets: the stalled watchdog's retry-cap park, or a
# BLOCKED_ON_USER row with no disposition at all (e.g. idle-watchdog's
# silently-idle park, which never stamps a task-level disposition — see
# reconcile/idle.py's park_disposition_by_tid, sourced from an unset
# ReapCandidate.paused_status).
_FALSE_PARK_ELIGIBLE_DISPOSITIONS: frozenset[str | None] = frozenset(
    {_STALLED_CAP_PARKED_REASON, None}
)

# Session-level park markers (recipe 2's own domain — see _has_park_marker
# below). A null-disposition BLOCKED_ON_USER row behind one of these markers
# is deliberately excluded from recipe 1: it needs recipe 2's stricter
# evidence gate (consecutive_salvage_skips >= 1 AND per-stage-floor 45m
# staleness), not recipe 1's plain 5-minute flatness check. Without this
# exclusion the two recipes would race for the same row and recipe 1's
# looser gate would always win first.
_PARK_MARKER_REASONS: frozenset[str] = frozenset(
    {_SILENTLY_IDLE_REASON, _NEEDS_SALVAGE_REASON}
)


def _has_park_marker(session: Session) -> bool:
    """True when session.last_result carries a park-marker paused_status."""
    return (
        isinstance(session.last_result, dict)
        and session.last_result.get("paused_status") in _PARK_MARKER_REASONS
    )


def _detect_false_park_candidates(
    state: CwState,
    tasks: list[TicketTask],
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
) -> list[ConciergeCandidate]:
    """Pure classification phase for recipe 1. Makes zero writes."""
    candidates: list[ConciergeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.BLOCKED_ON_USER:
            continue
        if task.disposition not in _FALSE_PARK_ELIGIBLE_DISPOSITIONS:
            continue
        session = _find_session_for_ticket(state, task.client, task.ticket_id)
        if session is not None and _has_park_marker(session):
            continue  # recipe 2's domain — see module comment above.
        if not _is_session_dead(session, native_live):
            continue
        if not _transcript_is_flat(
            session, now, window_seconds=TRANSCRIPT_LIVENESS_WINDOW_SECONDS
        ):
            continue
        candidates.append(
            ConciergeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                recipe=RECIPE_FALSE_PARK_REQUEUE,
                evidence={
                    "disposition": task.disposition,
                    "attempts": task.attempts,
                    "session_id": session.id if session else None,
                },
                session_id=session.id if session else None,
                refused_ceiling=task.attempts >= config.global_attempt_ceiling,
            )
        )
    return candidates


def _act_on_false_park_candidates(candidates: list[ConciergeCandidate]) -> list[str]:
    """Act phase for recipe 1: emit-then-requeue under dev_queue_lock."""
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
            if task.disposition not in _FALSE_PARK_ELIGIBLE_DISPOSITIONS:
                continue
            if candidate.refused_ceiling:
                continue
            record_event(
                OrchestratorEventType.CONCIERGE_RECOVERED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "recipe": RECIPE_FALSE_PARK_REQUEUE,
                    "evidence": candidate.evidence,
                },
                correlation_id=task.ticket_id,
            )
            transition_task_status(task, QueueItemStatus.PENDING)
            task.session_id = None
            task.stage_base_ref = None
            recovered.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return recovered


# ---------------------------------------------------------------------------
# Recipe 2: park-marker-poison clear
# ---------------------------------------------------------------------------
# _PARK_MARKER_REASONS / _has_park_marker live above, next to recipe 1's
# exclusion check that references them.


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
        if not _is_session_dead(session, native_live):
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
                refused_ceiling=task.attempts >= config.global_attempt_ceiling,
            )
        )
    return candidates


def _close_confirmed_dead_session(session_id: str, now: datetime) -> bool:
    """Flip a confirmed-dead session to COMPLETED/CRASHED. Returns True if changed.

    Fresh load_state()/save_state() pair — safe under the sessions_lock the
    reconcile-tick caller already holds (mirrors revert_timed_out_tasks's own
    load_state()/save_state() pattern rather than re-acquiring the lock).
    """
    state = load_state()
    changed = False
    for session in state.sessions:
        if session.id != session_id:
            continue
        if session.status in _LIVE_STATUSES:
            session.status = SessionStatus.COMPLETED
            session.completed_at = now
            session.completed_reason = CompletionReason.CRASHED
            changed = True
        break
    if changed:
        save_state(state)
    return changed


def _act_on_park_marker_poison_candidates(
    candidates: list[ConciergeCandidate], *, now: datetime
) -> list[str]:
    """Act phase for recipe 2: emit, close the dead session, then requeue."""
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
            if candidate.session_id is not None:
                _close_confirmed_dead_session(candidate.session_id, now)
            transition_task_status(task, QueueItemStatus.PENDING)
            task.session_id = None
            task.stage_base_ref = None
            recovered.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return recovered


# ---------------------------------------------------------------------------
# Recipe 3: cancelled-row dead-end normalization
# ---------------------------------------------------------------------------


def _detect_cancelled_row_candidates(
    tasks: list[TicketTask],
) -> list[ConciergeCandidate]:
    """Pure classification phase for recipe 3. Makes zero writes.

    No attempt-ceiling gate (per the ticket's one-time reasoned omission —
    this recovers committed work, it is not a retry) and no ``attempts``
    increment on restore (A3): the next real claim increments as usual.
    """
    candidates: list[ConciergeCandidate] = []
    for task in tasks:
        if task.status != QueueItemStatus.CANCELLED:
            continue
        if task.worktree_path is None or not task.worktree_path.exists():
            continue
        try:
            client_cfg = get_client(task.client)
        except CwError:
            continue
        if not _has_commits_beyond_base(task.worktree_path, client_cfg.default_branch):
            continue
        candidates.append(
            ConciergeCandidate(
                ticket_id=task.ticket_id,
                client=task.client,
                recipe=RECIPE_CANCELLED_ROW_RESTORE,
                evidence={
                    "worktree_path": str(task.worktree_path),
                    "default_branch": client_cfg.default_branch,
                },
            )
        )
    return candidates


def _act_on_cancelled_row_candidates(candidates: list[ConciergeCandidate]) -> list[str]:
    """Act phase for recipe 3: emit-then-restore CANCELLED rows to PENDING."""
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
            if task.status != QueueItemStatus.CANCELLED:
                continue
            record_event(
                OrchestratorEventType.CONCIERGE_RECOVERED,
                {
                    "ticket_id": task.ticket_id,
                    "client": task.client,
                    "recipe": RECIPE_CANCELLED_ROW_RESTORE,
                    "evidence": candidate.evidence,
                },
                correlation_id=task.ticket_id,
            )
            transition_task_status(task, QueueItemStatus.PENDING)
            recovered.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return recovered


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def run_concierge_recoveries(
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
) -> list[str]:
    """Run all enabled concierge recipes for one reconcile tick.

    No-op (returns ``[]`` immediately) unless ``config.concierge_enabled`` is
    True (Q1). Loads fresh state/dev-queue snapshots itself rather than
    accepting them from the caller — by the time reconcile's
    ``_reconcile_locked`` reaches this wiring point, several prior sweeps
    (stalled/idle/phantom/backstop) have already mutated and saved both
    files, so a caller-supplied snapshot would be stale. Safe to call while
    the caller already holds ``sessions_lock`` — this function never
    acquires that lock itself, only ``dev_queue_lock`` per recipe act phase
    (mirrors ``revert_timed_out_tasks``'s own fresh load/save pattern).

    Returns the combined list of ticket IDs recovered across all 3 recipes.
    """
    if not config.concierge_enabled:
        return []

    state = load_state()
    tasks = load_dev_queue().tasks

    recovered: list[str] = []

    if resolve_concierge_recipe_enabled(config, RECIPE_FALSE_PARK_REQUEUE):
        false_park_candidates = _detect_false_park_candidates(
            state, tasks, now=now, native_live=native_live, config=config
        )
        recovered.extend(_act_on_false_park_candidates(false_park_candidates))

    if resolve_concierge_recipe_enabled(config, RECIPE_PARK_MARKER_POISON_CLEAR):
        park_marker_candidates = _detect_park_marker_poison_candidates(
            state, tasks, now=now, native_live=native_live, config=config
        )
        recovered.extend(
            _act_on_park_marker_poison_candidates(park_marker_candidates, now=now)
        )

    if resolve_concierge_recipe_enabled(config, RECIPE_CANCELLED_ROW_RESTORE):
        cancelled_candidates = _detect_cancelled_row_candidates(tasks)
        recovered.extend(_act_on_cancelled_row_candidates(cancelled_candidates))

    return recovered
