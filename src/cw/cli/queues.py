"""Dev-queue ``peek`` inspection and orchestrator event-bus (``event``) commands."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from cw import queue_peek as _queue_peek
from cw.cli._base import handle_errors, main, parse_iso_before
from cw.cli._hook_io import context_identity
from cw.config import load_state
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
    OrchestratorEvent,
    OrchestratorEventType,
)
from cw.session_outcomes import (
    build_report,
    format_report,
    live_ids_from_state,
    report_to_json,
)

# --- Queue command group ---


@main.group()
def queue() -> None:
    """Inspect running dev-queue sessions."""


@queue.command(name="peek")
@click.option("--client", "-c", default=None, help="Filter to one client.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@handle_errors
def queue_peek(client: str | None, as_json: bool) -> None:
    """In-flight inspection of RUNNING dev-queue sessions.

    For each RUNNING task, reports age, idle gap, last sentinel status, PR
    state, and a WAIT / PEEK / STOP recommendation from the peek-stop ladder.
    Parked rows and COMPLETED shipped PRs with a failing required check
    (RED_SHIPPED_PR, recovered with ``cw dev-queue recover-red-pr``) are listed
    too, never as stop candidates. Reports only — never stops sessions itself.

    To stop a session after reviewing:
      cw spawn close <session_id>
    """
    now = datetime.now(UTC)
    rows = _queue_peek.build_peek_rows(client, now)
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
    else:
        _queue_peek.print_table(rows)


# --- Event bus command group ---


@main.group()
def event() -> None:
    """Manage the orchestrator event bus."""


_VALID_EVENT_TYPES = {e.value for e in OrchestratorEventType}

# Stage events whose ``session_id`` is derived from cw-context.json (#2651).
# Pinned equal to ``cw.events._STAGE_EVENT_TYPES`` by a drift test.
_CONTEXT_SESSION_EVENT_TYPES = (
    OrchestratorEventType.STAGE_ENTERED,
    OrchestratorEventType.STAGE_ERRORED,
)


_NO_SESSION_WARNING = (
    "warning: stage event recorded without a session_id: no cw-context.json"
    " session_id was found from the working directory upward and the payload"
    " supplies none"
)


def _canonical_ticket(value: object) -> str:
    """Compare form of a ticket id: ``str()``, stripped, leading ``#`` dropped."""
    return str(value).strip().lstrip("#")


def _ticket_mismatch(payload_ticket: object, context_ticket: str | None) -> bool:
    """True when both ticket ids are present and differ once canonicalized (#3312)."""
    if context_ticket is None or payload_ticket is None:
        return False
    payload_form = _canonical_ticket(payload_ticket)
    return bool(payload_form) and payload_form != _canonical_ticket(context_ticket)


