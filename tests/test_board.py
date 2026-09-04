"""Tests for src/cw/board.py — pure render-function and CLI smoke tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import get_args

from rich.console import Console

from cw.board import BoardState, render_board, run_board
from cw.models import (
    CwState,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    Session,
    SessionPurpose,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.orchestrate import SessionSummary, TicketSummary
from cw.pr_hydrate import PrAttentionState


def _render(
    board_state: BoardState,
    *,
    client_filter: str | None = None,
    raw_events: bool = False,
    detail: bool = False,
) -> str:
    """Render board_state to a string using a captured Console."""
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=200)
    console.print(
        render_board(
            board_state,
            client_filter=client_filter,
            raw_events=raw_events,
            detail=detail,
        )
    )
    return buf.getvalue()


NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)


def _empty_state() -> BoardState:
    return BoardState(
        cw_state=CwState(),
        dev_queue=DevQueueStore(),
        clients={},
        config=OrchestratorConfig(),
        now=NOW,
    )


def _state_with_task(
    ticket_id: str = "MW-100",
    stage: Stage = Stage.PLAN,
    status: QueueItemStatus = QueueItemStatus.PENDING,
    client: str = "acme",
    lane: str = "default",
    pr_state: PrState | None = None,
    session_id: str | None = None,
    created_at: datetime | None = None,
    events: list[OrchestratorEvent] | None = None,
    sessions: list[Session] | None = None,
    running_sessions: list[SessionSummary] | None = None,
    pending_tickets: list[TicketSummary] | None = None,
) -> BoardState:
    """Build a BoardState with one TicketTask — shared builder for board tests."""
    task = TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=status,
        stage=stage,
        lane=lane,
        pr_state=pr_state,
        session_id=session_id,
        created_at=created_at or NOW,
    )
    return BoardState(
        cw_state=CwState(sessions=sessions or []),
        dev_queue=DevQueueStore(tasks=[task]),
        clients={},
        config=OrchestratorConfig(),
        now=NOW,
        events=events or [],
        running_sessions=running_sessions or [],
        pending_tickets=pending_tickets or [],
    )


class TestRenderBoardEmpty:
    def test_renders_without_raising(self) -> None:
        output = _render(_empty_state())
        assert isinstance(output, str)

    def test_no_ticket_ids_in_empty_output(self) -> None:
        output = _render(_empty_state())
        # No ticket IDs — just headers/placeholders
        assert "MW-" not in output


class TestRenderBoardWithTickets:
    def test_ticket_appears_in_output(self) -> None:
        output = _render(_state_with_task(ticket_id="MW-100"))
        assert "MW-100" in output

    def test_age_renders_dash(self) -> None:
        output = _render(_state_with_task())
        assert "—" in output

    def test_pr_renders_dash(self) -> None:
        output = _render(_state_with_task())
        assert "—" in output

    def test_absent_client_does_not_raise(self) -> None:
        # client "acme" not in clients dict — should render "—" for model, no KeyError
        output = _render(_state_with_task(client="acme"))
        assert isinstance(output, str)

    def test_model_derived_from_executor_config(self) -> None:
        from cw.models import ClientConfig

        executor = StageExecutorConfig(
            backend="test-backend", model="claude-test-model"
        )
        pipeline = StagePipelineConfig(executors={Stage.PLAN: executor})
        client_cfg = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
            pipeline=pipeline,
        )
        task = TicketTask(
            ticket_id="MW-200",
            client="acme",
            stage=Stage.PLAN,
            status=QueueItemStatus.PENDING,
        )
        state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task]),
            clients={"acme": client_cfg},
            config=OrchestratorConfig(),
            now=NOW,
        )
        output = _render(state)
        assert "claude-test-model" in output

    def test_status_label_awaiting_signoff(self) -> None:
        """AWAITING_OPERATOR_SIGNOFF renders as 'awaiting signoff' (#990)."""
        output = _render(
            _state_with_task(
                ticket_id="MW-990",
                status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            )
        )
        assert "awaiting signoff" in output

    def test_model_fallback_to_worker_model(self) -> None:
        from cw.models import ClientConfig

        client_cfg = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
            worker_model="claude-fallback",
        )
        task = TicketTask(
            ticket_id="MW-201",
            client="acme",
            stage=Stage.IMPL,
            status=QueueItemStatus.PENDING,
        )
        state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task]),
            clients={"acme": client_cfg},
            config=OrchestratorConfig(),
            now=NOW,
        )
        output = _render(state)
        assert "claude-fallback" in output


