"""Dev-queue status rendering: aggregate table, lane breakdown, per-tick summary.

#2696 added the "Paused lanes:" block (each paused lane's cause, recent spawn
errors, auto-resume time and ``cw lane resume`` hint, shown with or without
tick data or tasks), the per-client "Effective config" line that compares the
last tick's cap with the configured ceiling, and the matching ``paused_lanes``
and ``effective`` keys in ``--json``.
"""

from __future__ import annotations

import functools
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import click

from cw._lock_hold import format_longest, read_window, summarize
from cw.cli._base import _emit_freshness_subline, handle_errors
from cw.cli.config_cmds import _client_ceiling
from cw.config import (
    _load_concurrency_overrides,
    get_client,
    load_effective_clients,
    load_effective_config,
    load_state,
    state_dir,
)
from cw.dev_queue import list_tickets
from cw.dispatch import TICK_STALE_SECONDS
from cw.dispatch_state import ExecutorBlockedMarker, load_executor_blocked_markers
from cw.exceptions import CwError
from cw.lane_pause import (
    paused_lane_views,
    render_lane_pause_lines,
    worktree_breaker_threshold,
)
from cw.models import (
    DEFAULT_LANE,
    OCCUPIED_LANE_STATUSES,
    DispatchSkipReason,
    OrchestratorConfig,
    QueueItemStatus,
    TicketTask,
)
from cw.orchestrate import latest_tick_summary_by_client
from cw.reconcile.routed_result_sessions import (
    format_orphan_sessions,
    unowned_ceiling_sessions,
)

from ._group import _ACTIVE_STATUSES, _PAUSED_LANE_MARKER, dev_queue
from .tasks import _count_needs_attn, _needs_attn_by_client

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.lane_pause import LanePauseView
    from cw.models import ClientConfig, ConcurrencyOverrides, Session
    from cw.orchestrate import TickSummary

_RESUMED_SINCE_TICK = " [resumed since this tick]"


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
        override = overrides.lanes.get(f"{lane_tasks[0].client}/{lane_name}")
        # A paused lane is noteworthy even with zero occupants -- without
        # this, a single-default-lane client with a circuit-paused lane
        # rendered as if nothing were wrong (#1630's literal bug report), and
        # #2696 dropped the "with pending work" condition so a paused lane
        # never hides.
        paused = override is not None and bool(override.paused)
        noteworthy = (blocked + signoff) > 0 or at_cap or paused
        if single_default_lane and not noteworthy:
            continue
        marker = _PAUSED_LANE_MARKER if paused else ""
        click.echo(
            f"    lane {lane_name}:"
            f" pending={pending} running={running} blocked={blocked}"
            f" signoff={signoff}{marker}"
        )
        if noteworthy and occupants:
            occupant_str = ", ".join(
                f"{t.ticket_id} ({t.status.value})" for t in occupants
            )
            # "lane full" only when actually at/over cap -- a lane with spare
            # capacity that merely has a blocked/signoff occupant is not full,
            # and mislabeling it that way reintroduces the same misdiagnosis
            # risk (#1243) on a lane that could still claim more work.
            label = "lane full" if at_cap else "lane occupants"
            click.echo(f"      {label}: {occupant_str}")


def _stale_tick_annotation(
    client_name: str,
    *,
    age: int,
    markers: dict[str, ExecutorBlockedMarker],
    now: datetime,
) -> str:
    """Render the suffix for a client whose last tick has gone stale (#1742).

    Tick age alone cannot tell a dead dispatch loop from a live one blocked
    inside an executor's review — and the operator's correct response is
    opposite between the two (restart vs. leave it alone). A live marker for
    *client_name* resolves the ambiguity, so it wins over ``[STALE]``.

    *elapsed* is measured from the marker's own ``started_at``, never from the
    tick's *age*: those are two different clocks (how long since the loop last
    reported vs. how long this review has been running).
    """
    blocked = sorted(
        (m for m in markers.values() if m.client == client_name),
        key=lambda m: m.started_at,
    )
    if not blocked:
        return f" [STALE — no tick in {age}s]"
    oldest = blocked[0]
    elapsed = int((now - oldest.started_at).total_seconds())
    more = f" (+{len(blocked) - 1} more)" if len(blocked) > 1 else ""
    return (
        f" [BLOCKED — {oldest.executor} review,"
        f" ticket {oldest.ticket_id}, {elapsed}s{more}]"
    )


