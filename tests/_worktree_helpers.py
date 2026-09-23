"""Shared test helpers for the ``cw.worktree`` package's split test files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.worktree import (
    _freshness,
    _git,
    _lifecycle,
    _paths,
    _refresh,
    _scope,
    _unsaved,
)

if TYPE_CHECKING:
    import pytest

_SUBMODULES = (_git, _paths, _scope, _freshness, _unsaved, _refresh, _lifecycle)


def patch_worktree(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    """Patch *name* on every ``cw.worktree`` submodule that binds it.

    Submodules import shared helpers by name (``from cw.worktree._git import
    _run_git``), so each holds its own binding and one ``setattr`` on the
    defining submodule misses callers in the others. Patching every binding
    keeps the pre-split meaning of a single ``cw.worktree.<name>`` patch.
    """
    bound = [mod for mod in _SUBMODULES if hasattr(mod, name)]
    if not bound:
        msg = f"no cw.worktree submodule binds {name!r}"
        raise AttributeError(msg)
    for mod in bound:
        monkeypatch.setattr(mod, name, value)
