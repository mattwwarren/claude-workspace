"""Bounded locks in ``cw.cli.config_cmds`` (GitHub #2501).

The lane and concurrency-override commands are operator entry points with no
side effect before their lock, so they acquire ``clients_lock`` /
``concurrency_override_lock`` (and, for ``lane rm``, the dev-queue lock) with
``bounded=True``: a held lock surfaces as a clean non-zero exit naming that
lock instead of a silent hang. The broader lane/concurrency behaviour tests
live in ``tests/test_cli.py``.

Also covers ``cw config concurrency get -c/--client`` (#2690) and its
``--json`` output over a lane override holding a timestamp (#2696), and the
client dispatch-ceiling setter ``cw config concurrency set|clear ceiling -c``
with its worker refusal and ``lane.config_changed`` audit event (#2697).
"""

from __future__ import annotations

import fcntl
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

import cw.config
from cw import _flock
from cw._flock import SESSIONS_LOCK_TIMEOUT_ENV
from cw._lock_guard import held_locks
from cw.cli import main
from cw.models import (
    ClientConcurrencyOverride,
    ConcurrencyOverrides,
    LaneConcurrencyOverride,
    LaneConfig,
    OrchestratorEventType,
)
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _FakeClock, _hold_flock

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from tests.conftest import CapturedEvent

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
def _operator_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The `-c` ceiling commands and `lane set` refuse inside a headless worker
    # before their lock, and this suite may itself run inside one.
    _enter_operator_machine(monkeypatch, tmp_path)


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
    "ceiling-set": ["config", "concurrency", "set", "ceiling=4", "-c", _CLIENT],
    "ceiling-clear": ["config", "concurrency", "clear", "ceiling", "-c", _CLIENT],
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

    def test_lane_set(self, capture_events: Callable[..., list[CapturedEvent]]) -> None:
        events = capture_events("cw.cli.config_cmds")
        lock_path = cw.config.clients_lock_file()
        before = cw.config.clients_file().read_bytes()

        with _hold_flock(lock_path):
            result = CliRunner().invoke(
                main, ["lane", "set", _CLIENT, "fast", "max_parallel=3"]
            )

        assert result.exit_code != 0
        assert _no_serve_message("clients", lock_path) in result.output
        assert cw.config.clients_file().read_bytes() == before
        assert events == []

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
            ["config", "concurrency", "set", "ceiling=4", "-c", _CLIENT],
            ["config", "concurrency", "clear", "ceiling", "-c", _CLIENT],
            ["lane", "set", _CLIENT, "fast", "max_parallel=3"],
        ],
        ids=" ".join,
    )
    def test_command_succeeds(self, args: list[str]) -> None:
        result = CliRunner().invoke(main, args)

        assert result.exit_code == 0, result.output
        assert held_locks() == ()


def _concurrency_get_json(*args: str) -> dict[str, object]:
    result = CliRunner().invoke(main, ["config", "concurrency", "get", *args, "--json"])
    assert result.exit_code == 0, result.output
    data: dict[str, object] = json.loads(result.output)
    return data


def _lane_override_json(override: LaneConcurrencyOverride) -> dict[str, object]:
    """``override.lanes["acme/default"]`` from ``config concurrency get --json``."""
    cw.config._save_concurrency_overrides(
        ConcurrencyOverrides(lanes={f"{_CLIENT}/default": override})
    )
    data = _concurrency_get_json()
    override_json = data["override"]
    assert isinstance(override_json, dict)
    lane: dict[str, object] = override_json["lanes"][f"{_CLIENT}/default"]
    return lane


class TestConcurrencyGetJsonLaneOverride:
    """``--json`` must serialize an override's datetimes (#2696, R14)."""

    def test_json_with_paused_lane_override_is_valid(self) -> None:
        lane = _lane_override_json(
            LaneConcurrencyOverride(
                paused=True,
                paused_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
                pause_source="operator",
                circuit_auto_resume_at=datetime(2026, 10, 18, 12, 15, tzinfo=UTC),
            )
        )

        assert lane["paused_at"] == "2026-10-18T12:00:00Z"
        assert lane["circuit_auto_resume_at"] == "2026-10-18T12:15:00Z"

    def test_json_with_starved_debounce_stamp_is_valid(self) -> None:
        lane = _lane_override_json(
            LaneConcurrencyOverride(
                lane_starved_notify_next_eligible_at=datetime(
                    2026, 10, 18, 12, 0, tzinfo=UTC
                )
            )
        )

        assert lane["lane_starved_notify_next_eligible_at"] == "2026-10-18T12:00:00Z"


