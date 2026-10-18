"""Per-ticket ``dev-queue wait`` state and its progress / stuck output (#2693).

Clock-free on purpose: every helper takes the monotonic seconds or the wall
``now`` it needs as an argument, so ``wait``'s existing tests keep faking
``cw.cli.dev_queue.wait.time`` and nothing here reads a clock.

- :class:`TicketWait` -- what one waited-on ticket has been seen doing: the
  session id observed (the reap-revert check), the warned malformed
  sentinels, the stuck-pending tracker, when a dispatch hand-off was first
  seen, the last progress key printed and the probe throttle.
- :func:`format_waiting_line` / :func:`format_handoff_line` -- the stderr
  ``WAITING | ...`` progress lines (stdout stays one result document).
- :func:`emit_wait_stuck` -- the exit-5 result on stdout, text or JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import click

from cw.models.ticket_view import DispatchSource
from cw.stuck_pending import SECONDS_PER_MINUTE

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models.ticket_view import DispatchView
    from cw.stuck_pending import PendingBlock, PendingTracker

# The ticket view is probed (and a WAITING line printed) at most this often.
WAIT_PROBE_SECONDS = 30.0
STUCK_STATE = "stuck"
_NO_SIGNAL = "-"
# A lane-pause code is read live from the row's lane, not from a tick.
_ROW_SOURCE = DispatchSource.ROW.value


@dataclass
class TicketWait:
    """What ``wait`` has observed about one ticket across polls."""

    ticket_id: str
    client: str
    tracker: PendingTracker
    observed_session_id: str | None = None
    warned_blocks: set[str] = field(default_factory=set)
    handoff_since: float | None = None
    last_progress: str | None = None
    last_probe_at: float | None = None

    def probe_due(self, at: float) -> bool:
        """True (and records *at*) when the throttled probe should run now."""
        if (
            self.last_probe_at is not None
            and at - self.last_probe_at < WAIT_PROBE_SECONDS
        ):
            return False
        self.last_probe_at = at
        return True

    def progress_changed(self, key: str) -> bool:
        """True (and records *key*) when the progress state differs from last."""
        if key == self.last_progress:
            return False
        self.last_progress = key
        return True


def format_waiting_line(
    ticket_id: str,
    dispatch: DispatchView | None,
    block: PendingBlock | None,
    *,
    now: datetime,
) -> tuple[str, str]:
    """``(line, progress key)`` for a PENDING ticket.

    The code is the stuck block's (a lane pause has no tick of its own), else
    the dispatch skip reason. ``<age>`` is ``now - as_of`` in whole seconds,
    0 for a live row-sourced code.
    """
    code = block.code if block is not None else None
    if code is None and dispatch is not None:
        code = dispatch.skip_reason
    if code is None or dispatch is None:
        return f"WAITING | #{ticket_id} | pending: no dispatch signal yet", _NO_SIGNAL
    source = dispatch.source.value
    if dispatch.source == DispatchSource.NONE:
        source = _ROW_SOURCE
    age = 0
    if source != _ROW_SOURCE and dispatch.as_of is not None:
        age = int((now - dispatch.as_of).total_seconds())
    line = f"WAITING | #{ticket_id} | pending: {code} ({source}, {age}s ago)"
    if block is not None:
        line += f" | {block.detail}"
    return line, f"{code}|{source}"


def format_handoff_line(ticket_id: str, label: str, seconds: float) -> str:
    """The ``WAITING`` line for a RUNNING row mid dispatch hand-off."""
    return (
        f"WAITING | #{ticket_id} | running: dispatch hand-off {label} ({int(seconds)}s)"
    )


def emit_wait_stuck(
    *,
    ticket_id: str,
    client: str,
    status: str,
    session_id: str | None,
    block: PendingBlock,
    age_seconds: float,
    sentinel_status: str | None,
    output_json: bool,
) -> None:
    """Print the exit-5 result document (JSON) or its two text lines."""
    age = int(age_seconds)
    if output_json:
        click.echo(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "client": client,
                    "status": status,
                    "session_id": session_id,
                    "state": STUCK_STATE,
                    "sentinel_status": sentinel_status,
                    "pr_url": None,
                    "reason": block.code,
                    "detail": block.detail,
                    "age_seconds": age,
                    "next_command": block.command,
                }
            )
        )
        return
    minutes = age // SECONDS_PER_MINUTE
    click.echo(
        f"Stuck: {ticket_id} ({status}, {minutes}m): {block.code} - {block.detail}"
    )
    if block.command is not None:
        click.echo(f"Next: {block.command}")
