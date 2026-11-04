"""The ``cw doctor --reap`` remedy for actionable wedge findings.

:func:`_reap_wedge_findings` applies the queue mutations for classes 2, 3, 5
and 7 (class 13 is advisory and never reaches a mutation), then the
post-queue tail (:func:`_reap_sessions_and_sweep`): the class-6/8 session
reaps under a bounded ``sessions_lock``, the class-11
stranded-routed-result close (which lives in ``cw.doctor.routed_result_wedge``,
#2524) and the class-10 leaked-worker sweep. A bounded-lock timeout is
reported as a failing ``wedge-reap`` check (:func:`_reap_timeout_check`).

``_reap_wedge_findings`` mutates state directly (``save_dev_queue``) outside
``mutate_state()`` by design — kept colocated here rather than extracted into a
shared mutation helper (#1314 constraint).

Imports :func:`_reap_session_by_selector` from ``loop_health`` at top level,
plus ``_constants`` and ``blocked_on_user``. Split out of the flat
``doctor/wedge.py`` (#2164).
"""

from __future__ import annotations

import json
from dataclasses import replace

from pydantic import ValidationError

from cw.config import load_state
from cw.dev_queue import dev_queue_lock, save_dev_queue, transition_task_status
from cw.doctor import _deps
from cw.doctor._shared import CheckResult, ReapScope, WedgeFinding
from cw.doctor.loop_health import _reap_session_by_selector
from cw.doctor.pending_skip_wedge import (
    WEDGE_PENDING_WORKTREE_OCCUPIED,
    PendingWorktreeOccupiedFinding,
)
from cw.doctor.routed_result_wedge import (
    WEDGE_ROUTED_RESULT_STRANDED,
    has_pending_routed_result_audits,
    reap_routed_result_findings,
)
from cw.doctor.wedge._constants import (
    _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
    _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
    _WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN,
    _WEDGE_BLOCKED_DEAD_SESSION,
    _WEDGE_LEAKED_DAEMON_WORKER,
    _WEDGE_OPERATOR_GATED_PARK,
    _WEDGE_TERMINAL_SIBLING,
)
from cw.doctor.wedge.blocked_on_user import (
    _cancel_terminal_sibling_parks,
    _collapse_blocked_on_user_tasks,
)
from cw.exceptions import SessionsLockTimeoutError
from cw.models import QueueItemStatus
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import ticket_id_for_session
from cw.reconcile.leaked_workers import sweep_leaked_daemon_workers

_REAP_CHECK_NAME = "wedge-reap"
# Warn-level check a scoped reap (#2716) emits when it skipped a class-6/8
# session whose record was unreadable or belonged to another client.
_SCOPED_SKIP_CHECK_NAME = "scoped-reap/session-skipped"


def _scoped_daemon_findings(
    findings: list[tuple[str, str]], scope: ReapScope
) -> tuple[list[tuple[str, str]], list[str]]:
    """Keep only the class-6/8 sessions whose record is in *scope* (R5, #2716).

    Returns ``(kept, skipped)``; *skipped* holds one ``"<id> (<reason>)"``
    label per dropped session. The client comes from the session record,
    never from the :class:`WedgeFinding`, which carries no client. A record
    that cannot be read (the state fails to load, or the id has no record)
    is out of scope: it is skipped, never reaped. Exceptions other than the
    three a state load is expected to raise propagate, before any mutation.
    """
    if not findings:
        return [], []
    try:
        state = load_state()
    except (OSError, json.JSONDecodeError, ValidationError):
        return [], [f"{sid} (unreadable record)" for sid, _ in findings]
    session_by_id = {s.id: s for s in state.sessions}
    kept: list[tuple[str, str]] = []
    skipped: list[str] = []
    for session_id, wedge_class in findings:
        session = session_by_id.get(session_id)
        if session is None:
            skipped.append(f"{session_id} (unreadable record)")
        elif not scope.covers(session.client, ticket_id_for_session(session.name)):
            skipped.append(f"{session_id} (out of scope: client {session.client})")
        else:
            kept.append((session_id, wedge_class))
    return kept, skipped


