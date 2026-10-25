"""``wedge/pending-worktree-occupied`` doctor class (#2516).

1:1 test module of ``cw.doctor.pending_skip_wedge``. The inbox read is stubbed
at ``cw.doctor.pending_skip_wedge.read_events`` with in-memory events from
``tests._dispatch_tick_helpers``. The detector stamps ``now`` with the real
clock, so every test runs under ``freeze_time(_NOW)`` and builds its rows with
an explicit ``created_at`` before the first event.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import main
from cw.config import state_file
from cw.dispatch import TICK_STALE_SECONDS
from cw.doctor.pending_skip_wedge import (
    PENDING_SKIP_WEDGE_THRESHOLD,
    WEDGE_PENDING_WORKTREE_OCCUPIED,
    _check_wedge_pending_worktree_occupied,
)
from cw.models import DevQueueStore, QueueItemStatus
from tests._dispatch_tick_helpers import skip_tick_events
from tests._ticket_view_helpers import ACME, ACME_TICKET, make_task

if TYPE_CHECKING:
    from cw.doctor import WedgeFinding
    from cw.models import OrchestratorEvent, TicketTask

_NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_STEP = timedelta(seconds=30)
_LAST_GAP = timedelta(seconds=10)
_OCCUPIED = "worktree_occupied"


def _row(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {"created_at": _NOW - timedelta(days=1)}
    kwargs.update(overrides)
    return make_task(**kwargs)


def _run(
    count: int,
    *,
    ticket_id: str = ACME_TICKET,
    reason: str = _OCCUPIED,
    last_gap: timedelta = _LAST_GAP,
) -> list[OrchestratorEvent]:
    """*count* skip ticks for *ticket_id*, the newest *last_gap* before ``_NOW``."""
    start = _NOW - last_gap - (count - 1) * _STEP
    return skip_tick_events(ACME, ticket_id, reason, count=count, start=start)


def _check(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[TicketTask],
    events: list[OrchestratorEvent],
) -> list[WedgeFinding]:
    monkeypatch.setattr(
        "cw.doctor.pending_skip_wedge.read_events", lambda **_kw: events
    )
    with freeze_time(_NOW):
        return _check_wedge_pending_worktree_occupied(DevQueueStore(tasks=rows))


class TestDetector:
    def test_streak_at_threshold_yields_one_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run(PENDING_SKIP_WEDGE_THRESHOLD)

        (finding,) = _check(monkeypatch, [_row()], events)

        assert finding.wedge_class == WEDGE_PENDING_WORKTREE_OCCUPIED
        assert finding.session_id is None
        assert finding.ticket_id == ACME_TICKET
        assert finding.state_file == str(state_file())
        assert f"(client {ACME})" in finding.recipe
        assert f"{PENDING_SKIP_WEDGE_THRESHOLD} consecutive" in finding.recipe
        assert f"cw dev-queue show {ACME_TICKET} -c {ACME}" in finding.recipe
        assert "cw spawn close --confirmed-dead" in finding.recipe
        assert "cw doctor --reap" in finding.recipe

    def test_one_below_threshold_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run(PENDING_SKIP_WEDGE_THRESHOLD - 1)

        assert _check(monkeypatch, [_row()], events) == []

    def test_stale_streak_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events = _run(
            PENDING_SKIP_WEDGE_THRESHOLD,
            last_gap=timedelta(seconds=TICK_STALE_SECONDS + 1),
        )

        assert _check(monkeypatch, [_row()], events) == []

    def test_other_newest_reason_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run(PENDING_SKIP_WEDGE_THRESHOLD, reason="tracker_mcp_gate_blocked")

        assert _check(monkeypatch, [_row()], events) == []

    def test_non_pending_row_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events = _run(PENDING_SKIP_WEDGE_THRESHOLD)

        assert _check(monkeypatch, [_row(status=QueueItemStatus.RUNNING)], events) == []

    def test_no_pending_rows_skips_the_inbox_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(**_kw: object) -> list[OrchestratorEvent]:
            msg = "read_events must not be called without a PENDING row"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.doctor.pending_skip_wedge.read_events", _fail)
        store = DevQueueStore(tasks=[_row(status=QueueItemStatus.BLOCKED_ON_USER)])

        assert _check_wedge_pending_worktree_occupied(store) == []
        assert _check_wedge_pending_worktree_occupied(DevQueueStore()) == []

    def test_two_held_rows_sorted_by_client_and_ticket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [_row(ticket_id="ACM-9"), _row(ticket_id="ACM-2")]
        events = _run(PENDING_SKIP_WEDGE_THRESHOLD, ticket_id="ACM-9") + _run(
            PENDING_SKIP_WEDGE_THRESHOLD + 5, ticket_id="ACM-2"
        )

        findings = _check(monkeypatch, rows, events)

        assert [f.ticket_id for f in findings] == ["ACM-2", "ACM-9"]


class TestDoctorHelp:
    def test_help_lists_the_class_with_the_threshold(self) -> None:
        result = CliRunner().invoke(main, ["doctor", "--help"])

        assert result.exit_code == 0, result.output
        assert "wedge/pending-worktree-occupied (advisory only)" in result.output
        assert (
            f"worktree_occupied for {PENDING_SKIP_WEDGE_THRESHOLD}\n" in result.output
        )
