"""Reconcile cw session state with live worker backends.

A cw session is "live" if its ``surface_ref`` is registered with at least
one backend that supplies a liveness oracle. Today there are two:

- the multiplexer (tmux/cmux/fake) for interactive sessions and legacy
  daemon-via-tmux paths;
- the native Claude background daemon for dispatched workers spawned via
  ``claude --bg`` (see GitHub issue #150).

:func:`compute_drift` unions both backends' live sets and returns a
:class:`ReconcileReport` naming the sessions whose ``surface_ref``
appears in neither. :func:`reconcile` applies the report.

The split is deliberate: ``compute_drift`` is pure and testable in
isolation; ``reconcile`` does the side-effecting work (state mutation,
event emission, dev-queue revert).

Transient-outage safety: ``reconcile`` refuses to mutate state when
*both* backends report zero live entries but the persisted state still
contains ACTIVE/IDLE sessions with surface refs. A temporary multiplexer
hiccup, or a missing/unreadable native roster, would otherwise
irreversibly mark every session as CRASHED. ``compute_drift`` stays pure
and does not apply this guard — callers that want drift-without-side-
effects still get the full phantom list.

Race note: ``reconcile`` does ``load_state → mutate → save_state`` without
a dedicated ``sessions.json`` file lock. This matches every other
``save_state`` call site in the codebase (``cw.session``, ``cw.cli``, …);
a unified state lock is a larger refactor tracked separately. In
practice the race window is the in-memory mutation between load and save,
and concurrent writers are rare in the single-user model this tool
targets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.models import (
    CompletionReason,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client

if TYPE_CHECKING:
    from cw.cmux import MultiplexerAdapter
    from cw.models import CwState, Session
    from cw.native_daemon import NativeDaemonClient


# Session-name prefix for DAEMON sessions spawned by the dispatch loop. The
# full name is ``<client>/<AUTO_DEV_LABEL_PREFIX><ticket_id>``; reconciliation
# uses it to recover the ticket id when reverting phantom tickets. Defined
# here (not in ``cw.dispatch``) to avoid a circular import — ``cw.dispatch``
# imports :func:`reconcile` from this module.
AUTO_DEV_LABEL_PREFIX = "auto-dev/"

# Wall-clock budget for headless daemon sessions. Mirrors the constant in
# cli.py signal_stop; cli.py imports this value so there is a single source
# of truth. See GitHub issue #185.
HEADLESS_TIMEOUT_SECONDS = 1800  # 30 minutes


# Only these two statuses imply "the multiplexer should have a surface".
# BACKGROUNDED sessions intentionally have no pane (that's the whole point);
# COMPLETED is terminal. Both are ignored by reconciliation.
_LIVE_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.ACTIVE,
        SessionStatus.IDLE,
    }
)

# Shell process names indicating a pane's foreground process is an idle
# shell — claude (or ``cw run-claude``) has exited and the pane is back
# at the prompt. A session whose ``pane_current_command`` is in this
# set is treated as a zombie phantom even though the pane itself still
# exists. See GitHub issue #144.
# Known limitation: a user who manually attaches to a cw pane and drops
# to a subshell will also match this predicate and be reaped on the
# next reconcile tick. cw auto-spawn does not produce this pattern in
# normal use.
_IDLE_SHELL_COMMANDS: frozenset[str] = frozenset(
    {"bash", "zsh", "sh", "fish", "dash", "tcsh", "ksh"}
)


@dataclass(frozen=True)
class ReconcileReport:
    """What reconciliation would do / did.

    ``phantom_session_ids`` — sessions whose ``surface_ref`` is not in the
    live set. Ordered by the original order in ``state.sessions``.
    ``phantom_session_names`` — session names in the same order as
    ``phantom_session_ids``. Populated by :func:`reconcile`; empty after
    :func:`compute_drift`.
    ``reverted_ticket_ids`` — ticket IDs whose TicketTasks got reverted
    from RUNNING to PENDING. Populated by :func:`reconcile`; empty after
    :func:`compute_drift`.
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    phantom_session_names: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)


