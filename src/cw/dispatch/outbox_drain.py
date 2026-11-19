"""The dispatch tick's outbox-drain launch (#3706, RFC 0019 O2).

``launch_outbox_drains`` launches at most one detached ``cw outbox drain
--client <c>`` per client per tick, outside every lock. Per client, in order:

1. ``outbox.enabled`` false: nothing at all (no read, no event, no registry).
2. A drain this process launched is still running: skip.
3. Read the held list and peek the pending entries (held ids excluded, at most
   ``TICK_SCAN_MAX_RECORDS`` newest records). No non-held pending entry: skip.
4. Every pending role has an unreachable marker: skip (a manual drain or
   ``--force`` probes it).
5. Another process holds the drain lease: skip.
6. Launch.

No persisted progress counter, launch baseline or warning registry drives any
of it (#3706 item 35): every pending entry a drain cannot make progress on
already ends held or under a binding marker, so the tick needs no more state.
A crash-looping drain (a bug) is surfaced by an in-memory streak of non-zero
exits in this process: it logs one warning per exit and never suppresses a
launch. No timer, age or wall clock drives any of it (ADR-0014). A failure for
one client is logged and never fails the tick or another client.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cw.exceptions import CwError
from cw.executor_launch import _launch_logged_subprocess
from cw.outbox import (
    TICK_SCAN_MAX_RECORDS,
    drain_lease_held,
    marked_roles,
    peek_pending,
    read_held,
)
from cw.outbox._layout import drain_log_path

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Mapping

    from cw.models import ClientConfig

_log = logging.getLogger("cw.dispatch")


@dataclass(frozen=True)
class _Launch:
    """A drain this process launched; its live ``Popen`` covers the lease gap."""

    proc: subprocess.Popen[bytes]


# Process-lifetime state, in memory only. ``_LAUNCHED`` covers the
# launch-to-acquire gap while the child is alive. ``_CRASH_STREAK`` counts
# consecutive non-zero exits per client, for the warning alone.
_LAUNCHED: dict[str, _Launch] = {}
_CRASH_STREAK: dict[str, int] = {}
_WARNED_TRUNCATED: set[str] = set()

# Exit status of a drain that found another drain holding the lease
# (``os.EX_TEMPFAIL``): a normal outcome, not a crash.
_LEASE_HELD_EXIT = os.EX_TEMPFAIL


def spawn_drain(client: str) -> subprocess.Popen[bytes]:
    """Launch ``cw outbox drain --client <client>`` detached.

    ``start_new_session=True``, stdin ``/dev/null``, output to
    ``state_dir()/outbox/<client>/drain.log`` (truncated per launch).
    """
    argv = [sys.executable, "-m", "cw", "outbox", "drain", "--client", client]
    log = drain_log_path(client)
    return _launch_logged_subprocess(log.parent, argv, dict(os.environ), Path(log.name))


def _note_exit(client: str, previous: _Launch) -> None:
    """Count a finished drain's exit; warn on a crash streak. Never gates a launch."""
    code = previous.proc.poll()
    if code is None or code in (0, _LEASE_HELD_EXIT):
        _CRASH_STREAK.pop(client, None)
        return
    streak = _CRASH_STREAK.get(client, 0) + 1
    _CRASH_STREAK[client] = streak
    _log.warning(
        "outbox: a drain of client %s exited %d (%d consecutive non-zero exits in"
        " this process); launching again. Run 'cw outbox drain -c %s' to see the"
        " error",
        client,
        code,
        streak,
        client,
    )


def _warn_truncated(client: str) -> None:
    if client in _WARNED_TRUNCATED:
        return
    _WARNED_TRUNCATED.add(client)
    _log.warning(
        "outbox: client %s has more than %d records; the tick sees only the"
        " newest (retention is #3715). Run 'cw outbox drain -c %s' to drain"
        " the rest",
        client,
        TICK_SCAN_MAX_RECORDS,
        client,
    )


def _maybe_launch(client: str, cfg: ClientConfig) -> None:
    if not cfg.outbox.enabled:
        return
    previous = _LAUNCHED.get(client)
    if previous is not None:
        if previous.proc.poll() is None:
            return
        _note_exit(client, previous)
    peek = peek_pending(
        client, scan_limit=TICK_SCAN_MAX_RECORDS, exclude_ids=read_held(client)
    )
    if peek.truncated:
        _warn_truncated(client)
    if not peek.entry_ids:
        return
    if peek.roles <= marked_roles(client) or drain_lease_held(client):
        return
    _LAUNCHED[client] = _Launch(spawn_drain(client))


def launch_outbox_drains(clients: Mapping[str, ClientConfig]) -> None:
    """Launch at most one outbox drain per client; never raises for one client."""
    for client, cfg in clients.items():
        try:
            _maybe_launch(client, cfg)
        except (OSError, CwError) as exc:
            _log.warning(
                "outbox: could not launch a drain for client %s (%s)",
                client,
                type(exc).__name__,
            )
