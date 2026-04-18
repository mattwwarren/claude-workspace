"""Tests for the orchestrator dashboard TUI.

Snapshot-style tests using rich's :class:`Console` with a captured buffer.
Rather than asserting exact byte-for-byte output (fragile under rich
version bumps), tests check for presence/absence of the meaningful tokens
-- client names, ticket IDs, detail-level-specific columns, and so on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from cw.orchestrate import (
    EventSummary,
    MonitoredPR,
    OrchestratorStatus,
    SessionSummary,
    TicketSummary,
)
from cw.tui import DetailLevel, render_dashboard, watch


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def sample_status(frozen_now: datetime) -> OrchestratorStatus:
    return OrchestratorStatus(
        generated_at=frozen_now,
        pending_tickets=[
            TicketSummary(
                ticket_id="MW-101",
                client="personal",
                priority=2,
                status="pending",
                created_at=frozen_now,
                scope_hint="refactor auth middleware",
            ),
            TicketSummary(
                ticket_id="MW-102",
                client="personal",
                priority=0,
                status="pending",
                created_at=frozen_now,
            ),
        ],
        running_sessions=[
            SessionSummary(
                id="abc12345",
                name="personal/impl",
                client="personal",
                status="active",
                purpose="impl",
                started_at=datetime(2026, 4, 18, 11, 55, 0, tzinfo=UTC),
                surface_ref="surf-1",
                worktree_path=Path("/home/matthew/workspace/personal/wt/abc"),
            ),
            SessionSummary(
                id="xyz98765",
                name="lgbtqplus.map/impl",
                client="lgbtqplus.map",
                status="active",
                purpose="impl",
                started_at=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
                worktree_path=None,
            ),
        ],
        monitored_prs=[
            MonitoredPR(
                repo="mattwwarren/claude-workspace",
                pr_number=42,
                role="author",
                status="watching",
                unresolved_threads=2,
            ),
        ],
        recent_events=[
            EventSummary(
                id="evt-1",
                type="session.completed",
                payload={"session_id": "abc12345", "reason": "HANDOFF"},
                correlation_id=None,
                created_at=frozen_now,
            ),
            EventSummary(
                id="evt-2",
                type="pr.merged",
                payload={"repo": "mattwwarren/claude-workspace", "pr_number": 42},
                correlation_id=None,
                created_at=frozen_now,
            ),
        ],
    )


def _render(
    status: OrchestratorStatus,
    level: DetailLevel,
    *,
    frozen_now: datetime,
    client_filter: str | None = None,
) -> str:
    buffer = StringIO()
    console = Console(file=buffer, width=120, record=False, force_terminal=False)
    console.print(
        render_dashboard(
            status,
            level=level,
            client_filter=client_filter,
            now=frozen_now,
            home="/home/matthew",
        ),
    )
    return buffer.getvalue()


class TestRenderDashboard:
    def test_default_level_shows_every_section(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(sample_status, DetailLevel.DEFAULT, frozen_now=frozen_now)

        # Clients grouped and rendered.
        assert "personal" in output
        assert "lgbtqplus.map" in output

        # Sessions visible.
        assert "abc12345" in output
        assert "xyz98765" in output

        # Tickets visible.
        assert "MW-101" in output
        assert "MW-102" in output

        # PRs visible.
        assert "mattwwarren/claude-workspace#42" in output

        # Events panel present with both events.
        assert "session.completed" in output
        assert "pr.merged" in output

    def test_compact_shows_counts_only(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(sample_status, DetailLevel.COMPACT, frozen_now=frozen_now)

        # Counts line renders per client.
        assert "running:" in output
        assert "pending:" in output
        assert "PRs:" in output

        # Per-client tables are suppressed in compact mode -- no ticket IDs.
        # (Session IDs may still appear in the events panel payload, which
        # is the audit log and stays visible at every level.)
        assert "MW-101" not in output
        assert "refactor auth middleware" not in output

    def test_verbose_adds_scope_and_surface_columns(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(sample_status, DetailLevel.VERBOSE, frozen_now=frozen_now)

        # Scope hint appears only at VERBOSE.
        assert "refactor auth middleware" in output
        # Surface ref column value appears only at VERBOSE.
        assert "surf-1" in output

    def test_client_filter_hides_others(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(
            sample_status,
            DetailLevel.DEFAULT,
            frozen_now=frozen_now,
            client_filter="personal",
        )
        assert "personal" in output
        assert "lgbtqplus.map" not in output

    def test_empty_status_renders_without_raising(
        self,
        frozen_now: datetime,
    ) -> None:
        empty = OrchestratorStatus(generated_at=frozen_now)
        output = _render(empty, DetailLevel.DEFAULT, frozen_now=frozen_now)
        assert "No active clients" in output

    def test_worktree_path_shortened_to_tilde(
        self,
        sample_status: OrchestratorStatus,
        frozen_now: datetime,
    ) -> None:
        output = _render(sample_status, DetailLevel.DEFAULT, frozen_now=frozen_now)
        # /home/matthew/... is collapsed to ~/...
        assert "~/workspace/personal/wt/abc" in output


class TestWatch:
    def test_ticks_mode_renders_n_frames(
        self,
        sample_status: OrchestratorStatus,
    ) -> None:
        buffer = StringIO()
        console = Console(file=buffer, width=120, force_terminal=False)
        watch(
            interval=1,
            level=DetailLevel.COMPACT,
            console=console,
            ticks=3,
            status_fn=lambda: sample_status,
        )
        # "personal" appears once per frame.
        assert buffer.getvalue().count("personal") >= 3

    def test_interval_clamped_high(
        self,
        sample_status: OrchestratorStatus,
    ) -> None:
        # Sanity: interval=9999 must not raise; ticks=0 means no frames at all.
        buffer = StringIO()
        console = Console(file=buffer, width=120, force_terminal=False)
        watch(
            interval=9999,
            level=DetailLevel.COMPACT,
            console=console,
            ticks=0,
            status_fn=lambda: sample_status,
        )
        assert buffer.getvalue() == ""