def _note_skipped_sessions(
    check: CheckResult | None, skipped: list[str]
) -> CheckResult | None:
    """Report scoped-reap session skips on the reap's returned check (#2716).

    With no skips *check* is returned unchanged, so the unscoped path (which
    never skips) is untouched. Otherwise a ``None`` check becomes the
    warn-level ``scoped-reap/session-skipped`` check, and a failing timeout
    check gets the skip note appended to its detail.
    """
    if not skipped:
        return check
    note = "skipped: " + ", ".join(skipped)
    if check is None:
        return CheckResult(_SCOPED_SKIP_CHECK_NAME, ok=True, warn=True, detail=note)
    return replace(check, detail=f"{check.detail}; {note}")


def _reap_daemon_sessions(
    findings: list[tuple[str, str]],
    *,
    scope: ReapScope | None = None,
) -> tuple[list[str], list[str], list[str], SessionsLockTimeoutError | None]:
    """Reap each ``(session_id, wedge_class)`` pair under a bounded lock (#2504).

    Returns ``(reaped, not_found, remaining, timeout)``. *reaped* holds every
    id whose ``_reap_session_by_selector`` call returned without raising
    (``True`` or ``False`` alike); *not_found* is the subset that returned
    ``False`` (the selector matched no session). On the first
    :class:`SessionsLockTimeoutError` the loop stops (fail-fast: each further
    attempt would wait out the full bound again) and *remaining* names the
    failing id plus every id after it; the timeout is raised at lock entry,
    before any mutation, so the failing session is cleanly un-reaped.

    A scoped reap (#2716) also passes ``collapse_client=scope.client`` so the
    selector's BLOCKED_ON_USER collapse stays inside the scope. The unscoped
    call keeps its exact kwargs; two literal calls (not one ``**kwargs`` call)
    keep both visible to the bounded-lock scan.
    """
    reaped: list[str] = []
    not_found: list[str] = []
    for index, (session_id, wedge_class) in enumerate(findings):
        try:
            if scope is None:
                found = _reap_session_by_selector(
                    session_id, proposed_action=wedge_class, bounded=True
                )
            else:
                found = _reap_session_by_selector(
                    session_id,
                    proposed_action=wedge_class,
                    bounded=True,
                    collapse_client=scope.client,
                )
        except SessionsLockTimeoutError as exc:
            return reaped, not_found, [sid for sid, _ in findings[index:]], exc
        reaped.append(session_id)
        if not found:
            not_found.append(session_id)
    return reaped, not_found, [], None


def _reap_timeout_check(
    timeout: SessionsLockTimeoutError,
    *,
    reaped: list[str],
    not_found: list[str],
    remaining: list[str],
    routed_status: str | None,
    swept: bool,
    queue_saved: bool,
) -> CheckResult:
    """Describe a bounded-lock timeout during ``--reap`` as a failing check (#2504).

    ``detail`` is ``"; "``-joined segments: the queue-revert note (only when
    the queue pass saved), ``reaped:`` / ``not found:`` / ``NOT reaped:`` id
    lists (each only when non-empty), the routed-result close status (only when
    a close was due), the leaked-worker sweep status, the timeout's
    operator-facing text verbatim, and the re-run hint. Mirrors
    :func:`cw.doctor.linkage._check_reconcile`.
    """
    segments: list[str] = []
    if queue_saved:
        segments.append("queue revert saved")
    if reaped:
        segments.append(f"reaped: {', '.join(reaped)}")
    if not_found:
        segments.append(f"not found: {', '.join(not_found)}")
    if remaining:
        segments.append(f"NOT reaped: {', '.join(remaining)}")
    if routed_status is not None:
        segments.append(f"routed-result close: {routed_status}")
    segments.append(f"leaked-worker sweep: {'ran' if swept else 'n/a'}")
    segments.append(str(timeout))
    segments.append("re-run `cw doctor --reap` once the holder releases")
    return CheckResult(_REAP_CHECK_NAME, ok=False, detail="; ".join(segments))