def compute_drift(
    state: CwState,
    adapter: MultiplexerAdapter | None = None,
    native_daemon: NativeDaemonClient | None = None,
) -> ReconcileReport:
    """Return a report naming sessions whose surface is no longer live.

    An ACTIVE or IDLE session is phantom when:
    - it has a ``surface_ref`` (None means it was never spawned), AND
    - that ref is not in the multiplexer's live set, AND
    - that ref is not in the native daemon's live set.

    The two backends are unioned so a daemon-origin session with a short
    Claude session id passes liveness via the roster, while an
    interactive session with a tmux pane ref passes via the adapter.

    *adapter* is optional. When ``None``, the multiplexer side of the union
    is treated as empty; only the native daemon oracle is consulted. Phase D
    will drop the adapter parameter entirely once the multiplexer is removed
    from all call sites.

    This function does not mutate state. It also does not distinguish
    "backend reports zero live entries" from "backend is unreachable";
    that guard lives in :func:`reconcile`.
    """
    daemon = native_daemon or get_native_daemon_client()
    tmux_live = adapter.list_surfaces() if adapter is not None else set()
    native_live = daemon.list_live_session_short_ids()
    # Second-pass zombie filter: panes that exist but whose foreground
    # process is a bare shell are not actually live cw sessions. An empty
    # command map means the backend can't enumerate — skip the filter
    # (fail-open) rather than risk false-positive reaping.
    surface_commands = (
        adapter.list_live_surface_commands() if adapter is not None else {}
    )
    zombie_refs: set[str] = set()
    if surface_commands:
        zombie_refs = {
            ref for ref, cmd in surface_commands.items() if cmd in _IDLE_SHELL_COMMANDS
        }

    phantoms: list[str] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.surface_ref is None:
            continue
        if session.surface_ref in tmux_live and session.surface_ref not in zombie_refs:
            continue
        if session.surface_ref in native_live:
            continue
        phantoms.append(session.id)
    return ReconcileReport(phantom_session_ids=phantoms)


def ticket_id_for_session(session_name: str) -> str | None:
    """Extract the ticket id from a daemon session name, or None."""
    _, _, tail = session_name.partition("/")
    if tail.startswith(AUTO_DEV_LABEL_PREFIX):
        return tail[len(AUTO_DEV_LABEL_PREFIX) :]
    return None


def _looks_like_backend_outage(
    state: CwState, tmux_live: set[str], native_live: set[str]
) -> bool:
    """True when both backends are empty and the state still has live refs.

    If either backend returned a non-empty set, treat its absence on the
    other as "the other backend has nothing live", not an outage — the
    common case is dispatch using only the native daemon while the
    multiplexer is idle (or vice versa).

    If both backends are empty *and* the persisted state has at least one
    ACTIVE/IDLE session that was once given a surface_ref, assume the
    backends are unreachable rather than "somehow every session died at
    once". Aborting here is the difference between a 5-second restart and
    permanent data loss.
    """
    if tmux_live or native_live:
        return False
    return any(
        s.surface_ref is not None and s.status in _LIVE_STATUSES for s in state.sessions
    )


def _is_headless(session: Session) -> bool:
    """Return True if session's worktree has a headless cw-context.json.

    Fail-open: returns False when worktree_path is None, or when the context
    file is missing or unreadable — a deleted worktree must not be falsely
    flagged as headless. Mirrors cli.py signal_stop at line 1003-1005.
    """
    if session.worktree_path is None:
        return False
    context_path = session.worktree_path / ".claude" / "cw-context.json"
    try:
        context = json.loads(context_path.read_text())
        return bool(context.get("headless")) if isinstance(context, dict) else False
    except (OSError, json.JSONDecodeError):
        return False


