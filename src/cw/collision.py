"""Wave file-collision detection for parallel dispatch.

Detects when two RUNNING tasks in the same wave touch overlapping files,
emitting a WAVE_COLLISION event so operators can reorder or serialize the
conflicting work before merge time.

Detection is warning-only — no auto-serialization. Any git failure
returns an empty frozenset so collision detection never blocks dispatch.
"""

from __future__ import annotations

import itertools
import logging
import subprocess
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.models import OrchestratorEventType, QueueItemStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask

_log = logging.getLogger(__name__)

_EMIT_FILES_PREVIEW_COUNT = 3


def _git_changed_files(worktree: Path, base_ref: str) -> frozenset[str]:
    """Return files changed in *worktree* since *base_ref*.

    Returns empty frozenset when the worktree is missing, git exits non-zero,
    or any OS-level error occurs — failure must never block dispatch.
    """
    if not worktree.exists():
        return frozenset()
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", base_ref, "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        _log.debug("collision: git subprocess failed for %s", worktree)
        return frozenset()
    if result.returncode != 0:
        _log.debug(
            "collision: git diff exited %d for %s: %s",
            result.returncode,
            worktree,
            result.stderr,
        )
        return frozenset()
    return frozenset(line for line in result.stdout.splitlines() if line.strip())


def detect_wave_collisions(
    tasks: list[TicketTask],
    *,
    warned_collision: set[frozenset[str]] | None,
    emit: Callable[[str], None] | None = None,
) -> None:
    """Detect file-set overlaps across in-flight RUNNING tasks in the same wave.

    Compares the changed-file sets of all RUNNING tasks that have a
    ``stage_base_ref`` and ``worktree_path``. For each colliding pair, emits
    a WAVE_COLLISION event and (if *emit* is not None) a warning line.

    Args:
        tasks: All tasks in the current queue snapshot; non-RUNNING tasks,
            tasks without ``stage_base_ref``, and tasks without
            ``worktree_path`` are skipped silently.
        warned_collision: Mutable set of ``frozenset({ticket_id_a, ticket_id_b})``
            pairs already warned this loop run. When ``None``, dedup is skipped
            but the event is still recorded. Caller owns the set; mutated
            in-place.
        emit: Optional callable for operator-facing stdout lines. When None,
            stdout output is suppressed (quiet mode).
    """
    # Build (task, file_set) pairs for tasks eligible for comparison.
    eligible: list[tuple[TicketTask, frozenset[str]]] = []
    for task in tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.stage_base_ref is None:
            continue
        if task.worktree_path is None:
            continue
        files = _git_changed_files(task.worktree_path, task.stage_base_ref)
        eligible.append((task, files))

    for (task_a, files_a), (task_b, files_b) in itertools.combinations(eligible, 2):
        if task_a.client != task_b.client:
            continue
        overlap = files_a & files_b
        if not overlap:
            continue

        pair_key: frozenset[str] = frozenset({task_a.ticket_id, task_b.ticket_id})
        if warned_collision is not None and pair_key in warned_collision:
            continue

        sorted_ids = sorted([task_a.ticket_id, task_b.ticket_id])
        sorted_files = sorted(overlap)

        record_event(
            OrchestratorEventType.WAVE_COLLISION,
            payload={
                "ticket_ids": sorted_ids,
                "files": sorted_files,
                "client": task_a.client,
            },
        )

        if warned_collision is not None:
            warned_collision.add(pair_key)

        if emit is not None:
            ids_str = " ↔ ".join(sorted_ids)
            preview_limit = _EMIT_FILES_PREVIEW_COUNT
            files_preview = ", ".join(sorted_files[:preview_limit])
            if len(sorted_files) > preview_limit:
                files_preview += f" (+{len(sorted_files) - preview_limit} more)"
            emit(
                f"COLLISION [{task_a.client}] {ids_str} share"
                f" {len(sorted_files)} file(s): {files_preview}"
            )
