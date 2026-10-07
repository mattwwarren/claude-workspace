"""Pre-spawn stale ``.cw/context.json`` invalidation for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503). Also the one home
of ``_LOGGER_NAME``: this module imports no other gating submodule, so every
sibling can import the logger name from here without a cycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.executor import resolve_executor_config
from cw.models import (
    CONTEXT_JSON_RELATIVE_PATH,
    LOCAL_BACKEND,
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
