"""The dispatch loop's per-client usage-limit back-off windows (#2999).

Extracted from ``cw.dispatch.loop``, which re-imports all three helpers.
They consult no name the suite patches on ``cw.dispatch.loop``; the
``dispatch.usage_limit_cleared`` emit and its transition handler read the
patched ``record_event`` and stay in ``loop``. Not to be confused with
``cw.dispatch.gating.usage_limit``, the spawn-time gate. Logs as
``cw.dispatch``, the pre-split logger name.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dispatch_state import (
    load_usage_limited_until,
    merge_usage_limited_until,
    record_usage_limit_armed,
    resolve_usage_limited_until,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_log = logging.getLogger("cw.dispatch")


def _merge_persisted_usage_limited_until(
    usage_limited_until: Mapping[str, datetime],
) -> dict[str, datetime]:
    """Merge the on-disk usage-limit sidecar into the in-memory windows (#1346).

    Re-read every tick: the pre-loop load in :func:`run_dispatch_loop` only
    protects a fresh restart (#804) -- it is never revisited, so this
    process's in-memory windows silently diverge from what any OTHER
    dispatch process (or this same process's own earlier tick) has
    persisted. Merge rather than overwrite, and merge PER CLIENT (#1409) via
    :func:`~cw.dispatch_state.merge_usage_limited_until`: take the later of
    {in-memory, on-disk} for each client name, and keep a client the other
    side has never heard of. This is a read-only refresh -- unlike the
    post-tick arm-and-save path, nothing is persisted here, so an unlocked
    read is fine. ``load_usage_limited_until()`` omits an entry that is
    malformed or merely expired (dispatch_state.py) -- a bare assignment
    would let a transient disk-read failure silently reopen the spawn gate
    mid-backoff. A read must never shorten an active window.
    """
    return merge_usage_limited_until(usage_limited_until, load_usage_limited_until())


def _arm_usage_limit_windows(
    usage_limited_until: Mapping[str, datetime],
    detections: Mapping[str, datetime | None],
    *,
    backoff_seconds: int,
) -> dict[str, datetime]:
    """Open a back-off window for each client that hit a limit this tick (#1409).

    *detections* maps a client name to the reset instant its spawn-time message
    named, or None when nothing parsed (and for every reconcile-derived
    detection, which names no client and so arms the whole known fleet). Each
    is resolved independently through :func:`resolve_usage_limited_until`, so
    one client's parsed reset can never set another client's deadline — the
    fleet-wide lockout this ticket exists to remove.

    Clients absent from *detections* keep the window they already had. A client
    present in both is overwritten rather than extended: a detection can only
    fire when that client's gate was open (``dispatch_tick`` skips a client
    whose window is still live), so the prior value is by definition lapsed.

    Emits one :attr:`~cw.models.OrchestratorEventType.USAGE_LIMIT_ARMED` per
    client — the set-side counterpart to ``dispatch.usage_limit_cleared``,
    which only ever fired on the way out. Without it the only durable record of
    a multi-hour lockout was a log line.
    """
    now = datetime.now(UTC)
    armed = dict(usage_limited_until)
    for client, reset_at in detections.items():
        until = resolve_usage_limited_until(now, reset_at, backoff_seconds)
        armed[client] = until
        source = record_usage_limit_armed(client, until=until, reset_at=reset_at)
        _log.warning(
            "dispatch: usage limit detected for %s; backing off until %s (%s)",
            client,
            until,
            source,
        )
    return armed


def _usage_limit_window_is_active(usage_limited_until: Mapping[str, datetime]) -> bool:
    """Is ANY client's usage-limit backoff window currently active (#1343)?

    Single source of truth for the "armed and not yet expired" check, shared
    by the loop's pre-loop seed and :func:`_handle_usage_limit_window_transition`
    -- mirrors the wall-clock check :func:`~cw.dispatch.tick.dispatch_tick`
    itself uses to gate spawning, folded over the per-client windows (#1409).

    Deliberately fleet-level even though the windows are now per client: it
    feeds the ``dispatch.usage_limit_cleared`` transition, which is a single
    event carrying the whole ``clients_affected`` cohort (#1343 R1). "Cleared"
    therefore means the LAST client's window lapsed. The per-client set side is
    audited by :attr:`~cw.models.OrchestratorEventType.USAGE_LIMIT_ARMED`
    instead.
    """
    now = datetime.now(UTC)
    return any(until > now for until in usage_limited_until.values())
