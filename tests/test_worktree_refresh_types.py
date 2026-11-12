"""Tests for cw.worktree._refresh_types - reuse refresh result types (#2213)."""

from __future__ import annotations

import dataclasses

import pytest

from cw.native_daemon import FakeNativeDaemonClient
from cw.worktree import (
    RefreshOutcome,
    RefreshResult,
    ReuseRefreshOptions,
    ReuseRefreshReport,
)


class TestReuseRefreshOptions:
    """The reuse-refresh opt-in is one frozen value (#2238)."""

    def test_defaults_are_none(self) -> None:
        options = ReuseRefreshOptions()

        assert options.report is None
        assert options.ticket_id is None
        assert options.native_daemon is None

    @pytest.mark.parametrize("name", ["report", "ticket_id", "native_daemon"])
    def test_is_frozen(self, name: str) -> None:
        options = ReuseRefreshOptions()

        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(options, name, None)

    def test_held_report_stays_the_callers_mutable_object(self) -> None:
        # The frozen wrapper freezes its own attributes only: the report is
        # still an out-parameter the refresh fills in for the caller.
        report = ReuseRefreshReport()
        options = ReuseRefreshOptions(report=report)

        report.notes.append("fetch failed")
        report.outcome = RefreshOutcome.NOT_REFRESHED

        assert options.report is report
        assert options.report.notes == ["fetch failed"]
        assert options.report.outcome is RefreshOutcome.NOT_REFRESHED

    def test_equal_fields_compare_equal(self) -> None:
        daemon = FakeNativeDaemonClient()

        assert ReuseRefreshOptions(
            ticket_id="GEN-2238", native_daemon=daemon
        ) == ReuseRefreshOptions(ticket_id="GEN-2238", native_daemon=daemon)
        assert ReuseRefreshOptions(ticket_id="a") != ReuseRefreshOptions(ticket_id="b")


class TestReuseRefreshReport:
    """The caller-supplied report starts empty and is per-instance."""

    def test_defaults_are_empty(self) -> None:
        report = ReuseRefreshReport()

        assert report.notes == []
        assert report.outcome is None
        assert report.reason is None

    def test_notes_are_not_shared_between_instances(self) -> None:
        first = ReuseRefreshReport()
        second = ReuseRefreshReport()

        first.notes.append("fetch failed")

        assert second.notes == []


class TestRefreshResult:
    """The refresh helper's verdict is an immutable value."""

    @pytest.mark.parametrize("name", ["outcome", "reason"])
    def test_is_frozen(self, name: str) -> None:
        result = RefreshResult(RefreshOutcome.REFRESHED, "fast-forwarded")

        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(result, name, "changed")


class TestRefreshOutcome:
    """The outcome values are part of the logged/reported vocabulary."""

    def test_values_are_exact(self) -> None:
        assert {member.value for member in RefreshOutcome} == {
            "refreshed",
            "not_refreshed",
            "occupied_by_live_session",
        }
