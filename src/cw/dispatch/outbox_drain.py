"""The dispatch tick's outbox-drain launch (#3706, RFC 0019 O2).

``launch_outbox_drains`` launches at most one detached ``cw outbox drain
--client <c>`` per client per tick, outside every lock. Per client, in order:

1. ``outbox.enabled`` false: nothing at all (no read, no event, no registry).
2. A drain this process launched is still running: skip.
3. Read the held list and peek the pending entries (held ids excluded, at most
   ``TICK_SCAN_MAX_RECORDS`` newest records). No non-held pending entry: skip.
4. No-progress bound (P11): a previous launch has exited, the non-held
   pending id set is the one recorded at that launch, and the client's
   monotonic progress counter (``drain.progress``: entries that left
   ``pending``) has not moved, so it made no progress whatever its exit code.
   Skip, with one WARNING per client stalled state. A new enqueue, or a run that
   drained something, earns one more launch. The counter, not the id set,
   sees work done outside the tick's bounded scan window. The bound is in
   memory: a restarted loop gets one more try.
5. Every pending role has an unreachable marker: skip (a manual drain or
   ``--force`` probes it).
6. Another process holds the drain lease: skip.
7. Launch and record the launch-time pending set.

No timer, age or wall clock drives any of it (ADR-0014). A failure for one
client is logged and never fails the tick or another client.
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
    read_progress,
)
from cw.outbox._layout import drain_log_path

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Mapping

    from cw.models import ClientConfig

_log = logging.getLogger("cw.dispatch")


@dataclass(frozen=True)
class _Launch:
    """A drain this process launched and the non-held pending ids it saw."""

    proc: subprocess.Popen[bytes]
    pending_ids: frozenset[str]
    progress: int


# Process-lifetime registry: the launch-to-acquire gap is covered by the live
# ``Popen`` (the child may not hold the lease yet), and the recorded id set and
# progress count bound relaunches of a drain that makes no progress (P11).
_LAUNCHED: dict[str, _Launch] = {}
_WARNED_TRUNCATED: set[str] = set()
# One entry per client (its latest stalled state), so the registry is bounded by
# the client count however many id sets a long-lived loop sees.
_WARNED_STALLED: dict[str, tuple[frozenset[str], int]] = {}


def spawn_drain(client: str) -> subprocess.Popen[bytes]:
    """Launch ``cw outbox drain --client <client>`` detached.

    ``start_new_session=True``, stdin ``/dev/null``, output to
    ``state_dir()/outbox/<client>/drain.log`` (truncated per launch).
    """
    argv = [sys.executable, "-m", "cw", "outbox", "drain", "--client", client]
    log = drain_log_path(client)
    return _launch_logged_subprocess(log.parent, argv, dict(os.environ), Path(log.name))


def _stalled(
    client: str, previous: _Launch | None, pending: frozenset[str], progress: int
) -> bool:
    if (
        previous is None
        or previous.pending_ids != pending
        or previous.progress != progress
    ):
        return False
    state = (pending, progress)
    if _WARNED_STALLED.get(client) != state:
        _WARNED_STALLED[client] = state
        _log.warning(
            "outbox: the last drain of client %s made no progress on its %d"
            " pending entries; not relaunching until the set changes. Run"
            " 'cw outbox drain -c %s' to see the error",
            client,
            len(pending),
            client,
        )
    return True


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
    if previous is not None and previous.proc.poll() is None:
        return
    held = read_held(client)
    peek = peek_pending(client, scan_limit=TICK_SCAN_MAX_RECORDS, exclude_ids=held)
    if peek.truncated:
        _warn_truncated(client)
    progress = read_progress(client)
    if not peek.entry_ids or _stalled(client, previous, peek.entry_ids, progress):
        return
    if peek.roles <= marked_roles(client) or drain_lease_held(client):
        return
    _LAUNCHED[client] = _Launch(spawn_drain(client), peek.entry_ids, progress)


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
