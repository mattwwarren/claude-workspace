"""Claim-time disk-pressure preflight gate for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503): the per-client probe
of the worktree-base mount's free bytes (#1887) and free inodes (#2470), the
operator WARN line, skip and bypass events, and the per-client
``host_tmp_exhausted`` attention latch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.disk import (
    check_disk_usage,
    check_inode_usage,
    effective_min_free_inodes,
    inodes_exhausted,
)
from cw.dispatch.gating.context_json import _LOGGER_NAME
from cw.dispatch_state import (
    HostTmpProbeCache,
    load_host_tmp_probe_cache,
    save_host_tmp_probe_cache,
)
from cw.events import record_event
from cw.models import (
    DispatchSkipReason,
    OrchestratorEventType,
)
from cw.worktree import resolve_worktree_base

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        DevQueueStore,
    )
from cw.dispatch.claim import _lane_occupants_for_client, _lane_stats_for_client

_log = logging.getLogger(_LOGGER_NAME)


# paused_status written to SESSION_NEEDS_ATTENTION when a client's worktree-base
# mount (which holds every worker's .cw/tmp TMPDIR) runs low on free inodes
# (#2470). Per-client, unlike _AVAILABILITY_OUTAGE_REASON above: the event
# carries the real client name, never client="".
_HOST_TMP_EXHAUSTED_REASON = "host_tmp_exhausted"


class _DiskPressure(NamedTuple):
    """Both dimensions of one client's disk-pressure probe (#1887, #2470).

    ``free_inodes``/``min_free_inodes`` are ``None`` when the inode dimension
    does not apply this tick: the inode probe raised, or the filesystem
    reports no fixed inode budget (btrfs, ``total_inodes == 0``).
    """

    gated_gb: bool
    gated_inodes: bool
    free_gb: float
    free_inodes: int | None
    min_free_inodes: int | None

    @property
    def gated(self) -> bool:
        return self.gated_gb or self.gated_inodes


def _resolve_inode_pressure(
    client: ClientConfig,
    base: Path,
    *,
    min_free_inodes: float,
    min_free_inode_fraction: float,
) -> tuple[bool, int | None, int | None]:
    """Probe *base*'s inodes, returning ``(gated, free_inodes, min_free_inodes)``.

    The effective floor is ``max(min_free_inodes, min_free_inode_fraction x
    total)`` (#2470 R1, via :func:`cw.disk.effective_min_free_inodes`). Fails
    OPEN on ``OSError`` exactly as the byte probe does, and reports the inode
    dimension as not applicable (``None`` counts) on a zero-total mount.
    """
    try:
        usage = check_inode_usage(base)
    except OSError:
        _log.warning(
            "dispatch_tick: inode-pressure probe failed for %s; proceeding",
            client.name,
        )
        return (False, None, None)
    if usage.total_inodes == 0:
        return (False, None, None)
    floor = effective_min_free_inodes(
        usage.total_inodes,
        min_free_inodes=min_free_inodes,
        min_free_inode_fraction=min_free_inode_fraction,
    )
    gated = inodes_exhausted(
        usage,
        min_free_inodes=min_free_inodes,
        min_free_inode_fraction=min_free_inode_fraction,
    )
    return (gated, usage.free_inodes, floor)


def _resolve_disk_pressure(
    client: ClientConfig,
    *,
    min_free_gb: float,
    min_free_inodes: float,
    min_free_inode_fraction: float,
) -> _DiskPressure:
    """Probe *client*'s worktree-base mount for free bytes and free inodes.

    Probes :func:`~cw.worktree.resolve_worktree_base` -- the filesystem that
    will actually receive new worktree data (and, since #2470, every
    worker's ``<worktree>/.cw/tmp`` TMPDIR) -- NOT ``client.workspace_path``:
    an operator can point ``worktree_base`` at a different, more
    space-constrained mount, and that is precisely the disk a claimed task's
    checkout fills.

    Each dimension fails OPEN on ``OSError`` independently, mirroring
    :func:`_resolve_freshness`'s posture rather than the SSH-key gate's
    fail-closed one: a probe error is not evidence of disk pressure, and
    holding the whole fleet PENDING on an unreadable mount would be a
    self-inflicted outage.
    """
    base = resolve_worktree_base(client)
    try:
        usage = check_disk_usage(base)
    except OSError:
        _log.warning(
            "dispatch_tick: disk-pressure probe failed for %s; proceeding",
            client.name,
        )
        gated_gb, free_gb = False, 0.0
    else:
        gated_gb, free_gb = usage.free_gb < min_free_gb, usage.free_gb
    gated_inodes, free_inodes, floor = _resolve_inode_pressure(
        client,
        base,
        min_free_inodes=min_free_inodes,
        min_free_inode_fraction=min_free_inode_fraction,
    )
    return _DiskPressure(gated_gb, gated_inodes, free_gb, free_inodes, floor)


def _disk_pressure_warn_line(
    client: ClientConfig, pressure: _DiskPressure, *, min_free_gb: float
) -> str:
    """Build the operator WARN line naming whichever dimension(s) gated."""
    causes: list[str] = []
    if pressure.gated_gb:
        causes.append(
            f"worktree disk low — {pressure.free_gb:.1f} GB free,"
            f" need {min_free_gb:.1f} GB"
        )
    if pressure.gated_inodes:
        causes.append(
            f"worktree mount low on inodes — {pressure.free_inodes:,} free,"
            f" need {pressure.min_free_inodes:,}"
        )
    return (
        f"WARN {client.name}: {'; '.join(causes)};"
        " client held PENDING until space frees up"
    )


def _emit_disk_pressure_skip(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    pending_count: int,
    running_count: int,
    cap: int,
    emit: Callable[[str], None] | None,
    warned_disk_pressure: set[str] | None,
    pressure: _DiskPressure,
    min_free_gb: float,
) -> None:
    """Emit the operator WARN line (once per client) + dispatch.tick skip event.

    Mirrors :func:`_emit_ssh_key_skip`'s shape (``skip_reason=
    DISK_PRESSURE_GATE``, plus ``disk_free_gb``/``disk_min_free_gb`` and,
    since #2470, ``disk_free_inodes``/``disk_min_free_inodes``), but
    ``warned_disk_pressure`` is keyed on ``client.name`` rather than a shared
    fleet sentinel: unlike a single local ssh-agent, disk pressure genuinely
    varies per client, since each client's ``worktree_base`` may sit on its
    own mount.
    """
    free_gb = pressure.free_gb
    if emit is not None and (
        warned_disk_pressure is None or client.name not in warned_disk_pressure
    ):
        emit(_disk_pressure_warn_line(client, pressure, min_free_gb=min_free_gb))
        if warned_disk_pressure is not None:
            warned_disk_pressure.add(client.name)
    lane_occupants = _lane_occupants_for_client(client, queue_snapshot)
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client.name,
            "claimed": 0,
            "pending": pending_count,
            "running": running_count,
            "cap": cap,
            "skip_reason": DispatchSkipReason.DISK_PRESSURE_GATE,
            "disk_free_gb": free_gb,
            "disk_min_free_gb": min_free_gb,
            "disk_free_inodes": pressure.free_inodes,
            "disk_min_free_inodes": pressure.min_free_inodes,
            "lanes": _lane_stats_for_client(
                client, queue_snapshot, occupants=lane_occupants
            ),
            "lane_occupants": lane_occupants,
            "occupied": sum(len(v) for v in lane_occupants.values()),
        },
    )


def _emit_disk_pressure_bypass(
    client: ClientConfig,
    *,
    pressure: _DiskPressure,
    min_free_gb: float,
) -> None:
    """Record the operator-forwarded bypass event (GitHub #1887).

    Sibling of :func:`_emit_disk_pressure_skip`, NOT a reuse of it: called
    instead of that helper when the probe reports pressure but
    ``OrchestratorConfig.disk_pressure_gate_enabled`` is False, so the
    would-be skip is suppressed and the client dispatches anyway. Same
    no-stdout-line rationale as :func:`_emit_ssh_key_bypass`:
    DISK_PRESSURE_GATE_BYPASSED is in the default operator-channel
    forward-set, so a duplicate emit would be noise.
    """
    record_event(
        OrchestratorEventType.DISK_PRESSURE_GATE_BYPASSED,
        {
            "client": client.name,
            "disk_free_gb": pressure.free_gb,
            "disk_min_free_gb": min_free_gb,
            "disk_free_inodes": pressure.free_inodes,
            "disk_min_free_inodes": pressure.min_free_inodes,
        },
    )


def _record_host_tmp_exhausted_block(
    client_name: str, *, now: datetime, was_latched: bool, detail: str
) -> None:
    """Persist this client's exhausted probe; fire attention once per episode.

    Per-client sibling of :func:`_record_availability_block` (#2470): same
    edge-triggered ``session.needs_attention`` shape, but the latch lives in
    a per-client :class:`~cw.dispatch_state.HostTmpProbeCache` entry and the
    event carries the real ``client`` name -- threaded through the way
    :func:`~cw.dispatch.lanes._record_client_freshness_block` threads it --
    because the disk-pressure gate probes each client's own mount. Only the
    inode dimension latches: a tmpfs out of inodes is the 2026-09-27 ENOSPC
    incident this signal exists for, and it needs an operator (clearing
    stale scratch trees) rather than time.
    """
    save_host_tmp_probe_cache(
        client_name, HostTmpProbeCache(probed_at=now, exhausted=True, latched=True)
    )
    if not was_latched:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": "",
                "session_name": "",
                "client": client_name,
                "ticket_id": None,
                "claude_session_id": None,
                "paused_status": _HOST_TMP_EXHAUSTED_REASON,
                "breadcrumbs": detail,
                "crashed": False,
            },
            correlation_id=None,
        )


def _reset_host_tmp_exhausted_block(client_name: str, *, now: datetime) -> None:
    """Clear this client's latch so its next exhaustion episode re-fires (#2470)."""
    save_host_tmp_probe_cache(
        client_name, HostTmpProbeCache(probed_at=now, exhausted=False, latched=False)
    )


def _update_host_tmp_latch(client: ClientConfig, pressure: _DiskPressure) -> None:
    """Drive the per-client ``host_tmp_exhausted`` latch from one probe (#2470).

    Runs whether or not the gate is enforced: the attention signal is
    informational and independent of the ``disk_pressure_gate_enabled``
    bypass. A not-applicable inode probe (raised, or zero-total mount) leaves
    the latch untouched; a healthy probe resets it only when it is set, so a
    healthy fleet never writes the sidecar.
    """
    if pressure.free_inodes is None:
        return
    cache = load_host_tmp_probe_cache().get(client.name)
    was_latched = cache is not None and cache.latched
    now = datetime.now(UTC)
    if pressure.gated_inodes:
        _record_host_tmp_exhausted_block(
            client.name,
            now=now,
            was_latched=was_latched,
            detail=(
                f"free_inodes={pressure.free_inodes}"
                f" min_free_inodes={pressure.min_free_inodes}"
            ),
        )
    elif was_latched:
        _reset_host_tmp_exhausted_block(client.name, now=now)


def _apply_disk_pressure_gate(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    pending_count: int,
    running_count: int,
    cap: int,
    emit: Callable[[str], None] | None,
    warned_disk_pressure: set[str] | None,
    min_free_gb: float,
    min_free_inodes: float,
    min_free_inode_fraction: float,
    gate_enabled: bool,
) -> bool:
    """Run the claim-time disk-pressure gate; True means hold *client* PENDING.

    Extracted as its own helper (rather than inlined into
    ``cw.dispatch.tick._run_preflight_gates`` the way the ssh-key block is)
    specifically to keep that caller under the PLR0911 six-return ceiling: it
    has 4 returns today, and folding this gate's two branches in would put it
    exactly at 6 with no headroom left for the next gate.

    Gates on free bytes (#1887) OR free inodes (#2470); the inode dimension
    also drives the per-client ``host_tmp_exhausted`` attention latch.
    """
    pressure = _resolve_disk_pressure(
        client,
        min_free_gb=min_free_gb,
        min_free_inodes=min_free_inodes,
        min_free_inode_fraction=min_free_inode_fraction,
    )
    _update_host_tmp_latch(client, pressure)
    if not pressure.gated:
        return False
    if not gate_enabled:
        _emit_disk_pressure_bypass(client, pressure=pressure, min_free_gb=min_free_gb)
        return False
    _emit_disk_pressure_skip(
        client,
        queue_snapshot,
        pending_count=pending_count,
        running_count=running_count,
        cap=cap,
        emit=emit,
        warned_disk_pressure=warned_disk_pressure,
        pressure=pressure,
        min_free_gb=min_free_gb,
    )
    return True
