"""The GUIDE's pure-read list is a tested contract (#2938, #2955).

``cw guide`` publishes a "Pure reads" list that external pollers rely on. Each
listed command must be a pure read: no ``sessions_lock`` acquisition, no state
write under the config or state trees, no subprocess launch. #2938 established
that for ``cw list`` / ``cw status``; #2955 extends the same tests to every
command the GUIDE lists (``_PURE_READS``, 11 entries; ``cw queue peek`` stays
under "Not pure reads" because it runs ``gh``).

Each entry runs against an armed world (``reap_policy: auto``,
``review_recipes_enabled``, a pending fix dispatch, a reapable phantom, a
COMPLETED session, a commit-stage pre-commit config) so every reconcile and
probe path is live. The file-tree snapshot skips ``*.lock`` names:
``cw status`` reads the event inbox, whose leaf lock opens
``events/.inbox.lock`` in ``"w"`` mode (``cw.events._inbox_lock``). That
zero-byte advisory flock file is a lock sentinel, not state.

``orchestrator.yaml`` is the one documented first-touch exception (GUIDE
caveat 3: commands that load the orchestrator config create it with defaults if
absent). Only entries with ``loads_orchestrator_config=True`` are pre-seeded,
so every other entry stays strictly "no new files"; a flag-accuracy guard keeps
the flags honest. The sessions-lock contract is asserted here; the GUIDE's
dev-queue-lock claim is untested and deferred to #3112.
"""

from __future__ import annotations

import ast
import fcntl
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import pytest
from click.testing import CliRunner

import cw.cli.sessions
from cw import _flock
from cw.cli import main
from cw.config import load_state, orchestrator_config_file
from cw.models import OrchestratorConfig, ReapPolicy, SessionStatus
from cw.reconcile import fix_dispatch
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._commit_hook_helpers import write_commit_hook_config
from tests._doctor_helpers import write_orchestrator_yaml
from tests._reconcile_helpers import (
    _FIX_LOOP_CLIENT,
    _FIX_LOOP_TICKET,
    _make_pending_fix_dispatch,
    _seed_task,
)
from tests._shared.lock_helpers import _FakeClock
from tests._shared.session_seeding import _make_daemon_session, _seed_sessions

_READ_COMMANDS = ("list", "status")
_PHANTOM_ID = "phantom1"
_PHANTOM_NAME = f"{_FIX_LOOP_CLIENT}/auto-dev/{_FIX_LOOP_TICKET}"
_DONE_ID = "done0001"
_DONE_LAST_RESULT = {"status": "shipped", "pr": 1}
_Snapshot = dict[str, tuple[bytes, int]]
_GUIDE_BULLET_SPAN = re.compile(r"`(cw [^`]+)`")


@dataclass(frozen=True)
class _PureRead:
    """One GUIDE pure-read entry: its key, argv, and a recorded output marker.

    ``loads_orchestrator_config`` marks commands that load ``orchestrator.yaml``
    (creating it when absent); only those are pre-seeded before the snapshot.
    """

    guide_key: str
    argv: tuple[str, ...]
    marker: str
    loads_orchestrator_config: bool = False
    exit_code: int = 0


_PURE_READS: tuple[_PureRead, ...] = (
    _PureRead("list", ("list",), _PHANTOM_ID),
    _PureRead("status", ("status",), "Active sessions:    1"),
    _PureRead("session show", ("session", "show", _PHANTOM_ID), "id: phantom1"),
    _PureRead("session list", ("session", "list"), _PHANTOM_ID),
    _PureRead(
        "session result",
        ("session", "result", _DONE_ID),
        '"status": "shipped"',
    ),
    _PureRead(
        "session wait",
        ("session", "wait", _DONE_ID),
        f"session {_DONE_ID}: completed",
    ),
    _PureRead(
        "dev-queue status --json",
        ("dev-queue", "status", "--json"),
        _FIX_LOOP_CLIENT,
        loads_orchestrator_config=True,
    ),
    _PureRead(
        "dev-queue tasks",
        ("dev-queue", "tasks"),
        _FIX_LOOP_TICKET,
        loads_orchestrator_config=True,
    ),
    _PureRead(
        "dev-queue show",
        ("dev-queue", "show", _FIX_LOOP_TICKET),
        f"ticket: {_FIX_LOOP_TICKET}",
        loads_orchestrator_config=True,
    ),
    _PureRead("orchestrate status", ("orchestrate", "status"), _PHANTOM_ID),
    _PureRead(
        "board --once",
        ("board", "--once"),
        _FIX_LOOP_CLIENT,
        loads_orchestrator_config=True,
    ),
)


