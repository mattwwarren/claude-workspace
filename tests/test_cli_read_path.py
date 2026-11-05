"""``cw list`` / ``cw status`` are pure reads (#2938).

Both commands used to run a full ``reconcile()``: ``sessions_lock``, ``gh`` /
``git`` pre-passes, ``claude agents --json``, phantom reaping, fix-worker
dispatch and reviewer requests. A poll of either could therefore spawn a
worker or rewrite the dev-queue. These tests arm every one of those paths
(``reap_policy: auto``, ``review_recipes_enabled``, a pending fix dispatch, a
reapable phantom) and assert the commands touch none of them.

The file-tree snapshot skips ``*.lock`` names: ``cw status`` reads the event
inbox, whose leaf lock opens ``events/.inbox.lock`` in ``"w"`` mode
(``cw.events._inbox_lock``). That zero-byte advisory flock file is a lock
sentinel, not state; the sessions lock is covered separately by
:func:`test_read_command_does_not_take_sessions_lock`.
"""

from __future__ import annotations

import ast
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

import cw.cli.sessions
from cw import _flock
from cw.cli import main
from cw.config import load_state
from cw.models import OrchestratorConfig, ReapPolicy, SessionStatus
from cw.reconcile import fix_dispatch
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._reconcile_helpers import (
    _FIX_LOOP_CLIENT,
    _FIX_LOOP_TICKET,
    _make_pending_fix_dispatch,
    _seed_task,
)
from tests.conftest import _FakeClock, _make_daemon_session, _seed_sessions

_READ_COMMANDS = ("list", "status")
_PHANTOM_ID = "phantom1"
_PHANTOM_NAME = f"{_FIX_LOOP_CLIENT}/auto-dev/{_FIX_LOOP_TICKET}"
_Snapshot = dict[str, tuple[bytes, int]]


class _CallRecorder:
    """Callable stand-in that records each call and returns a fixed value."""

    def __init__(self, result: object) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self._result = result

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return self._result


class _ArmedWorld:
    """The recorders ``armed_world`` patches over reconcile's side effects."""

    def __init__(self, roster: _CallRecorder, dispatch: _CallRecorder) -> None:
        self.roster = roster
        self.dispatch = dispatch