def _unowned_ceiling_sessions(
    client_name: str, tasks: Sequence[TicketTask]
) -> tuple[Session, ...]:
    """Sessions counted against *client_name*'s ceiling that no occupied row owns.

    Read-only (#2524): one plain ``load_state()`` read, filtered by the shared
    pure helper, oldest first. An unreadable state file yields ``()``, so the
    annotation falls back to its generic text instead of failing status.
    """
    try:
        state = load_state()
    except (OSError, ValueError, CwError):
        return ()
    return unowned_ceiling_sessions(state.sessions, tasks, client_name)


def _running_row_divergence_annotation(
    tick_running: int,
    task_running: int,
    unowned_sessions: Sequence[Session] = (),
) -> str:
    """Render the suffix for a tick counting more sessions than RUNNING rows (#2142).

    The two numbers on this line come from two independently-computed metrics.
    ``tick.running`` is session-based by deliberate design (``dispatch/lanes.py``:
    a pre-existing DAEMON session is real host load whether or not the queue
    tracks it) and is what ``cap_full`` gates on; the table above counts task
    rows. So an excess is not a contradiction the admission math should
    resolve — but it IS the exact shape an orphaned fix-agent session produces,
    and an operator reading ``running=2/2 cap_full`` beside a single RUNNING row
    otherwise has no way to tell that from ordinary snapshot lag.

    Reporting only: this annotates the divergence, it never changes what is
    admitted. Phrased as a question because snapshot lag can produce it too.

    *unowned_sessions* (#2524) names the ceiling-counted sessions no occupied
    row owns -- e.g. one stranded after its result was already routed -- as
    ``<id> (<ticket>)``, capped at ``ORPHAN_SESSION_ID_CAP``. Empty keeps the
    original generic text.
    """
    counts = (
        f" [ORPHAN? — {tick_running} session(s) counted against the ceiling"
        f" vs {task_running} RUNNING row(s)"
    )
    if not unowned_sessions:
        return f"{counts}; run cw doctor]"
    named = format_orphan_sessions(unowned_sessions)
    return f"{counts}; no occupied row owns: {named}; run cw doctor]"


def _echo_client_rows(
    clients_seen: list[str],
    by_client: dict[str, list[TicketTask]],
    *,
    show_all: bool,
) -> dict[str, int]:
    """Print one per-client summary row and return each client's RUNNING count.

    The returned counts are what ``_running_row_divergence_annotation`` compares
    the dispatch tick's session-based ``running`` against, so the table and the
    annotation can never disagree about how many RUNNING rows a client has.
    """
    running_row_counts: dict[str, int] = {}
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
        running_row_counts[client_name] = len(running_tasks)
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
    return running_row_counts


def _effective_clients_or_empty() -> dict[str, ClientConfig]:
    """The effective clients, or none when ``clients.yaml`` cannot be loaded.

    Same tolerance as :func:`_lane_caps_for_client`: an invalid file must not
    stop status from naming the override-held pauses it can still see.
    """
    try:
        return load_effective_clients()
    except CwError:
        return {}


def _status_paused_views(
    client: str | None,
    overrides: ConcurrencyOverrides,
    config: Callable[[], OrchestratorConfig],
) -> list[LanePauseView]:
    """Every paused lane (filtered to *client* when given), sorted (#2696).

    *config* is called only when a lane is paused, for the auto-resume knob,
    so a run with nothing paused never needs the orchestrator config.
    """
    clients = _effective_clients_or_empty()

    def _views(*, auto_resume_enabled: bool) -> list[LanePauseView]:
        return [
            view
            for view in paused_lane_views(
                clients, overrides, auto_resume_enabled=auto_resume_enabled
            )
            if client is None or view.client == client
        ]

    views = _views(auto_resume_enabled=True)
    if views and config().lane_circuit_auto_resume_minutes <= 0:
        views = _views(auto_resume_enabled=False)
    return views


def _echo_paused_lanes(views: list[LanePauseView], *, now: datetime) -> None:
    """Print the "Paused lanes:" block, one cause block per lane (#2696)."""
    if not views:
        return
    click.echo("")
    click.echo("Paused lanes:")
    for view in views:
        first, *rest = render_lane_pause_lines(view, now=now)
        click.echo(f"  {view.client}/{view.lane}: {first}")
        for line in rest:
            click.echo(f"  {line}")