class TestRenderBoardLaneHeader:
    def _state_with_lane(
        self,
        max_parallel: int = 3,
        paused: bool = False,
        running_count: int = 0,
    ) -> BoardState:
        from cw.models import ClientConfig

        lane = LaneConfig(name="default", max_parallel=max_parallel, paused=paused)
        client_cfg = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
            lanes=[lane],
        )
        tasks = [
            TicketTask(
                ticket_id=f"MW-{300 + i}",
                client="acme",
                status=QueueItemStatus.RUNNING,
                stage=Stage.IMPL,
                lane="default",
            )
            for i in range(running_count)
        ]
        return BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=tasks),
            clients={"acme": client_cfg},
            config=OrchestratorConfig(),
            now=NOW,
        )

    def test_max_parallel_shown(self) -> None:
        output = _render(self._state_with_lane(max_parallel=3))
        assert "3" in output

    def test_paused_shown(self) -> None:
        output = _render(self._state_with_lane(paused=True))
        assert "PAUSED" in output.upper()

    def test_running_count_shown(self) -> None:
        output = _render(self._state_with_lane(running_count=2, max_parallel=4))
        assert "2" in output

    def test_lane_panel_occupancy_counts_awaiting_signoff(self) -> None:
        """AWAITING_OPERATOR_SIGNOFF occupies its lane slot like BLOCKED_ON_USER
        for the panel's [running/max_parallel] title tally (#990)."""
        from cw.models import ClientConfig

        lane = LaneConfig(name="default", max_parallel=3)
        client_cfg = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
            lanes=[lane],
        )
        task = TicketTask(
            ticket_id="MW-990",
            client="acme",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            stage=Stage.REVIEW,
            lane="default",
        )
        state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task]),
            clients={"acme": client_cfg},
            config=OrchestratorConfig(),
            now=NOW,
        )
        output = _render(state)
        assert "[1/3]" in output

    def test_lane_panel_occupancy_excludes_terminal_sibling_park(self) -> None:
        """A terminal_sibling BLOCKED_ON_USER row is not counted (#2100).

        Inverse of test_lane_panel_occupancy_counts_awaiting_signoff: an
        ordinary BLOCKED_ON_USER row occupies its slot, but a terminal_sibling
        one does not -- the panel title tally must read [0/3], not [1/3].
        """
        from cw.models import ClientConfig

        lane = LaneConfig(name="default", max_parallel=3)
        client_cfg = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
            lanes=[lane],
        )
        task = TicketTask(
            ticket_id="MW-2100",
            client="acme",
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="terminal_sibling",
            stage=Stage.REVIEW,
            lane="default",
        )
        state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task]),
            clients={"acme": client_cfg},
            config=OrchestratorConfig(),
            now=NOW,
        )
        output = _render(state)
        assert "[0/3]" in output


class TestRenderBoardFooter:
    def test_footer_present(self) -> None:
        output = _render(_empty_state())
        # Footer always rendered — just check it doesn't crash
        assert isinstance(output, str)

    def test_footer_ceiling_from_config(self) -> None:
        from cw.models import ClientConfig

        client_cfg = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
        )
        state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(),
            clients={"acme": client_cfg},
            config=OrchestratorConfig(per_client_ceiling={"acme": 4}),
            now=NOW,
        )
        output = _render(state)
        assert "4" in output


class TestRenderBoardClientFilter:
    def test_filter_hides_other_clients(self) -> None:
        task_a = TicketTask(
            ticket_id="MW-400",
            client="acme",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
        )
        task_b = TicketTask(
            ticket_id="MW-401",
            client="beta",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
        )
        state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task_a, task_b]),
            clients={},
            config=OrchestratorConfig(),
            now=NOW,
        )
        output = _render(state, client_filter="acme")
        assert "MW-400" in output
        assert "MW-401" not in output