class _CallRecorder:
    """Callable stand-in that records each call and returns a fixed value."""

    def __init__(self, result: object) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self._result = result

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return self._result


class _ArmedWorld:
    """The recorders and seed hooks ``armed_world`` sets up."""

    def __init__(
        self, roster: _CallRecorder, dispatch: _CallRecorder, workspace: Path
    ) -> None:
        self.roster = roster
        self.dispatch = dispatch
        self.workspace = workspace

    def seed_orchestrator_config(self) -> None:
        """Pre-create ``orchestrator.yaml`` for a loading command (GUIDE caveat 3).

        Called per matrix entry, before the snapshot, so a loader must leave the
        seeded file untouched and a non-loader that creates it fails the test.
        """
        write_orchestrator_yaml("default_ceiling: 3\n")


@pytest.fixture
def armed_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ArmedWorld:
    """Arm every reconcile side effect and probe so unchanged code is red.

    A reapable phantom under ``reap_policy: auto``, a RUNNING row carrying a
    pending fix dispatch, live review recipes, a non-empty roster that defeats
    reconcile's outage guard, a COMPLETED session for ``session result`` /
    ``session wait``, and a commit-stage pre-commit config that arms the
    ``dev-queue status`` commit-hook probe.

    ``orchestrator.yaml`` is deliberately NOT seeded here: see
    :meth:`_ArmedWorld.seed_orchestrator_config`.
    """
    workspace = tmp_path / "workspace"
    write_clients_yaml(
        ClientSpec(_FIX_LOOP_CLIENT, workspace_path=workspace),
        ensure_workspaces=True,
    )
    write_commit_hook_config(workspace)
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
        ),
        _make_daemon_session(
            id=_DONE_ID,
            name=f"{_FIX_LOOP_CLIENT}/auto-dev/2018",
            client=_FIX_LOOP_CLIENT,
            workspace_path=workspace,
            worktree_path=None,
            status=SessionStatus.COMPLETED,
            last_result=_DONE_LAST_RESULT,
        ),
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
    return _ArmedWorld(roster=roster, dispatch=dispatch, workspace=workspace)


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
    """Map every non-lock file under *root* to its bytes and ``st_mtime_ns``.

    The root is the whole isolated ``tmp_path``: state, config
    (``.claude-workspace/``) and the workspace. ``*.lock`` sentinels are
    skipped; see the module docstring.
    """
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


@pytest.mark.parametrize("command", _PURE_READS, ids=lambda c: c.guide_key)
def test_read_command_is_side_effect_free(
    tmp_path: Path,
    armed_world: _ArmedWorld,
    no_subprocess: list[object],
    command: _PureRead,
) -> None:
    if command.loads_orchestrator_config:
        armed_world.seed_orchestrator_config()
    before = _snapshot(tmp_path)

    result = CliRunner().invoke(main, list(command.argv))

    assert result.exit_code == command.exit_code, result.output
    assert _snapshot(tmp_path) == before
    phantom = load_state().find_by_name_or_id(_PHANTOM_ID)
    assert phantom is not None
    assert phantom.status == SessionStatus.ACTIVE
    assert "Reaped phantom" not in result.output
    # Each command prints content derived from the still-ACTIVE phantom or the
    # seeded world, not an empty or row-less output.
    assert command.marker in result.output, result.output
    assert no_subprocess == []
    assert armed_world.roster.calls == []
    assert armed_world.dispatch.calls == []


