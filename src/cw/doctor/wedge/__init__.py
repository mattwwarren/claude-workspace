"""Wedge detection and reap for ``cw doctor``.

Split out of ``cw.doctor.core`` (#1314, part 2). Holds the wedge-condition
detectors (RUNNING tasks with no/completed/dead session, repo-ahead-of-queue,
BLOCKED_ON_USER dead-session, ACTIVE-no-daemon-entry, ACTIVE-daemon-stale,
ACTIVE-null-liveness-orphan) plus the reap that acts
on actionable findings (:func:`_reap_wedge_findings`) and the
BLOCKED_ON_USER collapse helper (:func:`_collapse_blocked_on_user_tasks`).
The class-11 stranded-routed-result detector and its operator close live in
``cw.doctor.routed_result_wedge`` (#2524); this module only wires its close
into the reap tail.

``_reap_wedge_findings`` mutates state directly (``save_dev_queue``) outside
``mutate_state()`` by design — kept colocated here rather than extracted into a
shared mutation helper (#1314 constraint).

This module imports :func:`_gh_pr_states` and :func:`_reap_session_by_selector`
from ``loop_health`` at top level (2-symbol direction); ``loop_health``'s reach
back for :func:`_collapse_blocked_on_user_tasks` is a function-local deferred
import to break the cycle.

This package was split out of a single ``doctor/wedge.py`` module (#2164);
every ``from cw.doctor.wedge import X`` site is preserved here via
re-exports. Submodules, in dependency order:

- ``_constants`` -- the wedge-class and park-disposition constants, and the
  pinned :data:`_LOGGER_NAME`. Imports from no sibling.
- ``task_running`` -- the RUNNING-task detectors (classes 2, 3 and 4) and
  :func:`_resolve_wedge_branch`; the only reader of ``run_git``. Imports no
  sibling.
- ``blocked_on_user`` -- the BLOCKED_ON_USER detectors (classes 5 and 7),
  their predicates, and the collapse/cancel queue mutators; the only
  submodule that logs. Imports ``_constants``.
- ``session_liveness`` -- the ACTIVE-session liveness detectors (classes 6
  and 8) and the :func:`_daemon_supervisor_alive` outage guard; the only
  reader of ``_ROSTER_PATH`` and ``load_orchestrator_config``. Imports
  ``_constants``.

A test that monkeypatches a module global the code reads
(``get_native_daemon_client``, ``run_git``, ``_ROSTER_PATH``, ...) must
target the submodule that owns the reading function: this package re-exports
only its own names, so a patch on its namespace raises ``AttributeError``
instead of silently not intercepting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw.config import load_state, state_file
from cw.dev_queue import dev_queue_lock, save_dev_queue, transition_task_status
from cw.dispatch.claim import _find_running_row
from cw.doctor import _deps
from cw.doctor._shared import CheckResult, WedgeFinding
from cw.doctor.loop_health import _reap_session_by_selector
from cw.doctor.routed_result_wedge import (
    WEDGE_ROUTED_RESULT_STRANDED,
    has_pending_routed_result_audits,
    reap_routed_result_findings,
)
from cw.doctor.wedge._constants import (
    _DIRTY_WORKTREE_DISPOSITION,
    _HUMAN_GATED_PARK_DISPOSITIONS,
    _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
    _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
    _WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN,
    _WEDGE_BLOCKED_DEAD_SESSION,
    _WEDGE_LEAKED_DAEMON_WORKER,
    _WEDGE_TERMINAL_SIBLING,
)
from cw.doctor.wedge._constants import (
    _LOGGER_NAME as _LOGGER_NAME,
)
from cw.doctor.wedge.blocked_on_user import (
    _cancel_terminal_sibling_parks,
    _check_wedge_dead_session_blocked_on_user,
    _check_wedge_terminal_sibling_park,
    _collapse_blocked_on_user_tasks,
    _is_dead_session_task,
    _is_terminal_sibling_disposition,
    _is_terminal_sibling_park,
    _log,
)
from cw.doctor.wedge.session_liveness import (
    _check_wedge_active_daemon_stale_no_sentinel,
    _check_wedge_active_no_daemon_entry,
    _daemon_supervisor_alive,
)
from cw.doctor.wedge.task_running import (
    _check_wedge_repo_ahead,
    _check_wedge_task_running_completed_session,
    _check_wedge_task_running_no_session,
    _resolve_wedge_branch,
)
from cw.exceptions import CwError, SessionsLockTimeoutError
from cw.executor import resolve_executor_config
from cw.models import (
    CODEX_BACKEND,
    QueueItemStatus,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import (
    SPAWN_GRACE_SECONDS,
    ticket_id_for_session,
)
from cw.reconcile.leaked_workers import (
    find_leaked_daemon_workers,
    sweep_leaked_daemon_workers,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, DevQueueStore, Session

__all__ = [
    "_DIRTY_WORKTREE_DISPOSITION",
    "_HUMAN_GATED_PARK_DISPOSITIONS",
    "_REAP_CHECK_NAME",
    "_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL",
    "_WEDGE_ACTIVE_NO_DAEMON_ENTRY",
    "_WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN",
    "_WEDGE_BLOCKED_DEAD_SESSION",
    "_WEDGE_LEAKED_DAEMON_WORKER",
    "_WEDGE_TERMINAL_SIBLING",
    "_cancel_terminal_sibling_parks",
    "_check_wedge_active_daemon_stale_no_sentinel",
    "_check_wedge_active_no_daemon_entry",
    "_check_wedge_active_null_liveness_orphan",
    "_check_wedge_dead_session_blocked_on_user",
    "_check_wedge_leaked_daemon_worker",
    "_check_wedge_repo_ahead",
    "_check_wedge_task_running_completed_session",
    "_check_wedge_task_running_no_session",
    "_check_wedge_terminal_sibling_park",
    "_collapse_blocked_on_user_tasks",
    "_daemon_supervisor_alive",
    "_is_dead_session_task",
    "_is_null_liveness_candidate",
    "_is_terminal_sibling_disposition",
    "_is_terminal_sibling_park",
    "_log",
    "_null_liveness_orphan_recipe",
    "_reap_daemon_sessions",
    "_reap_sessions_and_sweep",
    "_reap_timeout_check",
    "_reap_wedge_findings",
    "_resolve_backend_for_orphan_check",
    "_resolve_wedge_branch",
]


def _resolve_backend_for_orphan_check(
    session: Session,
    ticket_id: str | None,
    queue: DevQueueStore,
    clients: dict[str, ClientConfig],
) -> tuple[str | None, str | None]:
    """Resolve *session*'s executor backend: ``(backend, None)`` or ``(None, why)``.

    Threads ``(ticket_id, client) -> RUNNING task -> backend`` the way
    ``cw.reconcile.codex_boot`` does, including its identity check: the RUNNING
    row must carry ``session_id == session.id`` (via
    :func:`~cw.dispatch.claim._find_running_row`), so a lingering zombie can
    never borrow the backend of a row since re-dispatched onto a fresh session.
    Every miss is a *resolution failure* (the "backend unresolved" advisory
    variant), never a silent skip. A ``None`` *ticket_id* short-circuits first,
    before any client or queue lookup -- mirroring class-8's
    ``task_by_ticket.get(ticket_id) if ticket_id else None`` convention.
    """
    if ticket_id is None:
        return None, "session name does not encode a ticket id"
    client = clients.get(session.client)
    if client is None:
        return None, f"no clients.yaml entry for client {session.client!r}"
    if not any(
        t.ticket_id == ticket_id
        and t.client == session.client
        and t.status == QueueItemStatus.RUNNING
        for t in queue.tasks
    ):
        return None, f"no RUNNING task for ticket {ticket_id!r}"
    task = _find_running_row(queue, ticket_id, session.client, session_id=session.id)
    if task is None:
        return None, "RUNNING task belongs to a different session"
    return resolve_executor_config(task.stage, task, client).backend, None


def _is_null_liveness_candidate(session: Session, cutoff: datetime) -> bool:
    """True iff *session* is a live DAEMON row with neither liveness channel.

    ``local_liveness is None`` keeps this disjoint from
    ``cw.reconcile.local``'s harvest, which owns every null-surface_ref row
    that DOES carry a local-process handle. ORCHESTRATE is excluded as in
    ``compute_drift``. *cutoff* only debounces reporting (ADR-0014 allows a
    threshold to delay a signal); a row younger than it yields no finding.
    """
    return (
        session.origin is SessionOrigin.DAEMON
        and session.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
        and session.purpose is not SessionPurpose.ORCHESTRATE
        and session.surface_ref is None
        and session.local_liveness is None
        and session.started_at <= cutoff
    )


def _null_liveness_orphan_recipe(
    session_id: str, backend: str | None, reason: str | None
) -> str:
    """Recipe text for a class-9 finding; names the session id, never its name."""
    if backend is not None:
        return (
            f"ACTIVE session {session_id} has no daemon surface and no liveness "
            "record past its spawn grace — it holds a ceiling slot. "
            f"Run: cw spawn close {session_id}"
        )
    return (
        f"ACTIVE session {session_id} has no daemon surface; could not resolve "
        f"its executor backend ({reason}). "
        f"Run: cw spawn close {session_id} if it is not running."
    )


def _check_wedge_active_null_liveness_orphan(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect DAEMON ACTIVE/IDLE sessions invisible to every reaper (#2237).

    A row with no ``surface_ref`` AND no ``local_liveness`` past
    ``SPAWN_GRACE_SECONDS`` is skipped by ``compute_drift`` (so class-6 and
    the reconcile phantom sweep never see it) and by class-8 (which needs a
    roster-present ref), while ``dispatch/tick.py``'s ``running_count`` still
    counts it against the client ceiling.

    Advisory only (ADR-0014): eligibility is absence plus elapsed time, with
    no roster, dead-PID, or terminal-result evidence, so
    :func:`_reap_wedge_findings` never mutates on it. The recipe names the
    per-session operator command, ``cw spawn close <id>``.

    ``CODEX_BACKEND`` sessions are excluded silently: CodexExecutor never sets
    either liveness channel by design (``executor.py``, #1727) and its orphans
    are owned by ``cw.reconcile.codex_boot`` / ``codex_reparks``
    (#2285/#2307). A backend that cannot be resolved at all still yields a
    finding, using the "backend unresolved" recipe variant, so a drifted queue
    row or a removed client entry cannot make the session invisible again.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=SPAWN_GRACE_SECONDS)
    candidates = [s for s in state.sessions if _is_null_liveness_candidate(s, cutoff)]
    if not candidates:
        return []
    # A broken clients.yaml must not crash the doctor run; degrade to no
    # clients, which drives every candidate through the "no clients.yaml
    # entry" advisory variant (mirrors _check_wedge_repo_ahead's guard).
    try:
        clients = _deps.load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        clients = {}

    findings: list[WedgeFinding] = []
    for session in candidates:
        ticket_id = ticket_id_for_session(session.name)
        backend, reason = _resolve_backend_for_orphan_check(
            session, ticket_id, queue, clients
        )
        if backend == CODEX_BACKEND:
            continue
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN,
                session_id=session.id,
                ticket_id=ticket_id,
                recipe=_null_liveness_orphan_recipe(session.id, backend, reason),
                state_file=str(state_file()),
            )
        )
    return findings


def _check_wedge_leaked_daemon_worker(state: CwState) -> list[WedgeFinding]:
    """Detect daemon roster workers whose owning cw session is gone (#2480).

    Surfaces every :class:`~cw.reconcile.leaked_workers.LeakedWorker`
    (:func:`~cw.reconcile.leaked_workers.find_leaked_daemon_workers`) as a
    wedge finding, even on a plain ``cw doctor`` run with no ``--reap`` --
    an operator asking "why won't this ticket re-dispatch" can see the leak
    named here before deciding to clear it. ``daemon_short_id`` carries the
    roster id the ``--reap`` remedy (:func:`_reap_wedge_findings`) needs,
    since a leaked worker with no matching cw session has no ``session_id``
    to key off.

    Returns no findings when the roster cannot be read
    (:func:`~cw.reconcile.leaked_workers.find_leaked_daemon_workers` returns
    ``None``) -- there is nothing to safely report or act on this run.
    """
    leaked = find_leaked_daemon_workers(state, daemon=get_native_daemon_client())
    if not leaked:
        return []
    findings: list[WedgeFinding] = []
    for worker in leaked:
        session = worker.session
        if session is not None:
            owner = f"owning session {session.id} is {session.status.value}"
        else:
            owner = "no matching cw session"
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_LEAKED_DAEMON_WORKER,
                session_id=session.id if session is not None else None,
                ticket_id=(
                    ticket_id_for_session(session.name) if session is not None else None
                ),
                recipe=(
                    f"Daemon worker {worker.short_id} at {worker.cwd} is leaked"
                    f" ({owner}). Run: cw doctor --reap to stop it."
                ),
                state_file=str(state_file()),
                daemon_short_id=worker.short_id,
            )
        )
    return findings


_REAP_CHECK_NAME = "wedge-reap"


def _reap_daemon_sessions(
    findings: list[tuple[str, str]],
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
    """
    reaped: list[str] = []
    not_found: list[str] = []
    for index, (session_id, wedge_class) in enumerate(findings):
        try:
            found = _reap_session_by_selector(
                session_id, proposed_action=wedge_class, bounded=True
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
) -> CheckResult | None:
    """Run the post-queue reap tail; return a failing check on lock timeout (#2504).

    Order: class-6/8 session reaps, the class-11 close, then the class-10
    sweep. The reaps and the close take a bounded ``sessions_lock``; the sweep
    is lock-free, so it runs even after a timeout. A timeout during the reaps
    skips the close (it would wait out the same bound again). Returns ``None``
    when nothing timed out.
    """
    reaped, not_found, remaining, timeout = _reap_daemon_sessions(daemon_reap_findings)
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
) -> CheckResult | None:
    """Apply mutations for actionable wedge classes.

    Returns ``None`` on success. When a bounded ``sessions_lock`` times out
    during the session reaps or the class-11 close (#2504), the timeout is
    caught and returned as a failing ``wedge-reap`` :class:`CheckResult`
    naming what was and was not reaped; the already-saved queue revert is not
    rolled back and the lock-free class-10 sweep still runs.

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
        ``cw spawn close <id>`` for the operator.
    Class-10 (leaked-daemon-worker, #2480): stop every worker
        :func:`_check_wedge_leaked_daemon_worker` found via
        ``cw.reconcile.leaked_workers.sweep_leaked_daemon_workers`` (the same
        detect+stop+audit authority the unconditional reconcile-pass sweep
        uses) — re-reads state fresh rather than trusting the findings'
        snapshot, mirroring class-6/8's per-session re-selection. Listed in
        the running_ticket_ids exclusion set: its remedy is a daemon stop, not
        a queue-task revert (a RUNNING task whose session already completed is
        class-3's job, not this class's).
    Class-11 (active-routed-result-stranded, #2524): operator-only close via
        ``cw.doctor.routed_result_wedge.reap_routed_result_findings``, which
        re-detects fresh, flips only the session, stops its worker and owns
        its own bounded ``sessions_lock``. It never reverts a row (the row
        already advanced past this session), so it is listed in the
        running_ticket_ids exclusion set -- left out, the default-inclusive
        set would revert a RUNNING row of the same ticket claimed by a new
        session.

    The former class-1 (pane-idle-but-active) wedge was removed with the
    multiplexer substrate — under the native daemon there are no panes to
    inspect for an idle shell (see #504).
    """
    running_ticket_ids: set[str] = {
        f.ticket_id
        for f in findings
        if f.ticket_id
        and f.wedge_class
        not in {
            "wedge/repo-ahead-of-queue",
            _WEDGE_BLOCKED_DEAD_SESSION,
            _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
            _WEDGE_TERMINAL_SIBLING,
            _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
            _WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN,
            _WEDGE_LEAKED_DAEMON_WORKER,
            WEDGE_ROUTED_RESULT_STRANDED,
        }
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
    has_leaked_worker_findings = any(
        f.wedge_class == _WEDGE_LEAKED_DAEMON_WORKER for f in findings
    )
    pending_routed_result_audits = has_pending_routed_result_audits()
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
        if reap_routed_result
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
        return None

    with dev_queue_lock():
        queue = _deps.load_dev_queue()
        changed = False
        for task in queue.tasks:
            if (
                task.ticket_id in running_ticket_ids
                and task.status == QueueItemStatus.RUNNING
            ):
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                changed = True
        if blocked_ticket_ids:
            blocked_changed = _collapse_blocked_on_user_tasks(queue, blocked_ticket_ids)
            changed = changed or blocked_changed
        if terminal_sibling_ticket_ids:
            sibling_changed = _cancel_terminal_sibling_parks(
                queue, terminal_sibling_ticket_ids
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
    return _reap_sessions_and_sweep(
        daemon_reap_findings,
        routed_result_findings,
        run_routed=bool(routed_result_findings or pending_routed_result_audits),
        has_leaked=has_leaked_worker_findings,
        queue_saved=queue_saved,
    )