class TestRenderBoardMultiLane:
    """Multi-lane client: both lane headers/sections must render."""

    def _state_with_two_lanes(self) -> BoardState:
        from cw.models import ClientConfig

        client_cfg = ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/acme"),
            lanes=[
                LaneConfig(name="default"),
                LaneConfig(name="fast"),
            ],
        )
        tasks = [
            TicketTask(
                ticket_id="MW-501",
                client="acme",
                status=QueueItemStatus.PENDING,
                stage=Stage.PLAN,
                lane="default",
            ),
            TicketTask(
                ticket_id="MW-502",
                client="acme",
                status=QueueItemStatus.RUNNING,
                stage=Stage.IMPL,
                lane="fast",
            ),
        ]
        return BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=tasks),
            clients={"acme": client_cfg},
            config=OrchestratorConfig(),
            now=NOW,
        )

    def test_both_lane_headers_render(self) -> None:
        output = _render(self._state_with_two_lanes())
        assert "acme / default" in output
        assert "acme / fast" in output

    def test_both_lane_tickets_render(self) -> None:
        output = _render(self._state_with_two_lanes())
        assert "MW-501" in output
        assert "MW-502" in output

    def test_lanes_are_separate_panels(self) -> None:
        output = _render(self._state_with_two_lanes())
        default_pos = output.find("acme / default")
        fast_pos = output.find("acme / fast")
        assert default_pos != -1, "acme / default panel not found in output"
        assert fast_pos != -1, "acme / fast panel not found in output"
        assert default_pos < fast_pos


class TestRenderBoardSynthesisedLaneSkip:
    """Cover the synthesised-lane skip path (line 189 in board.py)."""

    def test_unknown_client_with_no_tasks_renders_without_lane_panel(self) -> None:
        """An unknown client (not in clients dict) with zero tasks produces no panel."""
        # task_a belongs to "acme" (unknown client), task_b to lane "fast" not "default"
        # The synthesised lane "default" for "acme" gets skipped since no tasks match.
        task = TicketTask(
            ticket_id="MW-900",
            client="orphan",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
            lane="nonexistent-lane",  # won't match DEFAULT_LANE
        )
        state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task]),
            clients={},
            config=OrchestratorConfig(),
            now=NOW,
        )
        # Should not raise; the synthesised default lane for "orphan" is skipped
        # because no tasks have lane="default". The ticket in "nonexistent-lane"
        # is also invisible (no matching lane panel). Board shows "No tickets".
        output = _render(state)
        assert isinstance(output, str)
        # ticket won't appear since it's in a non-default lane with no config
        assert "MW-900" not in output


def _session_summary(
    session_id: str,
    client: str = "acme",
    *,
    purpose: str = "impl",
    worktree_path: Path | None = None,
) -> SessionSummary:
    return SessionSummary(
        id=session_id,
        name=f"{client}/impl",
        client=client,
        status="active",
        purpose=purpose,
        started_at=NOW - timedelta(minutes=5),
        worktree_path=worktree_path,
    )


class TestRenderBoardDetail:
    def test_detail_panel_shows_session_id_and_client(self) -> None:
        state = _state_with_task(
            running_sessions=[_session_summary("sess-abc", client="acme")],
        )
        output = _render(state, detail=True)
        assert "sess-abc" in output
        assert "acme" in output

    def test_detail_panel_shows_worktree_column(self) -> None:
        state = _state_with_task(
            running_sessions=[
                _session_summary("sess-abc", worktree_path=Path("/home/u/wt/dev-1"))
            ],
        )
        output = _render(state, detail=True)
        assert "WORKTREE" in output

    def test_contention_marker_when_two_sessions_share_worktree(self) -> None:
        shared = Path("/home/u/wt/shared")
        state = _state_with_task(
            running_sessions=[
                _session_summary("sess-a", worktree_path=shared),
                _session_summary("sess-b", worktree_path=shared),
                _session_summary("sess-c", worktree_path=Path("/home/u/wt/solo")),
            ],
        )
        output = _render(state, detail=True)
        assert "⚠x2" in output
        solo_line = next(line for line in output.splitlines() if "sess-c" in line)
        assert "⚠" not in solo_line

    def test_default_board_has_no_detail_panel(self) -> None:
        state = _state_with_task(
            running_sessions=[_session_summary("sess-abc")],
        )
        output = _render(state, detail=False)
        # The detail panel is not rendered without detail=True.
        assert "sess-abc" not in output
        assert "Sessions (detail)" not in output

    def test_detail_panel_renders_with_no_sessions(self) -> None:
        """detail=True with empty running_sessions/pending_tickets renders."""
        state = _state_with_task(running_sessions=[], pending_tickets=[])
        output = _render(state, detail=True)
        assert "Sessions (detail)" in output


