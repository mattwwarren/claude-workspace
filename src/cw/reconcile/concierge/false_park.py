"""Recipe 1: wall-clock false-park requeue.

A row parked ``stalled_retry_cap_parked`` (or with no disposition at all)
whose owning session is confirmed dead (absent from the daemon roster,
transcript flat) is requeued to PENDING at its current stage, with the
GitHub #1030 dead-on-arrival backoff and the GitHub #1674
hook-context-conflict refusal. See the package docstring.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from cw._util import exponential_backoff_seconds
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    OrchestratorEventType,
    QueueItemStatus,
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
#
# GitHub #1823: BRANCH_STALENESS_GATE_DISPOSITION ("branch_behind_main") is
# excluded on identical reasoning and must not be added here either. Requeuing
# a conflicting branch does not resolve it -- the row would spin straight back
# into the same park (or worse, slip past it) without anyone merging. The
# recovery is an operator merge of the default branch (resolving the conflict)
# followed by an explicit requeue/drain (#2979).
#
# GitHub #2123: REVIEW_STALENESS_GATE_DISPOSITION ("review_artifacts_stale") is
# excluded for the same reason, with one deliberate asymmetry worth naming. It
# IS a DRAIN_DISPOSITIONS member -- an *operator-initiated* drain re-runs review
# against the current HEAD, which is the correct recovery. Concierge's
# auto-requeue is not that: it fires unattended, on a heuristic that says "this
# park looks like a session glitch", and would silently re-dispatch a ticket
# whose review genuinely did not cover the tree that would ship. Batch-
# releasable on request, never auto-released.
from cw.reconcile._shared import (
    _REAP_ELIGIBLE_DISPOSITIONS_BASE as _FALSE_PARK_ELIGIBLE_DISPOSITIONS,
)
from cw.reconcile._shared import (
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    _transcript_age_seconds,
)
from cw.reconcile.concierge._common import (
    RECIPE_FALSE_PARK_REQUEUE,
    ConciergeCandidate,
    _find_session_for_ticket,
    _has_park_marker,
    _is_session_dead,
    _refused_at_attempt_ceiling,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import (
        CwState,
        OrchestratorConfig,
        Session,
        TicketTask,
    )

# Explicit export list (#2895): ``_FALSE_PARK_ELIGIBLE_DISPOSITIONS`` is bound
# by an ``import ... as`` rename, which mypy --strict treats as unexported, so
# the package ``__init__`` could not re-export it without this list. It names
# every binding a sibling or the package imports from this module.
__all__ = [
    "_FALSE_PARK_ELIGIBLE_DISPOSITIONS",
    "_act_on_false_park_candidates",
    "_detect_false_park_candidates",
    "_resolve_false_park_recovery_backoff",
]

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
# #976: _SILENTLY_IDLE_REASON is included too, even though the package's
# _has_park_marker check (in ``_common``) already routes a *live* silently-idle
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
                # GitHub #1750/#1751: mirrors the dispatch claim path's
                # ceiling — both must read the same counter, and (since the
                # ceiling became lane-scoped) the same resolved number, or the
                # concierge would refuse a requeue the claim path would have
                # allowed.
                refused_ceiling=_refused_at_attempt_ceiling(task, config),
                dead_on_arrival=_compute_dead_on_arrival(
                    session, now, transcript_age_seconds=transcript_age_seconds
                ),
                refused_hook_context_conflict=refused_hook_context_conflict,
            )
        )
    return candidates


def _resolve_false_park_recovery_backoff(count: int) -> float:
    """Return the backoff delay (seconds) for the *count*-th recovery.

    Same doubling-with-cap shape as the spawn-error backoff, via the shared
    :func:`cw._util.exponential_backoff_seconds` (#2717):
    ``min(INITIAL * 2**(count - 1), CAP)``, where *count* is the
    already-incremented ``false_park_recovery_count`` (so the 1st recovery
    uses the INITIAL delay unscaled).
    """
    return float(
        exponential_backoff_seconds(
            count,
            initial_seconds=_FALSE_PARK_RECOVERY_BACKOFF_INITIAL_SECONDS,
            cap_seconds=_FALSE_PARK_RECOVERY_BACKOFF_CAP_SECONDS,
        )
    )


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
