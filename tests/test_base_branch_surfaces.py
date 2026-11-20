"""``base_branch`` on the operator surfaces: ``move``, ``show``, ``tasks --json``.

GitHub #3779.

``move`` edits lane and priority only, so a set ``base_branch`` must survive it
(the mutation-proof case below). ``show`` and ``tasks --json`` print the raw
stored value, never the resolved one, so a null row reads ``null``/``-`` even
though the client default is ``main``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cw.dev_queue import load_dev_queue, move_ticket
from cw.models.ticket_view import TicketView
from cw.ticket_view import ViewSources, build_ticket_view, render_ticket_view
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    install_acme_client,
    invoke_dev_queue,
    make_task,
    seed_queue,
)

_NOW_TEXT = "2026-10-15T18:00:00+00:00"


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)


def _view(base_branch: str | None) -> TicketView:
    seed_queue([make_task(base_branch=base_branch)])
    from datetime import UTC, datetime

    return build_ticket_view(
        ACME_TICKET,
        ACME,
        now=datetime.fromisoformat(_NOW_TEXT).replace(tzinfo=UTC),
        sources=ViewSources(),
    )


class TestMoveKeepsBaseBranch:
    def test_lane_change_keeps_a_set_base(self) -> None:
        seed_queue([make_task(base_branch="epic/3778")])
        move_ticket(ACME_TICKET, ACME, to_lane="fast")
        task = load_dev_queue().tasks[0]
        assert task.lane == "fast"
        assert task.base_branch == "epic/3778"

    def test_priority_change_keeps_a_set_base(self) -> None:
        seed_queue([make_task(base_branch="epic/3778")])
        move_ticket(ACME_TICKET, ACME, priority=7)
        task = load_dev_queue().tasks[0]
        assert task.priority == 7
        assert task.base_branch == "epic/3778"


class TestViewBaseBranch:
    def test_null_row_view_is_none(self) -> None:
        assert _view(None).base_branch is None

    def test_set_row_view_carries_the_stored_value(self) -> None:
        assert _view("epic/3778").base_branch == "epic/3778"

    def test_render_null_shows_dash_and_sits_after_stage_base_ref(self) -> None:
        from datetime import UTC, datetime

        text = render_ticket_view(
            _view(None), now=datetime.fromisoformat(_NOW_TEXT).replace(tzinfo=UTC)
        )
        lines = text.splitlines()
        base_idx = next(
            i for i, ln in enumerate(lines) if ln.startswith("base_branch:")
        )
        ref_idx = next(
            i for i, ln in enumerate(lines) if ln.startswith("stage_base_ref:")
        )
        assert base_idx == ref_idx + 1
        assert lines[base_idx] == "base_branch: -"

    def test_render_set_shows_the_value(self) -> None:
        from datetime import UTC, datetime

        text = render_ticket_view(
            _view("epic/3778"),
            now=datetime.fromisoformat(_NOW_TEXT).replace(tzinfo=UTC),
        )
        assert "base_branch: epic/3778" in text.splitlines()

    @pytest.mark.parametrize(
        ("base", "expected"), [(None, None), ("epic/3778", "epic/3778")]
    )
    def test_json_carries_the_raw_value(
        self, base: str | None, expected: str | None
    ) -> None:
        assert json.loads(_view(base).model_dump_json())["base_branch"] == expected


class TestTasksJson:
    def test_null_row_prints_null_not_the_client_default(self) -> None:
        seed_queue([make_task(base_branch=None)])
        result = invoke_dev_queue("tasks", "--json")
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        row = rows[0] if isinstance(rows, list) else rows["tasks"][0]
        assert "base_branch" in row
        assert row["base_branch"] is None

    def test_set_row_prints_its_value(self) -> None:
        seed_queue([make_task(base_branch="epic/3778")])
        result = invoke_dev_queue("tasks", "--json")
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        row = rows[0] if isinstance(rows, list) else rows["tasks"][0]
        assert row["base_branch"] == "epic/3778"