@pytest.fixture
def armed_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ArmedWorld:
    """Arm every reconcile side effect so unchanged code is red.

    A reapable phantom under ``reap_policy: auto``, a RUNNING row carrying a
    pending fix dispatch, live review recipes, and a non-empty roster that
    defeats reconcile's outage guard (same shape as the deleted
    ``test_display_status_reconciles_phantom_active_sessions``).
    """
    workspace = tmp_path / "workspace"
    write_clients_yaml(
        ClientSpec(_FIX_LOOP_CLIENT, workspace_path=workspace),
        ensure_workspaces=True,
    )
    _seed_sessions(
        _make_daemon_session(
            id=_PHANTOM_ID,
            name=_PHANTOM_NAME,
            client=_FIX_LOOP_CLIENT,
            workspace_path=workspace,
            worktree_path=None,
            surface_ref="gone",
            # Older than SPAWN_GRACE_SECONDS, so the grace window does not
            # protect it from phantom-reaping.
            started_at=datetime(2026, 4, 19, tzinfo=UTC),
        )
    )
    _seed_task(
        client=_FIX_LOOP_CLIENT,
        pending_fix_dispatch=_make_pending_fix_dispatch(
            label=f"fix-{_FIX_LOOP_TICKET}"
        ),
    )
    monkeypatch.setattr(
        "cw.reconcile.core.load_orchestrator_config",
        lambda: OrchestratorConfig(
            reap_policy=ReapPolicy.AUTO, review_recipes_enabled=True
        ),
    )
    roster = _CallRecorder([{"sessionId": "decoy000"}])
    monkeypatch.setattr("cw.reconcile.core._claude_agents_json", roster)
    dispatch = _CallRecorder("fix-sess")
    monkeypatch.setattr(fix_dispatch, "dispatch_fix_agent", dispatch)
    return _ArmedWorld(roster=roster, dispatch=dispatch)


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Fail on, and record, every process launch (``gh``, ``git``, ``claude``).

    ``subprocess.run`` / ``check_output`` construct a ``Popen``, so patching
    ``Popen.__init__`` covers them; ``run`` is also patched explicitly.
    """
    launched: list[object] = []

    def _refuse(args: object) -> None:
        launched.append(args)
        msg = f"read path launched a subprocess: {args!r}"
        raise AssertionError(msg)

    def _popen_init(
        self: subprocess.Popen[bytes], args: object, *_a: object, **_k: object
    ) -> None:
        _refuse(args)

    def _run(args: object, *_a: object, **_k: object) -> None:
        _refuse(args)

    monkeypatch.setattr(subprocess.Popen, "__init__", _popen_init)
    monkeypatch.setattr(subprocess, "run", _run)
    return launched


def _snapshot(root: Path) -> _Snapshot:
    """Map every non-lock file under *root* to its bytes and ``st_mtime_ns``."""
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


@pytest.mark.parametrize("command", _READ_COMMANDS)
def test_read_command_is_side_effect_free(
    tmp_path: Path,
    armed_world: _ArmedWorld,
    no_subprocess: list[object],
    command: str,
) -> None:
    before = _snapshot(tmp_path)

    result = CliRunner().invoke(main, [command])

    assert result.exit_code == 0, result.output
    assert _snapshot(tmp_path) == before
    phantom = load_state().find_by_name_or_id(_PHANTOM_ID)
    assert phantom is not None
    assert phantom.status == SessionStatus.ACTIVE
    assert "Reaped phantom" not in result.output
    assert no_subprocess == []
    assert armed_world.roster.calls == []
    assert armed_world.dispatch.calls == []


@pytest.mark.parametrize("command", _READ_COMMANDS)
def test_read_command_does_not_take_sessions_lock(
    held_sessions_lock: Path,
    armed_world: _ArmedWorld,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(_flock, "time", clock)

    result = CliRunner().invoke(main, [command])

    assert result.exit_code == 0, result.output
    assert clock.now == 0
    assert clock.sleeps == []


def _pure_reads_section(guide: str) -> str:
    """The ``**Pure reads`` subsection, up to the next bold or ``#`` header."""
    start = guide.index("**Pure reads")
    rest = guide[start + 2 :]
    ends = [i for i in (rest.find("\n**"), rest.find("\n#")) if i != -1]
    return guide[start : start + 2 + min(ends)] if ends else guide[start:]


def test_guide_pure_reads_section() -> None:
    result = CliRunner().invoke(main, ["guide"])

    assert result.exit_code == 0, result.output
    section = _pure_reads_section(result.output)
    for command in (
        "cw list",
        "cw status",
        "cw session show|list|result|wait",
        "cw dev-queue status|tasks|show",
        "cw orchestrate status",
        "cw board --once",
        ".sessions.json.0.x-backup",
        "cw session list --json",
        "cw doctor --reap",
    ):
        assert command in section, command
    assert "cw queue peek" in section
    assert section.index("cw queue peek") > section.index("Not pure reads")


@pytest.mark.parametrize("command", _READ_COMMANDS)
def test_list_status_help_says_read_only(command: str) -> None:
    result = CliRunner().invoke(main, [command, "--help"])

    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "Read-only" in text
    assert "never reconciles" in text


def test_sessions_module_no_longer_reconciles() -> None:
    tree = ast.parse(Path(cw.cli.sessions.__file__).read_text(encoding="utf-8"))

    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    functions = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    assert "reconcile" not in imported
    assert "operator_read_lock_wait_seconds" not in imported
    assert "_check_and_mark_dead_sessions" not in functions