def _reap_sessions_and_sweep(
    daemon_reap_findings: list[tuple[str, str]],
    routed_result_findings: list[WedgeFinding],
    *,
    run_routed: bool,
    has_leaked: bool,
    queue_saved: bool,
    scope: ReapScope | None = None,
) -> CheckResult | None:
    """Run the post-queue reap tail; return a failing check on lock timeout (#2504).

    Order: class-6/8 session reaps, the class-11 close, then the class-10
    sweep. The reaps and the close take a bounded ``sessions_lock``; the sweep
    is lock-free, so it runs even after a timeout. A timeout during the reaps
    skips the close (it would wait out the same bound again). Returns ``None``
    when nothing timed out. *scope* is forwarded to the session reaps (#2716).
    """
    reaped, not_found, remaining, timeout = _reap_daemon_sessions(
        daemon_reap_findings, scope=scope
    )
    routed_status: str | None = None
    if timeout is not None:
        routed_status = "not attempted" if run_routed else None
    elif run_routed:
        # Class-11 (#2524): session-only close; takes its own bounded lock.
        try:
            reap_routed_result_findings(routed_result_findings)
        except SessionsLockTimeoutError as exc:
            timeout = exc
            routed_status = "timed out"
    # Class-10 (#2480): re-detect fresh (state may have changed since the
    # findings were collected) and stop every leaked worker still leaked,
    # via the shared reconcile authority so the audit-event payload matches
    # the unconditional reconcile-pass sweep exactly.
    if has_leaked:
        sweep_leaked_daemon_workers(load_state(), daemon=get_native_daemon_client())
    if timeout is None:
        return None
    return _reap_timeout_check(
        timeout,
        reaped=reaped,
        not_found=not_found,
        remaining=remaining,
        routed_status=routed_status,
        swept=has_leaked,
        queue_saved=queue_saved,
    )


