"""Refuse an operator-only command inside a headless dispatch worker.

Shared by ``cw review settle`` (#2210), ``cw dev-queue resolve`` (#2702),
``cw dev-queue approve`` (#2776), ``cw lane set`` and
``cw config concurrency set``: each records an operator decision the pipeline
must never mint for itself. Moved out of ``cw.cli.review.commands``; each
caller passes its own refusal message.

``find_cw_context`` is looked up on its module at call time rather than bound
at import, as the function-local import it replaces did, so a test that
patches ``cw.cli._hook_io.find_cw_context`` reaches every caller.
"""

from __future__ import annotations

import os
from pathlib import Path

from cw.cli import _hook_io
from cw.exceptions import CwError


def _path_is_in_headless_worker(path: Path) -> bool:
    """True iff the nearest cw context at or above *path* reports ``headless: true``."""
    context = _hook_io.find_cw_context(path)
    return context is not None and context.get("headless") is True


def refuse_inside_dispatch_worker(message: str) -> None:
    """Raise ``CwError(message)`` from inside a headless worker (#2210).

    A worker recording an operator decision about its own run is the pipeline
    adjudicating itself, so it is refused outright. The refusal fires only on
    positive evidence of a worker (ADR-0020): the nearest
    ``.claude/cw-context.json`` (searched upward from cwd) reports
    ``headless: true``, or ``$TMPDIR`` lies inside a worktree whose context
    does. Every other context proceeds — the main checkout, a directory
    outside any repository, a worktree with no context file, a context file
    that cannot be read. Failing to prove a session is not a worker is not a
    reason to hand the command to a human; the orchestrator session runs it.

    ``$TMPDIR`` is checked first: every cw executor points a worker's
    ``TMPDIR`` at its own worktree (``apply_worker_tmpdir``, #2470), and that
    survives a ``cd`` that would get a worker past the cwd check.
    """
    tmpdir = os.environ.get("TMPDIR")
    if (tmpdir and _path_is_in_headless_worker(Path(tmpdir))) or (
        _path_is_in_headless_worker(Path.cwd())
    ):
        raise CwError(message)
