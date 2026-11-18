"""The dispatch tick's outbox-drain launch (#3706, RFC 0019 O2).

``launch_outbox_drains`` launches at most one detached ``cw outbox drain`` per
client. ``spawn_drain`` is replaced by a recorder returning a stub ``Popen``
except in the argv test, where ``subprocess.Popen`` itself is patched.
"""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cw.outbox_drain import run_drain

from cw.config import state_dir
from cw.dispatch import dispatch_tick
from cw.dispatch import outbox_drain as tick_drain
from cw.exceptions import CwError, SessionsLockTimeoutError
from cw.models import ClientConfig, OrchestratorConfig
from cw.models.bindings import Bindings
from cw.models.client import OutboxConfig
from cw.native_daemon import FakeNativeDaemonClient
from cw.outbox import (
    TICK_SCAN_MAX_RECORDS,
    OutboxReason,
    OutboxRole,
    OutboxState,
    UnreachableMarker,
    _layout,
    append_held,
    clear_held,
    write_marker,
)
from tests._clients_yaml import write_clients_yaml
from tests._shared.lock_helpers import _hold_flock, spy_no_locks_held
from tests._shared.outbox_builders import CLIENT, GhThread, client_config, enqueue

_SRC = Path(__file__).resolve().parent.parent / "src" / "cw"
_OTHER = "client-b"
_TICK_CLIENTS = (CLIENT, _OTHER)
# The pinned per-tick record scan bound (plan P8).
_SCAN_BOUND = 200


class _StubProc:
    """A ``Popen`` stand-in whose ``poll()`` is scripted."""

    def __init__(self) -> None:
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code


class _Spawner:
    def __init__(self) -> None:
        self.launched: list[str] = []
        self.procs: list[_StubProc] = []

    def __call__(self, client: str) -> _StubProc:
        self.launched.append(client)
        proc = _StubProc()
        self.procs.append(proc)
        return proc


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick_drain, "_LAUNCHED", {})
    monkeypatch.setattr(tick_drain, "_WARNED_TRUNCATED", set())
    monkeypatch.setattr(tick_drain, "_WARNED_STALLED", set())


@pytest.fixture
def spawner(monkeypatch: pytest.MonkeyPatch) -> _Spawner:
    recorder = _Spawner()
    monkeypatch.setattr("cw.dispatch.outbox_drain.spawn_drain", recorder)
    return recorder


def _clients(tmp_path: Path, **overrides: Any) -> dict[str, ClientConfig]:
    cfg = client_config(tmp_path)
    if overrides:
        cfg = cfg.model_copy(update=overrides)
    return {CLIENT: cfg}


def _disabled(tmp_path: Path) -> dict[str, ClientConfig]:
    return _clients(tmp_path, outbox=OutboxConfig(enabled=False))


def _stall_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "cw outbox drain -c" in r.getMessage()
    ]


