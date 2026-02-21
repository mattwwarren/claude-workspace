"""Tests for cw.tui module."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import ListItem, Static

from cw.history import EventType, HistoryEvent
from cw.models import (
    ClientConfig,
    CwState,
    QueueItem,
    QueueItemStatus,
    QueueStore,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TaskSpec,
)
from cw.plan import PlanPhase, PlanSummary, PlanTask
from cw.tui import (
    ClientList,
    ConfirmScreen,
    CwDashboard,
    PlanPanel,
    QueuePanel,
    SessionDetailScreen,
    SessionTable,
    _format_event,
    _format_origin,
    _format_plan_summary,
    _format_queue_status,
    _format_status,
    _session_time,
)


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


# --- Plan panel tests ---


def _make_plan_summary(
    tmp_path: Path, *, done: int = 1, total: int = 3,
) -> PlanSummary:
    """Build a PlanSummary with controllable progress."""
    tasks = [
        PlanTask(text=f"Task {i}", completed=i < done, phase="P1")
        for i in range(total)
    ]
    return PlanSummary(
        path=tmp_path / "plan.md",
        title="Test Plan",
        phases=[PlanPhase(name="Phase 1", tasks=tasks)],
    )


class TestFormatPlanSummary:
    def test_renders_title_and_progress(self, tmp_path: Path) -> None:
        summary = _make_plan_summary(tmp_path, done=1, total=3)
        result = _format_plan_summary(summary)
        assert "Test Plan" in result
        assert "[1/3]" in result
        assert "33%" in result

    def test_phase_shown_with_progress(self, tmp_path: Path) -> None:
        summary = _make_plan_summary(tmp_path, done=1, total=3)
        result = _format_plan_summary(summary)
        assert "Phase 1" in result
        assert "1/3" in result

    def test_completed_plan_returns_empty(self, tmp_path: Path) -> None:
        summary = _make_plan_summary(tmp_path, done=3, total=3)
        result = _format_plan_summary(summary)
        assert result == ""

    def test_no_tasks_returns_empty(self, tmp_path: Path) -> None:
        summary = PlanSummary(
            path=tmp_path / "plan.md",
            title="Empty",
            phases=[PlanPhase(name="P1")],
        )
        result = _format_plan_summary(summary)
        assert result == ""

    def test_completed_phase_shows_done(self, tmp_path: Path) -> None:
        summary = PlanSummary(
            path=tmp_path / "plan.md",
            title="Mixed",
            phases=[
                PlanPhase(
                    name="Done Phase",
                    tasks=[PlanTask(text="A", completed=True, phase="Done Phase")],
                ),
                PlanPhase(
                    name="WIP Phase",
                    tasks=[PlanTask(text="B", completed=False, phase="WIP Phase")],
                ),
            ],
        )
        result = _format_plan_summary(summary)
        assert "Done" in result
        assert "0/1" in result


@pytest.mark.asyncio
async def test_dashboard_has_plan_panel(tmp_config_dir: Path) -> None:
    """PlanPanel widget exists in the mounted dashboard."""
    with (
        patch("cw.config.load_clients", return_value=_make_clients()),
        patch("cw.config.load_state", return_value=_make_state()),
        patch("cw.history.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            panel = app.query_one(PlanPanel)
            assert panel is not None


@pytest.mark.asyncio
async def test_refresh_plans_no_client(tmp_config_dir: Path) -> None:
    """_refresh_plans handles no selected client gracefully."""
    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=CwState()),
        patch("cw.tui.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_client = None
            app._refresh_plans()
            await pilot.pause()
            panel = app.query_one(PlanPanel)
            assert str(panel.render()) == ""


@pytest.mark.asyncio
async def test_refresh_plans_shows_active_plans(tmp_config_dir: Path) -> None:
    """_refresh_plans populates panel with plan summaries."""
    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=CwState()),
        patch("cw.tui.load_history", return_value=[]),
        patch("cw.tui.find_plan_files") as mock_find,
        patch("cw.tui.parse_plan") as mock_parse,
    ):
        mock_find.return_value = [Path("/fake/plan.md")]
        mock_parse.return_value = PlanSummary(
            path=Path("/fake/plan.md"),
            title="My Plan",
            phases=[
                PlanPhase(
                    name="Setup",
                    tasks=[
                        PlanTask(text="Init", completed=True, phase="Setup"),
                        PlanTask(text="Config", completed=False, phase="Setup"),
                    ],
                ),
            ],
        )

        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._clients = _make_clients()
            app._selected_client = "alpha"
            app._refresh_plans()
            await pilot.pause()

            mock_find.assert_called_once_with(Path("/home/test/alpha"))

            panel = app.query_one(PlanPanel)
            content = str(panel.render())
            assert "My Plan" in content
            assert "1/2" in content


# --- Queue panel tests ---


def _make_queue_store() -> QueueStore:
    """Build a QueueStore with mixed status items."""
    return QueueStore(
        items=[
            QueueItem(
                id="q1",
                client="alpha",
                task=TaskSpec(
                    description="Fix ruff violations",
                    purpose=SessionPurpose.DEBT,
                    prompt="Run ruff --fix",
                ),
                status=QueueItemStatus.PENDING,
            ),
            QueueItem(
                id="q2",
                client="alpha",
                task=TaskSpec(
                    description="Review PR #42",
                    purpose=SessionPurpose.DEBT,
                    prompt="Review the PR",
                ),
                status=QueueItemStatus.RUNNING,
            ),
            QueueItem(
                id="q3",
                client="alpha",
                task=TaskSpec(
                    description="Old completed task",
                    purpose=SessionPurpose.DEBT,
                    prompt="Done",
                ),
                status=QueueItemStatus.COMPLETED,
            ),
        ]
    )


def test_format_queue_status() -> None:
    """Queue status formatting includes color markup."""
    assert "[yellow]" in _format_queue_status("pending")
    assert "[green]" in _format_queue_status("running")
    assert "[dim]" in _format_queue_status("completed")
    assert "[red]" in _format_queue_status("failed")
    assert _format_queue_status("unknown") == "unknown"


@pytest.mark.asyncio
async def test_queue_panel_shows_pending_and_running(
    tmp_config_dir: Path,
) -> None:
    """QueuePanel shows PENDING and RUNNING items, not COMPLETED."""
    store = _make_queue_store()

    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=_make_state()),
        patch("cw.tui.load_history", return_value=[]),
        patch("cw.tui.load_queue", return_value=store),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._clients = _make_clients()
            app._selected_client = "alpha"
            app._refresh_queue()
            await pilot.pause()

            queue_table = app.query_one(QueuePanel)
            # 2 active items (pending + running), not 3
            assert queue_table.row_count == 2


@pytest.mark.asyncio
async def test_queue_panel_empty_when_no_client(
    tmp_config_dir: Path,
) -> None:
    """QueuePanel is empty when no client is selected."""
    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=CwState()),
        patch("cw.tui.load_history", return_value=[]),
        patch("cw.tui.load_queue", return_value=QueueStore()),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_client = None
            app._refresh_queue()
            await pilot.pause()

            queue_table = app.query_one(QueuePanel)
            assert queue_table.row_count == 0


# --- Confirm dialog tests ---


@pytest.mark.asyncio
async def test_confirm_screen_dismiss_on_cancel(
    tmp_config_dir: Path,
) -> None:
    """ConfirmScreen dismisses with False on Cancel."""
    results: list[bool | None] = []

    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=CwState()),
        patch("cw.tui.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            def on_result(result: bool | None) -> None:
                results.append(result)

            app.push_screen(
                ConfirmScreen("Test?"), on_result,
            )
            await pilot.pause()
            await pilot.click("#confirm-no")
            await pilot.pause()

            assert results == [False]


@pytest.mark.asyncio
async def test_confirm_screen_dismiss_on_confirm(
    tmp_config_dir: Path,
) -> None:
    """ConfirmScreen dismisses with True on Confirm."""
    results: list[bool | None] = []

    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=CwState()),
        patch("cw.tui.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            def on_result(result: bool | None) -> None:
                results.append(result)

            app.push_screen(
                ConfirmScreen("Test?"), on_result,
            )
            await pilot.pause()
            await pilot.click("#confirm-yes")
            await pilot.pause()

            assert results == [True]


# --- Session detail tests ---


@pytest.mark.asyncio
async def test_session_detail_screen_shows_fields(
    tmp_config_dir: Path,
) -> None:
    """SessionDetailScreen shows session fields."""
    session = Session(
        id="s1",
        name="alpha/impl",
        client="alpha",
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        origin=SessionOrigin.DELEGATE,
        workspace_path="/home/test/alpha",
        branch="feat/search",
        started_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
    )

    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=CwState()),
        patch("cw.tui.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(SessionDetailScreen(session))
            await pilot.pause()

            # The detail screen should be the active screen
            screen = app.screen
            assert isinstance(screen, SessionDetailScreen)


@pytest.mark.asyncio
async def test_expand_key_with_no_session(
    tmp_config_dir: Path,
) -> None:
    """Pressing 'e' with no session selected does nothing."""
    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=CwState()),
        patch("cw.tui.load_history", return_value=[]),
    ):
        app = CwDashboard()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # No sessions loaded, pressing 'e' should not crash
            await pilot.press("e")
            await pilot.pause()


# --- Phase 2: Origin column and sidebar badge tests ---


def test_format_origin_delegate() -> None:
    """Delegate origin shows cyan badge."""
    assert "[cyan]" in _format_origin("delegate")
    assert "[delegate]" in _format_origin("delegate")


def test_format_origin_daemon() -> None:
    """Daemon origin shows magenta badge."""
    assert "[magenta]" in _format_origin("daemon")
    assert "[daemon]" in _format_origin("daemon")


def test_format_origin_user_is_empty() -> None:
    """User origin returns empty string (no badge needed)."""
    assert _format_origin("user") == ""


@pytest.mark.asyncio
async def test_session_table_has_origin_column(
    tmp_config_dir: Path,
) -> None:
    """Session table includes Origin column."""
    state = CwState(
        sessions=[
            Session(
                id="s1",
                name="alpha/impl",
                client="alpha",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                origin=SessionOrigin.DELEGATE,
                workspace_path="/home/test/alpha",
                started_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
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
            app._clients = _make_clients()
            app._selected_client = "alpha"
            app._refresh_sessions()
            await pilot.pause()

            table = app.query_one(SessionTable)
            # Table should have 6 columns now
            assert len(table.columns) == 6


@pytest.mark.asyncio
async def test_sidebar_badge_update(
    tmp_config_dir: Path,
) -> None:
    """_refresh_sidebar updates ListItem labels with session counts."""
    clients = _make_clients()
    state = _make_state()  # alpha: 1 active, 1 backgrounded

    with (
        patch("cw.tui.load_clients", return_value={}),
        patch("cw.tui.load_state", return_value=CwState()),
        patch("cw.tui.load_history", return_value=[]),
    ):
        # Construct dashboard with clients pre-loaded so compose
        # creates sidebar items.
        app = CwDashboard()
        app._clients = clients
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Now set state and refresh sidebar
            app._state = state
            app._refresh_sidebar()
            await pilot.pause()

            sidebar = app.query_one(ClientList)
            found_alpha = False
            for item in sidebar.query(ListItem):
                static = item.query_one(Static)
                content = str(static.render())
                if "alpha" in content:
                    found_alpha = True
                    assert "A:1" in content
                    assert "B:1" in content

            assert found_alpha, "alpha client not found in sidebar"
