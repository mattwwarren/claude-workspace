"""Transcript-staleness liveness bucketing for reconcile (RFC 0008 W2).

Classifies each live DAEMON session's transcript-mtime staleness into one of
four latched buckets (:class:`~cw.models.LivenessBucket`) and emits
``session.liveness_changed`` on each crossing. Pure observability: unlike the
idle/stalled sweeps in this package, this sweep never dispositions a session
or mutates the dev queue — it only stamps ``Session.liveness_bucket`` and
records an event. See GitHub #1001.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.config import save_state
from cw.dev_queue import load_dev_queue
from cw.events import record_event
from cw.models import (
    DEFAULT_STAGE,
    LivenessBucket,
    OrchestratorEventType,
    SessionOrigin,
)
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _transcript_age_seconds,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, OrchestratorConfig, Stage, TicketTask

# Fallback floor (minutes) used when both liveness_buckets_minutes is empty
# and a stage has no per-stage override — mirrors the historical
# IDLE_WATCHDOG_SECONDS-style hardcoded fallback pattern in _shared.py.
_DEFAULT_LIVENESS_FIRST_BUCKET_MINUTES = 15

# Index positions within OrchestratorConfig.liveness_buckets_minutes.
# Named to avoid PLR2004 magic-value comparisons in the length checks below;
# the ladder degrades gracefully (threshold simply unreachable) when the
# configured list is shorter than expected.
_STALE_30M_INDEX = 1
_STALE_45M_INDEX = 2


@dataclass(frozen=True)
class LivenessCandidate:
    """Classification result from the liveness detect phase.

    A pure observation: no ``ProposedAction``, no disposition, no queue
    mutation. Only produced when the newly-classified bucket differs from
    the session's currently-persisted ``liveness_bucket`` (edge-detect, not
    a consecutive-observation counter like ``idle_observation_count``).
    See GitHub #1001.
    """

    session_id: str
    ticket_id: str | None
    client: str | None
    stage: Stage
    old_bucket: LivenessBucket
    new_bucket: LivenessBucket
    stale_minutes: float


def _classify_liveness_bucket(
    stale_minutes: float,
    *,
    stage: Stage,
    config: OrchestratorConfig,
) -> LivenessBucket:
    """Classify transcript staleness into a bucket via floor-suppression.

    The per-stage floor (``liveness_first_bucket_by_stage``, falling back to
    ``liveness_buckets_minutes[0]``) is checked FIRST: below it a session is
    always LIVE, regardless of the global thresholds. Global thresholds are
    then evaluated top-down (45m, then 30m, else 15m). Branch order is
    load-bearing — the floor check must precede the global-threshold checks,
    or a session at 32m with a 35m floor and a 30m global stale_30m threshold
    would wrongly classify stale_30m instead of live.

    The stale_30m threshold is additionally gated on exceeding the floor: a
    global threshold at or below the effective floor for a stage is entirely
    swallowed by the floor's own (stale_15m) rung rather than separately
    firing, since every stale_minutes value that clears the floor trivially
    also clears a smaller global threshold. Concretely, an IMPL session
    (floor=35, global stale_30m=30) ascending 30/34/38/42/44 minutes must
    never emit stale_30m — it goes straight from LIVE (<35) to stale_15m
    (>=35, <45). Labels otherwise keep their global-threshold identity; only
    the entry point moves. stale_45m needs no such guard: it is always the
    top rung, so crossing it is correct regardless of where the floor sits.
    See the RFC 0008 W2 round-2 binding decision (GitHub #1001).

    Degrades gracefully when ``liveness_buckets_minutes`` is misconfigured to
    a length other than 3: missing thresholds are simply never reached.
    """
    thresholds = config.liveness_buckets_minutes
    default_floor = (
        thresholds[0] if thresholds else _DEFAULT_LIVENESS_FIRST_BUCKET_MINUTES
    )
    floor = config.liveness_first_bucket_by_stage.get(stage, default_floor)
    if stale_minutes < floor:
        return LivenessBucket.LIVE
    threshold_45m = (
        thresholds[_STALE_45M_INDEX] if len(thresholds) > _STALE_45M_INDEX else None
    )
    if threshold_45m is not None and stale_minutes >= threshold_45m:
        return LivenessBucket.STALE_45M
    threshold_30m = (
        thresholds[_STALE_30M_INDEX] if len(thresholds) > _STALE_30M_INDEX else None
    )
    if (
        threshold_30m is not None
        and threshold_30m > floor
        and stale_minutes >= threshold_30m
    ):
        return LivenessBucket.STALE_30M
    return LivenessBucket.STALE_15M


def _detect_liveness_candidates(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask],
) -> list[LivenessCandidate]:
    """Pure classification phase for the liveness-bucket sweep.

    Returns a list of :class:`LivenessCandidate` objects, one per session
    whose newly-classified bucket differs from its persisted
    ``liveness_bucket``. Makes zero writes to state, queue, or event bus.

    Gating (RFC 0008 W2 round-1, R2): DAEMON origin + status in
    ``_LIVE_STATUSES`` + ``surface_ref`` present in *native_live* — the same
    3-condition gate as ``_detect_idle_candidates``, deliberately WITHOUT its
    ``_has_terminal_sentinel`` check (a session that already emitted a
    sentinel this tick can still cross a staleness bucket before its task
    routes). A session whose transcript cannot be located is skipped this
    tick (fail-open — no bucket assigned without positive staleness
    evidence). See GitHub #1001.
    """
    candidates: list[LivenessCandidate] = []
    for session in state.sessions:
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.status not in _LIVE_STATUSES:
            continue
        if session.surface_ref is None or session.surface_ref not in native_live:
            continue
        age_seconds = _transcript_age_seconds(session, now)
        if age_seconds is None:
            continue
        stale_minutes = age_seconds / 60.0
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        stage = task.stage if task is not None else DEFAULT_STAGE
        new_bucket = _classify_liveness_bucket(
            stale_minutes, stage=stage, config=config
        )
        if new_bucket == session.liveness_bucket:
            continue
        candidates.append(
            LivenessCandidate(
                session_id=session.id,
                ticket_id=ticket_id,
                client=session.client,
                stage=stage,
                old_bucket=session.liveness_bucket,
                new_bucket=new_bucket,
                stale_minutes=stale_minutes,
            )
        )
    return candidates


def _act_on_liveness_candidates(
    state: CwState,
    candidates: list[LivenessCandidate],
) -> None:
    """Act phase: latch each candidate's new bucket and emit its event.

    Unconditional — every candidate here already represents a real bucket
    crossing (edge-detect happened in the detect phase), so every candidate
    both mutates state and emits. Calls ``save_state(state)`` once when any
    candidates are present, mirroring the other reconcile act phases. See
    GitHub #1001.
    """
    if not candidates:
        return
    session_by_id = {s.id: s for s in state.sessions}
    for candidate in candidates:
        # Every candidate was derived from this same state.sessions list in
        # the detect phase (see _detect_liveness_candidates), so the lookup
        # always hits — matches the direct-index pattern used by sibling
        # act phases (e.g. stalled.py) rather than a defensive .get().
        session = session_by_id[candidate.session_id]
        session.liveness_bucket = candidate.new_bucket
        record_event(
            OrchestratorEventType.SESSION_LIVENESS_CHANGED,
            {
                "session_id": candidate.session_id,
                "ticket_id": candidate.ticket_id,
                "client": candidate.client,
                "stage": candidate.stage.value,
                "old_bucket": candidate.old_bucket.value,
                "new_bucket": candidate.new_bucket.value,
                "stale_minutes": candidate.stale_minutes,
            },
            correlation_id=candidate.ticket_id,
        )
    save_state(state)


def record_session_liveness_changes(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[LivenessCandidate]:
    """Detect and latch transcript-staleness bucket crossings for DAEMON sessions.

    Combines the detect and act phases (mirrors
    ``flag_silently_idle_daemon_sessions`` / ``revert_stalled_headless_sessions``
    in sibling submodules). ``task_by_ticket`` may be pre-loaded by the caller
    to avoid a duplicate dev-queue read within the same reconcile tick; when
    omitted it is loaded here. Returns the list of crossings applied this
    tick. See GitHub #1001.
    """
    resolved_task_by_ticket = (
        task_by_ticket
        if task_by_ticket is not None
        else {t.ticket_id: t for t in load_dev_queue().tasks}
    )
    candidates = _detect_liveness_candidates(
        state,
        now=now,
        native_live=native_live,
        config=config,
        task_by_ticket=resolved_task_by_ticket,
    )
    _act_on_liveness_candidates(state, candidates)
    return candidates
