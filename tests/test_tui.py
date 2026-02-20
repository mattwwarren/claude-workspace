"""Tests for cw.tui module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.models import (
    ClientConfig,
    CwState,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.tui import CwDashboard, _format_status, _relative_time

if TYPE_CHECKING:
    from pathlib import Path


def _make_clients() -> dict[str, ClientConfig]:
    return {
        "alpha": ClientConfig(
            name="alpha",
            workspace_path="/home/test/alpha",
        ),
        "beta": ClientConfig(
            name="beta",
            workspace_path="/home/test/beta",
        ),
    }


def _make_state() -> CwState:
    return CwState(
        sessions=[
            Session(
                id="s1",
                name="alpha/impl",
                client="alpha",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                workspace_path="/home/test/alpha",
                started_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
            ),
            Session(
                id="s2",
                name="alpha/review",
                client="alpha",
                purpose=SessionPurpose.REVIEW,
                status=SessionStatus.BACKGROUNDED,
                workspace_path="/home/test/alpha",
                started_at=datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC),
                backgrounded_at=datetime(2025, 6, 1, 11, 0, 0, tzinfo=UTC),
            ),
        ]
    )


@pytest.fixture
def mock_data() -> None:
    """Patch config loading for TUI tests."""
    with (
        patch("cw.tui.CwDashboard._load_clients") as mock_load,
    ):
        mock_load.return_value = None
        yield


@pytest.mark.asyncio
async def test_dashboard_mounts(tmp_config_dir: Path) -> None:
    """Dashboard app mounts without errors."""
    with (
        patch("cw.config.load_clients", return_value=_make_clients()),
        patch("cw.config.load_state", return_value=_make_state()),
        patch("cw.history.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            # App should mount without error
            assert pilot.app is not None
            assert pilot.app.title == "cw dashboard"


@pytest.mark.asyncio
async def test_dashboard_quit(tmp_config_dir: Path) -> None:
    """Dashboard quits on 'q' key."""
    with (
        patch("cw.config.load_clients", return_value={}),
        patch("cw.config.load_state", return_value=CwState()),
        patch("cw.history.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("q")


@pytest.mark.asyncio
async def test_dashboard_help_notification(tmp_config_dir: Path) -> None:
    """Help key shows notification."""
    with (
        patch("cw.config.load_clients", return_value=_make_clients()),
        patch("cw.config.load_state", return_value=_make_state()),
        patch("cw.history.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("?")
            # Notification should have been shown (no error = pass)


def test_format_status() -> None:
    """Status formatting includes color markup."""
    assert "[green]" in _format_status("active")
    assert "[yellow]" in _format_status("backgrounded")
    assert "[dim]" in _format_status("completed")
    assert _format_status("unknown") == "unknown"


def test_relative_time() -> None:
    """Relative time formatting for sessions."""
    session = Session(
        id="s1",
        name="test/impl",
        client="test",
        purpose=SessionPurpose.IMPL,
        workspace_path="/home/test/workspace",
        started_at=datetime.now(UTC),
    )
    result = _relative_time(session)
    assert result == "just now"
