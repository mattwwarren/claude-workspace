"""``cw dev-queue tasks --base`` (#3783, RFC 0020 B1): a pure read filter."""

from __future__ import annotations

import json

import pytest

from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.models import QueueItemStatus
from tests._ticket_view_helpers import ACME, invoke_dev_queue, make_task, seed_queue


@pytest.fixture
def seeded() -> None:
    seed_queue(
        [
            make_task(ticket_id="ACM-1", base_branch="epic/x"),
            make_task(ticket_id="ACM-2", base_branch="epic/y"),
            make_task(ticket_id="ACM-3", base_branch=None),
        ]
    )


def test_base_filter_lists_only_matching_rows(seeded: None) -> None:
    result = invoke_dev_queue("tasks", "--base", "epic/x")
    assert result.exit_code == 0, result.output
    assert "ACM-1" in result.stdout
    assert "ACM-2" not in result.stdout
    assert "ACM-3" not in result.stdout


def test_base_filter_json_carries_base_status_and_pr(seeded: None) -> None:
    result = invoke_dev_queue("tasks", "--base", "epic/x", "--json")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [r["ticket_id"] for r in rows] == ["ACM-1"]
    assert rows[0]["base_branch"] == "epic/x"
    assert "status" in rows[0]
    assert "pr_url" in rows[0]


def test_default_branch_prints_no_tasks_when_only_null_rows(seeded: None) -> None:
    seed_queue([make_task(ticket_id="ACM-3", base_branch=None)])
    result = invoke_dev_queue("tasks", "--base", "main")
    assert result.exit_code == 0, result.output
    assert "No tasks found." in result.stdout


def test_combines_with_client_filter(seeded: None) -> None:
    result = invoke_dev_queue("tasks", "--base", "epic/x", "-c", ACME, "--json")
    assert [r["ticket_id"] for r in json.loads(result.stdout)] == ["ACM-1"]


def test_combines_with_status_filter() -> None:
    seed_queue(
        [
            make_task(ticket_id="ACM-1", base_branch="epic/x"),
            make_task(
                ticket_id="ACM-2",
                base_branch="epic/x",
                status=QueueItemStatus.COMPLETED,
            ),
            make_task(
                ticket_id="ACM-3",
                base_branch="epic/y",
                status=QueueItemStatus.COMPLETED,
            ),
        ]
    )
    result = invoke_dev_queue("tasks", "--base", "epic/x", "-s", "completed", "--json")
    assert result.exit_code == 0, result.output
    assert [r["ticket_id"] for r in json.loads(result.stdout)] == ["ACM-2"]


def test_read_is_pure(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """R7: the filter compares the stored column; it never calls the seam."""

    def _forbidden(*_a: object, **_kw: object) -> str:
        msg = "tasks --base must not resolve a base through the seam"
        raise AssertionError(msg)

    monkeypatch.setattr("cw.models.resolve_base_branch", _forbidden)
    monkeypatch.setattr("cw.models.branch.resolve_base_branch", _forbidden)
    before = load_dev_queue().model_dump_json()
    events_before = len(list(read_events()))
    result = invoke_dev_queue("tasks", "--base", "epic/x")
    assert result.exit_code == 0, result.output
    assert load_dev_queue().model_dump_json() == before
    assert len(list(read_events())) == events_before


def test_help_names_the_base_filter() -> None:
    result = invoke_dev_queue("tasks", "--help")
    text = " ".join(result.output.split())
    assert "Filter by stored base_branch (exact match)." in text
    assert "--base <default-branch> prints 'No tasks found.'" in text
