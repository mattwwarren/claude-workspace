"""Positional / alias conveniences on ``cw dev-queue tasks`` and ``move`` (#2690).

``tasks`` takes the ticket positionally as well as via ``-t/--ticket`` (never
both); ``move`` accepts ``--lane`` as an alias of ``--to``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    DevQueueStore,
    LaneConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml

if TYPE_CHECKING:
    from pathlib import Path

_CLIENT = "acme"
_USAGE_ERROR_EXIT = 2


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
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(ticket_id="ACM-1", client=_CLIENT, lane="default"),
                TicketTask(ticket_id="ACM-2", client=_CLIENT, lane="default"),
                TicketTask(ticket_id="ACM-1", client="other", lane="default"),
                TicketTask(
                    ticket_id="ACM-10",
                    client=_CLIENT,
                    status=QueueItemStatus.PENDING,
                    lane="default",
                ),
            ]
        )
    )


def _tasks_json(args: list[str]) -> list[tuple[str, str]]:
    result = CliRunner().invoke(main, ["dev-queue", "tasks", *args, "--json"])
    assert result.exit_code == 0, result.output
    return [(row["ticket_id"], row["client"]) for row in json.loads(result.output)]


def _moved_to_lanes() -> list[object]:
    return [
        e.payload["to_lane"]
        for e in read_events()
        if e.type == OrchestratorEventType.TICKET_MOVED
    ]


class TestTasksPositionalTicket:
    def test_positional_ticket_filters_like_dash_t(self) -> None:
        positional = _tasks_json(["ACM-1"])
        assert positional == [("ACM-1", _CLIENT), ("ACM-1", "other")]
        assert positional == _tasks_json(["-t", "ACM-1"])

    def test_positional_ticket_combines_with_client_filter(self) -> None:
        assert _tasks_json(["ACM-1", "-c", _CLIENT]) == [("ACM-1", _CLIENT)]

    @pytest.mark.parametrize("flag_value", ["ACM-1", "ACM-2"])
    def test_positional_and_dash_t_together_is_a_usage_error(
        self, flag_value: str
    ) -> None:
        result = CliRunner().invoke(
            main, ["dev-queue", "tasks", "ACM-1", "-t", flag_value]
        )
        assert result.exit_code == _USAGE_ERROR_EXIT
        assert "not both" in result.output

    def test_bare_tasks_lists_everything(self) -> None:
        assert len(_tasks_json([])) == len(load_dev_queue().tasks)


class TestMoveLaneAlias:
    def test_lane_alias_moves_and_emits_ticket_moved(self) -> None:
        result = CliRunner().invoke(
            main, ["dev-queue", "move", "ACM-10", "-c", _CLIENT, "--lane", "fast"]
        )
        assert result.exit_code == 0, result.output
        moved = next(t for t in load_dev_queue().tasks if t.ticket_id == "ACM-10")
        assert moved.lane == "fast"
        assert _moved_to_lanes() == ["fast"]

    def test_to_still_works(self) -> None:
        result = CliRunner().invoke(
            main, ["dev-queue", "move", "ACM-10", "-c", _CLIENT, "--to", "fast"]
        )
        assert result.exit_code == 0, result.output
        assert _moved_to_lanes() == ["fast"]

    def test_neither_flag_names_the_alias(self) -> None:
        result = CliRunner().invoke(
            main, ["dev-queue", "move", "ACM-10", "-c", _CLIENT]
        )
        assert result.exit_code == _USAGE_ERROR_EXIT
        assert "--to" in result.output
        assert "--lane" in result.output
        assert "alias" in result.output

    def test_help_lists_lane(self) -> None:
        result = CliRunner().invoke(main, ["dev-queue", "move", "--help"])
        assert result.exit_code == 0
        assert "--lane" in result.output