@pytest.mark.parametrize("command", _PURE_READS, ids=lambda c: c.guide_key)
def test_read_command_does_not_take_sessions_lock(
    held_sessions_lock: Path,
    armed_world: _ArmedWorld,
    monkeypatch: pytest.MonkeyPatch,
    command: _PureRead,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(_flock, "time", clock)
    real_flock = fcntl.flock
    sessions_lock_inode = held_sessions_lock.stat().st_ino

    def fail_on_blocking_flock(fd: int | IO[str], operation: int) -> None:
        # An unbounded sessions_lock() calls flock(LOCK_EX) with no LOCK_NB and
        # would block forever on held_sessions_lock; fail instead of hanging.
        # The inbox lock shares this fcntl module and blocks by design, so only
        # the sessions lock file is guarded.
        fileno = fd if isinstance(fd, int) else fd.fileno()
        if (
            os.fstat(fileno).st_ino == sessions_lock_inode
            and operation & fcntl.LOCK_EX
            and not operation & fcntl.LOCK_NB
        ):
            msg = "blocking flock(LOCK_EX) taken on the sessions lock"
            raise AssertionError(msg)
        real_flock(fd, operation)

    monkeypatch.setattr(_flock.fcntl, "flock", fail_on_blocking_flock)

    result = CliRunner().invoke(main, list(command.argv))

    assert result.exit_code == command.exit_code, result.output
    assert clock.now == 0
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "command",
    [c for c in _PURE_READS if c.loads_orchestrator_config],
    ids=lambda c: c.guide_key,
)
def test_config_loading_commands_bootstrap_orchestrator_yaml(
    armed_world: _ArmedWorld,
    no_subprocess: list[object],
    command: _PureRead,
) -> None:
    """Flag-accuracy guard: a flagged entry really loads (and so creates) the file.

    Stale-True is caught here; stale-False is caught by the snapshot test.
    """
    assert not orchestrator_config_file().exists()

    result = CliRunner().invoke(main, list(command.argv))

    assert result.exit_code == command.exit_code, result.output
    assert orchestrator_config_file().exists()
    assert no_subprocess == []


def _pure_reads_section(guide: str) -> str:
    """The ``**Pure reads`` subsection, up to the next bold or ``#`` header."""
    start = guide.index("**Pure reads")
    rest = guide[start + 2 :]
    ends = [i for i in (rest.find("\n**"), rest.find("\n#")) if i != -1]
    return guide[start : start + 2 + min(ends)] if ends else guide[start:]


def _expand_guide_span(span: str) -> list[str]:
    """Expand one ``cw ...`` code span into matrix keys.

    ``cw session show|list|result|wait`` becomes four keys; a span with no
    ``|`` becomes one key. Only the first word group containing ``|`` expands.
    """
    tokens = span.removeprefix("cw ").split(" ")
    alt_index = next((i for i, t in enumerate(tokens) if "|" in t), None)
    if alt_index is None:
        return [" ".join(tokens)]
    return [
        " ".join([*tokens[:alt_index], alt, *tokens[alt_index + 1 :]])
        for alt in tokens[alt_index].split("|")
    ]


def _guide_pure_read_keys(section: str) -> set[str]:
    """Matrix keys named by the GUIDE pure-read bullet list.

    Only bullet lines (``- `cw ...```) count, so the ``--json`` script paragraph
    and the "Not pure reads" paragraph do not leak in. Parenthetical code spans
    such as ``wait`` do not start with ``cw ``, so they are skipped.
    """
    keys: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("- `cw "):
            continue
        for span in _GUIDE_BULLET_SPAN.findall(line):
            keys.update(_expand_guide_span(span))
    return keys


def test_guide_pure_reads_section() -> None:
    result = CliRunner().invoke(main, ["guide"])

    assert result.exit_code == 0, result.output
    section = _pure_reads_section(result.output)
    # Two-way drift protection: the GUIDE lists exactly the matrix commands.
    assert _guide_pure_read_keys(section) == {c.guide_key for c in _PURE_READS}
    for command in (
        "cw session list --json",
        "cw doctor --reap",
    ):
        assert command in section, command
    # #2951: the legacy backup moved to the first save; reads never copy it.
    flat = " ".join(section.split())
    assert "copies it once" not in flat
    assert "two first-touch exceptions" in flat
    assert "not by a read" in flat
    assert "cw queue peek" in section
    assert section.index("cw queue peek") > section.index("Not pure reads")
    # Negative control: text-mode dev-queue status is named only under
    # "Not pure reads", never as a pure-read bullet.
    assert section.index("cw dev-queue status`") > section.index("Not pure reads")


@pytest.mark.parametrize("command", _READ_COMMANDS)
def test_read_command_on_legacy_state_leaves_tree_identical(
    command: str, tmp_config_dir: Path
) -> None:
    """A pure read over a legacy sessions.json writes nothing (#2951)."""
    from tests._shared.session_seeding import write_legacy_sessions_json

    state_dir = tmp_config_dir / ".local" / "share" / "cw"
    write_legacy_sessions_json(state_dir, mtime_ns=1_000_000_000_000_000_000)
    before = _snapshot(tmp_config_dir)

    result = CliRunner().invoke(main, [command])

    assert result.exit_code == 0, result.output
    assert _snapshot(tmp_config_dir) == before
    assert not (state_dir / ".sessions.json.0.x-backup").exists()


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
