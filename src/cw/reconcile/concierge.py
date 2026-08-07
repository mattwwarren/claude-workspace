"""Daemon-side gate concierge: mechanical recovery reactor (RFC 0008 capstone).

Three non-destructive recovery recipes for TicketTask rows that got stuck
behind evidence-confirmed-dead sessions:

1. **Wall-clock false-park requeue** (:func:`_detect_false_park_candidates` /
   :func:`_act_on_false_park_candidates`) — a row parked
   ``stalled_retry_cap_parked`` (or with no disposition at all) whose owning
   session is confirmed dead (absent from the daemon roster, transcript
   flat) is requeued to PENDING at its current stage. Exception (GitHub
   #1674): the requeue is *refused* — the row is left parked and
   ``CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED`` is emitted — when the row's
   currently-resolved session is the exact session that already made a spawn
   attempt raise ``HookContextConflictError`` and is still non-terminal;
   respawning cannot succeed until that session is closed, so requeuing only
   burns attempts. The refusal clears itself once the session's status goes
   terminal (``cw spawn close --confirmed-dead <id>``) or a new session
   supersedes it by id — no separate manual unblock step.
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

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from cw.config import get_client, load_state, save_state
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
from cw.exceptions import CwError
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    CompletionReason,
    LivenessBucket,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _NEEDS_SALVAGE_REASON,
    _SILENTLY_IDLE_REASON,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    _apply_salvaged_completion,
    _foreign_result_target_queue_status,
    _queue_status_for_salvaged,
    _transcript_age_seconds,
    _validate_existing_result_for_routing,
    salvage_terminal_result,
    ticket_id_for_session,
)

# GitHub #1702: REVIEW_HEALTH_GATE_DISPOSITION ("review_health_gate") is
# deliberately NOT a member of this set, and must not be added to
# _REAP_ELIGIBLE_DISPOSITIONS_BASE to make it one. Every disposition here names
# a *technical* park -- a dead surface, an idle stall, a phantom, a session that
# died before it could report -- for which "requeue it and try again" is the
# correct mechanical answer. A review-health park is the opposite: the review
# genuinely ran and reported that it could not vouch for its own coverage.
# Auto-requeuing it would misclassify a real review-coverage finding as a
# session-death glitch and silently spin the ticket back through the pipeline
# without a human ever seeing the signal -- exactly what #1702 exists to
# prevent. Escalation (cw.reconcile.escalation) pages the operator on it
# instead; drain (cw.dev_queue.drain) releases it on an explicit operator call.
from cw.reconcile._shared import (
    _REAP_ELIGIBLE_DISPOSITIONS_BASE as _FALSE_PARK_ELIGIBLE_DISPOSITIONS,
)
from cw.reconcile.liveness import _classify_liveness_bucket
from cw.worktree import _has_commits_beyond_base

if TYPE_CHECKING:
    from datetime import datetime

    from cw.auto_dev_result import AutoDevResult, BlockedResult
    from cw.models import CwState, OrchestratorConfig, Session, TicketTask
    from cw.result import EmitOutcome

_log = logging.getLogger(__name__)

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

# GitHub #1030 — false_park_requeue churn backoff. A row whose previous
# mechanical recovery produced a session that died within seconds of spawn
# (never producing real output) is "dead on arrival" — recovering it again
# immediately just churns against the account session limit. active_lifespan
# below this threshold is unambiguous evidence of an instant death; a
# legitimately stalled-but-working session has an active lifespan in the tens
# of minutes. Fixed seconds, not a ratio and not the dispatch poll interval —
# lifespan is an absolute property of the death, not proportional to how long
# ago it happened or to unrelated dispatch machinery.
_DEAD_ON_ARRIVAL_LIFESPAN_SECONDS = 120.0
# Exponential backoff for the false_park_requeue recipe's own next-detect
# eligibility window, enforced locally in this recipe's detect phase (NOT the
# dispatch.py spawn-error claim gate — a distinct mechanism for a distinct
# failure class at a distinct call site). Scaled up from the sibling
# spawn-error backoff (2s/300s cap) because a dead-session recovery cycle
# takes tens of minutes, not seconds (the incident's ~30-35 min cadence).
_FALSE_PARK_RECOVERY_BACKOFF_INITIAL_SECONDS = 300
_FALSE_PARK_RECOVERY_BACKOFF_CAP_SECONDS = 3600


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

    ``refused_hook_context_conflict`` is the same shape for a different,
    likewise-futile requeue (GitHub #1674) — see the field comment below. A
    refused row keeps its disposition, so it too stays escalation-eligible.
    """

    ticket_id: str
    client: str
    recipe: str
    evidence: dict[str, object] = field(default_factory=dict)
    session_id: str | None = None
    refused_ceiling: bool = False
    # Recipe 1 only (GitHub #1030): True when this cycle's session shows the
    # dead-on-arrival signature — the previous mechanical recovery produced a
    # session that died within seconds of spawn. Guarded to False when
    # evidence is missing (no session, or transcript age unlocatable) so
    # missing evidence never arms the backoff. Consumed by the act phase to
    # arm/reset the false_park_recovery_count / next_eligible_at backoff.
    dead_on_arrival: bool = False
    # Recipe 1 only (GitHub #1674): True when this row's currently-resolved
    # session IS the session that already made a spawn attempt raise
    # HookContextConflictError (task.hook_context_conflict_session_id), and
    # that session is still non-terminal. Its cw-context.json still owns the
    # worktree, so a requeue can only fail the same way again and burn another
    # attempt. Unlike dead_on_arrival's backoff (a deferral of the next cycle),
    # the act phase skips the requeue entirely — elapsed time cannot clear this
    # condition, only an operator closing the session or a newer session
    # superseding it.
    refused_hook_context_conflict: bool = False


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


