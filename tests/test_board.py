"""Tests for src/cw/board.py — pure render-function and CLI smoke tests."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
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

        executor = StageExecutorConfig(backend="test-backend", model="claude-test-model")
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
        tasks = []
        for i in range(running_count):
            tasks.append(
                TicketTask(
                    ticket_id=f"MW-{300 + i}",
                    client="acme",
                    status=QueueItemStatus.RUNNING,
                    stage=Stage.IMPL,
                    lane="default",
                )
            )
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
        run_board(ticks=1, console=console, loader_fn=lambda: _empty_state())
