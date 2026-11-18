"""Pre-spawn stale ``.cw/context.json`` invalidation for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503). Also the one home
of ``_LOGGER_NAME``: this module imports no other gating submodule, so every
sibling can import the logger name from here without a cycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.exceptions import CwError
from cw.executor import resolve_executor_config
from cw.models import (
    CONTEXT_JSON_RELATIVE_PATH,
    LOCAL_BACKEND,
    OrchestratorEventType,
)
from cw.ticket_snapshot import (
    RenderFailureCause,
    SnapshotUntrustedError,
    digest_or_none,
    render_for_task,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        TicketTask,
    )

# Every gating submodule logs on the logger the flat ``gating.py`` module used,
# so ``caplog`` filters and operator log routing are unchanged by the split.
_LOGGER_NAME = "cw.dispatch"

_log = logging.getLogger(_LOGGER_NAME)

_RENDER_ERROR: RenderFailureCause = "render_error"


def _invalidate_stale_context_json(
    task: TicketTask, client: ClientConfig, worktree_path: Path
) -> None:
    """Delete a stale ``.cw/context.json`` before spawning a re-spawned task.

    Requeue idempotency guard (#1046): a re-spawned task (``attempts > 1``,
    covering true requeues as well as normal plan->impl->review stage
    advances) may reuse a worktree that still carries a prior session's
    materialized ``.cw/context.json``. Left in place, a worker can silently
    replan against stale ticket context and miss operator-folded
    resolutions/comments (the #1030 incident). Delete it before spawn so the
    new session always materializes fresh context.

    Excluded for LocalExecutor: ``local_runner.build_task_message`` reads
    ``.cw/context.json`` directly and degrades silently to an empty
    ``## Ticket:`` header if it is missing (the #952 regression class).
    """
    if task.attempts <= 1:
        return
    if resolve_executor_config(task.stage, task, client).backend == LOCAL_BACKEND:
        return
    stale_context = worktree_path / CONTEXT_JSON_RELATIVE_PATH
    if stale_context.exists():
        _log.info(
            "dispatch: invalidated stale .cw/context.json for"
            " ticket_id=%s attempts=%d worktree_path=%s",
            task.ticket_id,
            task.attempts,
            worktree_path,
        )
    stale_context.unlink(missing_ok=True)


def _emit_render_failure(
    task: TicketTask, cause: RenderFailureCause, error_class: str
) -> None:
    """Log and record ``ticket.snapshot_render_failed``. Never parks, never raises.

    Sources are the task row (RFC 0019 R31). The payload and the log carry the
    closed ``cause`` and the exception class name, never exception text or
    ticket content.
    """
    digest = digest_or_none(task.snapshot_digest)
    _log.warning(
        "dispatch: ticket snapshot not rendered; spawning without .cw/context.json"
        " ticket_id=%s client=%s cause=%s error_class=%s",
        task.ticket_id,
        task.client,
        cause,
        error_class,
    )
    try:
        record_event(
            OrchestratorEventType.TICKET_SNAPSHOT_RENDER_FAILED,
            {
                "ticket_id": task.ticket_id,
                "client": task.client,
                "snapshot_digest": digest,
                "cause": cause,
            },
            correlation_id=task.ticket_id,
        )
    except (CwError, OSError):
        _log.warning(
            "dispatch: ticket.snapshot_render_failed event not recorded ticket_id=%s",
            task.ticket_id,
            exc_info=True,
        )


def _remove_stale_context(worktree_path: Path) -> None:
    """Unlink any ``.cw/context.json`` left by a prior session, for every backend.

    The invalidation helper skips LOCAL executors, so a failed render must
    remove the file itself: a stale file must never survive a failed render
    (RFC 0019 T1 review item 26).
    """
    try:
        (worktree_path / CONTEXT_JSON_RELATIVE_PATH).unlink(missing_ok=True)
    except OSError as exc:
        _log.warning(
            "dispatch: could not remove stale .cw/context.json ticket_id=%s"
            " error_class=%s",
            worktree_path.name,
            type(exc).__name__,
        )


def _render_context_json_from_snapshot(task: TicketTask, worktree_path: Path) -> None:
    """Re-render ``.cw/context.json`` from the snapshot store on spawn (RFC 0019 T1).

    A no-op without a row digest. Store-only: no tracker call, no lock. On a
    failure any existing file is removed, the spawn proceeds without it, and the
    failure is recorded through :func:`_emit_render_failure`.
    """
    if task.snapshot_digest is None:
        return
    cause: RenderFailureCause
    try:
        render_for_task(task, worktree_path)
    except SnapshotUntrustedError as exc:
        cause, error_class = exc.reason, type(exc).__name__
    except OSError as exc:
        cause, error_class = _RENDER_ERROR, type(exc).__name__
    else:
        return
    _remove_stale_context(worktree_path)
    _emit_render_failure(task, cause, error_class)
