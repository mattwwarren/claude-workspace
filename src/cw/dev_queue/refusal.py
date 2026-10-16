"""Load the bound session for a refusal's next-command hint (#2692).

``next_commands.refusal_suffix`` is pure (``TestPurity`` pins that it reads no
state), so the one state read a refusal site needs -- the ``Session`` its row
is bound to -- lives here. ``bound_session`` is a bare, lock-free
``load_state()`` read, which ADR-0005 permits for read-only callers, including
inside the dev-queue lock (``move_ticket``). It is never called under
``sessions_lock``: ``unblock_ticket`` already holds the session and passes it
straight to ``refusal_suffix``.

The lookup is id-only (``Session.id == task.session_id``), the same policy
``show`` uses (``resolve_session_for_task``), and deliberately not
``find_by_name_or_id``: a legacy row whose ``session_id`` holds a session name
resolves to no session here (Adopted Assumption 19).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import load_state
from cw.dev_queue.next_commands import refusal_suffix
from cw.exceptions import CwError

if TYPE_CHECKING:
    from cw.models import Session, TicketTask


def bound_session(task: TicketTask) -> Session | None:
    """The ``Session`` *task* is bound to, or None (unbound, gone, unreadable)."""
    if task.session_id is None:
        return None
    try:
        state = load_state()
    except (OSError, ValueError, CwError):
        return None
    return next((s for s in state.sessions if s.id == task.session_id), None)


def refusal_hint(task: TicketTask, *, skip: str | None = None) -> str:
    """``refusal_suffix`` for *task*, with its bound session loaded."""
    return refusal_suffix(task, session=bound_session(task), skip=skip)
