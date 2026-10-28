"""Tests for ``cw.queue_peek.render`` -- the text table."""

from __future__ import annotations

import pytest

from cw import queue_peek

# ---------------------------------------------------------------------------
# print_table
# ---------------------------------------------------------------------------

_WAIT_ROW = {
    "ticket": "T-1",
    "session": "abc123456789",
    "client": "c",
    "attempts": 1,
    "age_min": 10.0,
    "idle_min": 2.0,
    "stage": None,
    "status": None,
    "pr": None,
    "pr_state": None,
    "recommend": "WAIT",
    "reason": "age 10min — early/healthy",
    "signal_source": "transcript",
    "jsonl_idle_min": None,
}

_STOP_ROW = {
    **_WAIT_ROW,
    "recommend": "STOP",
    "reason": "PR merged + worker idle 10min — stuck",
    "session": "stopabc123456",
    "ticket": "T-2",
}

_AWAITING_OPERATOR_ROW = {
    **_WAIT_ROW,
    "recommend": "AWAITING_OPERATOR",
    "reason": "parked, no answer yet",
    "age_min": None,
    "idle_min": None,
    "session": "parkedabc123",
    "ticket": "2212",
}


class TestPrintTable:
    def test_empty_rows_prints_no_running(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([])
        captured = capsys.readouterr()
        assert "No RUNNING tasks found." in captured.out

    def test_wait_row_printed_without_reason_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_WAIT_ROW])
        captured = capsys.readouterr()
        assert "T-1" in captured.out
        assert "└─" not in captured.out

    def test_non_wait_row_prints_reason_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_STOP_ROW])
        captured = capsys.readouterr()
        assert "└─" in captured.out
        assert "stuck" in captured.out

    def test_stop_row_prints_suggested_stops_footer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_STOP_ROW])
        captured = capsys.readouterr()
        assert "Suggested stops:" in captured.out
        assert "cw spawn close" in captured.out

    def test_wait_row_no_suggested_stops_footer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_WAIT_ROW])
        captured = capsys.readouterr()
        assert "Suggested stops:" not in captured.out

    def test_awaiting_operator_row_renders(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_AWAITING_OPERATOR_ROW])
        captured = capsys.readouterr()
        assert "AWAITING_OPERATOR" in captured.out
        assert "no answer yet" in captured.out

    def test_awaiting_operator_row_is_not_a_suggested_stop(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A parked row is an answer candidate, not a stop candidate (#2212)."""
        queue_peek.print_table([_AWAITING_OPERATOR_ROW])
        captured = capsys.readouterr()
        assert "Suggested stops:" not in captured.out

    def test_peek_blind_excluded_from_suggested_stops(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """PEEK-BLIND does not start with STOP — must not appear in the footer."""
        blind_row = {
            **_WAIT_ROW,
            "recommend": "PEEK-BLIND",
            "reason": "no resolvable transcript; newest jsonl 5m ago",
            "signal_source": "blind",
            "jsonl_idle_min": 5.0,
        }
        queue_peek.print_table([blind_row])
        captured = capsys.readouterr()
        assert "Suggested stops:" not in captured.out
