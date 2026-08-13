"""Tests for the ``cw guard-cwd`` PreToolUse hook command (#940 R9a).

The guard blocks a Bash tool call (exit 2) only when the hook's ``cwd``
resolves to the operator's main checkout (``workspace_path`` in
``cw-context.json``). Every other outcome — a distinct worktree, a missing or
malformed context, a missing ``workspace_path``, or unreadable stdin — is a
best-effort no-op (exit 0) so a broken hook never blocks every Bash call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.guard import _read_hook_cwd
from tests.conftest import _invoke_hook_command, _write_hook_context_file

if TYPE_CHECKING:
    from pathlib import Path

_BLOCK_EXIT = 2


def _write_context(worktree: Path, workspace_path: Path | None) -> None:
    """Thin alias onto the shared conftest materializer (#1646)."""
    _write_hook_context_file(worktree, workspace_path=workspace_path)


def _invoke(cwd: Path) -> int:
    return int(_invoke_hook_command("guard-cwd", {"cwd": str(cwd)}).exit_code)


def test_guard_blocks_when_cwd_is_main_checkout(tmp_path: Path) -> None:
    """cwd resolves to workspace_path (the main checkout) → exit 2 (block)."""
    main_dir = tmp_path / "main-checkout"
    main_dir.mkdir()
    _write_context(main_dir, workspace_path=main_dir)

    assert _invoke(main_dir) == _BLOCK_EXIT


def test_guard_allows_distinct_worktree(tmp_path: Path) -> None:
    """cwd is a worktree distinct from workspace_path → exit 0 (allow)."""
    main_dir = tmp_path / "main-checkout"
    main_dir.mkdir()
    wt_dir = tmp_path / "wt-impl"
    wt_dir.mkdir()
    _write_context(wt_dir, workspace_path=main_dir)

    assert _invoke(wt_dir) == 0


def test_guard_blocks_symlinked_cwd_resolving_to_main(tmp_path: Path) -> None:
    """A symlinked cwd that resolves to workspace_path → exit 2 (block)."""
    main_dir = tmp_path / "main-checkout"
    main_dir.mkdir()
    _write_context(main_dir, workspace_path=main_dir)
    link = tmp_path / "link-to-main"
    link.symlink_to(main_dir)

    assert _invoke(link) == _BLOCK_EXIT


def test_guard_noop_on_missing_context(tmp_path: Path) -> None:
    """No cw-context.json under cwd → exit 0 (best-effort no-op)."""
    bare = tmp_path / "bare"
    bare.mkdir()

    assert _invoke(bare) == 0


def test_guard_noop_on_malformed_context(tmp_path: Path) -> None:
    """Malformed cw-context.json → exit 0 (never crash the hook)."""
    cwd = tmp_path / "broken"
    (cwd / ".claude").mkdir(parents=True)
    (cwd / ".claude" / "cw-context.json").write_text("{ not json")

    assert _invoke(cwd) == 0


def test_guard_noop_when_workspace_path_absent(tmp_path: Path) -> None:
    """USER-origin-style context with null workspace_path → exit 0."""
    cwd = tmp_path / "user-home"
    cwd.mkdir()
    _write_context(cwd, workspace_path=None)

    assert _invoke(cwd) == 0


def test_guard_noop_on_malformed_stdin(tmp_path: Path) -> None:
    """Non-JSON stdin → exit 0 (no cwd extractable)."""
    runner = CliRunner()
    result = runner.invoke(main, ["guard-cwd"], input="not json at all")
    assert result.exit_code == 0


def test_guard_noop_on_empty_stdin(tmp_path: Path) -> None:
    """Empty stdin → exit 0."""
    runner = CliRunner()
    result = runner.invoke(main, ["guard-cwd"], input="")
    assert result.exit_code == 0


def test_guard_noop_on_non_dict_context(tmp_path: Path) -> None:
    """cw-context.json that parses to a non-object (list) → exit 0."""
    cwd = tmp_path / "listctx"
    (cwd / ".claude").mkdir(parents=True)
    (cwd / ".claude" / "cw-context.json").write_text("[]")

    assert _invoke(cwd) == 0


def test_guard_survives_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside the block check is swallowed → exit 0 (hook never wedges)."""

    def _boom() -> bool:
        msg = "unexpected"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.cli.guard._guard_cwd_blocks", _boom)
    runner = CliRunner()
    result = runner.invoke(main, ["guard-cwd"], input=json.dumps({"cwd": "/x"}))
    assert result.exit_code == 0


def test_read_hook_cwd_returns_none_on_stdin_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_read_hook_cwd returns None when stdin.read raises (best-effort).

    The actual stdin read lives in the shared ``cw.cli._hook_io`` module
    (#940 code review — deduplicated from cli/sessions.py's Stop-hook reader).
    """

    class _BoomStdin:
        def read(self) -> str:
            msg = "stdin gone"
            raise OSError(msg)

    monkeypatch.setattr("cw.cli._hook_io.sys.stdin", _BoomStdin())
    assert _read_hook_cwd() is None