@dataclass(frozen=True)
class _EffectiveConfigView:
    """One client's configuration as this command loaded it (#2696).

    ``asdict`` is the ``effective`` object of ``status --json``.
    """

    ceiling: int
    ceiling_source: str
    lane_caps: dict[str, int]
    circuit_breaker_threshold: int
    worktree_breaker_threshold: int
    auto_resume_minutes: int
    auto_resume_max_minutes: int
    last_tick_cap: int | None


def _effective_config_view(
    client_name: str,
    *,
    effective: OrchestratorConfig,
    overrides: ConcurrencyOverrides,
    tick: TickSummary | None,
) -> _EffectiveConfigView:
    ceiling = _client_ceiling(client_name, overrides, effective)
    threshold = effective.lane_circuit_breaker_threshold
    return _EffectiveConfigView(
        ceiling=ceiling["ceiling"],
        ceiling_source=ceiling["source"],
        lane_caps=_lane_caps_for_client(client_name, effective),
        circuit_breaker_threshold=threshold,
        worktree_breaker_threshold=worktree_breaker_threshold(threshold),
        auto_resume_minutes=effective.lane_circuit_auto_resume_minutes,
        auto_resume_max_minutes=effective.lane_circuit_auto_resume_max_minutes,
        last_tick_cap=tick.cap if tick is not None else None,
    )


def _effective_config_line(client_name: str, view: _EffectiveConfigView) -> str:
    """The ``effective:`` text line, flagging a tick that used another cap."""
    caps = ",".join(f"{name}:{cap}" for name, cap in view.lane_caps.items())
    disabled = " (disabled)" if view.auto_resume_minutes == 0 else ""
    line = (
        f"  {client_name}: effective: ceiling={view.ceiling} ({view.ceiling_source})"
        f" lane_caps={caps} breaker_threshold={view.circuit_breaker_threshold}"
        f" worktree_breaker_threshold={view.worktree_breaker_threshold}"
        f" auto_resume_minutes={view.auto_resume_minutes}{disabled}"
        f" auto_resume_max_minutes={view.auto_resume_max_minutes}"
    )
    if view.last_tick_cap is not None and view.last_tick_cap != view.ceiling:
        # The loop keeps its last-good config when a reload fails (only a
        # log warning), and ``serve --max-parallel`` replaces every ceiling;
        # right after an edit the mismatch can also last one tick.
        line += (
            f" [CONFIG? last tick used cap={view.last_tick_cap},"
            f" configured ceiling={view.ceiling};"
            " reload failed/pending or serve --max-parallel]"
        )
    return line


def _echo_effective_config(
    clients_seen: list[str],
    *,
    effective: OrchestratorConfig,
    overrides: ConcurrencyOverrides,
    tick_data: dict[str, TickSummary],
) -> None:
    """Print one ``effective:`` line per client in the table (#2696)."""
    click.echo("")
    click.echo(
        "Effective config (declared + runtime overrides, as loaded by this command):"
    )
    for client_name in clients_seen:
        view = _effective_config_view(
            client_name,
            effective=effective,
            overrides=overrides,
            tick=tick_data.get(client_name),
        )
        click.echo(_effective_config_line(client_name, view))


def _status_json(client: str | None) -> dict[str, dict[str, object]]:
    """The ``status --json`` payload: tick clients plus paused-lane clients.

    A client with a paused lane but no tick appears with null tick fields.
    The effective config is loaded once, and only when an entry is emitted.
    """
    tick_data = latest_tick_summary_by_client()
    attn_by_client = _needs_attn_by_client(list_tickets(client))
    overrides = _load_concurrency_overrides()
    config = functools.cache(load_effective_config)
    views = _status_paused_views(client, overrides, config)
    names = [c for c in tick_data if client is None or c == client]
    names += sorted({v.client for v in views} - set(names))
    payload: dict[str, dict[str, object]] = {}
    for name in names:
        tick = tick_data.get(name)
        payload[name] = {
            "skip_reason": tick.skip_reason if tick is not None else None,
            "freshness_detail": tick.freshness_detail if tick is not None else None,
            "blocked_branch": tick.blocked_branch if tick is not None else None,
            "needs_attn": attn_by_client.get(name, 0),
            "paused_lanes": [
                v.model_dump(mode="json") for v in views if v.client == name
            ],
            "effective": asdict(
                _effective_config_view(
                    name, effective=config(), overrides=overrides, tick=tick
                )
            ),
        }
    return payload


