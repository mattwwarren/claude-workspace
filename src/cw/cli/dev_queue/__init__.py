"""Orchestrator development-queue commands (``dev-queue`` group).

This package was split out of a single ``dev_queue.py`` CLI module (#1319); the
public ``from cw.cli.dev_queue import X`` surface is preserved here via
re-exports. The ``dev_queue`` click group lives in ``_group`` and each command
submodule decorates it via ``@dev_queue.command(...)`` — importing the submodules
below is what registers those commands. Submodules:

- ``_group`` — the ``dev_queue`` group object plus the ``_WAIT_EXIT_*`` /
  status-rendering constants shared across submodules.
- ``crud`` — queue mutation commands (add, move, approve, requeue, unblock,
  remove, cancel, clear).
- ``status`` — aggregate status table + lane breakdown rendering.
- ``run`` — dispatch-driving commands (run, serve, plan).
- ``wait`` — the sentinel-aware ``dev-queue wait`` loop and its emit helpers.
- ``tasks`` — task inspection (tasks) + repo refresh (refresh-all) + attention
  helpers shared with ``status``.
"""

from __future__ import annotations

from cw.cli.dev_queue import (  # noqa: F401  (command registration side effects)
    crud,
    run,
    status,
    tasks,
    wait,
)
from cw.cli.dev_queue._group import (
    _WAIT_EXIT_ATTENTION,
    _WAIT_EXIT_BLOCKED,
    _WAIT_EXIT_FAILED,
    _WAIT_EXIT_SIGNOFF,
    _WAIT_EXIT_TIMEOUT,
)

__all__ = [
    "_WAIT_EXIT_ATTENTION",
    "_WAIT_EXIT_BLOCKED",
    "_WAIT_EXIT_FAILED",
    "_WAIT_EXIT_SIGNOFF",
    "_WAIT_EXIT_TIMEOUT",
]