class TestConcurrencyGetClient:
    """``cw config concurrency get -c/--client`` (#2690)."""

    def test_default_ceiling_when_nothing_declared(self) -> None:
        data = _concurrency_get_json("-c", _CLIENT)
        assert data["client"] == {"name": _CLIENT, "ceiling": 2, "source": "default"}

    def test_declared_per_client_ceiling(self) -> None:
        path = cw.config.orchestrator_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("default_ceiling: 2\nper_client_ceiling:\n  acme: 3\n")

        data = _concurrency_get_json("--client", _CLIENT)

        assert data["client"] == {"name": _CLIENT, "ceiling": 3, "source": "declared"}

    def test_override_ceiling_wins(self) -> None:
        path = cw.config.orchestrator_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("default_ceiling: 2\nper_client_ceiling:\n  acme: 3\n")
        cw.config._save_concurrency_overrides(
            ConcurrencyOverrides(
                clients={_CLIENT: ClientConcurrencyOverride(ceiling=5)}
            )
        )

        data = _concurrency_get_json("-c", _CLIENT)

        assert data["client"] == {"name": _CLIENT, "ceiling": 5, "source": "override"}

    def test_override_without_ceiling_never_wins(self) -> None:
        path = cw.config.orchestrator_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("default_ceiling: 2\nper_client_ceiling:\n  acme: 3\n")
        cw.config._save_concurrency_overrides(
            ConcurrencyOverrides(clients={_CLIENT: ClientConcurrencyOverride()})
        )

        data = _concurrency_get_json("-c", _CLIENT)

        assert data["client"] == {"name": _CLIENT, "ceiling": 3, "source": "declared"}

    def test_text_mode_appends_client_line(self) -> None:
        path = cw.config.orchestrator_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("default_ceiling: 2\nper_client_ceiling:\n  acme: 3\n")

        result = CliRunner().invoke(
            main, ["config", "concurrency", "get", "-c", _CLIENT]
        )

        assert result.exit_code == 0, result.output
        assert "Declared (orchestrator.yaml):" in result.output
        assert "Override (concurrency_overrides.json):" in result.output
        assert "Effective (merged):" in result.output
        assert result.output.rstrip("\n").endswith(
            f"Client {_CLIENT} configured ceiling: 3 (source: declared)"
        )

    def test_without_client_output_is_unchanged(self) -> None:
        data = _concurrency_get_json()
        assert set(data) == {"declared", "override", "effective"}

        result = CliRunner().invoke(main, ["config", "concurrency", "get"])
        assert result.exit_code == 0, result.output
        assert "Client " not in result.output

    def test_unknown_client_fails(self) -> None:
        result = CliRunner().invoke(
            main, ["config", "concurrency", "get", "-c", "nope"]
        )
        assert result.exit_code != 0
        assert "Unknown client 'nope'" in result.output


# --- Client dispatch-ceiling setter (#2697) ---

_STAMP = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _concurrency(*args: str) -> Result:
    return CliRunner().invoke(main, ["config", "concurrency", *args])


def _client_override() -> ClientConcurrencyOverride | None:
    return cw.config._load_concurrency_overrides().clients.get(_CLIENT)


@pytest.fixture
def ceiling_events(
    capture_events: Callable[..., list[CapturedEvent]],
) -> list[CapturedEvent]:
    return capture_events(
        "cw.cli.config_cmds", OrchestratorEventType.LANE_CONFIG_CHANGED
    )


def _ceiling_event(old: int | None, new: int | None) -> CapturedEvent:
    return (
        OrchestratorEventType.LANE_CONFIG_CHANGED,
        {"client": _CLIENT, "lane": None, "changes": {"ceiling": [old, new]}},
        None,
    )


def _seed_client_override(ceiling: int | None) -> None:
    cw.config._save_concurrency_overrides(
        ConcurrencyOverrides(
            max_parallel_clients=6,
            clients={
                _CLIENT: ClientConcurrencyOverride(
                    ceiling=ceiling,
                    consecutive_freshness_blocks=3,
                    dispatch_stale_notify_next_eligible_at=_STAMP,
                )
            },
        )
    )