class TestLaunch:
    def test_launches_for_a_pending_entry(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        entry = enqueue().entry

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == [CLIENT]
        launch = tick_drain._LAUNCHED[CLIENT]
        assert launch.pending_ids == frozenset({entry.id})

    def test_once_per_client_while_the_first_is_live(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        enqueue()

        tick_drain.launch_outbox_drains(_clients(tmp_path))
        enqueue(marker="new")
        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == [CLIENT]

    def test_relaunch_after_exit_when_the_pending_set_changed(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        enqueue()
        tick_drain.launch_outbox_drains(_clients(tmp_path))
        spawner.procs[0].code = 0
        enqueue(marker="new")

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == [CLIENT, CLIENT]


class TestEnabled:
    def test_enabled_launches(self, tmp_path: Path, spawner: _Spawner) -> None:
        enqueue()

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == [CLIENT]

    def test_disabled_does_nothing_and_reads_nothing(
        self,
        tmp_path: Path,
        spawner: _Spawner,
        monkeypatch: pytest.MonkeyPatch,
        capture_events: Any,
    ) -> None:
        enqueue()
        events = capture_events("cw.outbox_drain")
        peeks = spy_no_locks_held(
            monkeypatch, "cw.dispatch.outbox_drain.peek_pending", returns=None
        )
        helds = spy_no_locks_held(
            monkeypatch, "cw.dispatch.outbox_drain.read_held", returns=None
        )

        tick_drain.launch_outbox_drains(_disabled(tmp_path))

        assert spawner.launched == []
        assert tick_drain._LAUNCHED == {}
        assert peeks == []
        assert helds == []
        assert events == []

    def test_binding_absent_still_launches_and_the_drain_releases(
        self,
        tmp_path: Path,
        spawner: _Spawner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = enqueue().entry
        clients = {CLIENT: client_config(tmp_path, bindings=Bindings())}

        tick_drain.launch_outbox_drains(clients)
        assert spawner.launched == [CLIENT]

        GhThread().install(monkeypatch)
        run_drain(CLIENT, clients[CLIENT], limit=None, force=False)
        record = _layout.read_record(CLIENT, entry.id)
        assert record.state is OutboxState.PENDING
        assert record.reason is OutboxReason.BINDING_UNREACHABLE


class TestSkip:
    def test_no_pending_entries(self, tmp_path: Path, spawner: _Spawner) -> None:
        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == []
        assert not (state_dir() / "outbox" / CLIENT).exists()

    def test_only_unverified_entries(
        self, tmp_path: Path, spawner: _Spawner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        GhThread().install(monkeypatch).script_comments("T-1", "timeout")
        monkeypatch.setattr("cw.tracker_post._READBACK_DELAY_SECONDS", 0.0)
        entry = enqueue().entry
        run_drain(CLIENT, client_config(tmp_path), limit=None, force=False)
        assert _layout.read_record(CLIENT, entry.id).state is OutboxState.UNVERIFIED

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == []

    def test_live_lease(self, tmp_path: Path, spawner: _Spawner) -> None:
        enqueue()

        with _hold_flock(_layout.drain_lock_path(CLIENT)):
            tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == []

    def test_marker_set_for_the_only_pending_role(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        enqueue()
        write_marker(
            CLIENT,
            UnreachableMarker(
                role=OutboxRole.TICKETS, reason=OutboxReason.TRANSIENT_FAILURE
            ),
        )

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == []

    def test_marker_on_one_role_with_another_role_pending_launches(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        enqueue()
        enqueue(role=OutboxRole.KNOWLEDGE, marker="k")
        write_marker(
            CLIENT,
            UnreachableMarker(
                role=OutboxRole.KNOWLEDGE, reason=OutboxReason.BINDING_UNREACHABLE
            ),
        )

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == [CLIENT]


class TestHeld:
    def test_only_held_entries_do_not_launch(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        entry = enqueue().entry
        append_held(CLIENT, entry.id)

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == []

    def test_held_plus_a_new_entry_launches(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        held = enqueue().entry
        append_held(CLIENT, held.id)
        fresh = enqueue(marker="fresh").entry

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == [CLIENT]
        assert tick_drain._LAUNCHED[CLIENT].pending_ids == frozenset({fresh.id})

    def test_cleared_held_file_counts_again(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        entry = enqueue().entry
        append_held(CLIENT, entry.id)
        tick_drain.launch_outbox_drains(_clients(tmp_path))
        assert spawner.launched == []

        clear_held(CLIENT)
        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == [CLIENT]

    def test_corrupt_held_file_reads_empty(
        self, tmp_path: Path, spawner: _Spawner
    ) -> None:
        enqueue()
        _layout.held_path(CLIENT).write_text("{nope", encoding="utf-8")

        tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == [CLIENT]


class TestNoProgressBound:
    @pytest.mark.parametrize("exit_code", [1, 0, 75])
    def test_stalled_drain_is_not_relaunched_until_the_set_changes(
        self,
        tmp_path: Path,
        spawner: _Spawner,
        caplog: pytest.LogCaptureFixture,
        exit_code: int,
    ) -> None:
        enqueue()
        clients = _clients(tmp_path)

        tick_drain.launch_outbox_drains(clients)
        spawner.procs[0].code = exit_code
        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            tick_drain.launch_outbox_drains(clients)
            assert len(_stall_warnings(caplog)) == 1
            tick_drain.launch_outbox_drains(clients)
            assert len(_stall_warnings(caplog)) == 1
        assert spawner.launched == [CLIENT]
        assert f"cw outbox drain -c {CLIENT}" in _stall_warnings(caplog)[0].message

        enqueue(marker="new")
        tick_drain.launch_outbox_drains(clients)

        assert spawner.launched == [CLIENT, CLIENT]

    def test_progress_with_entries_remaining_relaunches(
        self, tmp_path: Path, spawner: _Spawner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        GhThread().install(monkeypatch)
        monkeypatch.setattr("cw.tracker_post._READBACK_DELAY_SECONDS", 0.0)
        enqueue(marker="a")
        enqueue(marker="b")
        clients = _clients(tmp_path)
        tick_drain.launch_outbox_drains(clients)
        run_drain(CLIENT, clients[CLIENT], limit=1, force=False)
        spawner.procs[0].code = 0

        tick_drain.launch_outbox_drains(clients)

        assert spawner.launched == [CLIENT, CLIENT]

    def test_g2_becomes_held_so_no_crash_loop(
        self, tmp_path: Path, spawner: _Spawner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enqueue()
        clients = _clients(tmp_path)
        tick_drain.launch_outbox_drains(clients)

        def _unreadable(_entry: object) -> list[str]:
            from cw.outbox import OutboxRecordError

            msg = "body gone"
            raise OutboxRecordError(msg)

        monkeypatch.setattr("cw.outbox_drain.read_parts", _unreadable)
        run_drain(CLIENT, clients[CLIENT], limit=None, force=False)
        spawner.procs[0].code = 0

        tick_drain.launch_outbox_drains(clients)
        tick_drain.launch_outbox_drains(clients)

        assert spawner.launched == [CLIENT]


class TestBounds:
    def test_truncation_warns_once_per_client(
        self,
        tmp_path: Path,
        spawner: _Spawner,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        for marker in ("a", "b", "c"):
            enqueue(marker=marker)
        real = tick_drain.peek_pending
        seen: list[int] = []

        def _small(client: str, **kwargs: Any) -> Any:
            seen.append(kwargs["scan_limit"])
            return real(client, scan_limit=2, exclude_ids=kwargs["exclude_ids"])

        monkeypatch.setattr("cw.dispatch.outbox_drain.peek_pending", _small)

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            tick_drain.launch_outbox_drains(_clients(tmp_path))
            spawner.procs[0].code = 0
            enqueue(marker="d")
            tick_drain.launch_outbox_drains(_clients(tmp_path))

        truncation = [r for r in caplog.records if "#3715" in r.getMessage()]
        assert len(truncation) == 1
        assert seen == [TICK_SCAN_MAX_RECORDS, TICK_SCAN_MAX_RECORDS]
        assert TICK_SCAN_MAX_RECORDS == _SCAN_BOUND


class TestRobustness:
    def test_spawn_oserror_is_logged_and_other_clients_launch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        enqueue()
        enqueue(_OTHER)
        launched: list[str] = []

        def _spawn(client: str) -> _StubProc:
            if client == CLIENT:
                msg = "fork failed"
                raise OSError(msg)
            launched.append(client)
            return _StubProc()

        monkeypatch.setattr("cw.dispatch.outbox_drain.spawn_drain", _spawn)
        clients = {
            CLIENT: client_config(tmp_path),
            _OTHER: client_config(tmp_path).model_copy(update={"name": _OTHER}),
        }

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            tick_drain.launch_outbox_drains(clients)

        assert launched == [_OTHER]
        assert any(CLIENT in r.getMessage() for r in caplog.records)

    def test_peek_cwerror_is_logged(
        self,
        tmp_path: Path,
        spawner: _Spawner,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        enqueue()

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "bad"
            raise CwError(msg)

        monkeypatch.setattr("cw.dispatch.outbox_drain.peek_pending", _boom)

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            tick_drain.launch_outbox_drains(_clients(tmp_path))

        assert spawner.launched == []
        assert len(caplog.records) == 1

    def test_invalid_client_name_is_logged(
        self,
        tmp_path: Path,
        spawner: _Spawner,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        clients = {"../escape": client_config(tmp_path)}

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            tick_drain.launch_outbox_drains(clients)

        assert spawner.launched == []
        assert len(caplog.records) == 1


class TestSpawnDrain:
    def test_argv_detach_and_log_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        enqueue()
        calls: list[tuple[list[str], dict[str, Any]]] = []

        class _FakePopen:
            def __init__(self, argv: list[str], **kwargs: Any) -> None:
                calls.append((argv, kwargs))

        monkeypatch.setattr("cw.executor_launch.subprocess.Popen", _FakePopen)
        snapshots = spy_no_locks_held(
            monkeypatch, "cw.dispatch.outbox_drain.drain_lease_held", returns=False
        )

        tick_drain.launch_outbox_drains(_clients(Path("/tmp/x")))

        ((argv, kwargs),) = calls
        assert argv == [
            sys.executable,
            "-m",
            "cw",
            "outbox",
            "drain",
            "--client",
            CLIENT,
        ]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
        directory = state_dir() / "outbox" / CLIENT
        assert kwargs["cwd"] == directory
        assert Path(kwargs["stdout"].name) == directory / "drain.log"
        assert snapshots == [()]


@pytest.fixture
def tick_config() -> OrchestratorConfig:
    return OrchestratorConfig(
        tick_interval_seconds=30,
        per_client_max_parallel=dict.fromkeys(_TICK_CLIENTS, 1),
    )


def _write_tick_clients(tmp_path: Path) -> None:
    write_clients_yaml(
        *(
            ClientConfig(name=name, workspace_path=tmp_path / name)
            for name in _TICK_CLIENTS
        )
    )


class TestTickPlacement:
    def _limited(self) -> dict[str, datetime | None]:
        return dict.fromkeys(_TICK_CLIENTS, datetime.now(UTC) + timedelta(hours=1))

    def test_runs_before_the_all_limited_return_with_no_lock_held(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        tick_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_tick_clients(tmp_path)
        seen: list[tuple[tuple[str, ...], tuple[object, ...]]] = []

        def _spy(clients: Any) -> None:
            from cw._lock_guard import held_locks

            seen.append((tuple(clients), held_locks()))

        monkeypatch.setattr("cw.dispatch.tick.launch_outbox_drains", _spy)

        result = dispatch_tick(
            tick_config,
            native_daemon=FakeNativeDaemonClient(),
            usage_limited_until=self._limited(),
        )

        assert result.spawned == 0
        assert seen == [(_TICK_CLIENTS, ())]

    def test_usage_limited_fleet_still_launches_a_drain(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        tick_config: OrchestratorConfig,
        spawner: _Spawner,
    ) -> None:
        _write_tick_clients(tmp_path)
        enqueue(_OTHER)

        dispatch_tick(
            tick_config,
            native_daemon=FakeNativeDaemonClient(),
            usage_limited_until=self._limited(),
        )

        assert spawner.launched == [_OTHER]

    def test_client_filter_narrows(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        tick_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_tick_clients(tmp_path)
        seen: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            "cw.dispatch.tick.launch_outbox_drains",
            lambda clients: seen.append(tuple(clients)),
        )

        dispatch_tick(
            tick_config,
            native_daemon=FakeNativeDaemonClient(),
            usage_limited_until=self._limited(),
            client_filter=_OTHER,
        )

        assert seen == [(_OTHER,)]

    def test_skipped_when_the_sessions_lock_is_held(
        self,
        tmp_dispatch_dirs: Path,
        tmp_path: Path,
        tick_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_tick_clients(tmp_path)
        seen: list[object] = []
        monkeypatch.setattr("cw.dispatch.tick.launch_outbox_drains", seen.append)

        def _locked(**_kwargs: object) -> bool:
            msg = "held"
            raise SessionsLockTimeoutError(msg)

        monkeypatch.setattr("cw.dispatch.tick._reconcile_usage_limited", _locked)

        dispatch_tick(tick_config, native_daemon=FakeNativeDaemonClient())

        assert seen == []


def test_reconcile_does_not_import_the_drain_launcher() -> None:
    offenders: list[str] = []
    for path in (_SRC / "reconcile").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                names = {node.module, *(f"{node.module}.{a.name}" for a in node.names)}
            elif isinstance(node, ast.Import):
                names = {a.name for a in node.names}
            else:
                continue
            if "cw.dispatch.outbox_drain" in names:
                offenders.append(path.name)

    assert offenders == []
