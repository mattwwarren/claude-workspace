"""Hold a fix-loop row until its launched-but-unrecorded worker is confirmed stopped.

(GitHub #2590, refs #2502.) When ``dispatch_fix_agent`` raises
``WorkerLaunchedError`` the fix worker is live in the daemon roster but a later
spawn step failed, usually the ``sessions.json`` write, so cw holds no session
row that could say when the worker finishes. ``cw.reconcile.fix_dispatch``
stamps a :class:`~cw.models.LaunchedFixWorker` tombstone on the row in that
case, and its completions phase asks this module, per row, whether the row may
be unparked for a fresh REVIEW round.

The answer is yes only once a readable daemon roster no longer lists the
tombstone's surface. While the roster lists it, or cannot be read, the row
stays RUNNING and held (``fix_dispatch_session_id`` retained), whatever its
status, so a second worker is never started over the fix worker. A held row
pages ``SESSION_NEEDS_ATTENTION`` (``paused_status``
``fix_dispatch_worker_unconfirmed``) once the launch is ``_ATTENTION_GRACE``
old, then every ``_ATTENTION_REPAGE``; its breadcrumbs name the action that
actually releases the row.

This module only CONFIRMS. Stopping the worker stays with the leaked-worker
sweep (``cw.reconcile.leaked_workers``), which runs earlier in the same
reconcile tick. Apart from the row fields it is handed, it writes nothing: no
subprocess, no lock of its own (the caller holds ``dev_queue_lock``; the
event inbox lock is a leaf), and it never stops a worker. It must not import
``cw.reconcile.fix_dispatch`` (that module imports this one).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, NamedTuple

from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.reconcile import _deps

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import LaunchedFixWorker, TicketTask

_log = logging.getLogger(__name__)

# paused_status of the unconfirmed-worker page. Event-only, like
# cw.spawn.SPAWN_POST_LAUNCH_FAILED_REASON: no row disposition carries it, so it
# lives with its sole emitter and is not breadcrumb-eligible.
FIX_DISPATCH_WORKER_UNCONFIRMED_REASON = "fix_dispatch_worker_unconfirmed"

# The hold normally clears within a tick (the leaked-worker sweep stops the
# worker, then this module confirms it gone), so it pages only once the launch
# is this old.
_ATTENTION_GRACE = timedelta(minutes=5)
# Re-page cadence while the row stays held; matches the liveness sweep's
# default liveness_attention_renotify_interval_minutes.
_ATTENTION_REPAGE = timedelta(minutes=60)

_LISTED_BREADCRUMB = (
    "fix-loop worker {surface_ref} was launched but never recorded and is "
    "still in the daemon roster; the row is held so a second worker is not "
    "started over it. Stop it with `claude stop {surface_ref}`; the next "
    "reconcile tick confirms it is gone and releases the row. If "
    "`claude stop {surface_ref}` fails or {surface_ref} stays in the roster, "
    "the entry itself is stale: repair ~/.claude/daemon/roster.json (fix the "
    "corrupt content or remove the stale entry; the roster holds every live "
    "worker, so do not delete the whole file) and the next reconcile tick "
    "releases the row once {surface_ref} is no longer listed."
)
_UNREADABLE_BREADCRUMB = (
    "fix-loop worker {surface_ref} was launched but never recorded and cannot "
    "be confirmed stopped (daemon roster unreadable); the row is held so a "
    "second worker is not started over it. Stopping the worker does not "
    "release the row: repair ~/.claude/daemon/roster.json so it parses, then "
    "the next reconcile tick checks whether {surface_ref} is still listed and "
    "releases the row once it is not."
)


class HoldDecision(NamedTuple):
    """What the completions phase does with one row.

    *release*: unpark the row now. *dirty*: this call wrote a row field the
    caller must persist even when nothing unparks.
    """

    release: bool
    dirty: bool


def read_live_worker_roster() -> set[str] | None:
    """Read the daemon roster's live short ids, fail closed.

    ``None`` means the roster could not be read, which never confirms a worker
    gone. The client is constructed inside the same ``try`` so a construction
    failure also fails closed instead of escaping the reconcile tick.

    """
    try:
        daemon = _deps.get_native_daemon_client()
        return daemon.list_live_session_short_ids_fail_closed()
    except (OSError, ValueError):
        _log.warning(
            "fix_dispatch_hold: daemon roster read failed, treating it as unreadable",
            exc_info=True,
        )
        return None


def apply_launched_worker_hold(
    task: TicketTask,
    expected_surface_ref: str | None,
    live: set[str] | None,
    *,
    now: datetime,
) -> HoldDecision:
    """Decide whether *task* may unpark, given the roster snapshot *live*.

    *expected_surface_ref* is the tombstone surface the detect phase saw; it
    doubles as the identity check under the caller's lock. *live* is ``None``
    for an unreadable roster (or when no roster read was needed).

    - No tombstone: a plain row releases; a tombstone that vanished since
      detect is skipped this tick.
    - A tombstone that differs from the detect snapshot is held, untouched.
    - A readable roster without the surface releases and clears the tombstone.
    - Otherwise the row is held and paged when due.
    """
    worker = task.fix_dispatch_launched_worker
    if worker is None:
        return HoldDecision(release=expected_surface_ref is None, dirty=False)
    if worker.surface_ref != expected_surface_ref:
        return HoldDecision(release=False, dirty=False)
    if live is not None and worker.surface_ref not in live:
        task.fix_dispatch_launched_worker = None
        # Why: WARNING, not INFO: this line is the only audit record of the
        # release, and cw's default logging drops INFO (see native_daemon.py:235).
        _log.warning(
            "fix_dispatch_worker_confirmed_stopped ticket=%s client=%s surface=%s",
            task.ticket_id,
            task.client,
            worker.surface_ref,
        )
        return HoldDecision(release=True, dirty=True)
    if not _attention_due(worker, now):
        return HoldDecision(release=False, dirty=False)
    paged = _page_unconfirmed_worker(task, worker, roster_readable=live is not None)
    if paged:
        worker.attention_paged_at = now
    return HoldDecision(release=False, dirty=paged)


def _attention_due(worker: LaunchedFixWorker, now: datetime) -> bool:
    """True once the launch is past the grace and no page is recent."""
    if now - worker.launched_at < _ATTENTION_GRACE:
        return False
    paged = worker.attention_paged_at
    return paged is None or now - paged >= _ATTENTION_REPAGE


def _page_unconfirmed_worker(
    task: TicketTask, worker: LaunchedFixWorker, *, roster_readable: bool
) -> bool:
    """Emit the unconfirmed-worker page; return whether it was recorded.

    A failed emit is logged and reported as ``False`` so the caller leaves
    ``attention_paged_at`` unset and the next tick retries. No push
    notification is fired.
    """
    template = _LISTED_BREADCRUMB if roster_readable else _UNREADABLE_BREADCRUMB
    try:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                # The caller skips rows without a fix session, so the
                # fallback only satisfies the type checker.
                "session_id": task.fix_dispatch_session_id or "",
                "session_name": "",
                "client": task.client,
                "ticket_id": task.ticket_id,
                "claude_session_id": None,
                "paused_status": FIX_DISPATCH_WORKER_UNCONFIRMED_REASON,
                "breadcrumbs": template.format(surface_ref=worker.surface_ref),
                "crashed": False,
                "lane": task.lane,
            },
            correlation_id=task.ticket_id,
        )
    except (CwError, OSError):
        _log.warning(
            "fix_dispatch_worker_unconfirmed_page_failed ticket=%s",
            task.ticket_id,
            exc_info=True,
        )
        return False
    return True