def _apply_context_session_id(
    etype: OrchestratorEventType, payload_dict: dict[str, Any]
) -> None:
    """Set a stage event's ``session_id`` from the nearest cw-context (#2651).

    Dispatch stamps the same id on the ticket's row, so the context is the
    authoritative source. A missing, null or empty value is filled silently;
    a different value (including a non-string) is replaced with a stderr
    warning.

    Ticket guard (#3312): when the context has a ``ticket_id`` and the payload a
    non-empty one, and the two differ after canonicalizing (``str()``,
    whitespace stripped, a leading ``#`` dropped; case-sensitive), the context
    belongs to another ticket: ``session_id`` is not derived from it, a stderr
    warning names both ids, and the payload is recorded as given.

    No context session id (#3314): the payload is recorded as given, and when it
    supplies no non-empty string ``session_id`` either, a stderr warning says the
    event carries none.
    """
    if etype not in _CONTEXT_SESSION_EVENT_TYPES:
        return
    identity = context_identity(Path.cwd())
    supplied = payload_dict.get("session_id")
    derived = identity.session_id
    if derived is None:
        if not (isinstance(supplied, str) and supplied):
            click.echo(_NO_SESSION_WARNING, err=True)
        return
    payload_ticket = payload_dict.get("ticket_id")
    if _ticket_mismatch(payload_ticket, identity.ticket_id):
        click.echo(
            f"warning: payload ticket_id '{payload_ticket}' differs from"
            f" cw-context.json ticket_id '{identity.ticket_id}'; not deriving"
            " session_id from that context",
            err=True,
        )
        return
    if supplied not in (None, "", derived):
        click.echo(
            f"warning: payload session_id '{supplied}' disagrees with"
            f" cw-context.json session_id '{derived}'; recording '{derived}'",
            err=True,
        )
    payload_dict["session_id"] = derived


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

    For stage.entered and stage.errored, the payload's ``stage`` (required)
    and ``prev_stage`` (optional) must be headless-contract §10.2 stage
    identifiers; anything else is rejected and nothing is recorded.

    For stage.entered and stage.errored, the payload's ``session_id`` is taken
    from the nearest ``.claude/cw-context.json``, searched from the working
    directory upward: a missing or empty value is filled in, and a different
    value is replaced (warning on stderr), because dispatch stamped that id on
    the ticket's row. When the payload's non-empty ``ticket_id`` differs from
    the context's (compared after ``str()``, stripping whitespace and a
    leading ``#``), the context belongs to another ticket: ``session_id`` is not
    derived and stderr gets "warning: payload ticket_id '<p>' differs from
    cw-context.json ticket_id '<c>'; not deriving session_id from that context".
    With no context, or one without a string ``session_id``, the payload is
    recorded as given; if it supplies no non-empty ``session_id`` either,
    stderr gets "warning: stage event recorded without a session_id: no
    cw-context.json session_id was found from the working directory upward and
    the payload supplies none". Other event types are never touched.
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
    _apply_context_session_id(etype, payload_dict)

    recorded = record_event(
        etype,
        payload_dict,
        correlation_id=correlation_id,
    )
    click.echo(f"Recorded event: {recorded.id} ({recorded.type})")


