"""Dev-queue status rendering: aggregate table, lane breakdown, per-tick summary."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import click

from cw.cli._base import _emit_freshness_subline, handle_errors
from cw.config import (
    _load_concurrency_overrides,
    get_client,
    load_orchestrator_config,
)
from cw.dev_queue import list_tickets
from cw.dispatch import TICK_STALE_SECONDS
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_LANE,
    OCCUPIED_LANE_STATUSES,
    DispatchSkipReason,
    OrchestratorConfig,
    QueueItemStatus,
    TicketTask,
)
from cw.orchestrate import latest_tick_summary_by_client

from ._group import _ACTIVE_STATUSES, _PAUSED_LANE_MARKER, dev_queue
from .tasks import _count_needs_attn, _needs_attn_by_client


def _lane_caps_for_client(
    client_name: str, config: OrchestratorConfig
) -> dict[str, int]:
    """Resolve each lane's effective cap for *client_name* (dev-queue status rendering).

    Mirrors dispatch_tick's no-lanes override (dispatch.py's client loop): a
    client with no declared lanes is capped at the client ceiling, not
    LaneConfig's max_parallel=1 default. Falls back to a single default-lane
    entry at the ceiling when the client isn't found in clients.yaml -- dev-queue
    status must render even for a client dropped from config while tickets remain
    queued. See #1243.
    """
    ceiling = config.per_client_ceiling.get(client_name, config.default_ceiling)
    try:
        client = get_client(client_name)
    except CwError:
        return {DEFAULT_LANE: ceiling}
    if not client.lanes:
        return {DEFAULT_LANE: ceiling}
    return {lane.name: lane.max_parallel for lane in client.lanes}


def _emit_dev_queue_lane_breakdown(
    tasks: list[TicketTask], client_name: str, config: OrchestratorConfig
) -> None:
    """Print indented lane lines; occupant line(s) when a lane is noteworthy.

    A lane is noteworthy when it has non-RUNNING occupied status count > 0
    (blocked/signoff) OR is at/over its effective cap -- i.e. exactly when a
    reader could misdiagnose why nothing is claiming (#1243). For a
    single-default-lane client, the base + occupant lines are suppressed
    entirely unless the lane is noteworthy (unchanged quiet-case behaviour);
    for multi-/named-lane clients the base line still prints unconditionally per
    lane as before, with the occupant line added only when that lane is
    noteworthy.
    """
    lanes_seen: set[str] = {t.lane for t in tasks}
    single_default_lane = len(lanes_seen) <= 1 and lanes_seen <= {DEFAULT_LANE}
    lane_caps = _lane_caps_for_client(client_name, config)
    overrides = _load_concurrency_overrides()
    by_lane: dict[str, list[TicketTask]] = {}
    for task in tasks:
        by_lane.setdefault(task.lane, []).append(task)
    for lane_name in sorted(by_lane):
        lane_tasks = by_lane[lane_name]
        pending = sum(1 for t in lane_tasks if t.status == QueueItemStatus.PENDING)
        running = sum(1 for t in lane_tasks if t.status == QueueItemStatus.RUNNING)
        blocked = sum(
            1 for t in lane_tasks if t.status == QueueItemStatus.BLOCKED_ON_USER
        )
        signoff = sum(
            1
            for t in lane_tasks
            if t.status == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF
        )
        occupants = [t for t in lane_tasks if t.status in OCCUPIED_LANE_STATUSES]
        cap = lane_caps.get(lane_name, 1)
        at_cap = len(occupants) >= cap
        noteworthy = (blocked + signoff) > 0 or at_cap
        if single_default_lane and not noteworthy:
            continue
        override = overrides.lanes.get(f"{lane_tasks[0].client}/{lane_name}")
        marker = _PAUSED_LANE_MARKER if override is not None and override.paused else ""
        click.echo(
            f"    lane {lane_name}:"
            f" pending={pending} running={running} blocked={blocked}"
            f" signoff={signoff}{marker}"
        )
        if noteworthy:
            occupant_str = ", ".join(
                f"{t.ticket_id} ({t.status.value})" for t in occupants
            )
            # "lane full" only when actually at/over cap -- a lane with spare
            # capacity that merely has a blocked/signoff occupant is not full,
            # and mislabeling it that way reintroduces the same misdiagnosis
            # risk (#1243) on a lane that could still claim more work.
            label = "lane full" if at_cap else "lane occupants"
            click.echo(f"      {label}: {occupant_str}")


@dev_queue.command(name="status")
@click.option("--client", "-c", default=None, help="Filter by client.")
@click.option("--json", "output_json", is_flag=True, help="JSON dict keyed by client.")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Include completed/cancelled tickets in the TICKETS column (legacy view).",
)
@handle_errors
def dev_queue_status(client: str | None, output_json: bool, show_all: bool) -> None:
    """Show dev queue status grouped by client."""
    if output_json:
        tick_data = latest_tick_summary_by_client()
        attn_by_client = _needs_attn_by_client(list_tickets(client))
        click.echo(
            json.dumps(
                {
                    c: {
                        "skip_reason": tick.skip_reason,
                        "freshness_detail": tick.freshness_detail,
                        "blocked_branch": tick.blocked_branch,
                        "needs_attn": attn_by_client.get(c, 0),
                    }
                    for c, tick in tick_data.items()
                    if client is None or c == client
                }
            )
        )
        return

    tasks = list_tickets(client)

    if not tasks:
        click.echo("Dev queue is empty.")
        return

    # Group by client
    clients_seen: list[str] = []
    by_client: dict[str, list[TicketTask]] = {}
    for task in tasks:
        if task.client not in by_client:
            clients_seen.append(task.client)
            by_client[task.client] = []
        by_client[task.client].append(task)

    header = (
        f"{'CLIENT':<20} {'PENDING':>7}  {'RUNNING':>7}  {'BLOCKED':>7}"
        f"  {'COMPLETED':>9}  {'CANCELLED':>9}  {'NEEDS_ATTN':>10}  TICKETS"
    )
    click.echo(header)
    click.echo("-" * 90)
    for client_name in clients_seen:
        client_tasks = by_client[client_name]
        pending_tasks = [t for t in client_tasks if t.status == QueueItemStatus.PENDING]
        running_tasks = [t for t in client_tasks if t.status == QueueItemStatus.RUNNING]
        blocked_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.BLOCKED_ON_USER
        ]
        completed_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.COMPLETED
        ]
        cancelled_tasks = [
            t for t in client_tasks if t.status == QueueItemStatus.CANCELLED
        ]
        needs_attn = _count_needs_attn(client_tasks)
        display_tasks = (
            client_tasks
            if show_all
            else [t for t in client_tasks if t.status in _ACTIVE_STATUSES]
        )
        ticket_ids = ", ".join(t.ticket_id for t in display_tasks) or "—"
        click.echo(
            f"{client_name:<20} {len(pending_tasks):>7}  {len(running_tasks):>7}"
            f"  {len(blocked_tasks):>7}  {len(completed_tasks):>9}"
            f"  {len(cancelled_tasks):>9}  {needs_attn:>10}  {ticket_ids}"
        )

    tick_data = latest_tick_summary_by_client()
    if tick_data:
        config = load_orchestrator_config()
        click.echo("")
        click.echo("Last dispatch tick per client:")
        click.echo(
            "  (snapshot from the most recent dispatch tick"
            " — not live queue state; see the table above)"
        )
        now = datetime.now(UTC)
        for client_name in clients_seen:
            if client_name in tick_data:
                tick = tick_data[client_name]
                tick_line = (
                    f"  {client_name}: claimed={tick.claimed}  pending={tick.pending}"
                    f"  running={tick.running}/{tick.cap}  occupied={tick.occupied}"
                    f"  skip={tick.skip_reason}"
                )
                age_secs = (now - tick.tick_at).total_seconds()
                age = int(age_secs)
                if age_secs > TICK_STALE_SECONDS:
                    tick_line += f" [STALE — no tick in {age}s]"
                click.echo(tick_line)
                if tick.skip_reason == DispatchSkipReason.FRESHNESS_GATE:
                    n_pending = sum(
                        1
                        for t in by_client[client_name]
                        if t.status == QueueItemStatus.PENDING
                    )
                    _emit_freshness_subline(
                        client_name,
                        tick.freshness_detail,
                        tick.blocked_branch,
                        n_pending,
                    )
                _emit_dev_queue_lane_breakdown(
                    by_client[client_name], client_name, config
                )
