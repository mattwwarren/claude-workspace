"""Pre-spawn stale ``.cw/context.json`` invalidation for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503). Also the one home
of ``_LOGGER_NAME``: this module imports no other gating submodule, so every
sibling can import the logger name from here without a cycle.
"""

from __future__ import annotations

import logging
import re
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

_DIGEST_SHAPE = re.compile(r"[0-9a-f]{64}")
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


def _safe_digest(value: str | None) -> str | None:
    """The row digest when it has the sha256 shape, else None (never raw row text)."""
    if value is None or _DIGEST_SHAPE.fullmatch(value) is None:
        return None
    return value


def _emit_render_failure(
    task: TicketTask, cause: RenderFailureCause, error_class: str
) -> None:
    """Log and record ``ticket.snapshot_render_failed``. Never parks, never raises.

    Sources are the task row (RFC 0019 R31). The payload and the log carry the
    closed ``cause`` and the exception class name, never exception text or
    ticket content.
    """
    digest = _safe_digest(task.snapshot_digest)
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


def _render_context_json_from_snapshot(task: TicketTask, worktree_path: Path) -> None:
    """Re-render ``.cw/context.json`` from the snapshot store on spawn (RFC 0019 T1).

    A no-op without a row digest. Store-only: no tracker call, no lock. On a
    failure the file is left as it is and the spawn proceeds; the failure is
    recorded through :func:`_emit_render_failure`.
    """
    if task.snapshot_digest is None:
        return
    try:
        render_for_task(task, worktree_path)
    except SnapshotUntrustedError as exc:
        _emit_render_failure(task, exc.reason, type(exc).__name__)
    except OSError as exc:
        _emit_render_failure(task, _RENDER_ERROR, type(exc).__name__)