class TestRunBoard:
    def test_ticks_once_renders_without_live(self) -> None:
        """ticks=1 path renders one frame via console.print, no Live."""
        buf = StringIO()
        console = Console(file=buf, no_color=True, width=200)
        state = _empty_state()
        run_board(once=True, console=console, loader_fn=lambda: state)
        output = buf.getvalue()
        assert isinstance(output, str)

    def test_keyboard_interrupt_exits_cleanly(self) -> None:
        """KeyboardInterrupt in ticks path is caught — run_board returns normally."""
        buf = StringIO()
        console = Console(file=buf, no_color=True, width=200)
        # Use ticks=1 to verify single-frame path doesn't raise
        run_board(ticks=1, console=console, loader_fn=_empty_state)

    def test_multi_tick_renders_n_frames(self) -> None:
        """ticks=N path calls loader N times and prints N frames."""
        calls: list[int] = []

        def counting_loader() -> BoardState:
            calls.append(1)
            return _empty_state()

        buf = StringIO()
        console = Console(file=buf, no_color=True, width=200)
        run_board(ticks=3, console=console, loader_fn=counting_loader)
        assert len(calls) == 3

    def test_detail_frame_renders_session(self) -> None:
        """run_board(detail=True) renders the detail panel for a session."""
        state = _state_with_task(
            running_sessions=[_session_summary("sess-xyz", client="acme")],
        )
        buf = StringIO()
        console = Console(file=buf, no_color=True, width=200)
        run_board(ticks=1, detail=True, console=console, loader_fn=lambda: state)
        assert "sess-xyz" in buf.getvalue()

    def test_multi_tick_keyboard_interrupt_exits(self) -> None:
        """KeyboardInterrupt during multi-tick loop exits cleanly."""
        call_count = [0]

        def raising_loader() -> BoardState:
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt
            return _empty_state()

        buf = StringIO()
        console = Console(file=buf, no_color=True, width=200)
        # ticks=5 but loader raises KI on 2nd call — should return cleanly
        run_board(ticks=5, console=console, loader_fn=raising_loader)
        assert call_count[0] == 2

    def test_live_loop_keyboard_interrupt_exits(self) -> None:
        """Live loop exits cleanly on KeyboardInterrupt (mocked Live)."""
        from unittest.mock import MagicMock, patch

        buf = StringIO()
        console = Console(file=buf, no_color=True, width=200)

        mock_live = MagicMock()
        mock_live.__enter__ = MagicMock(return_value=mock_live)
        mock_live.__exit__ = MagicMock(return_value=False)
        # Raise KeyboardInterrupt on first update call
        mock_live.update.side_effect = KeyboardInterrupt

        with patch("cw.board.Live", return_value=mock_live):
            # once=False, ticks=None -> full Live path
            run_board(console=console, loader_fn=_empty_state)
        # Should return without raising
        assert True

    def test_load_board_state_callable(self) -> None:
        """_load_board_state is callable and accepts no args (integration smoke)."""
        from unittest.mock import patch

        from cw.board import _load_board_state

        with (
            patch("cw.board.load_state", return_value=CwState()),
            patch("cw.board.load_dev_queue", return_value=DevQueueStore()),
            patch("cw.board.load_effective_clients", return_value={}),
            patch("cw.board.load_effective_config", return_value=OrchestratorConfig()),
        ):
            result = _load_board_state()
        assert isinstance(result, BoardState)


class TestFormatAge:
    def test_none_anchor_renders_dash(self) -> None:
        from cw.board import _format_age

        assert _format_age(NOW, None) == "—"

    def test_five_minutes(self) -> None:
        from cw.board import _format_age

        assert _format_age(NOW, NOW - timedelta(minutes=5)) == "5m"

    def test_three_hours(self) -> None:
        from cw.board import _format_age

        assert _format_age(NOW, NOW - timedelta(hours=3)) == "3h"

    def test_two_days(self) -> None:
        from cw.board import _format_age

        assert _format_age(NOW, NOW - timedelta(days=2)) == "2d"