def _reap_wedge_findings(
    findings: list[WedgeFinding],
    *,
    routed_result_session_ids: set[str] | None = None,
    reap_routed_result: bool = True,
    scope: ReapScope | None = None,
) -> CheckResult | None:
    """Apply mutations for actionable wedge classes.

    Returns ``None`` on success. When a bounded ``sessions_lock`` times out
    during the session reaps or the class-11 close (#2504), the timeout is
    caught and returned as a failing ``wedge-reap`` :class:`CheckResult`
    naming what was and was not reaped; the already-saved queue revert is not
    rolled back and the lock-free class-10 sweep still runs.

    *scope* (#2716, ``cw doctor --reap --ticket <T> --client <C>``) limits
    every mutation to one ``(client, ticket)``: findings for other tickets are
    dropped up front (a coarse ticket-id pre-filter), the RUNNING revert and
    the class-5/7 helpers match only the scoped client's rows, a class-6/8
    session is reaped only when its session record (read here, never the
    finding, which carries no client) is in scope, and the class-10 sweep,
    the class-11 close and the audit-outbox gate are skipped outright. A
    skipped session (unreadable record or another client's) is reported as
    the warn-level ``scoped-reap/session-skipped`` check, or appended to a
    failing ``wedge-reap`` timeout check. Without *scope* the reap matches by
    ticket id across clients, as it always has; client-precise unscoped
    reaping is #2928. Maintenance rule: the scope gates here are
    conditionals, so any NEW mutation added to this function must be
    scope-gated too, or it runs in scoped mode; the reap-level fencing tests
    (``tests/test_doctor_scoped_reap.py``) pin the current gates.

    Class-2 (task-running-no-session): revert queue task to PENDING.
    Class-3 (task-running-completed-session): revert queue task to PENDING.
    Class-4 (repo-ahead-of-queue): advisory only — no mutations.
    Class-5 (blocked-on-user-dead-session): revert oldest to PENDING, cancel
        duplicates via _collapse_blocked_on_user_tasks — skipped entirely if
        the oldest task already has pr_url set.
    Class-6 (active-no-daemon-entry): call _reap_session_by_selector per
        phantom session; that helper marks COMPLETED, reverts queue task to
        PENDING, stops the daemon surface, and emits an audit event. The
        wedge_class is threaded through as proposed_action so the audit
        event can tell this near-certain-crash class apart from class-8.
    Class-7 (terminal-sibling-park, #2100): CANCEL every matching row via
        _cancel_terminal_sibling_parks — never a PENDING revert (see that
        function's docstring for why).
    Class-8 (active-daemon-stale-no-sentinel, #2078): same remedy as class-6
        — call _reap_session_by_selector per matching session; reused
        verbatim rather than a new completion-writer since the end state
        (COMPLETED, daemon stopped, owning task reverted to PENDING) is
        identical. Also passes its own wedge_class as proposed_action, since
        this class is inferred from a heuristic (unlike class-6's harder
        roster-absent signal) and a post-hoc investigator needs to tell them
        apart.
    Class-9 (active-null-liveness-orphan, #2237): advisory only — no
        mutations. Absent from daemon_reap_findings AND listed in the
        running_ticket_ids exclusion set (that set is default-inclusive, so
        leaving it out would still revert the ticket's RUNNING task to
        PENDING). ADR-0014: its eligibility is an elapsed-time cutoff with no
        roster/PID/terminal-result evidence; the recipe names
        ``cw spawn close <id>`` for the orchestrator. For an aider or
        opencode session the recipe first says to stop the worker process,
        since ``cw spawn close`` alone does not stop it (#2663).
    Class-10 (leaked-daemon-worker, #2480): stop every worker
        :func:`_check_wedge_leaked_daemon_worker` found via
        ``cw.reconcile.leaked_workers.sweep_leaked_daemon_workers`` (the same
        detect+stop+audit authority the unconditional reconcile-pass sweep
        uses) — re-reads state fresh rather than trusting the findings'
        snapshot, mirroring class-6/8's per-session re-selection. Listed in
        the running_ticket_ids exclusion set: its remedy is a daemon stop, not
        a queue-task revert (a RUNNING task whose session already completed is
        class-3's job, not this class's).
    Class-11 (active-routed-result-stranded, #2524): explicit-command close
        (the orchestrator session counts, ADR-0020) via
        ``cw.doctor.routed_result_wedge.reap_routed_result_findings``, which
        re-detects fresh, flips only the session, stops its worker and owns
        its own bounded ``sessions_lock``. It never reverts a row (the row
        already advanced past this session), so it is listed in the
        running_ticket_ids exclusion set -- left out, the default-inclusive
        set would revert a RUNNING row of the same ticket claimed by a new
        session.
    Class-12 (pending-worktree-occupied, #2516): advisory only. The finding
        names a PENDING row held by an occupied worktree; ``--reap`` changes
        nothing for it and writes no audit row. It is not part of the queue
        mutation path.
    Class-13 (operator-gated-park, #2739): advisory only. The finding names a
        BLOCKED_ON_USER row parked on a deliberate operator gate that holds a
        lane slot; ``--reap`` leaves the queue row unchanged and writes no
        audit row for it. (A session bound to the row is a separate surface,
        reaped only under class 6/8 on its own evidence.) It is listed in the
        running_ticket_ids exclusion set because that set is
        default-inclusive: left out, a RUNNING row of the same ticket id
        would be reverted to PENDING. It feeds no other selector
        (``blocked_ticket_ids``, ``terminal_sibling_ticket_ids``,
        ``daemon_reap_findings``, the leaked-worker sweep and the routed-result
        close each key off their own class). The collapse filters the row out
        again by ``_is_operator_gated_park``; the sibling cancel selects only
        the terminal_sibling shape (``_is_terminal_sibling_park``), which
        holds no lane slot and so is never a class-13 row (the two are
        disjoint by ``occupies_lane_slot``).

    The former class-1 (pane-idle-but-active) wedge was removed with the
    multiplexer substrate — under the native daemon there are no panes to
    inspect for an idle shell (see #504).
    """
    if scope is not None:
        findings = [f for f in findings if f.ticket_id == scope.ticket_id]
    running_ticket_ids: set[str] = {
        f.ticket_id
        for f in findings
        if f.ticket_id
        and f.wedge_class != WEDGE_PENDING_WORKTREE_OCCUPIED
        and f.wedge_class
        not in {
            "wedge/repo-ahead-of-queue",
            _WEDGE_BLOCKED_DEAD_SESSION,
            _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
            _WEDGE_TERMINAL_SIBLING,
            _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
            _WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN,
            _WEDGE_LEAKED_DAEMON_WORKER,
            _WEDGE_OPERATOR_GATED_PARK,
            WEDGE_ROUTED_RESULT_STRANDED,
        }
    }
    pending_worktree_occupied_keys: set[tuple[str, str]] = {
        (f.client, f.ticket_id)
        for f in findings
        if isinstance(f, PendingWorktreeOccupiedFinding) and f.client and f.ticket_id
    }
    pending_worktree_occupied_ticket_ids: set[str] = {
        f.ticket_id
        for f in findings
        if f.wedge_class == WEDGE_PENDING_WORKTREE_OCCUPIED
        and f.ticket_id
        and not getattr(f, "client", None)
    }
    blocked_ticket_ids: set[str] = {
        f.ticket_id
        for f in findings
        if f.ticket_id and f.wedge_class == _WEDGE_BLOCKED_DEAD_SESSION
    }
    terminal_sibling_ticket_ids: set[str] = {
        f.ticket_id
        for f in findings
        if f.ticket_id and f.wedge_class == _WEDGE_TERMINAL_SIBLING
    }
    # (session_id, wedge_class) pairs rather than a bare id list (#2078 fix
    # cycle): the audit trail needs to tell "daemon entry actually absent"
    # (_WEDGE_ACTIVE_NO_DAEMON_ENTRY, near-certain crash) apart from
    # "inferred stale from the liveness heuristic"
    # (_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL, can misfire) after the fact.
    daemon_reap_findings: list[tuple[str, str]] = [
        (f.session_id, f.wedge_class)
        for f in findings
        if f.session_id
        and f.wedge_class
        in {_WEDGE_ACTIVE_NO_DAEMON_ENTRY, _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL}
    ]
    # Scoped (#2716): drop sessions whose record is out of scope BEFORE the
    # queue lock, so a state-read error cannot leave a partial scoped reap.
    skipped_sessions: list[str] = []
    if scope is not None:
        daemon_reap_findings, skipped_sessions = _scoped_daemon_findings(
            daemon_reap_findings, scope
        )
    # The class-10 sweep, class-11 close and audit outbox are global: off
    # when scoped (#2716).
    routed_enabled = scope is None
    has_leaked_worker_findings = routed_enabled and any(
        f.wedge_class == _WEDGE_LEAKED_DAEMON_WORKER for f in findings
    )
    pending_routed_result_audits = routed_enabled and has_pending_routed_result_audits()
    routed_result_findings = (
        [
            f
            for f in findings
            if f.session_id
            and f.wedge_class == WEDGE_ROUTED_RESULT_STRANDED
            and (
                routed_result_session_ids is None
                or f.session_id in routed_result_session_ids
            )
        ]
        if reap_routed_result and routed_enabled
        else []
    )

    if not (
        running_ticket_ids
        or blocked_ticket_ids
        or terminal_sibling_ticket_ids
        or daemon_reap_findings
        or has_leaked_worker_findings
        or routed_result_findings
        or pending_routed_result_audits
    ):
        return _note_skipped_sessions(None, skipped_sessions)

    scoped_client = scope.client if scope is not None else None
    with dev_queue_lock():
        queue = _deps.load_dev_queue()
        changed = False
        for task in queue.tasks:
            if (
                task.ticket_id in running_ticket_ids
                and (task.client, task.ticket_id) not in pending_worktree_occupied_keys
                and task.ticket_id not in pending_worktree_occupied_ticket_ids
                and task.status == QueueItemStatus.RUNNING
                and (scope is None or scope.covers(task.client, task.ticket_id))
            ):
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                changed = True
        if blocked_ticket_ids:
            blocked_changed = _collapse_blocked_on_user_tasks(
                queue, blocked_ticket_ids, client=scoped_client
            )
            changed = changed or blocked_changed
        if terminal_sibling_ticket_ids:
            sibling_changed = _cancel_terminal_sibling_parks(
                queue, terminal_sibling_ticket_ids, client=scoped_client
            )
            changed = changed or sibling_changed
        queue_saved = changed
        if queue_saved:
            save_dev_queue(queue)

    # Reap phantom/stale sessions outside the queue lock —
    # _reap_session_by_selector acquires sessions_lock and dev_queue_lock
    # internally (sequential, no deadlock risk since we already released
    # dev_queue_lock above).
    # Why (#2491, #2504): operator `cw doctor --reap`, so the reaps are
    # bounded. Partial-state window: the queue changes above are already
    # saved, so a SessionsLockTimeoutError in the tail below is caught and
    # reported (not raised) as a failing `wedge-reap` check listing what was
    # and was not reaped. The findings are re-detected idempotently on the
    # next `cw doctor --reap`.
    return _note_skipped_sessions(
        _reap_sessions_and_sweep(
            daemon_reap_findings,
            routed_result_findings,
            run_routed=bool(routed_result_findings or pending_routed_result_audits),
            has_leaked=has_leaked_worker_findings,
            queue_saved=queue_saved,
            scope=scope,
        ),
        skipped_sessions,
    )
