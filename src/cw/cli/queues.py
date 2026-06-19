"""Local task-queue (``queue``) and orchestrator event-bus (``event``) commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import click

from cw.cli._base import _complete_client, handle_errors, main
from cw.config import load_clients
from cw.events import advance_cursor, init_cursor_at_end, read_events, record_event
from cw.exceptions import CwError
from cw.models import (
    WORKER_PURPOSES,
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
    """Validate and convert type_filter strings to OrchestratorEventType list."""
    if not type_filter:
        return None
    invalid = [t for t in type_filter if t not in _VALID_EVENT_TYPES]
    if invalid:
        valid = ", ".join(sorted(_VALID_EVENT_TYPES))
        msg = f"Unknown event type(s): {', '.join(invalid)}. Valid: {valid}"
        raise CwError(msg)
    return [OrchestratorEventType(t) for t in type_filter]


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
@click.option("--json", "as_json", is_flag=True, help="Output full event JSON.")
@handle_errors
def event_tail(
    since: str | None,
    type_filter: tuple[str, ...],
    as_json: bool,
) -> None:
    """Read events from the inbox.

    --since may be a consumer name (alphanumeric, e.g. 'daemon') whose
    persisted cursor determines the starting position, or an ISO 8601
    timestamp to filter by creation time.

    When a consumer name is given, the cursor advances automatically
    after reading.
    """
    consumer: str | None = None
    since_ts: datetime | None = None
    if since is not None:
        consumer, since_ts = _parse_since(since)

    etype_filter = _resolve_event_types(type_filter)

    # Initialize fresh cursor to "now" so first-use consumers don't replay history.
    if consumer is not None:
        init_cursor_at_end(consumer)

    events = read_events(
        consumer=consumer,
        since_ts=since_ts,
        event_types=etype_filter,
    )

    if not events:
        if as_json:
            click.echo("[]")
        else:
            click.echo("No events.")
        return

    for ev in events:
        if as_json:
            click.echo(ev.model_dump_json())
        else:
            ts = ev.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            corr = f" corr={ev.correlation_id}" if ev.correlation_id else ""
            click.echo(f"{ts}  {ev.id}  {ev.type}{corr}  {ev.payload}")

    # Advance consumer cursor to last event seen
    if consumer is not None and events:
        advance_cursor(consumer, events[-1].id)
        click.echo(f"Cursor advanced to: {events[-1].id}", err=True)
