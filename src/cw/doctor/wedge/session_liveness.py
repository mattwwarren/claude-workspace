"""ACTIVE-session liveness wedge detectors for ``cw doctor`` (classes 6 and 8).

ACTIVE/IDLE sessions absent from the daemon live roster (class-6,
``wedge/active-no-daemon-entry``) and ACTIVE DAEMON sessions still present in
the roster with a stale transcript and no terminal sentinel (class-8,
``wedge/active-daemon-stale-no-sentinel``), plus the
:func:`_daemon_supervisor_alive` outage guard. The only submodule that reads
``_ROSTER_PATH`` and ``load_orchestrator_config``. Imports ``_constants``.
Split out of the flat ``doctor/wedge.py`` (#2164).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw.config import load_orchestrator_config, state_file
from cw.doctor._shared import WedgeFinding
from cw.doctor.wedge._constants import (
    _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
    _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
)
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_STAGE,
    LivenessBucket,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import _ROSTER_PATH, get_native_daemon_client
from cw.reconcile import (
    _has_terminal_sentinel,
    _transcript_age_seconds,
    _unresolved_subagent_spawn_age_seconds,
    compute_drift,
    ticket_id_for_session,
)
from cw.reconcile.liveness import _classify_liveness_bucket
from cw.reconcile.liveness_page import (
    format_evidence_suffix,
    summarize_last_transcript_record,
)

if TYPE_CHECKING:
    from cw.models import CwState, DevQueueStore


def _daemon_supervisor_alive() -> bool:
    """Return True when roster.json reports a positive supervisorPid.

    Uses the same source as :func:`_check_daemon_reachable` so the outage
    guard is consistent between the health check and the wedge detector.
    """
    try:
        data: dict[str, object] = json.loads(_ROSTER_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    pid = data.get("supervisorPid", 0)
    return isinstance(pid, int) and pid > 0


def _check_wedge_active_no_daemon_entry(
    state: CwState,
) -> list[WedgeFinding]:
    """Detect ACTIVE/IDLE sessions absent from the daemon live roster.

    Guards on a positive supervisorPid before treating an empty live set
    as "sessions are dead" — a missing or zero supervisorPid means the
    daemon is restarting; skipping prevents mass-reap false-positives.

    Uses :func:`compute_drift` to apply the same four guards as reconcile:
    surface_ref present, ref absent from live set, past SPAWN_GRACE_SECONDS,
    purpose != ORCHESTRATE.
    """
    if not _daemon_supervisor_alive():
        return []

    native_live = get_native_daemon_client().list_live_session_short_ids()
    drift = compute_drift(state, native_live)

    session_by_id = {s.id: s for s in state.sessions}
    findings: list[WedgeFinding] = []
    for session_id in drift.phantom_session_ids:
        session = session_by_id.get(session_id)
        ticket_id = ticket_id_for_session(session.name) if session else None
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_ACTIVE_NO_DAEMON_ENTRY,
                session_id=session_id,
                ticket_id=ticket_id,
                recipe=(
                    "ACTIVE session has no live daemon entry — session crashed "
                    "without writing a terminal sentinel. "
                    "Run: cw doctor --reap to mark COMPLETED and release the "
                    "hook context lock."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _check_wedge_active_daemon_stale_no_sentinel(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect ACTIVE DAEMON sessions idle in the roster with no terminal sentinel.

    Closes #2078: for a non-headless (plain) DAEMON spawn,
    ``signal_stop()``'s ``background_tasks`` defer (``stop_hook/command.py``) can defer
    forever if the harness's own "next main-agent turn re-fires Stop" contract
    doesn't hold for one particular turn -- and nothing else in the ordinary
    lifecycle ever completes the session. The daemon only removes a worker
    from ``roster.json`` when ``signal_stop()`` itself calls
    ``native_daemon.stop(surface_ref)`` at the very end of that function
    (``stop_hook/command.py``), which is unreachable on the stuck-deferral
    path -- so the session's ``surface_ref`` stays in the live roster
    indefinitely and ``compute_drift``'s phantom check
    (``_check_wedge_active_no_daemon_entry``, class-6) never fires: that class
    is gated on the ref being ABSENT from the roster, and it never goes
    absent here.

    This is therefore the roster-PRESENT mirror of class-6, reusing the exact
    evidence set ``reconcile/liveness.py``'s distress computation already
    gathers for the identical scenario (it flags the session for operator
    attention but never dispositions it, per RFC 0008 W2's signal-only
    contract): no terminal sentinel (:func:`_has_terminal_sentinel`), a
    transcript classified into the top liveness bucket by the canonical
    classifier (:func:`~cw.reconcile.liveness._classify_liveness_bucket` --
    same function ``record_session_liveness_changes`` latches onto
    ``Session.liveness_bucket``, so this detector can never disagree with it
    about what counts as stale, including per-stage floor overrides and a
    degraded ``liveness_buckets_minutes`` config), and no outstanding subagent
    spawn still within its await deadline
    (:func:`_unresolved_subagent_spawn_age_seconds` vs.
    ``fix_loop_await_deadline_minutes``).

    Excludes ``SessionPurpose.ORCHESTRATE``, mirroring ``compute_drift``'s own
    exclusion -- a long-lived interactive orchestrator session must never be
    mistaken for a stuck plain spawn.
    """
    try:
        config = load_orchestrator_config()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        # Skip rather than detect with default thresholds: these findings feed
        # `cw doctor --reap`, and the operator's real thresholds are unreadable.
        # The failed orchestrator.yaml check already reports the cause.
        return []
    native_live = get_native_daemon_client().list_live_session_short_ids()
    deadline_seconds = config.fix_loop_await_deadline_minutes * 60
    task_by_ticket = {t.ticket_id: t for t in queue.tasks}
    now = datetime.now(UTC)

    findings: list[WedgeFinding] = []
    for session in state.sessions:
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.status is not SessionStatus.ACTIVE:
            continue
        if session.purpose is SessionPurpose.ORCHESTRATE:
            continue
        if session.surface_ref is None or session.surface_ref not in native_live:
            continue
        if _has_terminal_sentinel(session):
            continue
        age_seconds = _transcript_age_seconds(session, now)
        if age_seconds is None:
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        stage = task.stage if task is not None else DEFAULT_STAGE
        bucket = _classify_liveness_bucket(
            age_seconds / 60.0, stage=stage, config=config
        )
        if bucket is not LivenessBucket.STALE_45M:
            continue
        spawn_age = _unresolved_subagent_spawn_age_seconds(session.worktree_path, now)
        if spawn_age is not None and spawn_age < deadline_seconds:
            continue
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
                session_id=session.id,
                ticket_id=ticket_id,
                # #2153: the liveness page's evidence suffix ("; evidence
                # suggests ...; last record ...; flat ...h; if you have
                # confirmed ..., run: <close> then: <requeue>") joins the
                # sentence below in place of its old closing period.
                recipe=(
                    "ACTIVE session is idle in the daemon roster with a stale "
                    "transcript and no terminal sentinel — likely finished but "
                    "never signaled completion. Run: cw doctor --reap to mark "
                    "COMPLETED and release the worker"
                    + format_evidence_suffix(
                        summarize_last_transcript_record(session),
                        session_id=session.id,
                        ticket_id=ticket_id,
                        client=session.client,
                        stale_minutes=age_seconds / 60.0,
                    )
                ),
                state_file=str(state_file()),
            )
        )
    return findings
