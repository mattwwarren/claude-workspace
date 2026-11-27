"""Keep unit tests out of the dispatch worker's cw context (#3952).

A dispatched worker runs with ``TMPDIR`` inside its own worktree and with its
cwd inside that worktree, and both carry a headless ``.claude/cw-context.json``.
``cw.cli._worker_guard`` refuses an operator-only command when either input
reports ``headless: true``. The walk that finds the context
(:func:`cw.cli._hook_io.find_cw_context`) has no ceiling, so a test whose
``tmp_path`` sits under the worktree would find the worker's context and be
refused even though it means "not a worker".

:func:`confine_context_discovery` makes that walk stop at the pytest basetemp
and drops ``TMPDIR`` only when it carries a context. :func:`enter_dispatch_shaped_cwd`
starts a test in a headless-shaped cwd so the refusal reproduces in CI too.
``tests/_shared/autouse_isolation.py`` arms both for every non-integration test.

What the cwd guard cannot catch: a refusal test satisfied by the refusal itself
(assert the message, not only ``exit_code != 0``); a test that ``chdir``s back
to the checkout; child processes, which do not get the in-process ceiling;
other walkers such as ``repo_root``; and in-process ``tempfile`` directories
cached before the fixture runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from cw.cli import _hook_io
from cw.models import HOOK_CONTEXT_RELATIVE_PATH

if TYPE_CHECKING:
    import pytest

TMPDIR_VAR = "TMPDIR"


def _has_context_between(start: Path, bound: Path) -> bool:
    """True iff *start* or an ancestor not above *bound* holds a context file."""
    return any(
        (directory / HOOK_CONTEXT_RELATIVE_PATH).is_file()
        for directory in (start, *start.parents)
        if directory.is_relative_to(bound)
    )


def confine_context_discovery(monkeypatch: pytest.MonkeyPatch, ceiling: Path) -> None:
    """Stop the Python context walk at *ceiling* and drop a worker ``TMPDIR``.

    Must run before the walk is patched. A walk that starts inside *ceiling*
    returns ``None`` unless a context sits between the start and *ceiling*;
    a walk that starts outside it delegates to the real finder unchanged.
    """
    tmpdir = os.environ.get(TMPDIR_VAR)
    if tmpdir and _hook_io.find_cw_context(Path(tmpdir)) is not None:
        monkeypatch.delenv(TMPDIR_VAR)
    bound = ceiling.resolve()
    real = _hook_io.find_cw_context

    def _confined(start: Path) -> dict[str, object] | None:
        resolved = start.resolve()
        if resolved.is_relative_to(bound) and not _has_context_between(resolved, bound):
            return None
        return real(start)

    monkeypatch.setattr(_hook_io, "find_cw_context", _confined)


def enter_dispatch_shaped_cwd(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """``chdir`` into *root* after stamping it with a headless cw context."""
    from tests._cli_review_helpers import _write_session_context

    _write_session_context(
        root,
        headless=True,
        session_id="dispatch-shaped-cwd",
        client="dispatch-shaped-client",
        ticket_id="DISPATCH-0",
    )
    monkeypatch.chdir(root)