def revert_stalled_headless_sessions(
    state: CwState, *, now: datetime, budget_seconds: int
) -> list[str]:
    """Transition stalled headless DAEMON sessions past budget to TIMED_OUT.

    Passive backstop complementing signal_stop's Stop-hook-driven check.
    signal_stop can only fire at Claude turn boundaries; a session whose agent
    stalled mid-turn (classifier denial, OOM, long subagent chain) produces no
    further Stop firings and would sit ACTIVE forever without this sweep.

    Runs unconditionally before the outage guard so a transient backend hiccup
    does not delay enforcement of the wall-clock budget. The sweep is purely
    time-based; surface liveness is irrelevant.

    Calls save_state(state) when any sessions are transitioned — callers must
    not assume state is unchanged on return. On the phantom-handling path in
    reconcile(), save_state is called again at line 381; this double-save is
    benign because save_state is idempotent over identical content.

    Returns the list of ticket IDs whose TicketTask was reverted to PENDING.
    See GitHub issue #185.
    """
    pending: list[tuple[Session, str | None]] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if not _is_headless(session):
            continue
        elapsed = (now - session.started_at).total_seconds()
        if elapsed < budget_seconds:
            continue
        ticket_id = ticket_id_for_session(session.name)
        session.status = SessionStatus.TIMED_OUT
        session.completed_at = now
        session.completed_reason = CompletionReason.TIMED_OUT
        pending.append((session, ticket_id))

    if not pending:
        return []

    save_state(state)

    ticket_ids_to_revert = [tid for _, tid in pending if tid]
    reverted: list[str] = []
    if ticket_ids_to_revert:
        ticket_id_set = set(ticket_ids_to_revert)
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id in ticket_id_set
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.PENDING
                    task.session_id = None
                    reverted.append(task.ticket_id)
            if reverted:
                save_dev_queue(store)

    for session, ticket_id in pending:
        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "elapsed_seconds": (now - session.started_at).total_seconds(),
            "last_assistant_message_excerpt": "",
        }
        record_event(OrchestratorEventType.SESSION_TIMED_OUT, payload)
        if session.surface_ref is not None:
            get_native_daemon_client().stop(session.surface_ref)

    return reverted


def reconcile(
    adapter: MultiplexerAdapter | None = None,
    native_daemon: NativeDaemonClient | None = None,
) -> ReconcileReport:
    """Apply drift reconciliation against the persisted state.

    Flips phantom ACTIVE/IDLE sessions to COMPLETED with
    ``completed_reason = CRASHED``, emits a ``SESSION_COMPLETED`` event
    with ``crashed: True``, and reverts any RUNNING TicketTask whose
    ticket-id can be recovered from the session name back to PENDING so
    the dispatch loop will retry.

    Returns an empty report without mutating state when
    :func:`_looks_like_backend_outage` matches — a transient multiplexer
    restart (or a missing native roster) must not trigger mass-reaping.

    *adapter* is optional. When ``None``, the multiplexer side is treated
    as empty; only the native daemon oracle is consulted. Phase D will drop
    the adapter parameter entirely.

    Partial-failure note: state and the dev queue are separate files. If
    ``save_state`` succeeds but the subsequent dev-queue update raises,
    the session will be COMPLETED while its TicketTask stays RUNNING.
    The next ``reconcile()`` call will not pick this up because the
    session is no longer ACTIVE/IDLE — so a stranded RUNNING task can
    only be recovered by explicit operator action. This is an acceptable
    tradeoff for a file-based, single-user tool.
    """
    daemon = native_daemon or get_native_daemon_client()
    state = load_state()
    now = datetime.now(UTC)

    # Passive budget sweep: catches headless DAEMON sessions whose agent
    # stalled mid-turn and produced no further Stop hook firings. Runs before
    # the outage guard so a backend hiccup does not delay budget enforcement.
    # See GitHub issue #185.
    stalled_reverted = revert_stalled_headless_sessions(
        state, now=now, budget_seconds=HEADLESS_TIMEOUT_SECONDS
    )

    tmux_live = adapter.list_surfaces() if adapter is not None else set()
    native_live = daemon.list_live_session_short_ids()
    if _looks_like_backend_outage(state, tmux_live, native_live):
        return ReconcileReport(reverted_ticket_ids=stalled_reverted)

    drift = compute_drift(state, adapter, daemon)
    if not drift.phantom_session_ids:
        # No phantom sessions to reap, but still run the TIMED_OUT and
        # COMPLETED-silent sweeps so any tasks whose sessions completed or
        # timed out without reverting their queue task are recovered.
        timed_out_ticket_ids = revert_timed_out_tasks()
        completed_silent_ticket_ids = revert_completed_silent_tasks()
        all_reverted = list(
            dict.fromkeys(
                stalled_reverted + timed_out_ticket_ids + completed_silent_ticket_ids
            )
        )
        return ReconcileReport(reverted_ticket_ids=all_reverted)

    phantom_set = set(drift.phantom_session_ids)
    ticket_ids_to_revert: list[str] = []
    pending_events: list[dict[str, object]] = []
    phantom_names: list[str] = []
    for session in state.sessions:
        if session.id not in phantom_set:
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.CRASHED
        session.completed_at = now
        phantom_names.append(session.name)
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id and session.origin is SessionOrigin.DAEMON:
            ticket_ids_to_revert.append(ticket_id)
        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "crashed": True,
        }
        if ticket_id:
            payload["ticket_id"] = ticket_id
        pending_events.append(payload)

    save_state(state)
    for payload in pending_events:
        record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    reverted: list[str] = []
    if ticket_ids_to_revert:
        with dev_queue_lock():
            store = load_dev_queue()
            for task in store.tasks:
                if (
                    task.ticket_id in ticket_ids_to_revert
                    and task.status == QueueItemStatus.RUNNING
                ):
                    task.status = QueueItemStatus.PENDING
                    # Drop the stamp from the prior (now-crashed) session so
                    # the next dispatch_tick can re-stamp with the freshly
                    # spawned session_id without a window where the task
                    # carries a stale id. See GitHub issue #97.
                    task.session_id = None
                    reverted.append(task.ticket_id)
            if reverted:
                save_dev_queue(store)

    # Sweep for TIMED_OUT and DAEMON-COMPLETED sessions whose owning TicketTask
    # was not yet reverted (e.g. signal_stop crashed after setting status but
    # before touching the queue, or a headless session completed without
    # the dispatch consumer processing it). TIMED_OUT/COMPLETED sessions are
    # already terminal so no state mutation is needed — queue revert only.
    timed_out_ticket_ids = revert_timed_out_tasks()
    completed_silent_ticket_ids = revert_completed_silent_tasks()
    all_reverted = list(
        dict.fromkeys(
            stalled_reverted
            + reverted
            + timed_out_ticket_ids
            + completed_silent_ticket_ids
        )
    )

    return ReconcileReport(
        phantom_session_ids=drift.phantom_session_ids,
        phantom_session_names=phantom_names,
        reverted_ticket_ids=all_reverted,
    )


