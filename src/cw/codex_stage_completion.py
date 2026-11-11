"""Stage-neutral completion scaffolding for codex stage executors (#3054).

Everything here is the part of a codex stage run that does not depend on which
stage it is: stamping the running row with the session id, the executor-blocked
marker lifecycle, completing a session through the door under ``sessions_lock``,
and the unexpected-error completion plus claimed-row revert that the background
thread's except branch performs. ``cw.codex_background`` (the REVIEW stage) and
any future stage executor import these; none of it names a stage.

Deferred imports below break the ``cw.executor`` <-> this module cycle:
``cw.executor`` imports the stamp from here at its top, and the door and the
revert live in modules that import ``cw.executor`` at module level.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.codex_review import STAGE3_REVIEW, make_codex_blocked
from cw.codex_review_signals import COMMON_TURN_FAULTS
from cw.config import sessions_lock
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.dispatch_state import (
    ExecutorBlockedMarker,
    clear_executor_blocked_marker,
    save_executor_blocked_marker,
)
from cw.exceptions import CwError
from cw.local_runner import UNEXPECTED_ERROR
from cw.queue_rows import _find_running_row

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

    from cw.auto_dev_result import StageReached
    from cw.models import TicketTask

__all__ = [
    "STAGE_FAULTS",
    "clear_stage_blocked_marker",
    "complete_session_as_unexpected_error",
    "complete_session_through_door",
    "revert_claimed_row_after_unexpected_error",
    "run_guarded_stage",
    "save_stage_blocked_marker",
    "stamp_session_id_on_running_task",
]

_log = logging.getLogger(__name__)

#: The fault classes a guarded stage run converts into an unexpected-error
#: completion (#3060). The same set as the tracker-IO turn's ``_TURN_FAULTS``
#: (``cw.codex_review_persist``; not imported, because ``cw.executor`` imports
#: this module at its top) plus ``CwError``, cw's own failure base. A fault
#: outside it escapes ``cw codex run``: the session stays ACTIVE and
#: reconcile/local's codex branch is the crash-only fallback. Narrow on
#: purpose, so the guarded wrapper needs no broad ``except Exception``.
STAGE_FAULTS: tuple[type[Exception], ...] = (*COMMON_TURN_FAULTS, CwError)


def stamp_session_id_on_running_task(
    *, client_name: str, ticket_id: str, session_id: str, created_at: datetime
) -> None:
    """Stamp *session_id* onto the matching RUNNING dev-queue row (#1727 R1).

    Called by ``CodexExecutor``'s pre-flight immediately before it launches the
    detached ``cw codex run`` job (RFC 0014 A2, #2388), so the queue row already
    points at the live session by the time that job re-finds its task by
    ``(ticket_id, session_id)``. Dispatch stamps session_id too, but only
    *after* spawn() returns — a crash in that window would otherwise leave a
    running codex session with no row attributing it.

    Keyword-only because ``client_name``/``ticket_id``/``session_id`` are all
    plain ``str`` with no type-system distinction between them — mirrors
    ``_park_running_task_blocked_on_user``'s reasoning.

    Deliberately narrower than dispatch's own post-spawn stamp: session_id
    only, no error-counter reset and no stage_base_ref, so backoff semantics
    keep a single owner in claim.py.

    *created_at* is the spawning task's identity (#2219): the row is re-found
    via :func:`cw.queue_rows._find_running_row`, so a duplicate RUNNING
    row for the same ``(ticket_id, client)`` is never stamped in its place.
    Required, with no default, because the one caller always holds the task.

    A no-op when no RUNNING row matches that identity: the executor is
    reachable outside dispatch (direct construction in tests, a future one-off
    invocation), and there is nothing to attribute in that case.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        stored_task = _find_running_row(
            store, ticket_id, client_name, created_at=created_at
        )
        if stored_task is not None:
            stored_task.session_id = session_id
        save_dev_queue(store)


def save_stage_blocked_marker(
    *, client_name: str, ticket_id: str, session_id: str, executor: str = "codex"
) -> None:
    """Mark the loop as legitimately busy for a stage run (#1742).

    Called as the first statement inside the stage thread's ``try:``, so a save
    failure reaches the sanctioned broad except, which completes the session as
    UNEXPECTED_ERROR and reverts the row. Kept as a plain call, not a context
    manager: a ``with`` that saved on ``__enter__`` would sit outside the try and
    let a save failure escape the daemon thread's top frame.
    """
    save_executor_blocked_marker(
        ExecutorBlockedMarker(
            client=client_name,
            ticket_id=ticket_id,
            executor=executor,
            reviewer_role=None,
            started_at=datetime.now(UTC),
            session_id=session_id,
        )
    )


def clear_stage_blocked_marker(client_name: str, ticket_id: str) -> None:
    """Clear the marker written by :func:`save_stage_blocked_marker` (#1742).

    Called from the stage thread's ``finally:``, so it runs on every exit path,
    including a BaseException the except clause does not catch.
    """
    clear_executor_blocked_marker(client_name, ticket_id)


def complete_session_through_door(
    sid: str, payload: dict[str, Any], *, guard_already_completed: bool = False
) -> None:
    """Persist *payload* as the session's result through the completion door.

    Takes ``sessions_lock()`` here, so callers must not already hold it: the
    lock is not re-entrant (ADR-0019).
    """
    # Deferred: cw.executor imports this module at its top (executor/codex.py
    # takes the stamp from here), so a module-level import would be circular.
    from cw.executor import _complete_session_via_door

    with sessions_lock():
        _complete_session_via_door(
            sid=sid, payload=payload, guard_already_completed=guard_already_completed
        )


def complete_session_as_unexpected_error(
    sid: str,
    task: TicketTask,
    worktree: Path,
    *,
    stage_reached: StageReached = STAGE3_REVIEW,
) -> None:
    """Complete a session with a blocked/unexpected-error payload via the door.

    Used by the stage thread's own except branch, which may be racing the
    completion door's success path, hence ``guard_already_completed=True``.
    *stage_reached* names the stage that failed (#3060); the REVIEW default
    keeps the review thread's payload byte-identical.
    """
    complete_session_through_door(
        sid,
        make_codex_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=UNEXPECTED_ERROR,
            stage_reached=stage_reached,
        ).model_dump(mode="json"),
        guard_already_completed=True,
    )


def revert_claimed_row_after_unexpected_error(
    *, client_name: str, ticket_id: str, session_id: str
) -> None:
    """Return the claimed row to PENDING after an unexpected stage failure.

    ``expected_session_id`` (#2219) limits the revert to the row this session
    was stamped onto, never a duplicate RUNNING sibling.
    """
    # Deferred: claim.py imports cw.executor at module level, which imports this module.
    from cw.dispatch.claim import _revert_claimed_task_to_pending

    _revert_claimed_task_to_pending(
        client_name, ticket_id, stamp_backoff=True, expected_session_id=session_id
    )


def run_guarded_stage(
    *,
    client_name: str,
    task: TicketTask,
    worktree: Path,
    sid: str,
    stage_reached: StageReached,
    body: Callable[[], None],
) -> None:
    """Run a codex stage *body* under the shared top-frame guard (#3060).

    The stage-neutral shape of the REVIEW thread's sanctioned handler, with a
    narrow :data:`STAGE_FAULTS` tuple in place of a broad catch: the blocked
    marker is saved inside the ``try`` (so a save failure is handled like any
    other fault), a fault completes the session as ``unexpected_error`` with
    *stage_reached* and returns the claimed row to PENDING, and the marker is
    cleared in ``finally`` on every exit path, including an exception outside
    the tuple, which propagates.
    """
    try:
        save_stage_blocked_marker(
            client_name=client_name, ticket_id=task.ticket_id, session_id=sid
        )
        body()
    except STAGE_FAULTS:
        _log.exception(
            "codex stage run failed session=%s ticket=%s stage=%s",
            sid,
            task.ticket_id,
            stage_reached,
        )
        complete_session_as_unexpected_error(
            sid, task, worktree, stage_reached=stage_reached
        )
        revert_claimed_row_after_unexpected_error(
            client_name=client_name, ticket_id=task.ticket_id, session_id=sid
        )
    finally:
        clear_stage_blocked_marker(client_name, task.ticket_id)
