"""Shared logged fire-and-forget subprocess launcher (#2369).

A dependency-free leaf module (like ``cw.executor_diagnostics``) so both
``cw.local_runner`` and ``cw.opencode_runner`` can import it. It cannot live in
``cw.executor.core``: that module imports ``cw.reconcile``, whose package init
eagerly imports ``cw.reconcile.local`` → ``cw.local_runner``, so a
``local_runner → executor.core`` import would close a cycle.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _launch_logged_subprocess(
    worktree: Path,
    argv: list[str],
    env: dict[str, str],
    log_relative_path: Path,
) -> subprocess.Popen[bytes]:
    """Launch *argv* detached in *worktree*, output to a per-run log file.

    Output goes to ``worktree / log_relative_path``, never PIPE — nothing reads
    the pipe on this fire-and-forget path, and an unread full pipe buffer
    deadlocks the child; a file has no such backpressure. Truncated ("w") on
    every call so a retry into the same worktree does not bleed a prior
    attempt's output into the next harvest read. ``start_new_session=True``
    puts the child in its own process group (#2367).
    """
    log_path = worktree / log_relative_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log_file:
        return subprocess.Popen(
            argv,
            env=env,
            cwd=worktree,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