def _transcript_is_flat_for_age(
    age_seconds: float | None, *, window_seconds: int
) -> bool:
    """True when *age_seconds* indicates no recent transcript activity.

    Also true (fail-toward-dead) when *age_seconds* is None — no session, or
    a transcript that could not be located. Missing evidence is never
    grounds for refusing to recover a row whose other evidence (disposition,
    roster absence) already points to dead.

    Takes a precomputed age rather than a ``Session`` (GitHub #1030) so a
    single transcript lookup can be shared with :func:`_compute_dead_on_arrival`
    for the same candidate instead of each helper locating the transcript
    independently.
    """
    if age_seconds is None:
        return True
    return age_seconds >= window_seconds


def _compute_dead_on_arrival(
    session: Session | None, now: datetime, *, transcript_age_seconds: float | None
) -> bool:
    """True when *session* died within seconds of spawn (GitHub #1030).

    ``active_lifespan_seconds = max(0, elapsed_seconds - transcript_age_seconds)``
    is exactly "seconds from spawn to last transcript write"; below
    :data:`_DEAD_ON_ARRIVAL_LIFESPAN_SECONDS` is unambiguous evidence of an
    instant death.

    Guard: unlike ``_is_session_dead``/``_transcript_is_flat_for_age``,
    missing evidence here is treated as innocent, not guilty — this field
    gates *arming* an exponential backoff (a punitive, less-reversible
    consequence), not a recovery-eligibility veto. Returns False whenever
    *session* is None or *transcript_age_seconds* is None, without ever
    evaluating the formula against a missing operand.

    Takes the already-located *transcript_age_seconds* rather than locating
    it itself, so callers compute it once and share it with
    :func:`_transcript_is_flat_for_age`.
    """
    if session is None:
        return False
    if transcript_age_seconds is None:
        return False
    elapsed_seconds = (now - session.started_at).total_seconds()
    active_lifespan_seconds = max(0.0, elapsed_seconds - transcript_age_seconds)
    return active_lifespan_seconds < _DEAD_ON_ARRIVAL_LIFESPAN_SECONDS


# ---------------------------------------------------------------------------
# Recipe 1: wall-clock false-park requeue
# ---------------------------------------------------------------------------

