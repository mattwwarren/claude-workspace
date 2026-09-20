"""Tests for cw.reconcile.abandoned_exit (GitHub #2135).

Enablement resolution only: the park's own behaviour is pinned in
tests/test_reconcile_shared_policies.py and its Stop-hook wiring in
tests/test_cli.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cw.config import (
    clients_file,
    load_clients,
    load_orchestrator_config,
    orchestrator_config_file,
)
from cw.models import (
    ClientConfig,
    LaneConfig,
    OrchestratorConfig,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.reconcile.abandoned_exit import (
    PARK_ON_ABANDONED_EXIT_KEY,
    clear_park_config_cache,
    park_gate_open,
    park_on_abandoned_exit_open,
    resolve_park_on_abandoned_exit_enabled,
)
from tests.conftest import _make_ticket_task

KEY = PARK_ON_ABANDONED_EXIT_KEY


def _task(lane: str = "default", **kwargs: object) -> TicketTask:
    return _make_ticket_task(
        ticket_id="GEN-1",
        client="acme",
        status=QueueItemStatus.RUNNING,
        stage=Stage.IMPL,
        lane=lane,
        **kwargs,
    )


def _client(*lanes: LaneConfig) -> ClientConfig:
    return ClientConfig(
        name="acme",
        workspace_path=Path("/tmp/ws"),
        default_branch="main",
        lanes=list(lanes),
    )


class TestResolveParkOnAbandonedExitEnabled:
    """3-tier precedence: ticket > lane > hardcoded off (#2135)."""

    def test_tier1_ticket_override_wins_over_lane_in_both_directions(self) -> None:
        enabling_lane = {"acme": _client(LaneConfig(name="default"))}
        enabling_lane["acme"].lanes[0].park_on_abandoned_exit = {KEY: True}
        assert (
            resolve_park_on_abandoned_exit_enabled(
                _task(park_on_abandoned_exit={KEY: False}), enabling_lane
            )
            is False
        )

        disabling_lane = {
            "acme": _client(
                LaneConfig(name="default", park_on_abandoned_exit={KEY: False})
            )
        }
        assert (
            resolve_park_on_abandoned_exit_enabled(
                _task(park_on_abandoned_exit={KEY: True}), disabling_lane
            )
            is True
        )

    def test_tier2_lane_map_wins_over_the_hardcoded_default(self) -> None:
        clients = {
            "acme": _client(
                LaneConfig(name="default", park_on_abandoned_exit={KEY: True})
            )
        }

        assert resolve_park_on_abandoned_exit_enabled(_task(), clients) is True

    def test_tier2_matches_only_the_tasks_own_lane(self) -> None:
        """An enabled sibling lane must not arm a row on another lane."""
        clients = {
            "acme": _client(
                LaneConfig(name="fastlane", park_on_abandoned_exit={KEY: True}),
                LaneConfig(name="default"),
            )
        }

        assert resolve_park_on_abandoned_exit_enabled(_task(), clients) is False

    def test_tier3_floor_is_off(self) -> None:
        assert resolve_park_on_abandoned_exit_enabled(_task(), {}) is False

    def test_empty_ticket_map_falls_through_to_the_lane(self) -> None:
        """``{}`` is "no opinion", not "disabled"."""
        clients = {
            "acme": _client(
                LaneConfig(name="default", park_on_abandoned_exit={KEY: True})
            )
        }

        assert (
            resolve_park_on_abandoned_exit_enabled(
                _task(park_on_abandoned_exit={}), clients
            )
            is True
        )

    def test_missing_client_falls_to_the_floor_without_raising(self) -> None:
        clients = {"other": _client(LaneConfig(name="default"))}

        assert resolve_park_on_abandoned_exit_enabled(_task(), clients) is False

    def test_missing_lane_falls_to_the_floor_without_raising(self) -> None:
        clients = {
            "acme": _client(
                LaneConfig(name="fastlane", park_on_abandoned_exit={KEY: True})
            )
        }

        assert resolve_park_on_abandoned_exit_enabled(_task(lane="ghost"), clients) is (
            False
        )


class TestParkOnAbandonedExitOpen:
    """The master switch composed with the per-lane resolution (#2135)."""

    @staticmethod
    def _armed_clients() -> dict[str, ClientConfig]:
        return {
            "acme": _client(
                LaneConfig(name="default", park_on_abandoned_exit={KEY: True})
            )
        }

    def test_master_switch_off_closes_an_enabled_lane(self) -> None:
        config = OrchestratorConfig()

        assert config.park_on_abandoned_exit_enabled is False
        assert (
            park_on_abandoned_exit_open(config, _task(), self._armed_clients()) is False
        )

    def test_master_switch_on_with_a_disabled_lane_stays_closed(self) -> None:
        config = OrchestratorConfig(park_on_abandoned_exit_enabled=True)
        clients = {
            "acme": _client(
                LaneConfig(name="default", park_on_abandoned_exit={KEY: False})
            )
        }

        assert park_on_abandoned_exit_open(config, _task(), clients) is False

    def test_master_switch_on_with_an_enabled_lane_is_open(self) -> None:
        config = OrchestratorConfig(park_on_abandoned_exit_enabled=True)

        assert (
            park_on_abandoned_exit_open(config, _task(), self._armed_clients()) is True
        )


_LANE_ON_CLIENTS = {
    "clients": {
        "acme": {
            "workspace_path": "/tmp/ws",
            "lanes": [{"name": "default", "park_on_abandoned_exit": {KEY: True}}],
        }
    }
}


class TestParkGateOpen:
    """``park_gate_open`` is fail-closed and memoized per process (#2135)."""

    @staticmethod
    def _write_orchestrator(text: str) -> None:
        path = orchestrator_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    @staticmethod
    def _write_clients(content: str | bytes) -> None:
        path = clients_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)

    def _arm(self) -> None:
        self._write_orchestrator("park_on_abandoned_exit_enabled: true\n")
        self._write_clients(yaml.safe_dump(_LANE_ON_CLIENTS))

    @staticmethod
    def _count_loads(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """Count the two config reads the gate makes, keeping their behaviour."""
        counts = {"orchestrator": 0, "clients": 0}

        def _orchestrator() -> OrchestratorConfig:
            counts["orchestrator"] += 1
            return load_orchestrator_config()

        def _clients() -> dict[str, ClientConfig]:
            counts["clients"] += 1
            return load_clients()

        monkeypatch.setattr(
            "cw.reconcile.abandoned_exit.load_orchestrator_config", _orchestrator
        )
        monkeypatch.setattr("cw.reconcile.abandoned_exit.load_clients", _clients)
        return counts

    def test_opens_for_an_armed_lane_read_from_disk(self) -> None:
        self._arm()

        assert park_gate_open(_task()) is True

    def test_master_switch_off_never_reads_clients_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shipped default costs exactly one config read."""
        self._write_orchestrator("park_on_abandoned_exit_enabled: false\n")
        self._write_clients(yaml.safe_dump(_LANE_ON_CLIENTS))
        counts = self._count_loads(monkeypatch)

        assert park_gate_open(_task()) is False
        assert counts == {"orchestrator": 1, "clients": 0}

    def test_resolves_the_config_once_per_client_per_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm()
        counts = self._count_loads(monkeypatch)

        results = [park_gate_open(_task()) for _ in range(3)]

        assert results == [True, True, True]
        assert counts == {"orchestrator": 1, "clients": 1}

    def test_cache_is_keyed_by_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._arm()
        counts = self._count_loads(monkeypatch)
        other = _make_ticket_task(
            ticket_id="GEN-2",
            client="other",
            status=QueueItemStatus.RUNNING,
            stage=Stage.IMPL,
            park_on_abandoned_exit={KEY: True},
        )

        assert park_gate_open(_task()) is True
        # "other" is not in clients.yaml: disabled even with a ticket override.
        assert park_gate_open(other) is False
        assert counts == {"orchestrator": 2, "clients": 2}

    def test_clear_park_config_cache_forces_a_reload(self) -> None:
        self._arm()
        assert park_gate_open(_task()) is True

        self._write_orchestrator("park_on_abandoned_exit_enabled: false\n")
        assert park_gate_open(_task()) is True  # memoized

        clear_park_config_cache()
        assert park_gate_open(_task()) is False

    @pytest.mark.parametrize(
        "clients_content",
        [
            "clients: {unclosed\n",
            "clients: [not, a, mapping]\n",
            "clients:\n  '!bad-name':\n    workspace_path: /tmp/ws\n",
            "clients:\n  acme:\n    workspace_path: /tmp/ws\n    lanes: nope\n",
            b"\xff\xfe\x00 not utf-8",
        ],
        ids=[
            "invalid-yaml",
            "non-mapping-clients",
            "invalid-client-name",
            "pydantic-validation-error",
            "undecodable-bytes",
        ],
    )
    def test_an_unreadable_clients_file_is_disabled_and_logged_once(
        self,
        clients_content: str | bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Any failure reading clients.yaml closes the gate; nothing raises."""
        self._write_orchestrator("park_on_abandoned_exit_enabled: true\n")
        self._write_clients(clients_content)

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.abandoned_exit"):
            first = park_gate_open(_task())
            second = park_gate_open(_task())

        assert (first, second) == (False, False)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "acme" in warnings[0].getMessage()
        assert "config unreadable" in warnings[0].getMessage()
        assert warnings[0].exc_info is None

    @pytest.mark.parametrize(
        ("content", "error_name"),
        [
            ("park_on_abandoned_exit_enabled: not-a-bool\n", "ConfigValidationError"),
            ("park_on_abandoned_exit_enabled: [\n", "ParserError"),
        ],
        ids=["invalid-schema", "invalid-yaml"],
    )
    def test_an_unreadable_orchestrator_file_is_disabled_and_names_the_error(
        self,
        content: str,
        error_name: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._write_orchestrator(content)
        self._write_clients(yaml.safe_dump(_LANE_ON_CLIENTS))

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.abandoned_exit"):
            assert park_gate_open(_task()) is False

        assert [r.getMessage() for r in caplog.records if error_name in r.getMessage()]

    def test_an_unknown_client_is_disabled_even_with_a_ticket_override(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Absent from clients.yaml ⇒ no park, whatever the row's own map says."""
        self._write_orchestrator("park_on_abandoned_exit_enabled: true\n")
        self._write_clients(
            yaml.safe_dump({"clients": {"elsewhere": {"workspace_path": "/tmp/ws"}}})
        )

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.abandoned_exit"):
            opened = park_gate_open(_task(park_on_abandoned_exit={KEY: True}))

        assert opened is False
        assert "not in clients.yaml" in caplog.text

    def test_an_absent_lane_entry_is_disabled_by_the_floor(self) -> None:
        self._write_orchestrator("park_on_abandoned_exit_enabled: true\n")
        self._write_clients(
            yaml.safe_dump({"clients": {"acme": {"workspace_path": "/tmp/ws"}}})
        )

        assert park_gate_open(_task()) is False


class TestParkOnAbandonedExitKeyValidation:
    """An unrecognised key fails loud at model-validation time (#2135)."""

    def test_ticket_map_rejects_an_unknown_key(self) -> None:
        with pytest.raises(ValidationError, match="unrecognized key"):
            _task(park_on_abandoned_exit={"park_on_abandonned_exit": True})

    def test_lane_map_rejects_an_unknown_key(self) -> None:
        with pytest.raises(ValidationError, match="unrecognized key"):
            LaneConfig(name="default", park_on_abandoned_exit={"nope": True})

    @pytest.mark.parametrize("model_kwargs", [{"park_on_abandoned_exit": None}, {}])
    def test_absent_and_none_maps_are_accepted(
        self, model_kwargs: dict[str, object]
    ) -> None:
        assert LaneConfig(name="default", **model_kwargs).park_on_abandoned_exit is None
        assert _task(**model_kwargs).park_on_abandoned_exit is None