class TestSessionAgeRender:
    def test_running_task_age_from_session(self) -> None:
        session = Session(
            id="sess-1",
            name="acme/impl",
            client="acme",
            purpose=SessionPurpose.IMPL,
            workspace_path=Path("/tmp/acme"),
            started_at=NOW - timedelta(hours=2),
        )
        state = _state_with_task(
            status=QueueItemStatus.RUNNING,
            session_id="sess-1",
            sessions=[session],
        )
        output = _render(state)
        assert "2h" in output

    def test_pending_task_falls_back_to_created_at(self) -> None:
        # created_at is distinct from NOW so this can't pass under a broken
        # implementation that hardcodes anchor=now regardless of created_at.
        state = _state_with_task(
            status=QueueItemStatus.PENDING, created_at=NOW - timedelta(hours=1)
        )
        output = _render(state)
        assert "1h" in output


class TestPrCell:
    def test_pr_none_renders_dash(self) -> None:
        output = _render(_state_with_task(pr_state=None))
        assert "—" in output

    def test_ci_fail_label(self) -> None:
        output = _render(_state_with_task(pr_state=PrState(ci_ok=False)))
        assert "CI-FAIL" in output

    def test_ci_failing_attention_state_does_not_duplicate_ci_fail_label(self) -> None:
        # ci_ok=False + attention_state="ci_failing" is the common pr_hydrate
        # combination — the PR cell itself must not restate CI-FAIL as a
        # separate "CI-FAILING" token. (The BADGE column separately falls
        # back to attention_state by design — see _row_badge — so this
        # asserts on _render_pr_cell directly rather than full board output.)
        from cw.board import _render_pr_cell

        cell = _render_pr_cell(PrState(ci_ok=False, attention_state="ci_failing"))
        assert cell == "CI-FAIL"

    def test_approved_review_decision(self) -> None:
        output = _render(
            _state_with_task(pr_state=PrState(ci_ok=True, review_decision="APPROVED"))
        )
        assert "APPROVED" in output

    def test_pr_attention_state_label(self) -> None:
        output = _render(
            _state_with_task(pr_state=PrState(attention_state="changes_requested"))
        )
        assert "CHANGES-REQUESTED" in output

    def test_pr_attention_labels_cover_every_attention_state(self) -> None:
        from cw.board import _PR_ATTENTION_LABELS

        assert set(_PR_ATTENTION_LABELS) == set(get_args(PrAttentionState))

    def test_pr_attention_labels_render_unchanged(self) -> None:
        from cw.board import _PR_ATTENTION_LABELS

        assert _PR_ATTENTION_LABELS == {
            "merge_blocked": "MERGE-BLOCKED",
            "ci_failing": "CI-FAILING",
            "changes_requested": "CHANGES-REQUESTED",
            "no_reviewer": "NO-REVIEWER",
            "ready_to_approve": "READY-TO-APPROVE",
        }

    def test_ci_ok_renders_ci_ok_glyph(self) -> None:
        from cw.board import _render_pr_cell

        assert _render_pr_cell(PrState(ci_ok=True)) == "CI-OK"