# Dispositions recipe 1 targets: the stalled watchdog's retry-cap park, or a
# BLOCKED_ON_USER row with no disposition at all. As of #976, every reconcile
# park path (idle-watchdog's silently-idle park included) stamps a non-null
# task-level disposition via reconcile/idle.py's park_disposition_by_tid,
# sourced from ReapCandidate.paused_status — so `None` here now covers only
# legacy pre-#976 rows and any park path this module doesn't itself produce,
# not a documented "silently_idle parks as None" case.
#
# #976: the SIGNAL_ONLY reroute-to-BLOCKED_ON_USER path (shared by the
# stalled/idle/phantom sweeps via _apply_queue_mutations) used to leave
# disposition=None on these rows — which is exactly the "no disposition at
# all" case recipe 1's own docstring describes as in-scope. Now that the
# reroute stamps a real disposition (ReapReason.WALL_CLOCK_BUDGET/IDLE_STALL/
# PHANTOM_SURFACE), recipe 1 must keep tracking that population or these rows
# silently stop being auto-recoverable — the exact regression escalation.py's
# _ELIGIBLE_DISPOSITIONS extension (same ticket) was written to avoid for the
# escalation consumer.
#
# #976: _SILENTLY_IDLE_REASON is included too, even though the module's
# _has_park_marker check (below) already routes a *live* silently-idle
# session-with-marker to recipe 2's stricter gate. That marker check only
# fires when `session is not None` — a row whose session record has since
# been pruned entirely (not merely marker-bearing) has no marker to check,
# so pre-#976 (disposition=None) it was still recipe-1-eligible via the
# `None` branch. Omitting _SILENTLY_IDLE_REASON here would silently regress
# that no-session-record population the same way the wall-clock/idle-stall/
# phantom-surface values above would have. The `_has_park_marker` guard below
# still excludes any row whose session record *does* exist and carries the
# marker, so recipe 2's domain is unaffected.
#
# GitHub #1571: the 6-member frozenset itself now lives in
# _shared._REAP_ELIGIBLE_DISPOSITIONS_BASE (imported above, aliased to this
# name) -- it was hand-typed identically here and in escalation.py's
# _ELIGIBLE_DISPOSITIONS, synced only by a comment telling the reader to
# update both. The per-member reasoning above is recipe-1-specific and stays
# here; only the value moved.

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
        # #1030: a row still inside its dead-on-arrival backoff window is not
        # even considered this cycle — deferral, not a veto; the window
        # re-evaluates every tick and is finite, so it cannot go permanent.
        if (
            task.false_park_recovery_next_eligible_at is not None
            and now < task.false_park_recovery_next_eligible_at
        ):
            continue
        session = _find_session_for_ticket(state, task.client, task.ticket_id)
        if session is not None and _has_park_marker(session):
            continue  # recipe 2's domain — see module comment above.
        if not _is_session_dead(session, native_live):
            continue
        # #1030: locate the transcript once and share the age with both the
        # flatness check and dead_on_arrival below, instead of each calling
        # its own _locate_session_transcript independently (this row persists
        # across many ticks while parked, so the duplicate glob repeated
        # every cycle).
        transcript_age_seconds = (
            _transcript_age_seconds(session, now) if session is not None else None
        )
        if not _transcript_is_flat_for_age(
            transcript_age_seconds, window_seconds=TRANSCRIPT_LIVENESS_WINDOW_SECONDS
        ):
            continue
        # #1674: clause order is load-bearing. `session is not None` must be
        # evaluated first so neither `session.id` nor `session.status` is ever
        # dereferenced on a missing session, AND so the ordinary
        # never-conflicted row (no session record + the field at its None
        # default) can never satisfy the identity check by `None == None` —
        # missing evidence is innocent, not guilty (see
        # _compute_dead_on_arrival's guard). The status clause is what lets
        # `cw spawn close --confirmed-dead <id>` clear the refusal: closing
        # flips the session's status but not its id, and
        # _find_session_for_ticket keeps resolving that same session
        # regardless of status, so identity equality alone would stay True
        # forever after the operator did exactly what the runbook says.
        refused_hook_context_conflict = (
            session is not None
            and session.id == task.hook_context_conflict_session_id
            and session.status not in TERMINAL_SESSION_STATUSES
        )
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
                dead_on_arrival=_compute_dead_on_arrival(
                    session, now, transcript_age_seconds=transcript_age_seconds
                ),
                refused_hook_context_conflict=refused_hook_context_conflict,
            )
        )
    return candidates


def _resolve_false_park_recovery_backoff(count: int) -> float:
    """Return the backoff delay (seconds) for the *count*-th recovery.

    Same doubling-with-cap shape as dispatch.py's spawn-error backoff:
    ``min(INITIAL * 2**(count - 1), CAP)``, where *count* is the
    already-incremented ``false_park_recovery_count`` (so the 1st recovery
    uses the INITIAL delay unscaled).
    """
    delay_seconds = _FALSE_PARK_RECOVERY_BACKOFF_INITIAL_SECONDS * (2 ** (count - 1))
    return float(min(delay_seconds, _FALSE_PARK_RECOVERY_BACKOFF_CAP_SECONDS))


