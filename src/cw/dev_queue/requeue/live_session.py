"""Live-session refusal for a requeue (#3739).

Refuses while a worker still owns the ticket. ``load_state`` imports at module
level (``cw.config`` never imports ``cw.dev_queue``). ``cw.native_daemon`` is
imported only under ``TYPE_CHECKING``: ``NativeDaemonClient`` is annotation-only
here, and the runtime ``get_native_daemon_client`` import is in
``requeue/core.py``. ``_refuse_if_live_session`` imports
``cw.reconcile.find_live_sessions_for_ticket`` inside the function, because a
module-level import cycles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from cw.config import load_state
from cw.exceptions import RequeueLiveSessionError, RequeueRosterUnreadableError

if TYPE_CHECKING:
    from cw.native_daemon import NativeDaemonClient

RequeueRefusalTag = Literal["skipped_live_session", "skipped_roster_unreadable"]


_REASON_SKIPPED_LIVE_SESSION: RequeueRefusalTag = "skipped_live_session"


_REASON_SKIPPED_ROSTER_UNREADABLE: RequeueRefusalTag = "skipped_roster_unreadable"


def _live_short_ids_or_refuse(
    ticket_id: str, native_daemon: NativeDaemonClient
) -> set[str]:
    """The roster's live short ids, failing closed (GitHub #2275, review round 1).

    Same split as ``cw.worktree.live_home_reason`` (#2213): an *absent* roster
    is an empty set (no daemon, so no live sessions) and the requeue proceeds;
    an unreadable or malformed one -- the fail-closed read returns ``None``, or
    raises ``OSError``/``ValueError`` -- cannot rule out a live session, so
    raise :class:`RequeueRosterUnreadableError` rather than read it as "none".
    """
    roster_path = native_daemon.roster_path
    msg = (
        f"daemon roster unreadable at {roster_path}; cannot rule out a live"
        f" session for #{ticket_id}. Refusing to requeue: fix the roster, or run"
        " `cw spawn close <sid> --requeue` if the session is known."
    )
    try:
        live_short_ids = native_daemon.list_live_session_short_ids_fail_closed()
    except (OSError, ValueError) as exc:
        raise RequeueRosterUnreadableError(msg, roster_path=roster_path) from exc
    if live_short_ids is None:
        raise RequeueRosterUnreadableError(msg, roster_path=roster_path)
    return live_short_ids


def _refuse_if_live_session(
    ticket_id: str,
    client_name: str,
    *,
    native_daemon: NativeDaemonClient,
    ignore_session_ids: frozenset[str] = frozenset(),
) -> None:
    """Refuse a requeue while a daemon-live session exists for *ticket_id*
    (GitHub #2275), except sessions named in *ignore_session_ids*.

    A live session for the ticket that no dev-queue row points at any more
    (a signal_only park retains ``task.session_id`` but a sibling can still be
    running) would later complete and advance the row past the operator's
    requeue. Refusing here automates the runbook's "Liveness before state"
    check. Runs outside any lock, like ``unblock_ticket``'s fast pre-check.

    Deferred import: ``cw.reconcile._shared`` imports from ``cw.dev_queue`` at
    module level, so a module-top import back into ``cw.reconcile`` cycles.

    *ignore_session_ids* is a ``Session.id``-keyed post-filter on the matched
    sessions -- never compared against ``surface_ref``/the roster's short ids,
    which are a different namespace. It cannot exempt an unreadable roster:
    that refusal (:func:`_live_short_ids_or_refuse`) is about sessions no one
    can name, so it fires before any session is matched.
    """
    from cw.reconcile import find_live_sessions_for_ticket

    live_short_ids = _live_short_ids_or_refuse(ticket_id, native_daemon)
    live = [
        s
        for s in find_live_sessions_for_ticket(
            load_state(), ticket_id, client_name, live_short_ids
        )
        if s.id not in ignore_session_ids
    ]
    if not live:
        return
    names = ", ".join(f"{s.id!r} ({s.name})" for s in live)
    msg = (
        f"Cannot requeue ticket '{ticket_id}': a session for this ticket"
        f" is live in the daemon roster: {names}. Close it with"
        f" `cw spawn close {live[0].id} --requeue` or wait for it to"
        " finish, then retry."
    )
    raise RequeueLiveSessionError(msg, session_ids=tuple(s.id for s in live))


def classify_requeue_live_session_error(
    exc: RequeueLiveSessionError,
) -> tuple[RequeueRefusalTag, str]:
    """``(reason_tag, message)`` for a refused requeue (GitHub #2275).

    The one shared outcome-classification seam every ``RequeueLiveSessionError``
    catcher (the CLI, ``drain``, ``auto_fix_ci``) uses instead of each
    hand-formatting its own skipped-live-session string. An unreadable roster
    (:class:`RequeueRosterUnreadableError`) gets its own
    ``skipped_roster_unreadable`` tag: no session is known to be live, so the
    remediation differs (fix the roster).
    """
    if isinstance(exc, RequeueRosterUnreadableError):
        return _REASON_SKIPPED_ROSTER_UNREADABLE, str(exc)
    return _REASON_SKIPPED_LIVE_SESSION, str(exc)
