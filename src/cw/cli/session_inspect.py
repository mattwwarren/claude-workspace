"""Inspection subcommands for cw session state (``session`` group)."""

from __future__ import annotations

import json
import time

import click

from cw.cli._base import _relative_time, handle_errors
from cw.config import load_state
from cw.models import TERMINAL_SESSION_STATUSES, Session, SessionStatus
from cw.reconcile import ticket_id_for_session
from cw.session_retention import archive_file_count, find_session_by_id

_WAIT_POLL_INTERVAL: int = 5
_WAIT_DEFAULT_TIMEOUT: int = 300
_SESSION_WAIT_EXIT_TIMED_OUT: int = 1
_SESSION_WAIT_EXIT_HARD_TIMEOUT: int = 124

_SESSION_LIST_HEADERS = ["ID", "CLIENT", "PURPOSE", "STATUS", "NAME", "STARTED"]
_SESSION_LIST_WIDTHS = [10, 16, 10, 12, 30, 12]

# --status values whose result set can be silently truncated by session
# retention (#1983). `session list` deliberately does NOT merge archived
# sessions into its output — that would reintroduce the unbounded scan
# retention exists to remove — so it warns instead.
_ARCHIVE_NOTICE_STATUSES = frozenset(s.value for s in TERMINAL_SESSION_STATUSES)


def _resolve_session(session_ref: str) -> Session | None:
    """Prefix-match a session by short id or claude_session_id.

    Delegates to find_session_by_id, which checks sessions.json first
    and falls back to a newest-first scan of sessions.<date>.json
    archives on a complete hot-file miss (#1983).
    """
    return find_session_by_id(session_ref)


def _session_to_dict(session: Session) -> dict[str, object]:
    """Full Session field set for session show/list --json (GitHub #1624).

    Field count is driven entirely by the Session model, not a hand-listed
    subset — mirrors PR #1620's ``_task_to_dict`` (GitHub #1618). Note:
    datetime fields (``started_at``, ``completed_at``, ``idle_at``) now
    serialize with a "Z" suffix (``model_dump(mode="json")``'s pydantic-core
    representation) instead of the prior hand-rolled ``.isoformat()``'s
    "+00:00" — a disclosed, intentional breaking change.
    """
    return session.model_dump(mode="json")


def _print_session_human(session: Session) -> None:
    click.echo(f"id: {session.id}")
    click.echo(f"name: {session.name}")
    click.echo(f"client: {session.client}")
    click.echo(f"purpose: {session.purpose.value}")
    click.echo(f"status: {session.status.value}")
    click.echo(f"origin: {session.origin.value}")
    click.echo(f"started_at: {_relative_time(session.started_at)}")
    click.echo(f"completed_at: {_relative_time(session.completed_at)}")
    reason = session.completed_reason.value if session.completed_reason else None
    click.echo(f"completed_reason: {reason}")
    click.echo(f"idle_at: {_relative_time(session.idle_at)}")
    click.echo(f"worktree_path: {session.worktree_path}")
    click.echo(f"branch: {session.branch}")
    click.echo(f"surface_ref: {session.surface_ref}")
    click.echo(f"claude_session_id: {session.claude_session_id}")
    click.echo(f"lane: {session.lane}")
    click.echo(f"last_result: {session.last_result}")
    click.echo(f"cost_usd: {session.cost_usd}")
    stage = session.stage.value if session.stage else None
    click.echo(f"stage: {stage}")
    last_result_source = (
        session.last_result_source.value if session.last_result_source else None
    )
    click.echo(f"last_result_source: {last_result_source}")
    reap_reason = session.reap_reason.value if session.reap_reason else None
    click.echo(f"reap_reason: {reap_reason}")


def _print_session_list_human(sessions: list[Session]) -> None:
    if not sessions:
        click.echo("No sessions found.")
        return
    header = "  ".join(
        f"{h:<{w}}"
        for h, w in zip(_SESSION_LIST_HEADERS, _SESSION_LIST_WIDTHS, strict=True)
    )
    click.echo(header)
    click.echo("-" * len(header))
    for s in sessions:
        row = [
            s.id[:10],
            s.client[:16],
            s.purpose.value[:10],
            s.status.value[:12],
            s.name[:30],
            _relative_time(s.started_at)[:12],
        ]
        click.echo(
            "  ".join(
                f"{v:<{w}}" for v, w in zip(row, _SESSION_LIST_WIDTHS, strict=True)
            )
        )


@click.group(name="session")
def session_group() -> None:
    """Inspect cw session state. See also: cw list (human table view)."""


@session_group.command(name="show")
@click.argument("session_ref")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON.")
@handle_errors
def session_show(session_ref: str, output_json: bool) -> None:
    """Show details for one session.

    SESSION_REF is a short session id or claude_session_id prefix.
    See also: cw list (human table, excludes completed).
    """
    session = _resolve_session(session_ref)
    if session is None:
        click.echo(f"Session not found: {session_ref}", err=True)
        raise click.exceptions.Exit(1)
    if output_json:
        click.echo(json.dumps(_session_to_dict(session)))
    else:
        _print_session_human(session)


