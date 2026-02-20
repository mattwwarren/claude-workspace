"""Tests for cw.tui module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.history import EventType, HistoryEvent
from cw.models import (
    ClientConfig,
    CwState,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.tui import CwDashboard, _format_event, _format_status, _session_time

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
                name="alpha/idea",
                client="alpha",
                purpose=SessionPurpose.IDEA,
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


def test_session_time_uses_started_at() -> None:
    """_session_time falls back to started_at when no other timestamp."""
    session = Session(
        id="s1",
        name="test/impl",
        client="test",
        purpose=SessionPurpose.IMPL,
        workspace_path="/home/test/workspace",
        started_at=datetime.now(UTC),
    )
    result = _session_time(session)
    assert result == "just now"


def test_session_time_prefers_backgrounded_at() -> None:
    """_session_time uses backgrounded_at for backgrounded sessions."""
    old_start = datetime(2025, 6, 1, 8, 0, 0, tzinfo=UTC)
    bg_time = datetime.now(UTC)
    session = Session(
        id="s2",
        name="test/idea",
        client="test",
        purpose=SessionPurpose.IDEA,
        status=SessionStatus.BACKGROUNDED,
        workspace_path="/home/test/workspace",
        started_at=old_start,
        backgrounded_at=bg_time,
    )
    result = _session_time(session)
    # backgrounded_at is recent, so should be "just now" not hours ago
    assert result == "just now"


def test_session_time_prefers_resumed_at_over_backgrounded() -> None:
    """_session_time uses resumed_at when present, over backgrounded_at."""
    old_start = datetime(2025, 6, 1, 8, 0, 0, tzinfo=UTC)
    old_bg = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
    recent_resume = datetime.now(UTC)
    session = Session(
        id="s3",
        name="test/impl",
        client="test",
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        workspace_path="/home/test/workspace",
        started_at=old_start,
        backgrounded_at=old_bg,
        resumed_at=recent_resume,
    )
    result = _session_time(session)
    assert result == "just now"


def test_format_event_basic() -> None:
    """_format_event formats event_type, session name, and detail."""
    event = HistoryEvent(
        timestamp=datetime.now(UTC),
        event_type=EventType.SESSION_STARTED,
        client="alpha",
        session_name="alpha/impl",
        detail="Launched impl pane",
    )
    result = _format_event(event)
    assert "session_started" in result
    assert "[alpha/impl]" in result
    assert "Launched impl pane" in result
    assert " - " in result


def test_format_event_no_session_no_detail() -> None:
    """_format_event omits brackets and dash when fields are absent."""
    event = HistoryEvent(
        timestamp=datetime.now(UTC),
        event_type=EventType.DAEMON_STARTED,
        client="alpha",
    )
    result = _format_event(event)
    assert "daemon_started" in result
    assert "[" not in result
    assert " - " not in result


def test_format_event_no_detail_with_session() -> None:
    """_format_event includes session name but omits detail when absent."""
    event = HistoryEvent(
        timestamp=datetime.now(UTC),
        event_type=EventType.SESSION_BACKGROUNDED,
        client="alpha",
        session_name="alpha/idea",
    )
    result = _format_event(event)
    assert "[alpha/idea]" in result
    assert " - " not in result


@pytest.mark.asyncio
async def test_client_selection_updates_session_table(tmp_config_dir: Path) -> None:
    """Selecting beta client sets _selected_client and clears _sessions."""
    state = _make_state()  # has alpha sessions, no beta sessions

    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=state),
        patch("cw.tui.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Manually configure state as if alpha were selected with sessions loaded
            app._clients = _make_clients()
            app._selected_client = "alpha"
            app._sessions = [s for s in state.sessions if s.client == "alpha"]

            # Now simulate switching to beta via on_list_view_selected logic
            app._selected_client = "beta"
            app._refresh_sessions()
            await pilot.pause()

            # beta has no sessions in _make_state(), table should be empty
            assert app._selected_client == "beta"
            assert app._sessions == []


@pytest.mark.asyncio
async def test_client_selection_shows_alpha_sessions(tmp_config_dir: Path) -> None:
    """_refresh_sessions populates _sessions with alpha's non-completed entries."""
    state = _make_state()

    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=state),
        patch("cw.tui.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Set up as if alpha is selected
            app._selected_client = "alpha"
            app._refresh_sessions()
            await pilot.pause()

            # alpha has s1 (active) and s2 (backgrounded) - both non-completed
            assert len(app._sessions) == 2
            session_ids = {s.id for s in app._sessions}
            assert "s1" in session_ids
            assert "s2" in session_ids


@pytest.mark.asyncio
async def test_session_table_excludes_completed(tmp_config_dir: Path) -> None:
    """Completed sessions are filtered out by _refresh_sessions."""
    state = CwState(
        sessions=[
            Session(
                id="active1",
                name="alpha/impl",
                client="alpha",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                workspace_path="/home/test/alpha",
                started_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
            ),
            Session(
                id="done1",
                name="alpha/idea",
                client="alpha",
                purpose=SessionPurpose.IDEA,
                status=SessionStatus.COMPLETED,
                workspace_path="/home/test/alpha",
                started_at=datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC),
            ),
        ]
    )

    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=state),
        patch("cw.tui.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_client = "alpha"
            app._refresh_sessions()
            await pilot.pause()

            assert len(app._sessions) == 1
            assert app._sessions[0].id == "active1"
