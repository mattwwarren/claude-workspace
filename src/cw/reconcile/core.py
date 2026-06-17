"""Top-level reconcile orchestration.

``reconcile`` runs the lockless gh pre-pass, then ``_reconcile_locked`` under
``sessions_lock`` (the detect/emit/act sweeps for stalled, idle, and phantom
sessions), then the post-lock gh/git passes. See the package ``__init__``
docstring and ADR-0005/ADR-0006 for the invariants.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from cw.config import (
    load_orchestrator_config,
    load_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue
from cw.models import SessionOrigin, SessionStatus
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    ReconcileReport,
    _backfill_claude_session_ids,
    _claude_agents_json,
    _looks_like_daemon_outage,
    _SalvageCandidate,
    compute_drift,
    ticket_id_for_session,
)
from cw.reconcile.idle import _act_on_idle_candidates, _detect_idle_candidates
from cw.reconcile.phantom import (
    _act_on_phantom_candidates,
    _detect_phantom_candidates,
    _emit_reap_proposed,
)
from cw.reconcile.salvage import salvage_committed_no_pr_sessions
from cw.reconcile.stalled import (
    _act_on_stalled_candidates,
    _detect_stalled_candidates,
)
from cw.reconcile.tasks import (
    complete_timed_out_merged_tasks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)


def reconcile() -> ReconcileReport:
    """Apply drift reconciliation against the persisted state.

    Flips phantom ACTIVE/IDLE sessions to COMPLETED with
    ``completed_reason = CRASHED``, emits a ``SESSION_COMPLETED`` event
    with ``crashed: True``, and reverts any RUNNING TicketTask whose
    ticket-id can be recovered from the session name back to PENDING so
    the dispatch loop will retry.

    Returns an empty report without mutating state when
    :func:`_looks_like_daemon_outage` matches — a transient daemon hiccup
    must not trigger mass-reaping.

    Partial-failure note: state and the dev queue are separate files. If
    ``save_state`` succeeds but the subsequent dev-queue update raises,
    the session will be COMPLETED while its TicketTask stays RUNNING.
    The next ``reconcile()`` call will not pick this up because the
    session is no longer ACTIVE/IDLE — so a stranded RUNNING task can
    only be recovered by explicit operator action. This is an acceptable
    tradeoff for a file-based, single-user tool.
    """
    # Pre-pass: check PR merge state for ACTIVE/IDLE DAEMON sessions before
    # acquiring sessions_lock. gh subprocess must NOT run under the lock
    # (liveness requirement, #485). Mirrors complete_timed_out_merged_tasks().
    pre_state = load_state()
    _merged_tids: list[str] = []
    _gh_blocked_tids: list[str] = []
    _gh_available = True
    for _session in pre_state.sessions:
        if _session.status not in _LIVE_STATUSES:
            continue
        if _session.origin is not SessionOrigin.DAEMON:
            continue
        _ticket_id = ticket_id_for_session(_session.name)
        if _ticket_id is None:
            continue
        if not _gh_available:
            _gh_blocked_tids.append(_ticket_id)
            continue
        _merged, _gh_avail = _deps.pr_is_merged_for_ticket(
            _ticket_id, branch="dev/" + _ticket_id
        )
        if not _gh_avail:
            _gh_available = False
            _gh_blocked_tids.append(_ticket_id)
            continue
        if _merged is None:
            # merged=None is a transient per-ticket error (e.g. network blip on
            # a single PR lookup); fall through to normal revert so the session
            # is not silently stuck.  A structural gh outage sets _gh_avail=False
            # (above), which routes ALL subsequent tickets to gh_blocked_tids.
            continue
        if _merged:
            _merged_tids.append(_ticket_id)
    merged_ticket_ids = frozenset(_merged_tids)
    gh_blocked_ticket_ids = frozenset(_gh_blocked_tids)

    with sessions_lock():
        locked_report, salvage_git_candidates = _reconcile_locked(
            merged_ticket_ids=merged_ticket_ids,
            gh_blocked_ticket_ids=gh_blocked_ticket_ids,
        )

    # Post-pass: runs AFTER sessions_lock releases so no gh subprocess
    # executes under the session lock (liveness — #485 SHOULD_FIX 4).
    completed_ticket_ids = complete_timed_out_merged_tasks()
    salvaged_ticket_ids = salvage_committed_no_pr_sessions(salvage_git_candidates)

    if not completed_ticket_ids and not salvaged_ticket_ids:
        return locked_report

    return ReconcileReport(
        phantom_session_ids=locked_report.phantom_session_ids,
        phantom_session_names=locked_report.phantom_session_names,
        reverted_ticket_ids=locked_report.reverted_ticket_ids,
        completed_ticket_ids=locked_report.completed_ticket_ids + completed_ticket_ids,
        usage_limited=locked_report.usage_limited,
        salvaged_ticket_ids=salvaged_ticket_ids,
    )


def _reconcile_locked(
    *,
    merged_ticket_ids: frozenset[str] = frozenset(),
    gh_blocked_ticket_ids: frozenset[str] = frozenset(),
) -> tuple[ReconcileReport, list[_SalvageCandidate]]:
    """Body of reconcile(), called while sessions_lock is held.

    Separated so reconcile() holds exactly one lock acquisition and the
    helpers (revert_stalled_headless_sessions, flag_silently_idle_daemon_sessions)
    can save_state directly without re-acquiring the lock.

    merged_ticket_ids / gh_blocked_ticket_ids come from a lockless pre-pass in
    reconcile() (GitHub #637); no gh subprocess executes under sessions_lock.

    Returns a tuple of (ReconcileReport, salvage_git_candidates) where
    salvage_git_candidates is the list of git-state salvage candidates for
    the post-lock pass in salvage_committed_no_pr_sessions.
    """
    state = load_state()
    now = datetime.now(UTC)

    # Passive budget sweep: catches headless DAEMON sessions whose agent
    # stalled mid-turn and produced no further Stop hook firings. Runs before
    # the outage guard so a daemon hiccup does not delay budget enforcement.
    # See GitHub issue #185.
    orchestrator_config = load_orchestrator_config()
    # Load dev queue once here; pass to both sweeps to avoid a duplicate
    # filesystem read within the same reconcile tick. See GitHub issue #326.
    shared_task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    stalled_candidates = _detect_stalled_candidates(
        state,
        now=now,
        config=orchestrator_config,
        task_by_ticket=shared_task_by_ticket,
    )
    # native_live not yet known — stalled sweep is pre-daemon-query.
    _emit_reap_proposed(state, stalled_candidates, native_live=set(), now=now)
    stalled_reverted, merged_from_stalled = _act_on_stalled_candidates(
        state,
        stalled_candidates,
        now=now,
        config=orchestrator_config,
        merged_ticket_ids=merged_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
    )

    try:
        # `claude agents --json` returns sessionId as a full UUID
        # (e.g. "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"). cw's surface_ref
        # is the 8-char short id (prefix of the UUID) — same shape
        # `claude --bg` returns at spawn. Normalize to short id for
        # comparison; otherwise reconcile sees every native session as a
        # phantom because UUID != short-id.
        _agents = _claude_agents_json()
        native_live = {
            sid[:8] for a in _agents if isinstance(sid := a.get("sessionId"), str)
        }
        surface_to_full = {
            sid[:8]: sid for a in _agents if isinstance(sid := a.get("sessionId"), str)
        }
        daemon_errored = False
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        native_live = set()
        surface_to_full = {}
        daemon_errored = True
    if _looks_like_daemon_outage(state, daemon_errored, native_live):
        return ReconcileReport(
            reverted_ticket_ids=stalled_reverted,
            completed_ticket_ids=merged_from_stalled,
        ), []
    _backfill_claude_session_ids(state, surface_to_full)

    # Snapshot sessions that are already TIMED_OUT before the watchdog sweep,
    # so we can detect which sessions were newly reaped by usage_limit_cutoff.
    pre_watchdog_timed_out_ids = {
        s.id for s in state.sessions if s.status == SessionStatus.TIMED_OUT
    }
    idle_candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live=native_live,
        config=orchestrator_config,
        task_by_ticket=shared_task_by_ticket,
    )
    _emit_reap_proposed(state, idle_candidates, native_live=native_live, now=now)
    silently_idle_ticket_ids, merged_from_idle, salvage_git_candidates = (
        _act_on_idle_candidates(
            state,
            idle_candidates,
            now=now,
            config=orchestrator_config,
            merged_ticket_ids=merged_ticket_ids,
            gh_blocked_ticket_ids=gh_blocked_ticket_ids,
        )
    )
    # Check whether any session newly transitioned to TIMED_OUT has a usage-limit
    # transcript. The _detect_usage_limit I/O cost is minimal (OS-cached files).
    watchdog_usage_limited = any(
        s.status == SessionStatus.TIMED_OUT
        and s.id not in pre_watchdog_timed_out_ids
        and _shared.detect_usage_limit(s)
        for s in state.sessions
    )

    drift = compute_drift(state, native_live, now=now)
    if not drift.phantom_session_ids:
        # No phantom sessions to reap, but still run the TIMED_OUT and
        # COMPLETED-silent sweeps so any tasks whose sessions completed or
        # timed out without reverting their queue task are recovered.
        timed_out_ticket_ids = revert_timed_out_tasks()
        completed_silent_ticket_ids = revert_completed_silent_tasks()
        all_reverted = list(
            dict.fromkeys(
                stalled_reverted
                + silently_idle_ticket_ids
                + timed_out_ticket_ids
                + completed_silent_ticket_ids
            )
        )
        return ReconcileReport(
            reverted_ticket_ids=all_reverted,
            completed_ticket_ids=list(
                dict.fromkeys(merged_from_stalled + merged_from_idle)
            ),
            usage_limited=watchdog_usage_limited,
        ), salvage_git_candidates

    phantom_set = set(drift.phantom_session_ids)
    phantom_candidates = _detect_phantom_candidates(
        state, phantom_set, task_by_ticket=shared_task_by_ticket
    )
    _emit_reap_proposed(state, phantom_candidates, native_live=native_live, now=now)
    (
        reverted,
        phantom_names,
        _watchdog_usage_limited_phantom,
        _salvaged_phantom_ticket_ids,
        _salvaged_phantom_results,
        merged_from_phantom,
    ) = _act_on_phantom_candidates(
        state,
        phantom_candidates,
        now=now,
        config=orchestrator_config,
        merged_ticket_ids=merged_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
    )

    # Sweep for TIMED_OUT and DAEMON-COMPLETED sessions whose owning TicketTask
    # was not yet reverted (e.g. signal_stop crashed after setting status but
    # before touching the queue, or a headless session completed without
    # the dispatch consumer processing it). TIMED_OUT/COMPLETED sessions are
    # already terminal; the only state mutation for these sessions is the
    # reap_reason stamp inside revert_timed_out_tasks /
    # revert_completed_silent_tasks (in-place + save_state, serialized by
    # the sessions_lock this function runs under).
    timed_out_ticket_ids = revert_timed_out_tasks()
    completed_silent_ticket_ids = revert_completed_silent_tasks()
    all_reverted = list(
        dict.fromkeys(
            stalled_reverted
            + silently_idle_ticket_ids
            + reverted
            + timed_out_ticket_ids
            + completed_silent_ticket_ids
        )
    )

    all_merged_completed = list(
        dict.fromkeys(merged_from_stalled + merged_from_idle + merged_from_phantom)
    )
    return (
        ReconcileReport(
            phantom_session_ids=drift.phantom_session_ids,
            phantom_session_names=phantom_names,
            reverted_ticket_ids=all_reverted,
            completed_ticket_ids=all_merged_completed,
            usage_limited=watchdog_usage_limited,
        ),
        salvage_git_candidates,
    )
