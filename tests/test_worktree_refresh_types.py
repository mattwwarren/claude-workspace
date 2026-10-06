"""Tests for cw.worktree._refresh_types - reuse refresh result types (#2213)."""

from __future__ import annotations

import dataclasses

import pytest

from cw.worktree import RefreshOutcome, RefreshResult, ReuseRefreshReport


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