class TestClientCeilingSet:
    def test_set_writes_the_client_ceiling(
        self, ceiling_events: list[CapturedEvent]
    ) -> None:
        result = _concurrency("set", "ceiling=4", "-c", _CLIENT)

        assert result.exit_code == 0, result.output
        assert result.output == (
            f"Set ceiling=4 for client '{_CLIENT}' (override: (unset) -> 4).\n"
        )
        override = _client_override()
        assert override is not None
        assert override.ceiling == 4
        assert ceiling_events == [_ceiling_event(None, 4)]
        data = _concurrency_get_json("-c", _CLIENT)
        assert data["client"] == {"name": _CLIENT, "ceiling": 4, "source": "override"}

    def test_set_preserves_the_other_override_fields(
        self, ceiling_events: list[CapturedEvent]
    ) -> None:
        _seed_client_override(2)

        result = _concurrency("set", "ceiling=5", "--client", _CLIENT)

        assert result.exit_code == 0, result.output
        assert "(override: 2 -> 5)." in result.output
        overrides = cw.config._load_concurrency_overrides()
        assert overrides.max_parallel_clients == 6
        assert overrides.clients[_CLIENT] == ClientConcurrencyOverride(
            ceiling=5,
            consecutive_freshness_blocks=3,
            dispatch_stale_notify_next_eligible_at=_STAMP,
        )
        assert ceiling_events == [_ceiling_event(2, 5)]

    def test_resetting_the_same_value_is_a_no_op(
        self, ceiling_events: list[CapturedEvent]
    ) -> None:
        _seed_client_override(4)
        before = _snapshot(cw.config.concurrency_override_file())

        result = _concurrency("set", "ceiling=4", "-c", _CLIENT)

        assert result.exit_code == 0, result.output
        assert result.output == (
            f"Set ceiling=4 for client '{_CLIENT}' (override unchanged: 4).\n"
        )
        assert _snapshot(cw.config.concurrency_override_file()) == before
        assert ceiling_events == []

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["ceiling=4"], "Supported: max_parallel_clients"),
            (["ceiling=4"], "-c CLIENT"),
            (
                ["max_parallel_clients=4", "-c", _CLIENT],
                "With -c/--client the only concurrency key is 'ceiling'",
            ),
            (["ceiling=0", "-c", _CLIENT], "'ceiling' must be a positive integer"),
            (["ceiling=-1", "-c", _CLIENT], "'ceiling' must be a positive integer"),
            (["ceiling=abc", "-c", _CLIENT], "'ceiling' must be a positive integer"),
            (["ceiling", "-c", _CLIENT], "Expected key=value"),
            (["ceiling=4", "-c", "nope"], "Unknown client 'nope'"),
        ],
        ids=[
            "no-client-supported",
            "no-client-mentions-c",
            "host-key-with-c",
            "zero",
            "negative",
            "not-int",
            "no-equals",
            "unknown-client",
        ],
    )
    def test_refusals(
        self, ceiling_events: list[CapturedEvent], args: list[str], message: str
    ) -> None:
        before = _snapshot(cw.config.concurrency_override_file())

        result = _concurrency("set", *args)

        assert result.exit_code != 0
        assert message in result.output
        assert _snapshot(cw.config.concurrency_override_file()) == before
        assert ceiling_events == []

    def test_host_wide_set_is_unchanged(
        self, ceiling_events: list[CapturedEvent]
    ) -> None:
        result = _concurrency("set", "max_parallel_clients=4")

        assert result.exit_code == 0, result.output
        assert result.output == "Set max_parallel_clients=4\n"
        assert cw.config._load_concurrency_overrides().max_parallel_clients == 4
        assert ceiling_events == []

    def test_help_mentions_the_ceiling_and_the_worker_refusal(self) -> None:
        result = _concurrency("set", "--help")

        assert result.exit_code == 0, result.output
        text = " ".join(result.output.split())
        assert "one client's ceiling with -c" in text
        assert "refused inside a dispatch worker" in text