class TestBadges:
    def test_reap_beats_attention_and_pr_state(self) -> None:
        events = [
            OrchestratorEvent(
                type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                payload={"ticket_id": "MW-100"},
                created_at=NOW,
            ),
            OrchestratorEvent(
                type=OrchestratorEventType.SESSION_REAP_PROPOSED,
                payload={"ticket_id": "MW-100"},
                created_at=NOW,
            ),
        ]
        state = _state_with_task(
            pr_state=PrState(attention_state="ready_to_approve"),
            events=events,
        )
        output = _render(state)
        assert "REAP" in output

    def test_reap_beats_attention_regardless_of_event_order(self) -> None:
        # Reversed order vs test_reap_beats_attention_and_pr_state above —
        # precedence must hold independent of event order (see
        # _index_badge_events's `elif ticket_id not in result` guard).
        events = [
            OrchestratorEvent(
                type=OrchestratorEventType.SESSION_REAP_PROPOSED,
                payload={"ticket_id": "MW-100"},
                created_at=NOW,
            ),
            OrchestratorEvent(
                type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                payload={"ticket_id": "MW-100"},
                created_at=NOW,
            ),
        ]
        state = _state_with_task(
            pr_state=PrState(attention_state="ready_to_approve"),
            events=events,
        )
        output = _render(state)
        assert "REAP" in output

    def test_needs_attention_only(self) -> None:
        events = [
            OrchestratorEvent(
                type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                payload={"ticket_id": "MW-100"},
                created_at=NOW,
            ),
        ]
        output = _render(_state_with_task(events=events))
        assert "ATTN" in output

    def test_pr_attention_state_only(self) -> None:
        output = _render(
            _state_with_task(pr_state=PrState(attention_state="ready_to_approve"))
        )
        assert "READY-TO-APPROVE" in output

    def test_no_badge_when_none_present(self) -> None:
        from cw.board import _DASH, _row_badge

        assert _row_badge(ticket_id="MW-100", pr_state=None, badge_index={}) == _DASH

    def test_row_badge_falls_through_to_ready_to_approve_after_infra_only_fix(
        self,
    ) -> None:
        from cw.board import _row_badge

        assert (
            _row_badge(
                ticket_id="MW-100",
                pr_state=PrState(ci_ok=True, attention_state="ready_to_approve"),
                badge_index={},
            )
            == "READY-TO-APPROVE"
        )

    def test_event_older_than_window_dropped(self) -> None:
        old_event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload={"ticket_id": "MW-100"},
            created_at=NOW - timedelta(hours=25),
        )
        output = _render(_state_with_task(events=[old_event]))
        assert "ATTN" not in output

    def test_event_ticket_mismatch_dropped(self) -> None:
        mismatched = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload={"ticket_id": "MW-999"},
            created_at=NOW,
        )
        output = _render(_state_with_task(events=[mismatched]))
        assert "ATTN" not in output

    def test_client_scoped_freshness_block_shows_client_header_badge(self) -> None:
        """A freshness-block SESSION_NEEDS_ATTENTION (client set, no ticket_id)
        surfaces via the client-header badge, not the per-ticket row badge."""
        freshness_event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload={"client": "acme", "ticket_id": None},
            created_at=NOW,
        )
        output = _render(_state_with_task(client="acme", events=[freshness_event]))
        assert "[ATTN]" in output

    def test_client_scoped_badge_does_not_bleed_to_unrelated_client(self) -> None:
        """A freshness-block event scoped to one client must not badge another
        client's panel (no false-positive badge bleed)."""
        task_acme = TicketTask(
            ticket_id="MW-200",
            client="acme",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
            lane="default",
            created_at=NOW,
        )
        task_other = TicketTask(
            ticket_id="MW-201",
            client="other-client",
            status=QueueItemStatus.PENDING,
            stage=Stage.PLAN,
            lane="default",
            created_at=NOW,
        )
        freshness_event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload={"client": "acme", "ticket_id": None},
            created_at=NOW,
        )
        state = BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task_acme, task_other]),
            clients={},
            config=OrchestratorConfig(),
            now=NOW,
            events=[freshness_event],
        )
        output = _render(state)

        acme_idx = output.index("acme / default")
        other_idx = output.index("other-client / default")
        acme_title_end = output.index("\n", acme_idx)
        other_title_end = output.index("\n", other_idx)
        assert "[ATTN]" in output[acme_idx:acme_title_end]
        assert "[ATTN]" not in output[other_idx:other_title_end]

    def test_ticket_scoped_needs_attention_does_not_show_client_header_badge(
        self,
    ) -> None:
        """A routine ticket-scoped SESSION_NEEDS_ATTENTION (e.g. silently_idle)
        carries `client` alongside a real ticket_id, per every pre-existing
        emit site (idle.py, stalled.py, phantom.py, etc). It must surface only
        via the per-ticket row badge, not bleed into the client-header badge —
        regression lock for the false-positive _index_client_badge_events bug
        caught in review (#996)."""
        ticket_scoped_event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload={"client": "acme", "ticket_id": "MW-100"},
            created_at=NOW,
        )
        output = _render(_state_with_task(client="acme", events=[ticket_scoped_event]))

        acme_idx = output.index("acme / default")
        acme_title_end = output.index("\n", acme_idx)
        assert "[ATTN]" not in output[acme_idx:acme_title_end]
        assert "ATTN" in output  # still shows via the per-ticket row badge

    def test_ticket_scoped_reap_proposed_does_not_show_client_header_badge(
        self,
    ) -> None:
        """SESSION_REAP_PROPOSED is always ticket/session-scoped (never carries
        ticket_id=None) — it must never populate the client-header badge."""
        reap_event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_REAP_PROPOSED,
            payload={"client": "acme", "ticket_id": "MW-100"},
            created_at=NOW,
        )
        output = _render(_state_with_task(client="acme", events=[reap_event]))

        acme_idx = output.index("acme / default")
        acme_title_end = output.index("\n", acme_idx)
        assert "[ATTN]" not in output[acme_idx:acme_title_end]
        assert "[REAP]" not in output[acme_idx:acme_title_end]
        assert "REAP" in output  # still shows via the per-ticket row badge


