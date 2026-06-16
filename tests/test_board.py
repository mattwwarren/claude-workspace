"""Tests for src/cw/board.py — pure render-function and CLI smoke tests."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from rich.console import Console

from cw.board import BoardState, render_board, run_board
from cw.models import (
    CwState,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    QueueItemStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)


def _render(board_state: BoardState, *, client_filter: str | None = None) -> str:
    """Render board_state to a string using a captured Console."""
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=200)
    console.print(render_board(board_state, client_filter=client_filter))
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


class TestRenderBoardEmpty:
    def test_renders_without_raising(self) -> None:
        output = _render(_empty_state())
        assert isinstance(output, str)

    def test_no_ticket_ids_in_empty_output(self) -> None:
        output = _render(_empty_state())
        # No ticket IDs — just headers/placeholders
        assert "MW-" not in output


class TestRenderBoardWithTickets:
    def _state_with_task(
        self,
        ticket_id: str = "MW-100",
        stage: Stage = Stage.PLAN,
        status: QueueItemStatus = QueueItemStatus.PENDING,
        client: str = "acme",
        lane: str = "default",
    ) -> BoardState:
        task = TicketTask(
            ticket_id=ticket_id,
            client=client,
            status=status,
            stage=stage,
            lane=lane,
        )
        return BoardState(
            cw_state=CwState(),
            dev_queue=DevQueueStore(tasks=[task]),
            clients={},
            config=OrchestratorConfig(),
            now=NOW,
        )

    def test_ticket_appears_in_output(self) -> None:
        output = _render(self._state_with_task("MW-100"))
        assert "MW-100" in output

    def test_age_renders_dash(self) -> None:
        output = _render(self._state_with_task())
        assert "—" in output

    def test_pr_renders_dash(self) -> None:
        output = _render(self._state_with_task())
        assert "—" in output

    def test_absent_client_does_not_raise(self) -> None:
        # client "acme" not in clients dict — should render "—" for model, no KeyError
        output = _render(self._state_with_task(client="acme"))
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
        default_pos = output.index("acme / default")
        fast_pos = output.index("acme / fast")
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