class TestClientCeilingClear:
    def test_clear_resets_only_the_ceiling(
        self, ceiling_events: list[CapturedEvent]
    ) -> None:
        _seed_client_override(4)

        result = _concurrency("clear", "ceiling", "-c", _CLIENT)

        assert result.exit_code == 0, result.output
        assert result.output == (
            f"Cleared override for 'ceiling' on client '{_CLIENT}'"
            " (override: 4 -> (unset)).\n"
        )
        overrides = cw.config._load_concurrency_overrides()
        assert overrides.max_parallel_clients == 6
        assert overrides.clients[_CLIENT] == ClientConcurrencyOverride(
            consecutive_freshness_blocks=3,
            dispatch_stale_notify_next_eligible_at=_STAMP,
        )
        assert ceiling_events == [_ceiling_event(4, None)]

    @pytest.mark.parametrize("ceiling", [None, "absent"], ids=["unset", "no-entry"])
    def test_nothing_to_clear_is_a_no_op(
        self, ceiling_events: list[CapturedEvent], ceiling: str | None
    ) -> None:
        if ceiling is None:
            _seed_client_override(None)
        before = _snapshot(cw.config.concurrency_override_file())

        result = _concurrency("clear", "ceiling", "-c", _CLIENT)

        assert result.exit_code == 0, result.output
        assert result.output == (
            f"No ceiling override is set for client '{_CLIENT}'.\n"
        )
        assert _snapshot(cw.config.concurrency_override_file()) == before
        assert ceiling_events == []

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["-c", _CLIENT], "With -c/--client, KEY must be 'ceiling'"),
            (
                ["max_parallel_clients", "-c", _CLIENT],
                "With -c/--client, KEY must be 'ceiling'",
            ),
            (["ceiling"], "Supported: max_parallel_clients"),
            (["ceiling", "-c", "nope"], "Unknown client 'nope'"),
        ],
        ids=["bare-with-c", "host-key-with-c", "ceiling-without-c", "unknown-client"],
    )
    def test_refusals(
        self, ceiling_events: list[CapturedEvent], args: list[str], message: str
    ) -> None:
        _seed_client_override(4)
        before = _snapshot(cw.config.concurrency_override_file())

        result = _concurrency("clear", *args)

        assert result.exit_code != 0
        assert message in result.output
        assert _snapshot(cw.config.concurrency_override_file()) == before
        assert ceiling_events == []

    @pytest.mark.parametrize(
        ("args", "output"),
        [
            ([], "Cleared all concurrency overrides.\n"),
            (
                ["max_parallel_clients"],
                "Cleared override for 'max_parallel_clients'.\n",
            ),
        ],
        ids=["all", "host-key"],
    )
    def test_host_wide_clear_is_unchanged(
        self, ceiling_events: list[CapturedEvent], args: list[str], output: str
    ) -> None:
        _seed_client_override(4)

        result = _concurrency("clear", *args)

        assert result.exit_code == 0, result.output
        assert result.output == output
        assert cw.config._load_concurrency_overrides().max_parallel_clients is None
        assert ceiling_events == []


class TestClientCeilingWorkerRefusal:
    @pytest.mark.parametrize(
        "args",
        [["set", "ceiling=4", "-c", _CLIENT], ["clear", "ceiling", "-c", _CLIENT]],
        ids=" ".join,
    )
    def test_headless_context_refuses(
        self, tmp_path: Path, ceiling_events: list[CapturedEvent], args: list[str]
    ) -> None:
        _seed_client_override(2)
        _write_session_context(tmp_path, headless=True)
        before = _snapshot(cw.config.concurrency_override_file())

        result = _concurrency(*args)

        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert _snapshot(cw.config.concurrency_override_file()) == before
        assert ceiling_events == []

    def test_worker_refusal_precedes_the_client_lookup(self, tmp_path: Path) -> None:
        _write_session_context(tmp_path, headless=True)

        result = _concurrency("set", "ceiling=4", "-c", "nope")

        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert "Unknown client" not in result.output


class TestClientCeilingEmitFailure:
    def test_emit_failure_says_the_change_was_applied(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "inbox full"
            raise OSError(msg)

        monkeypatch.setattr("cw.cli.config_cmds.record_event", _fail)

        with caplog.at_level(logging.WARNING, logger="cw.cli.config_cmds"):
            result = _concurrency("set", "ceiling=4", "-c", _CLIENT)

        assert result.exit_code != 0
        text = " ".join(result.output.split())
        assert (
            f"The change to the dispatch ceiling of client '{_CLIENT}' WAS applied"
            " (ceiling: (unset) -> 4)"
        ) in text
        override = _client_override()
        assert override is not None
        assert override.ceiling == 4
        assert [
            r.levelno for r in caplog.records if r.name == "cw.cli.config_cmds"
        ] == [logging.WARNING]