def _revert_running_tasks_for_sessions(session_ids: set[str]) -> list[str]:
    """Revert RUNNING TicketTasks whose ``session_id`` is in *session_ids*.

    Shared helper for the per-status revert wrappers. Acquires
    ``dev_queue_lock`` for the read+write window; writes only when at least
    one task was reverted. Returns the list of reverted ticket IDs.
    """
    if not session_ids:
        return []

    reverted: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.session_id not in session_ids:
                continue
            task.status = QueueItemStatus.PENDING
            task.session_id = None
            reverted.append(task.ticket_id)
        if reverted:
            save_dev_queue(store)
    return reverted


def revert_timed_out_tasks() -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is TIMED_OUT.

    Called during :func:`reconcile` as a backstop for the case where
    ``signal_stop`` crashed after writing TIMED_OUT status but before
    reverting the dev-queue task. Returns the list of ticket IDs reverted.
    """
    state = load_state()
    session_ids = {
        s.id
        for s in state.sessions
        if s.status == SessionStatus.TIMED_OUT and s.origin is SessionOrigin.DAEMON
    }
    return _revert_running_tasks_for_sessions(session_ids)


def revert_completed_silent_tasks() -> list[str]:
    """Revert RUNNING TicketTasks whose owning session is DAEMON COMPLETED.

    Called during :func:`reconcile` as a backstop for sessions that completed
    without reverting their dev-queue task (e.g. the session wrote COMPLETED
    status but the dispatch consumer had not yet processed it). Returns the
    list of ticket IDs reverted.
    """
    state = load_state()
    session_ids = {
        s.id
        for s in state.sessions
        if s.status == SessionStatus.COMPLETED and s.origin is SessionOrigin.DAEMON
    }
    return _revert_running_tasks_for_sessions(session_ids)
