"""Local task-queue (``queue``) and orchestrator event-bus (``event``) commands."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import click

from cw import queue_peek as _queue_peek
from cw.cli._base import _complete_client, handle_errors, main
from cw.config import load_clients
from cw.events import (
    advance_cursor,
    init_cursor_at_end,
    load_cursor,
    prune_events,
    read_events,
    record_event,
    tail_events_follow,
    wait_for_event,
)
from cw.exceptions import CwError
from cw.models import (
    WORKER_PURPOSES,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItem,
    QueueItemStatus,
    SessionPurpose,
    TaskSpec,
)
from cw.queue import (
    add_item,
    claim_by_id,
    claim_next,
    clear_queue,
    complete_item,
    fail_item,
    load_queue,
    peek_next,
    remove_item,
)

# --- Queue command group ---


@main.group()
def queue() -> None:
    """Manage the task queue."""


@queue.command(name="add")
@click.argument("client", shell_complete=_complete_client)
@click.argument("description")
@click.option(
    "--purpose",
    type=click.Choice([p.value for p in WORKER_PURPOSES]),
    default="debt",
    help="Queue purpose.",
)
@click.option("--prompt", default=None, help="Exact prompt for Claude.")
@click.option("--priority", type=int, default=0, help="Priority (higher = sooner).")
@handle_errors
def queue_add(
    client: str,
    description: str,
    purpose: str,
    prompt: str | None,
    priority: int,
) -> None:
    """Add a work item to the queue."""
    task = TaskSpec(
        description=description,
        purpose=SessionPurpose(purpose),
        prompt=prompt or description,
        priority=priority,
    )
    item = add_item(client, task)
    click.echo(f"Added queue item: {item.id} ({description})")


def _filter_queue_items(
    items: list[QueueItem],
    purpose: str | None,
    status_filter: str | None,
) -> list[QueueItem]:
    if purpose:
        items = [i for i in items if i.task.purpose == purpose]
    if status_filter:
        items = [i for i in items if i.status == status_filter]
    return items


def _print_queue_table(items: list[QueueItem]) -> None:
    click.echo(f"{'ID':<10} {'STATUS':<12} {'PURPOSE':<10} {'DESCRIPTION'}")
    click.echo("-" * 60)
    for item in items:
        desc = item.task.description[:40]
        click.echo(f"{item.id:<10} {item.status:<12} {item.task.purpose:<10} {desc}")


@queue.command(name="list")
@click.argument("client", required=False, default=None, shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([p.value for p in WORKER_PURPOSES]),
    default=None,
    help="Filter by purpose.",
)
@click.option(
    "--status",
    "status_filter",
    type=click.Choice([e.value for e in QueueItemStatus]),
    default=None,
    help="Filter by status.",
)
@handle_errors
def queue_list(
    client: str | None,
    purpose: str | None,
    status_filter: str | None,
) -> None:
    """Show queue items for a client, or all clients if CLIENT is omitted."""
    if client is not None:
        items = _filter_queue_items(load_queue(client).items, purpose, status_filter)
        if not items:
            click.echo("Queue is empty.")
            return
        _print_queue_table(items)
    else:
        clients = load_clients()
        has_output = False
        for name in clients:
            items = _filter_queue_items(load_queue(name).items, purpose, status_filter)
            if not items:
                continue
            if has_output:
                click.echo()  # blank line between client sections, not after last
            click.echo(f"--- {name} ---")
            _print_queue_table(items)
            has_output = True
        if not has_output:
            click.echo("Queue is empty.")


@queue.command(name="remove")
@click.argument("client", shell_complete=_complete_client)
@click.argument("item_id")
@handle_errors
def queue_remove(client: str, item_id: str) -> None:
    """Remove an item from the queue."""
    remove_item(client, item_id)
    click.echo(f"Removed queue item: {item_id}")


@queue.command(name="clear")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([p.value for p in WORKER_PURPOSES]),
    default=None,
    help="Clear only items with this purpose.",
)
@click.option("--completed", is_flag=True, help="Clear only completed items.")
@handle_errors
def queue_clear(client: str, purpose: str | None, completed: bool) -> None:
    """Clear items from the queue."""
    purpose_enum = SessionPurpose(purpose) if purpose else None
    status_enum = QueueItemStatus.COMPLETED if completed else None
    removed = clear_queue(client, purpose=purpose_enum, status=status_enum)
    click.echo(f"Cleared {removed} item(s).")


@queue.command(name="next")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([p.value for p in WORKER_PURPOSES]),
    default=None,
    help="Filter by purpose.",
)
@click.option("--json", "as_json", is_flag=True, help="Output full QueueItem JSON.")
@handle_errors
def queue_next(client: str, purpose: str | None, as_json: bool) -> None:
    """Peek at the next pending item without claiming it."""
    purpose_enum = SessionPurpose(purpose) if purpose else None
    item = peek_next(client, purpose=purpose_enum)
    if item is None:
        click.echo("No pending items.")
        return
    if as_json:
        click.echo(item.model_dump_json(indent=2))
    else:
        click.echo(
            f"{item.id}  priority={item.task.priority}"
            f"  purpose={item.task.purpose}  {item.task.description}"
        )


@queue.command(name="peek")
@click.option("--client", "-c", default=None, help="Filter to one client.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@handle_errors
def queue_peek(client: str | None, as_json: bool) -> None:
    """In-flight inspection of RUNNING dev-queue sessions.

    For each RUNNING task, reports age, idle gap, last sentinel status, PR
    state, and a WAIT / PEEK / STOP recommendation from the peek-stop ladder.
    Reports only — never stops sessions itself.

    To stop a session after reviewing:
      cw spawn close <session_id>
    """
    now = datetime.now(UTC)
    rows = _queue_peek.build_peek_rows(client, now)
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
    else:
        _queue_peek.print_table(rows)


@queue.command(name="claim")
@click.argument("client", shell_complete=_complete_client)
@click.option(
    "--purpose",
    type=click.Choice([p.value for p in WORKER_PURPOSES]),
    default=None,
    help="Filter by purpose.",
)
@click.option("--id", "item_id", default=None, help="Claim a specific item by ID.")
@click.option("--json", "as_json", is_flag=True, help="Output full QueueItem JSON.")
@handle_errors
def queue_claim(
    client: str,
    purpose: str | None,
    item_id: str | None,
    as_json: bool,
) -> None:
    """Claim the next pending item (marks it RUNNING)."""
    item: QueueItem | None
    if item_id:
        item = claim_by_id(client, item_id)
    else:
        purpose_enum = SessionPurpose(purpose) if purpose else None
        item = claim_next(client, purpose=purpose_enum)
    if item is None:
        click.echo("No pending items to claim.")
        return
    if as_json:
        click.echo(item.model_dump_json(indent=2))
    else:
        click.echo(f"Claimed: {item.id} ({item.task.description})")


@queue.command(name="complete")
@click.argument("client", shell_complete=_complete_client)
@click.argument("item_id")
@click.option("--result", default="", help="Result summary text.")
@handle_errors
def queue_complete(client: str, item_id: str, result: str) -> None:
    """Mark a queue item as completed."""
    complete_item(client, item_id, result)
    click.echo(f"Completed: {item_id}")


@queue.command(name="fail")
@click.argument("client", shell_complete=_complete_client)
@click.argument("item_id")
@click.option("--error", "error_text", default="", help="Error description.")
@handle_errors
def queue_fail(client: str, item_id: str, error_text: str) -> None:
    """Mark a queue item as failed."""
    fail_item(client, item_id, error_text)
    click.echo(f"Failed: {item_id}")


# --- Event bus command group ---


@main.group()
def event() -> None:
    """Manage the orchestrator event bus."""


_VALID_EVENT_TYPES = {e.value for e in OrchestratorEventType}


@event.command(name="record")
@click.argument("event_type")
@click.option("--payload", default="{}", help="JSON payload string.")
@click.option("--correlation-id", default=None, help="Correlation ID to link events.")
@handle_errors
def event_record(
    event_type: str,
    payload: str,
    correlation_id: str | None,
) -> None:
    """Record an event to the inbox.

    EVENT_TYPE must be one of: ticket.enqueued, session.spawned,
    session.completed, session.timed_out, stage.entered, stage.errored,
    pr.registered, pr.ci_failed, pr.review_received, pr.mergeable,
    pr.merged.
    """
    if event_type not in _VALID_EVENT_TYPES:
        valid = ", ".join(sorted(_VALID_EVENT_TYPES))
        msg = f"Unknown event type '{event_type}'. Valid types: {valid}"
        raise CwError(msg)

    try:
        payload_dict = json.loads(payload)
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON payload: {exc}"
        raise CwError(msg) from exc

    if not isinstance(payload_dict, dict):
        msg = "Payload must be a JSON object (dict), not a scalar or list."
        raise CwError(msg)

    etype = OrchestratorEventType(event_type)
    recorded = record_event(
        etype,
        payload_dict,
        correlation_id=correlation_id,
    )
    click.echo(f"Recorded event: {recorded.id} ({recorded.type})")


def _parse_since(since: str) -> tuple[str | None, datetime | None]:
    """Parse the --since value into (consumer, since_ts).

    Consumer names are simple alphanumeric+underscore identifiers.  Everything
    else is treated as an ISO 8601 timestamp.  Raises CwError on parse failure.
    """
    if since.replace("_", "").isalnum():
        return since, None
    try:
        since_ts = datetime.fromisoformat(since)
    except ValueError as exc:
        msg = f"Cannot parse --since value '{since}' as consumer name or ISO timestamp."
        raise CwError(msg) from exc
    else:
        if since_ts.tzinfo is None:
            since_ts = since_ts.replace(tzinfo=UTC)
        return None, since_ts


def _resolve_event_types(
    type_filter: tuple[str, ...],
) -> list[OrchestratorEventType] | None:
    """Validate and convert type_filter strings to OrchestratorEventType list.

    Accepts both repeated flags (``--type a --type b``) and comma-separated
    values (``--type a,b``) so the ticket's documented UX examples work verbatim.
    """
    if not type_filter:
        return None
    expanded = tuple(t for raw in type_filter for t in raw.split(",") if t)
    invalid = [t for t in expanded if t not in _VALID_EVENT_TYPES]
    if invalid:
        valid = ", ".join(sorted(_VALID_EVENT_TYPES))
        msg = f"Unknown event type(s): {', '.join(invalid)}. Valid: {valid}"
        raise CwError(msg)
    return [OrchestratorEventType(t) for t in expanded]


def _print_event(ev: OrchestratorEvent, *, as_json: bool) -> None:
    """Print a single event to stdout and flush immediately (line-buffered output)."""
    if as_json:
        click.echo(ev.model_dump_json())
    else:
        ts = ev.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        corr = f" corr={ev.correlation_id}" if ev.correlation_id else ""
        click.echo(f"{ts}  {ev.id}  {ev.type}{corr}  {ev.payload}")
    sys.stdout.flush()


_TERMINAL_EVENT_TYPES: frozenset[OrchestratorEventType] = frozenset(
    {
        OrchestratorEventType.SESSION_TIMED_OUT,
        OrchestratorEventType.SESSION_REAP_PROPOSED,
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        OrchestratorEventType.SESSION_STAGE_TIMED_OUT_RETRIED,
    }
)


def _terminal_dedup_key(
    ev: OrchestratorEvent,
) -> tuple[str, str | None, str | None] | None:
    """Return dedup key for terminal events, or None if not a terminal event."""
    if ev.type not in _TERMINAL_EVENT_TYPES:
        return None
    return (
        str(ev.type),
        ev.payload.get("session_id"),
        ev.payload.get("paused_status"),
    )


def _dedup_terminal(events: list[OrchestratorEvent]) -> list[OrchestratorEvent]:
    """Filter repeated terminal events with same (type, session, paused_status) key."""
    seen: set[tuple[str, str | None, str | None]] = set()
    result: list[OrchestratorEvent] = []
    for ev in events:
        key = _terminal_dedup_key(ev)
        if key is None:
            result.append(ev)
        elif key not in seen:
            seen.add(key)
            result.append(ev)
    return result


def _follow_loop(
    since_cursor: str | None,
    since_ts: datetime | None,
    etype_filter: list[OrchestratorEventType] | None,
    *,
    as_json: bool,
    client_names: frozenset[str] | None = None,
    dedup_terminal: bool = False,
) -> None:
    """Stream events from the inbox until SIGINT or broken pipe."""
    seen_terminal: set[tuple[str, str | None, str | None]] = set()
    try:
        for ev in tail_events_follow(
            since_cursor=since_cursor,
            since_ts=since_ts,
            event_types=etype_filter,
            client_names=client_names,
        ):
            if dedup_terminal:
                key = _terminal_dedup_key(ev)
                if key is not None:
                    if key in seen_terminal:
                        continue
                    seen_terminal.add(key)
            _print_event(ev, as_json=as_json)
    except KeyboardInterrupt:
        raise click.exceptions.Exit(130) from None
    except BrokenPipeError:
        raise click.exceptions.Exit(0) from None


@event.command(name="tail")
@click.option(
    "--since",
    default=None,
    help="Consumer name (cursor) or ISO timestamp (e.g. 2025-01-01T00:00:00Z).",
)
@click.option(
    "--type",
    "type_filter",
    multiple=True,
    help="Filter by event type (repeatable).",
)
@click.option(
    "--client",
    "client_filter",
    multiple=True,
    help="Filter by payload.client field (repeatable).",
)
@click.option("--json", "as_json", is_flag=True, help="Output full event JSON.")
@click.option("--follow", "-f", is_flag=True, help="Stream new events as they arrive.")
@click.option(
    "--dedup-terminal",
    is_flag=True,
    help=(
        "Collapse repeated terminal re-fires (timed_out, reap_proposed, "
        "needs_attention, stage_timed_out_retried) for the same session to "
        "a single emission."
    ),
)
@handle_errors
def event_tail(
    since: str | None,
    type_filter: tuple[str, ...],
    client_filter: tuple[str, ...],
    as_json: bool,
    follow: bool,
    dedup_terminal: bool,
) -> None:
    """Read events from the inbox.

    --since may be a consumer name (alphanumeric, e.g. 'daemon') whose
    persisted cursor determines the starting position, or an ISO 8601
    timestamp to filter by creation time.

    When a consumer name is given, the cursor advances automatically
    after reading (one-shot mode only; --follow never advances the cursor).
    """
    consumer: str | None = None
    since_ts: datetime | None = None
    if since is not None:
        consumer, since_ts = _parse_since(since)

    etype_filter = _resolve_event_types(type_filter)
    client_names: frozenset[str] | None = (
        frozenset(c for raw in client_filter for c in raw.split(",") if c) or None
        if client_filter
        else None
    )

    if follow:
        since_cursor: str | None = None
        if consumer is not None:
            since_cursor = load_cursor(consumer)
            if since_cursor is None:
                click.echo(
                    f"Warning: consumer cursor '{consumer}' not found;"
                    " replaying from start",
                    err=True,
                )
        _follow_loop(
            since_cursor,
            since_ts,
            etype_filter,
            as_json=as_json,
            client_names=client_names,
            dedup_terminal=dedup_terminal,
        )
        return

    # One-shot mode: initialize fresh cursor to "now" so first-use consumers
    # don't replay history.
    if consumer is not None:
        init_cursor_at_end(consumer)

    events = read_events(
        consumer=consumer,
        since_ts=since_ts,
        event_types=etype_filter,
        client_names=client_names,
    )

    if dedup_terminal:
        events = _dedup_terminal(events)

    if not events:
        if as_json:
            click.echo("[]")
        else:
            click.echo("No events.")
        return

    for ev in events:
        _print_event(ev, as_json=as_json)

    # Advance consumer cursor to last event seen
    if consumer is not None and events:
        advance_cursor(consumer, events[-1].id)
        click.echo(f"Cursor advanced to: {events[-1].id}", err=True)


@event.command(name="wait")
@click.option("--ticket", default=None, help="Filter by correlation ID (ticket ID).")
@click.option(
    "--session",
    "session_id",
    default=None,
    help="Filter by payload session_id.",
)
@click.option(
    "--client",
    "client",
    default=None,
    help="Filter by payload client field.",
)
@click.option(
    "--type",
    "type_filter",
    multiple=True,
    help="Filter by event type (repeatable; comma-separated values also accepted).",
)
@click.option(
    "--timeout",
    default=3600.0,
    show_default=True,
    type=float,
    help="Max seconds to wait before giving up.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help="Stream all matches without exiting on first.",
)
@handle_errors
def event_wait(
    ticket: str | None,
    session_id: str | None,
    client: str | None,
    type_filter: tuple[str, ...],
    timeout: float,
    follow: bool,
) -> None:
    """Block until a matching event arrives in the inbox.

    Reads from the beginning of the inbox so events recorded before the
    command started are also matched.  Outputs one JSON line per match.
    Exits 0 on match (or --follow exhaustion); exits non-zero on timeout.
    """
    etype_filter = _resolve_event_types(type_filter)
    try:
        for ev in wait_for_event(
            event_types=etype_filter,
            correlation_id=ticket,
            session_id=session_id,
            client=client,
            timeout=timeout,
            follow=follow,
        ):
            _print_event(ev, as_json=True)
    except TimeoutError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        raise click.exceptions.Exit(130) from None
    except BrokenPipeError:
        raise click.exceptions.Exit(0) from None


def _parse_before(before: str) -> datetime:
    """Parse the --before value as an ISO 8601 timestamp. Raises CwError on failure."""
    try:
        before_ts = datetime.fromisoformat(before)
    except ValueError as exc:
        msg = f"Cannot parse --before value '{before}' as ISO timestamp."
        raise CwError(msg) from exc
    if before_ts.tzinfo is None:
        before_ts = before_ts.replace(tzinfo=UTC)
    return before_ts


@event.command(name="prune")
@click.option(
    "--before",
    default=None,
    help="ISO 8601 timestamp; prune events created before this.",
)
@click.option(
    "--keep",
    type=int,
    default=None,
    help="Keep only the newest N events; prune the rest.",
)
@click.option(
    "--delete",
    "delete_flag",
    is_flag=True,
    help="Discard pruned events instead of archiving them.",
)
@click.option("--json", "as_json", is_flag=True, help="Output the PruneResult as JSON.")
@handle_errors
def event_prune(
    before: str | None,
    keep: int | None,
    delete_flag: bool,
    as_json: bool,
) -> None:
    """Prune the event inbox by age or count.

    Exactly one of --before or --keep is required. By default, pruned events
    are archived to events/inbox.<date>.jsonl before being dropped from the
    inbox; pass --delete to discard them instead.
    """
    if (before is None) == (keep is None):
        msg = "Exactly one of --before or --keep is required."
        raise CwError(msg)
    if keep is not None and keep < 0:
        msg = "--keep must be non-negative."
        raise CwError(msg)

    before_ts = _parse_before(before) if before is not None else None
    result = prune_events(before=before_ts, keep=keep, archive=not delete_flag)

    if as_json:
        click.echo(result.model_dump_json())
    else:
        detail = f" (archive: {result.archive_path})" if result.archive_path else ""
        click.echo(
            f"Archived {result.archived_count}, deleted {result.deleted_count},"
            f" kept {result.kept_count}{detail}"
        )
