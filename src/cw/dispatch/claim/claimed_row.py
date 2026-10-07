"""The spawn-success stamp for one already-claimed RUNNING dev-queue row.

Extracted verbatim from the historical flat ``cw.dispatch.claim`` module by the
package split (#2378). The shared ``dev_queue_lock()`` load -> re-find ->
mutate -> save primitives it once sat beside (the #2219 identity re-find
:func:`_find_running_row`, the revert-to-PENDING and park-BLOCKED_ON_USER
transitions, and the in-memory :func:`_apply_spawn_success_fields`) moved to the
``cw.queue_rows`` leaf (#2613) so ``cw.reconcile`` can import them at module
scope; ``cw.dispatch.claim`` still re-exports them. Only the stamp stays here,
because it runs git (``git_output``) and logs on the ``cw.dispatch`` logger.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from cw._git import git_output
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
)
from cw.queue_rows import _apply_spawn_success_fields, _find_running_row

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

_log = logging.getLogger("cw.dispatch")


def _stamp_spawn_success(
    task: TicketTask,
    *,
    client_name: str,
    session_id: str,
    worktree_path: Path,
) -> None:
    """Persist the spawn-success state onto the stored RUNNING row.

    Stamps session_id so the completion consumer can match SESSION_COMPLETED
    events to the correct (current) session and reject stale events from prior
    crashed sessions for the same ticket (GitHub #97), clears the spawn-failure
    counters the successful spawn just invalidated, consumes the per-arrival
    regress markers (all via :func:`_apply_spawn_success_fields`), and records
    stage_base_ref.

    Extracted from :func:`_spawn_claimed_task` to keep that function inside the
    PLR statement budget, mirroring :func:`_codex_capability_gate`'s extraction
    for the same reason. Sole caller; it runs after the executor returns, so
    every write here is predicated on a spawn that genuinely succeeded.

    The stored row is re-found by the spawned task's ``created_at`` via
    :func:`_find_running_row` (#2219), so a duplicate RUNNING row for the same
    ``(ticket_id, client)`` is never stamped with this session in its place.
    """
    with dev_queue_lock():
        store = load_dev_queue()
        stored_task = _find_running_row(
            store, task.ticket_id, client_name, created_at=task.created_at
        )
        if stored_task is not None:
            _apply_spawn_success_fields(stored_task, session_id=session_id)
            # R5: stamp stage_base_ref -- non-fatal on failure
            try:
                head_sha = git_output(
                    ["-C", str(worktree_path), "rev-parse", "HEAD"], timeout=5
                )
                stored_task.stage_base_ref = head_sha.strip()
            except subprocess.SubprocessError as exc:
                _log.warning(
                    "dispatch: stage_base_ref failed for %s: %s",
                    task.ticket_id,
                    exc,
                )
        save_dev_queue(store)
