"""Tests for ``cw.cli._hook_io``'s shared hook read/write primitives (#1946).

``_hook_io`` now backs three independent hook commands (``cw agent-spawn-pre``,
``cw signal-stop``, and ``cw guard-busy-wait``) but had no dedicated test file
of its own -- every assertion about ``_context_lock`` /
``_write_cw_context_locked`` was reached indirectly through one consumer's CLI
surface. This file exercises the primitives directly so a change to the shared
discipline fails here first, rather than in whichever consumer happens to
cover the affected branch.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.cli._hook_io import (
    _context_lock,
    _read_cw_context,
    _write_cw_context_locked,
)
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from tests.conftest import _hold_context_lock, _write_hook_context_file

if TYPE_CHECKING:
    from pathlib import Path


def _seeded_worktree(tmp_path: Path, name: str = "wt") -> Path:
    worktree = tmp_path / name
    worktree.mkdir()
    _write_hook_context_file(worktree)
    return worktree


def test_context_lock_acquires_when_uncontended(tmp_path: Path) -> None:
    """An uncontended lock yields True and releases on exit."""
    worktree = _seeded_worktree(tmp_path)
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH

    with _context_lock(context_path) as acquired:
        assert acquired is True

    # Releasing must leave the lock immediately re-acquirable.
    with _context_lock(context_path) as acquired_again:
        assert acquired_again is True


def test_context_lock_yields_false_when_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held lock exhausts the bounded retry budget and yields False.

    Never raises: the caller's fail-open path must be an ordinary branch,
    not exception handling -- both consumers run synchronously inside the
    live worker's own turn, where blocking would hang the worker itself.
    """
    monkeypatch.setattr(
        "cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
    )
    worktree = _seeded_worktree(tmp_path)
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH

    with _hold_context_lock(worktree), _context_lock(context_path) as acquired:
        assert acquired is False


def test_write_cw_context_locked_applies_mutation(tmp_path: Path) -> None:
    """The happy path returns True and persists the mutated dict."""
    worktree = _seeded_worktree(tmp_path)

    def _mutate(context: dict[str, object]) -> dict[str, object]:
        context["marker_1946"] = "written"
        return context

    assert _write_cw_context_locked(str(worktree), _mutate) is True

    context = _read_cw_context(str(worktree))
    assert context is not None
    assert context["marker_1946"] == "written"


def test_write_cw_context_locked_returns_false_on_missing_file(
    tmp_path: Path,
) -> None:
    """No cw-context.json under the cwd -> silent False, no file created."""
    bare = tmp_path / "bare"
    bare.mkdir()

    calls: list[object] = []

    def _mutate(context: dict[str, object]) -> dict[str, object]:
        calls.append(context)
        return context

    assert _write_cw_context_locked(str(bare), _mutate) is False
    assert calls == []
    assert not (bare / HOOK_CONTEXT_RELATIVE_PATH).exists()


def test_write_cw_context_locked_returns_false_on_lock_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contended lock skips the write entirely -- mutate_fn never runs."""
    monkeypatch.setattr(
        "cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
    )
    worktree = _seeded_worktree(tmp_path)
    before = (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")

    calls: list[object] = []

    def _mutate(context: dict[str, object]) -> dict[str, object]:
        calls.append(context)
        return context

    with _hold_context_lock(worktree):
        assert _write_cw_context_locked(str(worktree), _mutate) is False

    assert calls == []
    assert (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8") == before


def test_write_cw_context_locked_returns_false_on_malformed_context(
    tmp_path: Path,
) -> None:
    """Unparseable JSON -> silent False rather than a crash in the hook."""
    worktree = tmp_path / "broken"
    (worktree / ".claude").mkdir(parents=True)
    (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_text("{ not json", encoding="utf-8")

    assert _write_cw_context_locked(str(worktree), lambda ctx: ctx) is False


def test_write_cw_context_locked_swallows_mutate_fn_errors(tmp_path: Path) -> None:
    """A mutate_fn that raises fails open -- the hook must never crash."""
    worktree = _seeded_worktree(tmp_path)
    before = (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")

    def _boom(_context: dict[str, object]) -> dict[str, object]:
        msg = "mutation blew up"
        raise RuntimeError(msg)

    assert _write_cw_context_locked(str(worktree), _boom) is False
    assert (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8") == before


def test_read_cw_context_returns_none_for_non_object_payload(tmp_path: Path) -> None:
    """A context file parsing to a list (not an object) reads as None."""
    worktree = tmp_path / "listctx"
    (worktree / ".claude").mkdir(parents=True)
    (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_text("[]", encoding="utf-8")

    assert _read_cw_context(str(worktree)) is None


def test_read_cw_context_round_trips_the_production_writer(tmp_path: Path) -> None:
    """The reader parses exactly what ``_write_hook_context`` produces."""
    worktree = _seeded_worktree(tmp_path)

    context = _read_cw_context(str(worktree))
    assert context is not None
    on_disk = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert context == on_disk