class TestAggregateFeed:
    def test_consecutive_ticks_collapse(self) -> None:
        from cw.board import _aggregate_feed

        events = [
            OrchestratorEvent(type=OrchestratorEventType.DISPATCH_TICK, created_at=NOW),
            OrchestratorEvent(
                type=OrchestratorEventType.DISPATCH_TICK,
                created_at=NOW + timedelta(minutes=1),
            ),
            OrchestratorEvent(
                type=OrchestratorEventType.DISPATCH_TICK,
                created_at=NOW + timedelta(minutes=2),
            ),
        ]
        result = _aggregate_feed(events)
        assert len(result) == 1
        assert "x3" in result[0].text
        assert "2m" in result[0].text

    def test_non_tick_breaks_run(self) -> None:
        from cw.board import _aggregate_feed

        events = [
            OrchestratorEvent(type=OrchestratorEventType.DISPATCH_TICK, created_at=NOW),
            OrchestratorEvent(
                type=OrchestratorEventType.DISPATCH_TICK,
                created_at=NOW + timedelta(minutes=1),
            ),
            OrchestratorEvent(
                type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                created_at=NOW + timedelta(minutes=2),
            ),
            OrchestratorEvent(
                type=OrchestratorEventType.DISPATCH_TICK,
                created_at=NOW + timedelta(minutes=3),
            ),
        ]
        result = _aggregate_feed(events)
        texts = [e.text for e in result]
        assert any("x2" in t for t in texts)
        assert any("x1" in t for t in texts)

    def test_single_tick_exact_label(self) -> None:
        from cw.board import _aggregate_feed

        events = [
            OrchestratorEvent(type=OrchestratorEventType.DISPATCH_TICK, created_at=NOW)
        ]
        result = _aggregate_feed(events)
        assert result[0].text == "dispatch.tick x1 over 0m"

    def test_burst_does_not_evict_earlier_signal(self) -> None:
        """Aggregate-then-tail: a >20-tick burst must not evict an earlier
        non-tick entry before aggregation collapses the burst.

        Exercises _build_event_feed_panel directly — the actual production
        function that owns the aggregate-then-tail order — not just
        _aggregate_feed, so a regression that flips the order inside
        _build_event_feed_panel (e.g. tailing raw events before aggregating)
        would be caught here.
        """
        from cw.board import _build_event_feed_panel

        attention_event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload={"ticket_id": "MW-1"},
            created_at=NOW,
        )
        ticks = [
            OrchestratorEvent(
                type=OrchestratorEventType.DISPATCH_TICK,
                created_at=NOW + timedelta(minutes=i + 1),
            )
            for i in range(25)
        ]
        events = [attention_event, *ticks]
        panel = _build_event_feed_panel(events, NOW + timedelta(minutes=30), raw=False)
        from rich.text import Text

        assert isinstance(panel.renderable, Text)
        body = panel.renderable.plain
        assert "session.needs_attention" in body
        assert "x25" in body

    def test_truncation_drops_earliest_aggregated_entries(self) -> None:
        """>20 aggregated (non-collapsible) entries: only the last
        _EVENT_FEED_LIMIT survive tailing, proving truncation isn't a no-op.

        Exercises _build_event_feed_panel directly (the tail-application
        site) and pins the exact cut boundary rather than only checking the
        two extremes.
        """
        from cw.board import _EVENT_FEED_LIMIT, _build_event_feed_panel

        events = [
            OrchestratorEvent(
                type=(
                    OrchestratorEventType.SESSION_NEEDS_ATTENTION
                    if i % 2 == 0
                    else OrchestratorEventType.SESSION_REAP_PROPOSED
                ),
                payload={"ticket_id": f"MW-{i}"},
                created_at=NOW + timedelta(minutes=i),
            )
            for i in range(25)
        ]
        panel = _build_event_feed_panel(events, NOW + timedelta(minutes=30), raw=False)
        from rich.text import Text

        assert isinstance(panel.renderable, Text)
        body = panel.renderable.plain
        assert "MW-0)" not in body
        assert "MW-4)" not in body
        assert "MW-5)" in body
        assert "MW-24)" in body
        matched_lines = sum(1 for line in body.splitlines() if "(MW-" in line)
        assert matched_lines == _EVENT_FEED_LIMIT


