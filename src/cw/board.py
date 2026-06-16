"""Live TUI board for cw dev-queue tickets - lane x stage cockpit (RFC 0005 D1)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cw.config import (
    load_effective_clients,
    load_effective_config,
    load_state,
)
from cw.dev_queue import load_dev_queue
from cw.models import (
    DEFAULT_LANE,
    ClientConfig,
    CwState,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    QueueItemStatus,
    Stage,
    StageExecutorConfig,
    TicketTask,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import RenderableType

_MIN_INTERVAL_SECONDS: int = 1
_MAX_INTERVAL_SECONDS: int = 60

# Status display map: status -> label
_STATUS_LABEL: dict[QueueItemStatus, str] = {
    QueueItemStatus.PENDING: "pending",
    QueueItemStatus.RUNNING: "running",
    QueueItemStatus.COMPLETED: "done",
    QueueItemStatus.FAILED: "failed",
    QueueItemStatus.CANCELLED: "cancelled",
    QueueItemStatus.BLOCKED_ON_USER: "blocked",
}


@dataclass
class BoardState:
    """Snapshot of all state needed to render one board frame."""

    cw_state: CwState
    dev_queue: DevQueueStore
    clients: dict[str, ClientConfig]
    config: OrchestratorConfig
    now: datetime


def _load_board_state() -> BoardState:
    """Read all state needed for one board frame.

    Does NOT call reconcile().
    # Why: board is read-only observer; calling reconcile() here would mutate
    # dev-queue state, violating lock-free observer contract.
    """
    return BoardState(
        cw_state=load_state(),
        dev_queue=load_dev_queue(),
        clients=load_effective_clients(),
        config=load_effective_config(),
        now=datetime.now(UTC),
    )


def _derive_model_display(
    task_stage: Stage,
    client_cfg: ClientConfig | None,
) -> str:
    """Derive the model string to display for a task row.

    Precedence: executor.model > client.worker_model > "—".
    Falls back to "—" when client_cfg is None (absent client guard - critical
    for Live safety when a task references a client not in the config).
    """
    if client_cfg is None:
        return "—"
    executor: StageExecutorConfig = client_cfg.pipeline.executors.get(
        task_stage, StageExecutorConfig()
    )
    if executor.model:
        return executor.model
    if client_cfg.worker_model:
        return client_cfg.worker_model
    return "—"


def _build_lane_panel(
    client_name: str,
    lane_name: str,
    max_parallel: int,
    paused: bool,
    tasks_in_lane: list[TicketTask],
    client_cfg: ClientConfig | None,
) -> Panel:
    """Build one Rich Panel for a single client/lane combination."""
    # Why: D1 (#624) specified one Panel per client containing a Table per lane.
    # We use one Panel per client x lane instead: flat layout is identical for
    # single-lane clients (today's norm) and simpler to render. Multi-lane
    # clients get separate panels rather than a nested grouping. Revisit if
    # per-client collapsing becomes a UX requirement.
    # Why: mirrors dispatch._lane_stats_for_client without importing the
    # private function.
    running = sum(
        1
        for t in tasks_in_lane
        if t.status in {QueueItemStatus.RUNNING, QueueItemStatus.BLOCKED_ON_USER}
    )

    pause_tag = " [PAUSED]" if paused else ""
    title = f"{client_name} / {lane_name}{pause_tag}  [{running}/{max_parallel}]"

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("TICKET", no_wrap=True)
    table.add_column("STAGE", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("MODEL", no_wrap=True)
    table.add_column("AGE", no_wrap=True)
    table.add_column("PR", no_wrap=True)

    for task in tasks_in_lane:
        model_display = _derive_model_display(task.stage, client_cfg)
        status_label = _STATUS_LABEL.get(task.status, str(task.status))
        table.add_row(
            task.ticket_id,
            task.stage.value,
            status_label,
            model_display,
            # Why: stage_entered_at lands in B2; render placeholder until then.
            "—",
            # Why: pr_url field does not exist on TicketTask (B2/REVIEW scope).
            "—",
        )

    return Panel(table, title=title)


def render_board(
    board_state: BoardState,
    *,
    client_filter: str | None = None,
) -> RenderableType:
    """Render a full board frame as a Rich renderable.

    Pure function - no I/O, no datetime.now() calls inside.
    """
    panels: list[RenderableType] = []

    # Gather all unique client names appearing in the queue (plus known clients).
    queue_clients: set[str] = {t.client for t in board_state.dev_queue.tasks}
    known_clients: set[str] = set(board_state.clients)
    all_clients = sorted(queue_clients | known_clients)

    if client_filter is not None:
        all_clients = [c for c in all_clients if c == client_filter]

    for client_name in all_clients:
        client_cfg = board_state.clients.get(client_name)

        # Determine lanes to show for this client.
        if client_cfg is not None:
            lanes = client_cfg.effective_lanes
        else:
            # Unknown client: synthesise a single default lane so orphan tasks show.
            lanes = [LaneConfig(name=DEFAULT_LANE)]

        client_tasks = [
            t for t in board_state.dev_queue.tasks if t.client == client_name
        ]

        # Synthesised flag: True when we made up a default lane for an unknown client.
        lanes_synthesised = client_cfg is None

        for lane_cfg in lanes:
            tasks_in_lane = [t for t in client_tasks if t.lane == lane_cfg.name]
            # Skip synthesised lanes with no tasks (unknown client, nothing to show).
            # Configured lanes are always shown so pause/max_parallel info is visible.
            if not tasks_in_lane and lanes_synthesised:
                continue

            panel = _build_lane_panel(
                client_name=client_name,
                lane_name=lane_cfg.name,
                max_parallel=lane_cfg.max_parallel,
                paused=lane_cfg.paused,
                tasks_in_lane=tasks_in_lane,
                client_cfg=client_cfg,
            )
            panels.append(panel)

    # Footer: active sessions vs total ceiling.
    active_count = len(board_state.cw_state.active_sessions())
    total_ceiling = sum(
        board_state.config.per_client_ceiling.get(c, board_state.config.default_ceiling)
        for c in board_state.clients
    )
    footer_text = Text(
        f"  Sessions: {active_count} active  |  Ceiling: {total_ceiling}  "
        f"|  Refreshed: {board_state.now.strftime('%H:%M:%S')} UTC"
    )

    if panels:
        return Group(*panels, footer_text)
    return Group(Text("No tickets in queue."), footer_text)


def run_board(
    *,
    once: bool = False,
    interval: int = 5,
    client_filter: str | None = None,
    console: Console | None = None,
    ticks: int | None = None,
    loader_fn: Callable[[], BoardState] | None = None,
) -> None:
    """Run the board - either a Live loop or a one-shot snapshot.

    Args:
        once: Print one frame and exit (for non-TTY/CI).
        interval: Seconds between data refreshes (clamped 1-60).
        client_filter: Only render this client when set.
        console: Rich Console to use (created if None).
        ticks: Test shim - render this many frames then return.
        loader_fn: Override the state loader (defaults to _load_board_state).
    """
    if console is None:
        console = Console()
    if loader_fn is None:
        loader_fn = _load_board_state

    interval = max(_MIN_INTERVAL_SECONDS, min(_MAX_INTERVAL_SECONDS, interval))

    def _build() -> RenderableType:
        return render_board(loader_fn(), client_filter=client_filter)

    # Single-frame paths (once or ticks=1).
    if once or ticks == 1:
        console.print(_build())
        return

    # Multi-tick test shim: render N frames, each via console.print.
    if ticks is not None:
        for _ in range(ticks):
            try:
                console.print(_build())
            except KeyboardInterrupt:
                return
        return

    # Full Live loop for interactive use.
    try:
        with Live(
            _build(),
            console=console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            while True:
                time.sleep(interval)
                live.update(_build())
    except KeyboardInterrupt:
        pass
