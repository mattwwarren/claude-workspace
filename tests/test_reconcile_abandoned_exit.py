"""Tests for cw.reconcile.abandoned_exit (GitHub #2135).

Enablement resolution only: the park's own behaviour is pinned in
tests/test_reconcile_shared_policies.py and its Stop-hook wiring in
tests/test_cli.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cw.config import (
    clients_file,
    load_orchestrator_config,
    orchestrator_config_file,
)
from cw.exceptions import ConfigValidationError
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
    load_armed_park_config,
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


class TestParkConfigLoaders:
    """Both loads are fail-closed: an unreadable config reads as disabled."""

    @staticmethod
    def _write_orchestrator(text: str) -> None:
        path = orchestrator_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    @staticmethod
    def _write_clients(text: str) -> None:
        path = clients_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_load_armed_park_config_returns_none_when_switch_is_off(self) -> None:
        self._write_orchestrator("park_on_abandoned_exit_enabled: false\n")

        assert load_armed_park_config() is None

    def test_load_armed_park_config_returns_the_config_when_armed(self) -> None:
        self._write_orchestrator("park_on_abandoned_exit_enabled: true\n")

        config = load_armed_park_config()

        assert config is not None
        assert config.park_on_abandoned_exit_enabled is True

    def test_load_armed_park_config_is_none_on_an_invalid_config(self) -> None:
        self._write_orchestrator("park_on_abandoned_exit_enabled: not-a-bool\n")

        with pytest.raises(ConfigValidationError):
            load_orchestrator_config()
        assert load_armed_park_config() is None

    def test_park_gate_open_is_false_on_an_unreadable_clients_file(self) -> None:
        self._write_clients("clients: [not, a, mapping]\n")
        config = OrchestratorConfig(park_on_abandoned_exit_enabled=True)

        assert park_gate_open(config, _task()) is False

    def test_park_gate_open_reads_the_lane_map_from_disk(self) -> None:
        self._write_clients(
            yaml.safe_dump(
                {
                    "clients": {
                        "acme": {
                            "workspace_path": "/tmp/ws",
                            "lanes": [
                                {
                                    "name": "default",
                                    "park_on_abandoned_exit": {KEY: True},
                                }
                            ],
                        }
                    }
                }
            )
        )
        config = OrchestratorConfig(park_on_abandoned_exit_enabled=True)

        assert park_gate_open(config, _task()) is True


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
