"""Bounded locks in ``cw.cli.config_cmds`` (GitHub #2501).

The lane and concurrency-override commands are operator entry points with no
side effect before their lock, so they acquire ``clients_lock`` /
``concurrency_override_lock`` (and, for ``lane rm``, the dev-queue lock) with
``bounded=True``: a held lock surfaces as a clean non-zero exit naming that
lock instead of a silent hang. The broader lane/concurrency behaviour tests
live in ``tests/test_cli.py``.
"""

from __future__ import annotations

import fcntl
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

import cw.config
from cw import _flock
from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
from cw._lock_guard import held_locks
from cw.cli import main
from cw.models import LaneConfig
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _FakeClock, _hold_flock

if TYPE_CHECKING:
    from pathlib import Path

_CLIENT = "acme"


def _no_serve_message(lock_name: str, lock_path: Path) -> str:
    return (
        f"Timed out after 0.5s waiting for the {lock_name} lock {lock_path}."
        f" Another cw process is holding it. Find the holder with `lsof"
        f" {lock_path}` (the lock file records no PID). If the holder is merely"
        " slow, retry, or raise the wait with CW_SESSIONS_LOCK_TIMEOUT_S"
        " (seconds; currently 0.5)."
    )


def _snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


@pytest.fixture(autouse=True)
def acme(tmp_config_dir: Path, tmp_path: Path) -> None:
    write_clients_yaml(
        ClientSpec(
            _CLIENT,
            tmp_path / "ws",
            lanes=[LaneConfig(name="default"), LaneConfig(name="fast")],
        ),
        ensure_workspaces=True,
    )


@pytest.fixture
def fake_lock_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound acquisitions at 0.5s on a fake clock (exact text, no waiting)."""
    monkeypatch.setenv(SESSIONS_LOCK_TIMEOUT_ENV, "0.5")
    monkeypatch.setattr(_flock, "time", _FakeClock())


_CONCURRENCY_COMMANDS: dict[str, list[str]] = {
    "lane-pause": ["lane", "pause", _CLIENT, "default"],
    "lane-resume": ["lane", "resume", _CLIENT, "default"],
    "concurrency-set": ["config", "concurrency", "set", "max_parallel_clients=4"],
    "concurrency-clear-all": ["config", "concurrency", "clear"],
    "concurrency-clear-key": [
        "config",
        "concurrency",
        "clear",
        "max_parallel_clients",
    ],
}


@pytest.mark.usefixtures("fake_lock_clock")
class TestHeldLockFailsCleanly:
    @pytest.mark.parametrize("command", list(_CONCURRENCY_COMMANDS))
    def test_concurrency_override_commands(self, command: str) -> None:
        lock_path = cw.config.concurrency_override_lock_file()
        overrides = cw.config.concurrency_override_file()
        before = _snapshot(overrides)

        with _hold_flock(lock_path):
            result = CliRunner().invoke(main, _CONCURRENCY_COMMANDS[command])

        assert result.exit_code != 0
        assert _no_serve_message("concurrency_override", lock_path) in result.output
        assert "cw dev-queue serve" not in result.output
        assert _snapshot(overrides) == before

    def test_lane_add(self) -> None:
        lock_path = cw.config.clients_lock_file()
        before = cw.config.clients_file().read_bytes()

        with _hold_flock(lock_path):
            result = CliRunner().invoke(main, ["lane", "add", _CLIENT, "slow"])

        assert result.exit_code != 0
        assert _no_serve_message("clients", lock_path) in result.output
        assert cw.config.clients_file().read_bytes() == before

    def test_lane_rm_with_clients_held_releases_the_outer_dev_queue_lock(
        self,
    ) -> None:
        clients_lock_path = cw.config.clients_lock_file()
        before = cw.config.clients_file().read_bytes()

        with _hold_flock(clients_lock_path):
            result = CliRunner().invoke(main, ["lane", "rm", _CLIENT, "fast"])

        assert result.exit_code != 0
        assert _no_serve_message("clients", clients_lock_path) in result.output
        assert cw.config.clients_file().read_bytes() == before
        assert held_locks() == ()
        # The outer dev-queue lock was released on unwind.
        with cw.config.dev_queue_lock().open("w") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_lane_rm_with_dev_queue_held(self) -> None:
        dev_queue_lock_path = cw.config.dev_queue_lock()
        before = cw.config.clients_file().read_bytes()

        with _hold_flock(dev_queue_lock_path):
            result = CliRunner().invoke(main, ["lane", "rm", _CLIENT, "fast"])

        assert result.exit_code != 0
        assert f"waiting for the dev_queue lock {dev_queue_lock_path}" in result.output
        assert "typically `cw dev-queue serve`" in result.output
        assert cw.config.clients_file().read_bytes() == before


class TestFreeLocksStillWork:
    """No env, no holder: the bounded commands behave exactly as before."""

    @pytest.mark.parametrize(
        "args",
        [
            ["lane", "pause", _CLIENT, "default"],
            ["lane", "resume", _CLIENT, "default"],
            ["lane", "add", _CLIENT, "slow"],
            ["lane", "rm", _CLIENT, "fast"],
            ["config", "concurrency", "set", "max_parallel_clients=4"],
            ["config", "concurrency", "clear", "max_parallel_clients"],
            ["config", "concurrency", "clear"],
        ],
        ids=" ".join,
    )
    def test_command_succeeds(self, args: list[str]) -> None:
        result = CliRunner().invoke(main, args)

        assert result.exit_code == 0, result.output
        assert held_locks() == ()