@event.command(name="session-outcomes")
@click.option(
    "--days",
    type=click.IntRange(min=1),
    default=14,
    show_default=True,
    help="Look back this many days.",
)
@click.option(
    "--client",
    "-c",
    "clients",
    multiple=True,
    help="Filter to one client (repeatable, or comma-separated).",
)
@click.option(
    "--samples",
    type=click.IntRange(min=0),
    default=3,
    show_default=True,
    help="Sample session ids to show per cause.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
@handle_errors
def event_session_outcomes(
    days: int,
    clients: tuple[str, ...],
    samples: int,
    as_json: bool,
) -> None:
    """Break down worker sessions that spawned but ended with no result.

    Counts every session.spawned worker in the window and buckets the ones
    with no session.result_emitted by cause (first match wins). The
    in-flight row is live sessions, not a failure. Samples are session ids
    for ``cw session show``. Lookback is bounded by inbox retention and
    ``cw event prune``.

    Read-only: writes no event, history or session data.
    """
    client_names = _parse_multi_filter(clients)
    now = datetime.now(UTC)
    try:
        state = load_state()
        report = build_report(
            days=days,
            now=now,
            client_names=client_names,
            live_session_ids=live_ids_from_state(state),
            samples=samples,
        )
    except (OSError, ValueError) as exc:
        msg = (
            "cannot read cw state, the event inbox or history"
            f" ({type(exc).__name__}): {exc}"
        )
        raise CwError(msg) from exc
    if as_json:
        click.echo(json.dumps(report_to_json(report), indent=2))
    else:
        click.echo(format_report(report))


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


def _parse_multi_filter(raw_values: tuple[str, ...]) -> frozenset[str] | None:
    """Parse repeatable, comma-separated CLI values into a frozenset, or None."""
    if not raw_values:
        return None
    return frozenset(v for raw in raw_values for v in raw.split(",") if v) or None


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


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop nested dict/list-of-dict fields; keep scalars and scalar-lists.

    Generic shape-based filter (not size-based, no per-event-type
    hardcoding). This is what makes the default (non-json) ``event tail``
    output compact: it drops e.g. ``dispatch.tick``'s ``lanes`` (dict) and
    ``lane_occupants`` (list[dict]) while keeping scalars (``client``,
    ``claimed``, ...) and scalar-lists like ``pr.ci_failed``'s
    ``failing_checks: list[str]``, regardless of list length.
    """
    return {
        key: value
        for key, value in payload.items()
        if not isinstance(value, dict)
        and not (isinstance(value, list) and any(isinstance(v, dict) for v in value))
    }


def _format_event_line(ev: OrchestratorEvent) -> str:
    """Render a single event as a compact, human-readable line."""
    ts = ev.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    corr = f" corr={ev.correlation_id}" if ev.correlation_id else ""
    payload = " ".join(f"{k}={v}" for k, v in _compact_payload(ev.payload).items())
    return f"{ts}  {ev.id}  {ev.type}{corr}  {payload}"


def _print_event(ev: OrchestratorEvent, *, as_json: bool) -> None:
    """Print a single event to stdout and flush immediately (line-buffered output)."""
    if as_json:
        click.echo(ev.model_dump_json())
    else:
        click.echo(_format_event_line(ev))
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
) -> tuple[str, str | None, str | None, str | None] | None:
    """Return dedup key for terminal events, or None if not a terminal event."""
    if ev.type not in _TERMINAL_EVENT_TYPES:
        return None
    return (
        str(ev.type),
        ev.payload.get("session_id"),
        ev.payload.get("paused_status"),
        ev.payload.get("renotify_marker"),
    )


def _dedup_terminal(events: list[OrchestratorEvent]) -> list[OrchestratorEvent]:
    """Filter repeated terminal events with same (type, session, paused_status,
    renotify_marker) key.
    """
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    result: list[OrchestratorEvent] = []
    for ev in events:
        key = _terminal_dedup_key(ev)
        if key is None:
            result.append(ev)
        elif key not in seen:
            seen.add(key)
            result.append(ev)
    return result


def _group_consecutive_runs(
    events: list[OrchestratorEvent],
) -> list[list[OrchestratorEvent]]:
    """Group consecutive events sharing (type, compact_payload) into runs."""
    runs: list[list[OrchestratorEvent]] = []
    for ev in events:
        if (
            runs
            and runs[-1][0].type == ev.type
            and (_compact_payload(runs[-1][0].payload) == _compact_payload(ev.payload))
        ):
            runs[-1].append(ev)
        else:
            runs.append([ev])
    return runs


def _format_collapsed_line(run: list[OrchestratorEvent]) -> str:
    """Render a collapsed run as one summary line: `type xN over Mm  k=v ...`."""
    span_minutes = int((run[-1].created_at - run[0].created_at).total_seconds() // 60)
    payload = " ".join(f"{k}={v}" for k, v in _compact_payload(run[0].payload).items())
    return f"{run[0].type} x{len(run)} over {span_minutes}m  {payload}"


def _print_collapsed(events: list[OrchestratorEvent]) -> None:
    """Print events grouped into collapsed runs; singleton runs print normally."""
    for run in _group_consecutive_runs(events):
        if len(run) == 1:
            _print_event(run[0], as_json=False)
        else:
            click.echo(_format_collapsed_line(run))
            sys.stdout.flush()


def _follow_loop(
    since_cursor: str | None,
    since_ts: datetime | None,
    etype_filter: list[OrchestratorEventType] | None,
    *,
    as_json: bool,
    client_names: frozenset[str] | None = None,
    lane_names: frozenset[str] | None = None,
    dedup_terminal: bool = False,
) -> None:
    """Stream events from the inbox until SIGINT or broken pipe."""
    seen_terminal: set[tuple[str, str | None, str | None, str | None]] = set()
    try:
        for ev in tail_events_follow(
            since_cursor=since_cursor,
            since_ts=since_ts,
            event_types=etype_filter,
            client_names=client_names,
            lane_names=lane_names,
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


def _emit_tail_events(
    events: list[OrchestratorEvent],
    *,
    as_json: bool,
    collapse_repeats: bool,
    consumer: str | None,
) -> None:
    """Print one-shot tail events (or the empty-path message); advance the cursor."""
    if not events:
        click.echo("[]" if as_json else "No events.")
        return

    if collapse_repeats and not as_json:
        _print_collapsed(events)
    else:
        for ev in events:
            _print_event(ev, as_json=as_json)

    if consumer is not None:
        advance_cursor(consumer, events[-1].id)
        click.echo(f"Cursor advanced to: {events[-1].id}", err=True)


def _validate_tail_limit(limit: int | None, *, follow: bool) -> None:
    """Raise CwError if --limit is non-positive or combined with --follow."""
    if limit is not None and limit < 1:
        msg = "--limit must be a positive integer."
        raise CwError(msg)
    if limit is not None and follow:
        msg = "--limit is not supported with --follow (follow streams unboundedly)."
        raise CwError(msg)


def _validate_collapse_repeats(collapse_repeats: bool, *, follow: bool) -> None:
    """Raise CwError if --collapse-repeats is combined with --follow."""
    if collapse_repeats and follow:
        msg = (
            "--collapse-repeats is not supported with --follow: collapsing "
            "requires buffering a run until it closes, which would delay "
            "events reaching --follow consumers beyond the immediate-flush "
            "contract."
        )
        raise CwError(msg)


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
    "-c",
    "client_filter",
    multiple=True,
    help="Filter by payload.client field (repeatable).",
)
@click.option(
    "--lane",
    "lane_filter",
    multiple=True,
    help="Filter by payload.lane field (repeatable).",
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
@click.option(
    "--limit",
    "-n",
    type=int,
    default=None,
    help="Return only the most recent N matching events. Not supported with --follow.",
)
@click.option(
    "--collapse-repeats",
    is_flag=True,
    help=(
        "Collapse consecutive events of the same type with an identical "
        "compact payload into a single `TYPE xN over Mm` summary line. "
        "Not supported with --follow."
    ),
)
@handle_errors
def event_tail(
    since: str | None,
    type_filter: tuple[str, ...],
    client_filter: tuple[str, ...],
    lane_filter: tuple[str, ...],
    as_json: bool,
    follow: bool,
    dedup_terminal: bool,
    limit: int | None,
    collapse_repeats: bool,
) -> None:
    """Read events from the inbox.

    --since may be a consumer name (alphanumeric, e.g. 'daemon') whose
    persisted cursor determines the starting position, or an ISO 8601
    timestamp to filter by creation time.

    When a consumer name is given, the cursor advances automatically
    after reading (one-shot mode only; --follow never advances the cursor).

    --client filters by payload.client; --lane filters by payload.lane.
    Both are repeatable and accept comma-separated values.

    --limit/-n bounds the already-filtered set to the most recent N events
    (filter-then-limit); it is not supported with --follow, which streams
    unboundedly. The default (non-json) output format is compact: nested
    dict/list-of-dict payload fields (e.g. dispatch.tick's lanes and
    lane_occupants) are omitted, while scalar fields and scalar-lists are
    kept regardless of length. --json is unaffected and always emits the
    full event.

    --collapse-repeats merges consecutive events sharing the same type and
    compact payload into a single `TYPE xN over Mm` summary line; a run
    broken by an unrelated event re-opens rather than merging across the
    gap. Not supported with --follow, since collapsing requires buffering a
    run until it closes. Composes with --dedup-terminal and --limit,
    applied last in the pipeline. --json is unaffected: passing
    --collapse-repeats with --json is a no-op, one JSON line per event.
    """
    _validate_tail_limit(limit, follow=follow)
    _validate_collapse_repeats(collapse_repeats, follow=follow)

    consumer: str | None = None
    since_ts: datetime | None = None
    if since is not None:
        consumer, since_ts = _parse_since(since)

    etype_filter = _resolve_event_types(type_filter)
    client_names = _parse_multi_filter(client_filter)
    lane_names = _parse_multi_filter(lane_filter)

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
            lane_names=lane_names,
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
        lane_names=lane_names,
        limit=limit,
    )

    if dedup_terminal:
        events = _dedup_terminal(events)

    _emit_tail_events(
        events,
        as_json=as_json,
        collapse_repeats=collapse_repeats,
        consumer=consumer,
    )


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
    "-c",
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

    before_ts = parse_iso_before(before) if before is not None else None
    result = prune_events(before=before_ts, keep=keep, archive=not delete_flag)

    if as_json:
        click.echo(result.model_dump_json())
    else:
        detail = f" (archive: {result.archive_path})" if result.archive_path else ""
        click.echo(
            f"Archived {result.archived_count}, deleted {result.deleted_count},"
            f" kept {result.kept_count}{detail}"
        )
