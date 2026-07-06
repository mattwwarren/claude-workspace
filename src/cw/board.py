"""Live TUI board for cw dev-queue tickets - lane x stage cockpit (RFC 0005 D1)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cw._util import _shorten_worktree
from cw.config import (
    load_effective_clients,
    load_effective_config,
    load_state,
)
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.models import (
    DEFAULT_LANE,
    OCCUPIED_LANE_STATUSES,
    ClientConfig,
    CwState,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.orchestrate import (
    SessionSummary,
    TicketSummary,
    summarise_session,
    summarise_ticket,
)
from cw.session_groups import (
    CONTENTION_THRESHOLD,
    group_by_client,
    sessions_by_client,
    worktree_contention,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import RenderableType

_MIN_INTERVAL_SECONDS: int = 1
_MAX_INTERVAL_SECONDS: int = 60

_DASH = "—"

# Status display map: status -> label
_STATUS_LABEL: dict[QueueItemStatus, str] = {
    QueueItemStatus.PENDING: "pending",
    QueueItemStatus.RUNNING: "running",
    QueueItemStatus.COMPLETED: "done",
    QueueItemStatus.FAILED: "failed",
    QueueItemStatus.CANCELLED: "cancelled",
    QueueItemStatus.BLOCKED_ON_USER: "blocked",
    QueueItemStatus.AWAITING_OPERATOR_SIGNOFF: "awaiting signoff",
}

# Bounded event-feed read: recency window + entry cap, replicating the
# #857 pattern (orchestrate.py's _ATTENTION_WINDOW/_RECENT_EVENTS_LIMIT)
# locally rather than importing orchestrate.py's private helpers.
_EVENT_FEED_WINDOW = timedelta(hours=24)
_EVENT_FEED_LIMIT = 20

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400

_PR_CI_OK = "CI-OK"
_PR_CI_FAIL = "CI-FAIL"
# Why: keys must mirror pr_hydrate._compute_attention_state's literal return
# values exactly ("merge_blocked"/"ci_failing"/"changes_requested"/
# "no_reviewer"/"ready_to_approve") — update both sites together if that
# function's contract changes.
_PR_ATTENTION_LABELS: dict[str, str] = {
    "merge_blocked": "MERGE-BLOCKED",
    "ci_failing": "CI-FAILING",
    "changes_requested": "CHANGES-REQUESTED",
    "no_reviewer": "NO-REVIEWER",
    "ready_to_approve": "READY-TO-APPROVE",
}

_BADGE_REAP = "REAP"
_BADGE_ATTENTION = "ATTN"
_BADGE_EVENT_TYPES: frozenset[OrchestratorEventType] = frozenset(
    {
        OrchestratorEventType.SESSION_REAP_PROPOSED,
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
    }
)


def _pr_attention_label(attention_state: str) -> str:
    """Map a pr_hydrate attention_state literal to its display label,
    falling back to the raw string for forward-compat with unknown values."""
    return _PR_ATTENTION_LABELS.get(attention_state, attention_state)


@dataclass
class BoardState:
    """Snapshot of all state needed to render one board frame."""

    cw_state: CwState
    dev_queue: DevQueueStore
    clients: dict[str, ClientConfig]
    config: OrchestratorConfig
    now: datetime
    events: list[OrchestratorEvent] = field(default_factory=list)
    # Detail-panel inputs (populated only when board is run with --detail);
    # default-empty so non-detail frames and existing builders stay valid.
    running_sessions: list[SessionSummary] = field(default_factory=list)
    pending_tickets: list[TicketSummary] = field(default_factory=list)


@dataclass(frozen=True)
class _FeedEntry:
    """One display row in the (possibly aggregated) event-feed panel."""

    text: str
    created_at: datetime


def _load_board_state(
    *, client_filter: str | None = None, detail: bool = False
) -> BoardState:
    """Read all state needed for one board frame.

    Does NOT call reconcile().
    # Why: board is read-only observer; calling reconcile() here would mutate
    # dev-queue state, violating lock-free observer contract.

    When ``detail`` is set, derive the session/ticket summaries the detail
    panel needs by reusing the already-loaded state through the orchestrate
    summarizers — no second read, no reconcile.
    """
    now = datetime.now(UTC)
    client_names = frozenset({client_filter}) if client_filter is not None else None
    cw_state = load_state()
    dev_queue = load_dev_queue()
    running_sessions: list[SessionSummary] = []
    pending_tickets: list[TicketSummary] = []
    if detail:
        running_sessions = [
            summarise_session(s, now=now)
            for s in cw_state.sessions
            if s.status in (SessionStatus.ACTIVE, SessionStatus.IDLE)
            and (client_filter is None or s.client == client_filter)
        ]
        pending_tickets = [
            summarise_ticket(t)
            for t in dev_queue.tasks
            if t.status == QueueItemStatus.PENDING
            and (client_filter is None or t.client == client_filter)
        ]
    return BoardState(
        cw_state=cw_state,
        dev_queue=dev_queue,
        clients=load_effective_clients(),
        config=load_effective_config(),
        now=now,
        events=read_events(
            since_ts=now - _EVENT_FEED_WINDOW,
            client_names=client_names,
        ),
        running_sessions=running_sessions,
        pending_tickets=pending_tickets,
    )


def _format_age(now: datetime, anchor: datetime | None) -> str:
    """Render a compact age string (Xm/Xh/Xd) from anchor to now. Pure."""
    if anchor is None:
        return _DASH
    total_seconds = (now - anchor).total_seconds()
    if total_seconds < _SECONDS_PER_HOUR:
        return f"{int(total_seconds // _SECONDS_PER_MINUTE)}m"
    if total_seconds < _SECONDS_PER_DAY:
        return f"{int(total_seconds // _SECONDS_PER_HOUR)}h"
    return f"{int(total_seconds // _SECONDS_PER_DAY)}d"


def _session_started_map(cw_state: CwState) -> dict[str, datetime]:
    """Map session id -> started_at for age-column lookups."""
    return {s.id: s.started_at for s in cw_state.sessions}


def _render_pr_cell(pr_state: PrState | None) -> str:
    """Render the PR/CI cell from a persisted PrState. No recomputation of
    attention_state — it is rendered as pr_hydrate._compute_attention_state
    already derived it."""
    if pr_state is None:
        return _DASH
    parts = [_PR_CI_OK if pr_state.ci_ok else _PR_CI_FAIL]
    if pr_state.review_decision:
        parts.append(pr_state.review_decision)
    # Why: "ci_failing" restates the CI-FAIL token already added above —
    # skip it here to avoid a redundant "CI-FAIL CI-FAILING" cell.
    redundant_with_ci_status = (
        pr_state.attention_state == "ci_failing" and not pr_state.ci_ok
    )
    if pr_state.attention_state and not redundant_with_ci_status:
        parts.append(_pr_attention_label(pr_state.attention_state))
    return " ".join(parts)


def _index_badge_events(
    events: list[OrchestratorEvent],
    now: datetime,
    known_ticket_ids: set[str],
) -> dict[str, str]:
    """Bounded (window + live-ticket join) index of ticket_id -> badge label.

    reap_proposed beats needs_attention regardless of event order — see
    Badge precedence (R4): reap > needs_attention > pr_state.attention_state.
    """
    cutoff = now - _EVENT_FEED_WINDOW
    result: dict[str, str] = {}
    for event in events:
        if event.type not in _BADGE_EVENT_TYPES:
            continue
        if event.created_at < cutoff:
            continue
        ticket_id = event.payload.get("ticket_id")
        if ticket_id is None or ticket_id not in known_ticket_ids:
            continue
        if event.type == OrchestratorEventType.SESSION_REAP_PROPOSED:
            result[ticket_id] = _BADGE_REAP
        elif ticket_id not in result:
            result[ticket_id] = _BADGE_ATTENTION
    return result


def _index_client_badge_events(
    events: list[OrchestratorEvent],
    now: datetime,
    client_names: set[str],
) -> dict[str, str]:
    """Bounded (window + live-client join) index of client -> badge label.

    Sibling of :func:`_index_badge_events`, keyed on ``client`` instead of
    ``ticket_id``. Surfaces client-scoped SESSION_NEEDS_ATTENTION events
    (e.g. ``paused_status="freshness_gate_blocked"``) which carry
    ``ticket_id=None`` / ``session_id=""`` and are otherwise invisible to
    every ticket-keyed consumption path.

    Why: every ticket-scoped SESSION_NEEDS_ATTENTION/SESSION_REAP_PROPOSED
    emit site ALSO sets ``client`` in its payload (alongside a real
    ``ticket_id``) — those already surface via the per-ticket row badge
    (:func:`_index_badge_events`) and must not additionally paint the
    client header, or routine per-ticket events would falsely badge the
    whole client. Restricting to SESSION_NEEDS_ATTENTION with
    ``ticket_id is None`` is what actually makes this client-scoped rather
    than a second, redundant path for ticket-scoped signals. No precedence
    rule is needed today — only one contributing signal type exists at
    client level (freshness_gate_blocked) — so this always writes
    ``_BADGE_ATTENTION``.
    """
    cutoff = now - _EVENT_FEED_WINDOW
    result: dict[str, str] = {}
    for event in events:
        if event.type != OrchestratorEventType.SESSION_NEEDS_ATTENTION:
            continue
        if event.created_at < cutoff:
            continue
        if event.payload.get("ticket_id") is not None:
            continue
        client = event.payload.get("client")
        if client is None or client not in client_names:
            continue
        result[client] = _BADGE_ATTENTION
    return result


def _row_badge(
    ticket_id: str,
    pr_state: PrState | None,
    badge_index: dict[str, str],
) -> str:
    """First-match badge for one row: reap > needs_attention > pr_state."""
    if ticket_id in badge_index:
        return badge_index[ticket_id]
    if pr_state is not None and pr_state.attention_state:
        # Why: pr_state.attention_state is intentionally surfaced both here
        # (lowest-precedence badge fallback) and in the PR cell
        # (_render_pr_cell) — two different columns showing the same signal
        # by design (R1 PR column + R4 badge precedence), not a duplication bug.
        return _pr_attention_label(pr_state.attention_state)
    return _DASH


def _aggregate_feed(events: list[OrchestratorEvent]) -> list[_FeedEntry]:
    """Collapse consecutive dispatch.tick events into one summary entry.

    Any other event type breaks the run and is emitted verbatim. Pure —
    operates over whatever event list it is given (no truncation here; see
    _build_event_feed_panel for the aggregate-then-tail truncation order).
    """
    entries: list[_FeedEntry] = []
    run: list[OrchestratorEvent] = []

    def _flush_tick_run() -> None:
        if not run:
            return
        span_seconds = (run[-1].created_at - run[0].created_at).total_seconds()
        span_minutes = int(span_seconds // _SECONDS_PER_MINUTE)
        entries.append(
            _FeedEntry(
                text=f"dispatch.tick x{len(run)} over {span_minutes}m",
                created_at=run[-1].created_at,
            )
        )
        run.clear()

    for event in events:
        if event.type == OrchestratorEventType.DISPATCH_TICK:
            run.append(event)
            continue
        _flush_tick_run()
        ticket_id = event.payload.get("ticket_id")
        label = f"{event.type.value} ({ticket_id})" if ticket_id else event.type.value
        entries.append(_FeedEntry(text=label, created_at=event.created_at))
    _flush_tick_run()
    return entries


def _build_event_feed_panel(
    events: list[OrchestratorEvent],
    now: datetime,
    *,
    raw: bool,
) -> Panel:
    """One global bounded event-feed panel. Aggregates dispatch.tick runs by
    default; --raw-events (raw=True) restores the unaggregated stream.

    Aggregate-then-tail: the full windowed event list is
    aggregated first, then the *aggregated* entries are tailed to
    _EVENT_FEED_LIMIT — not the other way around, so a tick burst can't evict
    earlier non-tick signal before aggregation ever runs.
    """
    cutoff = now - _EVENT_FEED_WINDOW
    windowed = [e for e in events if e.created_at >= cutoff]

    if raw:
        display = windowed[-_EVENT_FEED_LIMIT:]
        lines = [
            f"{e.created_at.strftime('%H:%M:%S')}  {e.type.value}" for e in display
        ]
    else:
        aggregated = _aggregate_feed(windowed)
        tailed = aggregated[-_EVENT_FEED_LIMIT:]
        lines = [f"{fe.created_at.strftime('%H:%M:%S')}  {fe.text}" for fe in tailed]

    body = "\n".join(lines) if lines else "No recent events."
    return Panel(Text(body), title="Event Feed")


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
    now: datetime,
    started_map: dict[str, datetime],
    badge_index: dict[str, str],
    client_badge: str | None = None,
) -> Panel:
    """Build one Rich Panel for a single client/lane combination.

    ``client_badge`` (when set) is the client-level badge from
    :func:`_index_client_badge_events` — surfaces client-scoped signals
    (e.g. freshness_gate_blocked) that carry no ticket_id, so they are
    invisible to the per-row ``BADGE`` column. Rendered in the panel title,
    bracketed to visually distinguish it from the per-ticket row badge.
    """
    # Why: D1 (#624) specified one Panel per client containing a Table per lane.
    # We use one Panel per client x lane instead: flat layout is identical for
    # single-lane clients (today's norm) and simpler to render. Multi-lane
    # clients get separate panels rather than a nested grouping. Revisit if
    # per-client collapsing becomes a UX requirement.
    # Why: mirrors dispatch._lane_stats_for_client without importing the
    # private function.
    running = sum(1 for t in tasks_in_lane if t.status in OCCUPIED_LANE_STATUSES)

    pause_tag = " [PAUSED]" if paused else ""
    title = f"{client_name} / {lane_name}{pause_tag}  [{running}/{max_parallel}]"
    if client_badge:
        title += f"  [{client_badge}]"

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("TICKET", no_wrap=True)
    table.add_column("STAGE", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("MODEL", no_wrap=True)
    table.add_column("AGE", no_wrap=True)
    table.add_column("PR", no_wrap=True)
    table.add_column("BADGE", no_wrap=True)

    for task in tasks_in_lane:
        model_display = _derive_model_display(task.stage, client_cfg)
        status_label = _STATUS_LABEL.get(task.status, str(task.status))
        anchor = (
            started_map.get(task.session_id) if task.session_id is not None else None
        )
        if anchor is None:
            anchor = task.created_at
        table.add_row(
            task.ticket_id,
            task.stage.value,
            status_label,
            model_display,
            _format_age(now=now, anchor=anchor),
            _render_pr_cell(task.pr_state),
            _row_badge(
                ticket_id=task.ticket_id,
                pr_state=task.pr_state,
                badge_index=badge_index,
            ),
        )

    return Panel(table, title=title)


def _build_detail_panel(board_state: BoardState) -> Panel:
    """Session-grouped detail panel with a worktree-contention column.

    Grouped by client (most-active first); one row per running session, sorted
    by start time. Reads board_state.now for the AGE column — no datetime.now().
    # Why: $HOME is read here only to tilde-collapse worktree paths for display;
    # it is not board state or a data source, so render_board stays pure.
    """
    sessions = board_state.running_sessions
    tickets = board_state.pending_tickets
    home = str(Path.home())
    contention = worktree_contention(sessions)
    by_client = sessions_by_client(sessions)

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("CLIENT", no_wrap=True)
    table.add_column("ID", no_wrap=True)
    table.add_column("PURPOSE", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("WORKTREE", overflow="fold")
    table.add_column("AGE", no_wrap=True)
    table.add_column("CONTENTION", no_wrap=True)

    for client in group_by_client(sessions, tickets):
        for sess in sorted(by_client.get(client, []), key=lambda s: s.started_at):
            path_key = (
                str(sess.worktree_path) if sess.worktree_path is not None else None
            )
            count = contention.get(path_key, 0) if path_key is not None else 0
            marker = f"⚠x{count}" if count >= CONTENTION_THRESHOLD else _DASH
            table.add_row(
                client,
                sess.id,
                sess.purpose,
                sess.status,
                _shorten_worktree(sess.worktree_path, home),
                _format_age(now=board_state.now, anchor=sess.started_at),
                marker,
            )

    return Panel(table, title="Sessions (detail)")


def render_board(
    board_state: BoardState,
    *,
    client_filter: str | None = None,
    raw_events: bool = False,
    detail: bool = False,
) -> RenderableType:
    """Render a full board frame as a Rich renderable.

    Pure function - no I/O, no datetime.now() calls inside. When ``detail`` is
    set, a session-grouped detail panel (with worktree contention) is appended
    before the event feed.
    """
    panels: list[RenderableType] = []

    # Gather all unique client names appearing in the queue (plus known clients).
    queue_clients: set[str] = {t.client for t in board_state.dev_queue.tasks}
    known_clients: set[str] = set(board_state.clients)
    all_clients = sorted(queue_clients | known_clients)

    if client_filter is not None:
        all_clients = [c for c in all_clients if c == client_filter]

    known_ticket_ids = {
        t.ticket_id
        for t in board_state.dev_queue.tasks
        if client_filter is None or t.client == client_filter
    }
    started_map = _session_started_map(board_state.cw_state)
    # Why: self-scope events here (not just rely on _load_board_state's
    # read_events(client_names=...) filter) so render_board stays correct
    # under --client even when called directly with an un-prescoped
    # BoardState — mirrors the known_ticket_ids client-scoping above.
    scoped_events = (
        board_state.events
        if client_filter is None
        else [e for e in board_state.events if e.payload.get("client") == client_filter]
    )
    badge_index = _index_badge_events(
        events=scoped_events, now=board_state.now, known_ticket_ids=known_ticket_ids
    )
    client_badge_index = _index_client_badge_events(
        events=scoped_events, now=board_state.now, client_names=set(all_clients)
    )

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
                now=board_state.now,
                started_map=started_map,
                badge_index=badge_index,
                client_badge=client_badge_index.get(client_name),
            )
            panels.append(panel)

    feed_panel = _build_event_feed_panel(
        events=scoped_events, now=board_state.now, raw=raw_events
    )

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

    tail: list[RenderableType] = []
    if detail:
        tail.append(_build_detail_panel(board_state))
    tail.extend((feed_panel, footer_text))

    if panels:
        return Group(*panels, *tail)
    return Group(Text("No tickets in queue."), *tail)


def run_board(
    *,
    once: bool = False,
    interval: int = 5,
    client_filter: str | None = None,
    console: Console | None = None,
    ticks: int | None = None,
    loader_fn: Callable[[], BoardState] | None = None,
    raw_events: bool = False,
    detail: bool = False,
) -> None:
    """Run the board - either a Live loop or a one-shot snapshot.

    Args:
        once: Print one frame and exit (for non-TTY/CI).
        interval: Seconds between data refreshes (clamped 1-60).
        client_filter: Only render this client when set.
        console: Rich Console to use (created if None).
        ticks: Test shim - render this many frames then return.
        loader_fn: Override the state loader (defaults to _load_board_state).
        raw_events: Show the raw event stream instead of aggregated ticks.
        detail: Append the session-grouped detail panel (worktree contention).
    """
    if console is None:
        console = Console()
    if loader_fn is None:
        # Zero-arg closure over client_filter — loader_fn's public type
        # (Callable[[], BoardState]) and every existing test override stay
        # unchanged; only this default path threads client-scoping in.
        def _default_loader() -> BoardState:
            return _load_board_state(client_filter=client_filter, detail=detail)

        loader_fn = _default_loader

    interval = max(_MIN_INTERVAL_SECONDS, min(_MAX_INTERVAL_SECONDS, interval))

    def _build() -> RenderableType:
        return render_board(
            loader_fn(),
            client_filter=client_filter,
            raw_events=raw_events,
            detail=detail,
        )

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