@session_group.command(name="list")
@click.option("--client", "-c", default=None, help="Filter by client name.")
@click.option(
    "--status",
    "-s",
    default=None,
    type=click.Choice([s.value for s in SessionStatus]),
    help="Filter by status. Default excludes completed and timed_out.",
)
@click.option("--purpose", "-p", default=None, help="Filter by session purpose.")
@click.option(
    "--ticket",
    "-t",
    default=None,
    help="Filter by ticket id (auto-dev sessions only).",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON array.")
@handle_errors
def session_list(
    client: str | None,
    status: str | None,
    purpose: str | None,
    ticket: str | None,
    output_json: bool,
) -> None:
    """List sessions. See also: cw list (human table view).

    Excludes completed and timed_out sessions by default.
    Use --status completed or --status timed_out to include terminal sessions.
    --ticket filters by ticket id parsed from auto-dev session names; USER-origin
    sessions (client/impl, etc.) have no ticket id and are always excluded.
    """
    state = load_state()
    sessions: list[Session] = state.sessions

    if status is None:
        sessions = [s for s in sessions if s.status not in TERMINAL_SESSION_STATUSES]
    else:
        target_status = SessionStatus(status)
        sessions = [s for s in sessions if s.status == target_status]

    if client is not None:
        sessions = [s for s in sessions if s.client == client]

    if purpose is not None:
        sessions = [s for s in sessions if s.purpose.value == purpose]

    if ticket is not None:
        sessions = [s for s in sessions if ticket_id_for_session(s.name) == ticket]

    if status in _ARCHIVE_NOTICE_STATUSES:
        archived = archive_file_count()
        if archived:
            click.echo(
                f"{archived} archived session files not shown; "
                "see sessions.<date>.json",
                err=True,
            )

    if output_json:
        click.echo(json.dumps([_session_to_dict(s) for s in sessions]))
    else:
        _print_session_list_human(sessions)


@session_group.command(name="wait")
@click.argument("session_ref")
@click.option(
    "--until",
    "until_str",
    default="completed,timed_out",
    help="Comma-separated statuses to wait for. Default: completed,timed_out",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=float,
    default=float(_WAIT_DEFAULT_TIMEOUT),
    show_default=True,
    help="Hard ceiling in seconds. Exit 124 on timeout.",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON.")
@handle_errors
def session_wait(
    session_ref: str,
    until_str: str,
    timeout_seconds: float,
    output_json: bool,
) -> None:
    """Block until SESSION_REF reaches one of the --until statuses.

    Exit codes:
      0 — completed
      1 — timed_out (or other non-completed terminal status in --until)
      124 — hard timeout (--timeout elapsed)
    """
    try:
        until_statuses: frozenset[SessionStatus] = frozenset(
            SessionStatus(s.strip()) for s in until_str.split(",") if s.strip()
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--until") from exc

    start = time.time()
    deadline = start + timeout_seconds

    while True:
        session = _resolve_session(session_ref)

        if session is not None and session.status in until_statuses:
            elapsed = time.time() - start
            if output_json:
                click.echo(
                    json.dumps(
                        {
                            "session_id": session.id,
                            "status": session.status.value,
                            "elapsed_seconds": elapsed,
                        }
                    )
                )
            else:
                click.echo(f"session {session.id}: {session.status.value}")
            exit_code = (
                0
                if session.status == SessionStatus.COMPLETED
                else _SESSION_WAIT_EXIT_TIMED_OUT
            )
            raise click.exceptions.Exit(exit_code)

        if time.time() >= deadline:
            elapsed = time.time() - start
            if output_json:
                click.echo(
                    json.dumps(
                        {
                            "session_id": session_ref,
                            "status": "timeout",
                            "elapsed_seconds": elapsed,
                        }
                    ),
                    err=True,
                )
            else:
                click.echo(f"Timeout waiting for session {session_ref}", err=True)
            raise click.exceptions.Exit(_SESSION_WAIT_EXIT_HARD_TIMEOUT)

        time.sleep(_WAIT_POLL_INTERVAL)


@session_group.command(name="result")
@click.argument("session_ref")
@handle_errors
def session_result(session_ref: str) -> None:
    """Print last_result JSON for a session.

    Exit 1 if the session is not found or has no recorded result.
    """
    session = _resolve_session(session_ref)
    if session is None:
        click.echo(f"Session not found: {session_ref}", err=True)
        raise click.exceptions.Exit(1)
    if session.last_result is None:
        click.echo(f"No result recorded for session {session.id}.", err=True)
        raise click.exceptions.Exit(1)
    click.echo(json.dumps(session.last_result, indent=2))