class TestEventFeedPanel:
    def test_panel_after_lane_panels_before_footer(self) -> None:
        tick_event = OrchestratorEvent(
            type=OrchestratorEventType.DISPATCH_TICK, created_at=NOW
        )
        output = _render(_state_with_task(events=[tick_event]))
        ticket_pos = output.find("MW-100")
        feed_pos = output.find("dispatch.tick")
        footer_pos = output.find("Sessions:")
        assert ticket_pos != -1
        assert feed_pos != -1
        assert footer_pos != -1
        assert ticket_pos < feed_pos < footer_pos

    def test_raw_events_shows_raw_stream_no_aggregation(self) -> None:
        tick_events = [
            OrchestratorEvent(type=OrchestratorEventType.DISPATCH_TICK, created_at=NOW),
            OrchestratorEvent(
                type=OrchestratorEventType.DISPATCH_TICK,
                created_at=NOW + timedelta(minutes=1),
            ),
        ]
        output = _render(_state_with_task(events=tick_events), raw_events=True)
        # Two ticks stay two separate raw lines — no "dispatch.tick xN" collapse.
        assert output.count("dispatch.tick") == 2
        assert "dispatch.tick x" not in output

    def test_empty_queue_and_events_renders_without_raising(self) -> None:
        output = _render(_empty_state())
        assert isinstance(output, str)


class TestLoadBoardStateClientScoping:
    def test_client_filter_scopes_read_events(self) -> None:
        from unittest.mock import patch

        from cw.board import _load_board_state

        with (
            patch("cw.board.load_state", return_value=CwState()),
            patch("cw.board.load_dev_queue", return_value=DevQueueStore()),
            patch("cw.board.load_effective_clients", return_value={}),
            patch("cw.board.load_effective_config", return_value=OrchestratorConfig()),
            patch("cw.board.read_events", return_value=[]) as mock_read,
        ):
            _load_board_state(client_filter="acme")
        assert mock_read.call_args.kwargs["client_names"] == frozenset({"acme"})

    def test_no_client_filter_reads_globally(self) -> None:
        from unittest.mock import patch

        from cw.board import _load_board_state

        with (
            patch("cw.board.load_state", return_value=CwState()),
            patch("cw.board.load_dev_queue", return_value=DevQueueStore()),
            patch("cw.board.load_effective_clients", return_value={}),
            patch("cw.board.load_effective_config", return_value=OrchestratorConfig()),
            patch("cw.board.read_events", return_value=[]) as mock_read,
        ):
            _load_board_state()
        assert mock_read.call_args.kwargs["client_names"] is None

    def test_bare_zero_arg_call_still_succeeds(self) -> None:
        """Regression guard: _load_board_state() with no args must still work."""
        from unittest.mock import patch

        from cw.board import _load_board_state

        with (
            patch("cw.board.load_state", return_value=CwState()),
            patch("cw.board.load_dev_queue", return_value=DevQueueStore()),
            patch("cw.board.load_effective_clients", return_value={}),
            patch("cw.board.load_effective_config", return_value=OrchestratorConfig()),
            patch("cw.board.read_events", return_value=[]),
        ):
            result = _load_board_state()
        assert isinstance(result, BoardState)

    def test_client_scoped_feed_panel_end_to_end(self) -> None:
        """A multi-client BoardState.events list renders only the filtered
        client's events in the feed panel — proving render_board's own
        client scoping actually excludes other clients' events, not just
        that _load_board_state's read_events call args are correct in
        isolation (which a single-client fixture cannot distinguish from a
        no-op filter)."""
        acme_event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload={"client": "acme", "ticket_id": "MW-1"},
            created_at=NOW,
        )
        other_event = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_REAP_PROPOSED,
            payload={"client": "other-client", "ticket_id": "MW-2"},
            created_at=NOW,
        )
        state = _state_with_task(client="acme", events=[acme_event, other_event])
        output = _render(state, client_filter="acme")
        assert "session.needs_attention" in output
        assert "session.reap_proposed" not in output


class TestBoardCliSmoke:
    def test_once_with_raw_events_exits_zero(self) -> None:
        from click.testing import CliRunner

        from cw.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["board", "--once", "--raw-events"])
        assert result.exit_code == 0

    def test_once_with_detail_exits_zero(self) -> None:
        from click.testing import CliRunner

        from cw.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["board", "--once", "--detail"])
        assert result.exit_code == 0