def _echo_tick_section(
    clients_seen: list[str],
    by_client: dict[str, list[TicketTask]],
    tasks: list[TicketTask],
    *,
    tick_data: dict[str, TickSummary],
    running_row_counts: dict[str, int],
    effective: OrchestratorConfig,
    paused_clients: set[str],
) -> None:
    """Print the "Last dispatch tick per client:" section."""
    markers = load_executor_blocked_markers()
    click.echo("")
    click.echo("Last dispatch tick per client:")
    click.echo(
        "  (snapshot from the most recent dispatch tick"
        " — not live queue state; see the table above)"
    )
    now = datetime.now(UTC)
    for client_name in clients_seen:
        if client_name not in tick_data:
            continue
        tick = tick_data[client_name]
        tick_line = (
            f"  {client_name}: claimed={tick.claimed}  pending={tick.pending}"
            f"  running={tick.running}/{tick.cap}  occupied={tick.occupied}"
            f"  skip={tick.skip_reason}"
        )
        # The tick is a snapshot and the pause list is live (#2696): a
        # lane_circuit_paused tick for a client with no paused lane left
        # predates a resume.
        if (
            tick.skip_reason == DispatchSkipReason.LANE_CIRCUIT_PAUSED
            and client_name not in paused_clients
        ):
            tick_line += _RESUMED_SINCE_TICK
        age_secs = (now - tick.tick_at).total_seconds()
        if age_secs > TICK_STALE_SECONDS:
            tick_line += _stale_tick_annotation(
                client_name, age=int(age_secs), markers=markers, now=now
            )
        # Reuses the count the table above already computed, not
        # tick.lanes: a client gated at cap_full breaks out of the lane
        # loop before any lane_stats entry is written, so tick.lanes is
        # empty in exactly the case this annotation exists for.
        task_running = running_row_counts[client_name]
        if tick.running > task_running:
            tick_line += _running_row_divergence_annotation(
                tick.running,
                task_running,
                _unowned_ceiling_sessions(client_name, tasks),
            )
        click.echo(tick_line)
        if tick.skip_reason == DispatchSkipReason.FRESHNESS_GATE:
            n_pending = sum(
                1 for t in by_client[client_name] if t.status == QueueItemStatus.PENDING
            )
            _emit_freshness_subline(
                client_name,
                tick.freshness_detail,
                tick.blocked_branch,
                n_pending,
            )
        _emit_dev_queue_lane_breakdown(by_client[client_name], client_name, effective)


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
    """Show dev queue status grouped by client.

    Paused lanes are listed with their cause and the ``cw lane resume`` hint
    even when the queue is empty or no dispatch tick has run (#2696).
    """
    if output_json:
        click.echo(json.dumps(_status_json(client)))
        return

    tasks = list_tickets(client)
    overrides = _load_concurrency_overrides()
    config = functools.cache(load_effective_config)
    views = _status_paused_views(client, overrides, config)
    now = datetime.now(UTC)

    if not tasks:
        click.echo("Dev queue is empty.")
        _echo_paused_lanes(views, now=now)
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
    running_row_counts = _echo_client_rows(clients_seen, by_client, show_all=show_all)

    tick_data = latest_tick_summary_by_client()
    _echo_paused_lanes(views, now=now)
    _echo_effective_config(
        clients_seen, effective=config(), overrides=overrides, tick_data=tick_data
    )
    if tick_data:
        _echo_tick_section(
            clients_seen,
            by_client,
            tasks,
            tick_data=tick_data,
            running_row_counts=running_row_counts,
            effective=config(),
            paused_clients={v.client for v in views},
        )

    _echo_longest_lock_holds()


def _echo_longest_lock_holds() -> None:
    """Print the last hour's longest completed lock holds, if any (#2637).

    Text mode only: ``--json`` is a dict keyed by client, so the
    machine-readable form lives in ``cw doctor --json`` (``lock-holds``). An
    absent or unreadable sidecar prints nothing.
    """
    now = datetime.now(UTC)
    summaries = summarize(read_window(state_dir(), now=now).records)
    if not summaries:
        return
    click.echo("")
    click.echo(
        f"Longest completed lock hold, last hour: {format_longest(summaries, now)}"
    )