def _act_on_false_park_candidates(
    candidates: list[ConciergeCandidate], *, now: datetime
) -> list[str]:
    """Act phase for recipe 1: emit-then-requeue under dev_queue_lock.

    ADR-0006: non-destructive. The only mutation is PENDING requeue of a row
    the detect phase already proved is behind a dead session (roster-absent,
    transcript flat) — no daemon is stopped, no worktree touched/removed.

    GitHub #1030: the requeue to PENDING always proceeds regardless of
    ``candidate.dead_on_arrival`` — backoff is a deferral of the *next*
    detection cycle, never a veto of this one. When ``dead_on_arrival`` is
    True, this also arms exponential backoff (increments
    ``false_park_recovery_count``, stamps
    ``false_park_recovery_next_eligible_at``, and emits
    ``CONCIERGE_RECOVERY_BACKOFF_ARMED``) before the unconditional
    ``CONCIERGE_RECOVERED`` emit + requeue. When False (a legitimate stall,
    including the missing-evidence cases), both fields are reset.

    GitHub #1674: ``candidate.refused_hook_context_conflict`` is the one
    condition that skips the requeue outright (rather than deferring it) —
    the row's worktree is still owned by a non-terminal session's hook
    context, so respawning is impossible until that session is closed. The
    row is left byte-identical and ``CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED``
    records the decision.
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
            if task.disposition not in _FALSE_PARK_ELIGIBLE_DISPOSITIONS:
                continue
            if candidate.refused_ceiling:
                continue
            if candidate.refused_hook_context_conflict:
                # #1674: no mutation at all — disposition, attempts and the
                # dead-on-arrival backoff fields are left exactly as they are.
                # Deliberately unlatched (unlike escalation.py's
                # escalation_fired_at): this re-fires every reconcile pass the
                # row stays parked against the same session. It is audit-only
                # telemetry, not forwarded, so repeat firing costs one event
                # line and keeps the operator's evidence current.
                record_event(
                    OrchestratorEventType.CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED,
                    {
                        "ticket_id": task.ticket_id,
                        "client": task.client,
                        "recipe": RECIPE_FALSE_PARK_REQUEUE,
                        "session_id": candidate.session_id,
                    },
                    correlation_id=task.ticket_id,
                )
                continue
            if candidate.dead_on_arrival:
                task.false_park_recovery_count += 1
                delay = _resolve_false_park_recovery_backoff(
                    task.false_park_recovery_count
                )
                next_eligible_at = now + timedelta(seconds=delay)
                task.false_park_recovery_next_eligible_at = next_eligible_at
                record_event(
                    OrchestratorEventType.CONCIERGE_RECOVERY_BACKOFF_ARMED,
                    {
                        "ticket_id": task.ticket_id,
                        "client": task.client,
                        "recovery_count": task.false_park_recovery_count,
                        "next_eligible_at": next_eligible_at.isoformat(),
                        "session_id": candidate.session_id,
                    },
                    correlation_id=task.ticket_id,
                )
            else:
                task.false_park_recovery_count = 0
                task.false_park_recovery_next_eligible_at = None
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


def _close_confirmed_dead_session(
    session_id: str, now: datetime
) -> tuple[bool, AutoDevResult | None, EmitOutcome | None]:
    """Flip a confirmed-dead session to COMPLETED.

    Returns ``(changed, salvage_result, refusal)``.

    GitHub #1353: attempts terminal-sentinel salvage before stamping CRASHED —
    mirrors idle.py/stalled.py/phantom.py's own pre-close salvage check
    (_shared.salvage_terminal_result). salvage_result is the recovered
    AutoDevResult when the session's transcript carried a valid terminal
    sentinel (the caller routes the owning task to completion/blocked-on-user
    instead of PENDING); None means the session is genuinely unrecoverable and
    the pre-existing CRASHED stamp applies unchanged.

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
                salvage_terminal_result(session)
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
    candidates: list[ConciergeCandidate], *, now: datetime
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
                    candidate.session_id, now
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

    Why this also restores operator-initiated cancels, not just mechanical
    ones: the ticket's own deliverable text names the target population as
    "operator/spawn-close-produced `cancelled` rows with a live worktree +
    committed work" — both sources are explicitly in scope by design. There is
    deliberately no field distinguishing "operator meant to cancel this for
    good" from "a mechanical process cancelled it in error": the ticket's
    stated intent is that committed work should never be silently lost to
    *any* cancel, of either origin. An operator who wants a ticket to stay
    cancelled despite committed work should set
    ``concierge_recoveries.cancelled_row_restore: false`` (Q7) or clear the
    worktree; this was reviewed and confirmed against the ticket text as
    intentional, not a defect.
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
    """Act phase for recipe 3: emit-then-restore CANCELLED rows to PENDING.

    ADR-0006: non-destructive. Restoring to PENDING never touches the
    worktree or stops any process — the detect phase already confirmed the
    worktree exists and has committed work ahead of its base branch.
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
        recovered.extend(_act_on_false_park_candidates(false_park_candidates, now=now))

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
