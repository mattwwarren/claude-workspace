"""Wedge detection and reap for ``cw doctor``.

Split out of ``cw.doctor.core`` (#1314, part 2). Holds the wedge-condition
detectors (RUNNING tasks with no/completed/dead session, repo-ahead-of-queue,
BLOCKED_ON_USER dead-session, ACTIVE-no-daemon-entry) plus the reap that acts
on actionable findings (:func:`_reap_wedge_findings`) and the
BLOCKED_ON_USER collapse helper (:func:`_collapse_blocked_on_user_tasks`).

``_reap_wedge_findings`` mutates state directly (``save_dev_queue``) outside
``mutate_state()`` by design — kept colocated here rather than extracted into a
shared mutation helper (#1314 constraint).

This module imports :func:`_gh_pr_states` and :func:`_reap_session_by_selector`
from ``loop_health`` at top level (2-symbol direction); ``loop_health``'s reach
back for :func:`_collapse_blocked_on_user_tasks` is a function-local deferred
import to break the cycle.
"""

from __future__ import annotations

import json
import logging
import subprocess as _sp
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw.auto_dev_result import PAUSED_FOR_USER_INPUT_STATUSES
from cw.config import state_file
from cw.dev_queue import dev_queue_lock, save_dev_queue, transition_task_status
from cw.doctor import _deps
from cw.doctor._shared import WedgeFinding
from cw.doctor.loop_health import _gh_pr_states, _reap_session_by_selector
from cw.exceptions import CwError
from cw.models import QueueItemStatus, ReapReason, SessionStatus
from cw.native_daemon import _ROSTER_PATH, get_native_daemon_client
from cw.reconcile import (
    SPAWN_GRACE_SECONDS,
    compute_drift,
    feature_branch_key,
    ticket_id_for_session,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, DevQueueStore, Session, TicketTask


# Wedge class for BLOCKED_ON_USER tasks whose sessions are dead (OOM/crash path).
_WEDGE_BLOCKED_DEAD_SESSION = "wedge/blocked-on-user-dead-session"

# Wedge class for BLOCKED_ON_USER tasks parked terminal_sibling (#2100): a
# duplicate row minted by a lock-contention race for a ticket whose real row
# already reached a terminal status (see
# ``cw.reconcile.tasks.park_terminal_sibling_tasks``). Deliberately distinct
# from ``_WEDGE_BLOCKED_DEAD_SESSION`` above: that class's remedy (revert the
# oldest blocked row to PENDING) has nothing to revert THIS row to — the
# ticket's real row already finished — so reverting it just gets it re-parked
# terminal_sibling on the very next reconcile pass, a silent ping-pong. Both
# the class-5 detector and ``_collapse_blocked_on_user_tasks`` exclude this
# disposition outright (see ``_is_terminal_sibling_park``) so a row can only
# ever surface here, with CANCEL — never a PENDING revert — as its --reap
# remedy (``_cancel_terminal_sibling_parks``).
_WEDGE_TERMINAL_SIBLING = "wedge/terminal-sibling-park"

# Dispositions marking a park that waits on a HUMAN, not on a wedge (#1653):
# the four sentinel statuses stamped verbatim onto the task at park time.
# A human-gated park's worker has legitimately exited, so the dead-session
# heuristic matches every one of them — but reverting one to PENDING
# mechanically re-dispatches a ticket with zero new information and produces
# the identical park (observed: 10 retries at a fixed cadence, ~20.5h, ending
# in manual queue removal). These parks are released by an operator verb
# (requeue/approve) or a gate recipe reading fresh tracker state, never by
# the reap path. Sourced from the schema constant so the sets cannot drift.
_HUMAN_GATED_PARK_DISPOSITIONS: frozenset[str] = PAUSED_FOR_USER_INPUT_STATUSES

# Wedge class for ACTIVE/IDLE sessions with no matching daemon entry (crash/SSH
# failure path that leaves roster absent but session still "active" in cw state).
_WEDGE_ACTIVE_NO_DAEMON_ENTRY = "wedge/active-no-daemon-entry"

_log = logging.getLogger(__name__)


def _check_wedge_task_running_no_session(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING queue tasks with no associated live session.

    Skips tasks within SPAWN_GRACE_SECONDS of creation — newly spawned
    tasks have not yet registered their session_id.
    """
    findings: list[WedgeFinding] = []
    _live = {SessionStatus.ACTIVE, SessionStatus.IDLE}
    live_session_ids = {s.id for s in state.sessions if s.status in _live}
    now = datetime.now(UTC)
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.session_id is None:
            created = task.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (now - created).total_seconds() < SPAWN_GRACE_SECONDS:
                continue
            findings.append(
                WedgeFinding(
                    wedge_class="wedge/task-running-no-session",
                    session_id=None,
                    ticket_id=task.ticket_id,
                    recipe=(
                        "Queue task RUNNING with no matching session. "
                        "Run: cw doctor --reap to revert task to PENDING."
                    ),
                    state_file=str(state_file()),
                )
            )
        elif task.session_id not in live_session_ids:
            # session_id set but points to a non-live session (missing from
            # state, TIMED_OUT, etc.).
            # _check_wedge_task_running_completed_session handles COMPLETED.
            # BACKGROUNDED is excluded: the session is intentionally paused
            # and will resume — flagging it as a wedge is a false positive.
            session_by_id_local = {s.id: s for s in state.sessions}
            sess = session_by_id_local.get(task.session_id)
            _non_wedge = {SessionStatus.COMPLETED, SessionStatus.BACKGROUNDED}
            if sess is None or sess.status not in _non_wedge:
                findings.append(
                    WedgeFinding(
                        wedge_class="wedge/task-running-no-session",
                        session_id=task.session_id,
                        ticket_id=task.ticket_id,
                        recipe=(
                            "Queue task RUNNING with no matching session. "
                            "Run: cw doctor --reap to revert task to PENDING."
                        ),
                        state_file=str(state_file()),
                    )
                )
    return findings


def _check_wedge_task_running_completed_session(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING queue tasks whose session is already COMPLETED."""
    findings: list[WedgeFinding] = []
    session_by_id = {s.id: s for s in state.sessions}
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING or task.session_id is None:
            continue
        session = session_by_id.get(task.session_id)
        if session is None or session.status != SessionStatus.COMPLETED:
            continue
        findings.append(
            WedgeFinding(
                wedge_class="wedge/task-running-completed-session",
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=(
                    "Queue task RUNNING but its session is already COMPLETED. "
                    "Run: cw doctor --reap to revert task to PENDING."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _resolve_wedge_branch(
    task: TicketTask,
    session_by_id: dict[str, Session],
    clients: dict[str, ClientConfig],
) -> str:
    """Branch for a wedge check: the session's branch, else the feature prefix.

    Falls back to ``<feature_branch_prefix>/<ticket>`` (``dev`` when the client
    is unknown), mirroring what the staged pipeline provisions and pushes (#712).
    """
    if task.session_id is not None:
        session = session_by_id.get(task.session_id)
        if session is not None and session.branch:
            return session.branch
    return feature_branch_key(task.client, task.ticket_id, clients)


def _check_wedge_repo_ahead(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING tasks whose branch is pushed to remote but queue not updated.

    Uses ``git ls-remote`` to check if the branch exists on the remote and
    ``gh pr list`` to determine whether a PR is open. Advisory only — no
    automatic reap.
    """
    findings: list[WedgeFinding] = []
    session_by_id = {s.id: s for s in state.sessions}
    # A broken clients.yaml must not crash the doctor run; degrade to no
    # clients and fall back to the default feature-branch prefix below
    # (mirrors the guard around load_clients in run_doctor).
    try:
        clients = _deps.load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        clients = {}
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.worktree_path is None:
            continue
        # Branch resolution: prefer session branch, fallback to the client's
        # configured feature branch (<feature_branch_prefix>/<ticket>, e.g.
        # dev/662 — what the staged pipeline provisions and pushes, #712).
        branch = _resolve_wedge_branch(task, session_by_id, clients)
        # Get remote URL from worktree
        try:
            remote_result = _sp.run(
                ["git", "-C", str(task.worktree_path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=False,
            )
            if remote_result.returncode != 0:
                continue
            remote_url = remote_result.stdout.strip()
        except OSError:
            continue
        # Check if branch exists on remote
        try:
            ls_result = _sp.run(
                ["git", "ls-remote", remote_url, f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if ls_result.returncode != 0 or not ls_result.stdout.strip():
                continue
        except (OSError, _sp.TimeoutExpired):
            continue
        # Check PR status via gh CLI
        recipe: str
        prs, _ = _gh_pr_states(branch)
        if not prs:
            recipe = (
                f"Branch {branch} is ahead of main with no open PR. "
                f"Suggested: cw spawn-complete {task.ticket_id} or open PR manually."
            )
        else:
            pr_state = prs[0].get("state", "OPEN")
            recipe = (
                f"Branch {branch} has {pr_state} PR but queue still RUNNING. "
                f"Suggested: cw spawn-complete {task.ticket_id} --status shipped."
            )
        findings.append(
            WedgeFinding(
                wedge_class="wedge/repo-ahead-of-queue",
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=recipe,
                state_file=str(state_file()),
            )
        )
    return findings


def _is_dead_session_task(
    task: TicketTask,
    session_by_id: dict[str, Session],
    live_short_ids: set[str],
) -> bool:
    """Return True when a BLOCKED_ON_USER task's session is dead.

    Dead = session_id is None (dirty-worktree / gh-blocked phantom paths),
    OR session not in state, OR surface_ref is None or absent from the live
    daemon roster. Covers all three BLOCKED_ON_USER creation paths (see #590).
    """
    if task.session_id is None:
        return True
    session = session_by_id.get(task.session_id)
    if session is None:
        return True
    if session.surface_ref is None:
        return True
    return session.surface_ref not in live_short_ids


def _is_terminal_sibling_disposition(task: TicketTask) -> bool:
    """True iff *task* carries the terminal_sibling park disposition (#2100).

    The BROAD exclusion — disposition alone, regardless of ``attempts`` or
    ``session_id`` — used by both
    :func:`_check_wedge_dead_session_blocked_on_user` and
    :func:`_collapse_blocked_on_user_tasks` to keep either from ever
    reverting a terminal_sibling row to PENDING: ``park_terminal_sibling_tasks``
    re-parks purely on disposition (a terminal sibling existing for the same
    (client, ticket_id)), not on ``attempts``, so ANY terminal_sibling row
    reverted to PENDING gets re-parked on the very next reconcile pass
    regardless of its claim history. Contrast :func:`_is_terminal_sibling_park`
    below — the NARROWER shape (``session_id is None`` + ``attempts == 0``)
    that is additionally safe to auto-CANCEL under ``--reap``.
    """
    return task.disposition == ReapReason.TERMINAL_SIBLING.value


def _check_wedge_dead_session_blocked_on_user(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect BLOCKED_ON_USER tasks whose sessions are dead (OOM/crash path).

    Guards daemon I/O: list_live_session_short_ids() is only called when at
    least one BLOCKED_ON_USER task exists in the queue.

    Human-gated parks (disposition in _HUMAN_GATED_PARK_DISPOSITIONS) are
    excluded outright (#1653): their worker exited by design, so they always
    look "dead" to this heuristic, but they are waiting on an operator, not
    wedged — reverting them re-runs the identical park.

    A terminal_sibling park (#2100) is likewise excluded outright: it too
    always has session_id is None (park_terminal_sibling_tasks clears it) and
    so always looks "dead" to this heuristic, but reverting ANY terminal_sibling
    row to PENDING just gets it re-parked terminal_sibling on the next
    reconcile pass (see _is_terminal_sibling_disposition) — its remedy is
    CANCEL, and only for the narrower reap-eligible shape (see
    _WEDGE_TERMINAL_SIBLING/_check_wedge_terminal_sibling_park).
    """
    candidates = [
        t
        for t in queue.tasks
        if t.status == QueueItemStatus.BLOCKED_ON_USER
        and t.disposition not in _HUMAN_GATED_PARK_DISPOSITIONS
        and not _is_terminal_sibling_disposition(t)
    ]
    if not candidates:
        return []

    session_by_id = {s.id: s for s in state.sessions}
    live_short_ids = get_native_daemon_client().list_live_session_short_ids()

    findings: list[WedgeFinding] = []
    for task in candidates:
        if not _is_dead_session_task(task, session_by_id, live_short_ids):
            continue
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_BLOCKED_DEAD_SESSION,
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=(
                    "BLOCKED_ON_USER task with dead session holds lane slot. "
                    "Run: cw doctor --reap to revert task to PENDING."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _is_terminal_sibling_park(task: TicketTask) -> bool:
    """True iff *task* is a #2100 terminal_sibling duplicate park.

    BLOCKED_ON_USER + disposition == ReapReason.TERMINAL_SIBLING +
    session_id is None + attempts == 0 — exactly the shape
    ``park_terminal_sibling_tasks`` (``cw.reconcile.tasks``) stamps: a row
    ``add_ticket`` minted during a lock-contention race, never claimed
    (attempts == 0, ``ever_spawned=False`` at construction — see
    ``cw.reconcile.review_recipes.auto_fix_ci``), for a ticket whose real row
    already reached COMPLETED or CANCELLED. The ``attempts == 0`` guard
    matters: a row with claim history is not this narrow duplicate shape and
    is left to the existing dead-session wedge class
    (``_check_wedge_dead_session_blocked_on_user``) instead. Shared by the
    class-7 detector and its ``--reap`` remedy so the two can never select a
    different row set.
    """
    return (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.disposition == ReapReason.TERMINAL_SIBLING.value
        and task.session_id is None
        and task.attempts == 0
    )


def _check_wedge_terminal_sibling_park(queue: DevQueueStore) -> list[WedgeFinding]:
    """Detect #2100 terminal_sibling duplicate parks holding a lane slot.

    Distinct from class-5 (``_check_wedge_dead_session_blocked_on_user``,
    which excludes this disposition outright): that class's remedy is to
    revert the row to PENDING, but a terminal_sibling park has nothing to
    revert TO — the ticket's real row already finished — so reverting it
    just gets it re-parked terminal_sibling on the very next reconcile pass
    (a silent ping-pong; see ``_cancel_terminal_sibling_parks`` for the
    correct CANCEL remedy). Needs no daemon/session lookup, unlike class-5:
    ``_is_terminal_sibling_park`` is a pure predicate over the row itself.
    """
    return [
        WedgeFinding(
            wedge_class=_WEDGE_TERMINAL_SIBLING,
            session_id=None,
            ticket_id=task.ticket_id,
            recipe=(
                "BLOCKED_ON_USER task parked terminal_sibling holds a lane"
                " slot for a ticket whose real row already finished. Run:"
                " cw doctor --reap to cancel this duplicate row."
            ),
            state_file=str(state_file()),
        )
        for task in queue.tasks
        if _is_terminal_sibling_park(task)
    ]


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


def _collapse_blocked_on_user_tasks(
    queue: DevQueueStore,
    blocked_ticket_ids: set[str],
) -> bool:
    """Revert oldest BLOCKED_ON_USER task to PENDING; cancel duplicates.

    For each ticket_id in blocked_ticket_ids, sorts BLOCKED_ON_USER tasks by
    created_at (ascending), reverts the first (oldest) to PENDING with
    session_id cleared, and cancels the rest. Skips the whole ticket
    (no mutation) when the oldest task already has ``pr_url`` set — see
    the inline comment at the guard for why.

    Human-gated parks are never touched (#1653): rows whose disposition is in
    _HUMAN_GATED_PARK_DISPOSITIONS are filtered out before any mutation, as
    defense in depth behind the class-5 detector's own exclusion — this
    helper is also reached from loop_health._reap_session_by_selector, whose
    callers select tickets by other criteria.

    A terminal_sibling park (#2100) is excluded the same way, for the same
    defense-in-depth reason — reverting one to PENDING here would just get it
    re-parked terminal_sibling on the next reconcile pass, the exact
    ping-pong #2100 reports; its correct --reap remedy is
    ``_cancel_terminal_sibling_parks`` instead. This is what stops the
    ping-pong even for a caller (loop_health._reap_session_by_selector) that
    never routes through the class-7 detector at all.

    Returns True when any mutation was applied.
    """
    changed = False
    for ticket_id in blocked_ticket_ids:
        all_blocked = [
            t
            for t in queue.tasks
            if t.ticket_id == ticket_id and t.status == QueueItemStatus.BLOCKED_ON_USER
        ]
        tasks_for_ticket = [
            t
            for t in all_blocked
            if t.disposition not in _HUMAN_GATED_PARK_DISPOSITIONS
            and not _is_terminal_sibling_disposition(t)
        ]
        if len(tasks_for_ticket) < len(all_blocked):
            _log.warning(
                "Ticket %s: %d BLOCKED_ON_USER row(s) parked on a human gate "
                "or terminal_sibling (#2100) left untouched by collapse; "
                "release via requeue/approve/a gate recipe, or "
                "cw doctor --reap for a terminal_sibling row.",
                ticket_id,
                len(all_blocked) - len(tasks_for_ticket),
            )
        if not tasks_for_ticket:
            continue
        # Stable sort preserves insertion order for equal created_at values.
        tasks_for_ticket.sort(key=lambda t: t.created_at)
        oldest = tasks_for_ticket[0]
        # Why: reverting a task that already has a pr_url clears it and
        # re-enables dispatch, which re-runs FINALIZE against a branch that
        # may already be merged — producing a duplicate/empty PR (#912).
        if oldest.pr_url:
            _log.warning(
                "Skipping _collapse_blocked_on_user_tasks for ticket %s: "
                "oldest BLOCKED_ON_USER task has pr_url set (%s). "
                "Will not revert to PENDING.",
                ticket_id,
                oldest.pr_url,
            )
            continue
        transition_task_status(oldest, QueueItemStatus.PENDING)
        oldest.session_id = None
        changed = True
        for dup in tasks_for_ticket[1:]:
            transition_task_status(dup, QueueItemStatus.CANCELLED)
            changed = True
    return changed


def _cancel_terminal_sibling_parks(queue: DevQueueStore, ticket_ids: set[str]) -> bool:
    """CANCEL every BLOCKED_ON_USER terminal_sibling park for *ticket_ids* (#2100).

    Deliberately NOT a reuse of ``_collapse_blocked_on_user_tasks``: that
    helper reverts the OLDEST blocked row of a ticket to PENDING because
    there IS a legitimate park to recover — a terminal_sibling park has
    nothing to recover, since the ticket's real row already reached
    COMPLETED/CANCELLED, so reverting it to PENDING would just get it
    re-parked terminal_sibling on the very next reconcile pass (the exact
    ping-pong #2100 reports). Every matching row is CANCELLED outright
    instead. Matches via ``_is_terminal_sibling_park`` — the same predicate
    the class-7 detector uses — so this can never touch a ticket's real,
    non-duplicate row, even when *ticket_ids* also names one.
    """
    changed = False
    for task in queue.tasks:
        if task.ticket_id in ticket_ids and _is_terminal_sibling_park(task):
            transition_task_status(task, QueueItemStatus.CANCELLED)
            changed = True
    return changed


def _reap_wedge_findings(findings: list[WedgeFinding]) -> None:
    """Apply mutations for actionable wedge classes.

    Class-2 (task-running-no-session): revert queue task to PENDING.
    Class-3 (task-running-completed-session): revert queue task to PENDING.
    Class-4 (repo-ahead-of-queue): advisory only — no mutations.
    Class-5 (blocked-on-user-dead-session): revert oldest to PENDING, cancel
        duplicates via _collapse_blocked_on_user_tasks — skipped entirely if
        the oldest task already has pr_url set.
    Class-6 (active-no-daemon-entry): call _reap_session_by_selector per
        phantom session; that helper marks COMPLETED, reverts queue task to
        PENDING, stops the daemon surface, and emits an audit event.
    Class-7 (terminal-sibling-park, #2100): CANCEL every matching row via
        _cancel_terminal_sibling_parks — never a PENDING revert (see that
        function's docstring for why).

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
    phantom_session_ids: list[str] = [
        f.session_id
        for f in findings
        if f.session_id and f.wedge_class == _WEDGE_ACTIVE_NO_DAEMON_ENTRY
    ]

    if not (
        running_ticket_ids
        or blocked_ticket_ids
        or terminal_sibling_ticket_ids
        or phantom_session_ids
    ):
        return

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
        if changed:
            save_dev_queue(queue)

    # Reap phantom sessions outside the queue lock — _reap_session_by_selector
    # acquires sessions_lock and dev_queue_lock internally (sequential, no
    # deadlock risk since we already released dev_queue_lock above).
    for session_id in phantom_session_ids:
        _reap_session_by_selector(session_id)
