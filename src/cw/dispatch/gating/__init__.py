"""Preflight gating for the dispatch loop.

Part of the ``cw.dispatch`` package split (#1310): the preflight gates that
decide whether a client is eligible to dispatch this tick.

This package was split out of a single ``gating.py`` module (#2503); the
import surface (``from cw.dispatch.gating import X``) is preserved here via
re-exports. Internal cross-references use the direct submodule path, never
this package. A test that patches a name one of these submodules imported
from elsewhere must target the submodule that looks it up at call time
(``cw.dispatch.gating.freshness.is_main_behind_origin``), not this package.
Submodules:

- ``availability`` — the fleet-wide TTL-cached ``gh auth status`` probe
  (RFC 0011 A5), its edge-triggered outage latch, and the skip event.
- ``context_json`` — pre-spawn invalidation of a stale ``.cw/context.json``
  (#1046); also the one home of ``_LOGGER_NAME``, the ``cw.dispatch`` logger
  name every submodule logs on.
- ``disk_pressure`` — the worktree-base free-bytes (#1887) and free-inodes
  (#2470) gate, its skip/bypass events, and the ``host_tmp_exhausted`` latch.
- ``freshness`` — the main-behind-origin gate, the auto fast-forward path,
  and the ``ticket.needs_sync`` skip events.
- ``ssh_key`` — the SSH-agent-key gate (#927), keyed on the push-remote
  scheme (#1495), with its skip and bypass (#1437) events.
- ``usage_limit`` — the best-effort ``reconcile()`` preamble that reports a
  usage limit, and the skip events emitted during its back-off window.
"""

from __future__ import annotations

from cw.dispatch.gating.availability import (
    _AVAILABILITY_OUTAGE_REASON,
    _AVAILABILITY_PROBE_TIMEOUT_SECONDS,
    _AVAILABILITY_PROBE_TTL_SECONDS,
    _emit_availability_skip,
    _record_availability_block,
    _reset_availability_block,
    _resolve_availability,
    _resolve_availability_once,
)
from cw.dispatch.gating.context_json import (
    _LOGGER_NAME as _LOGGER_NAME,
)
from cw.dispatch.gating.context_json import (
    _invalidate_stale_context_json,
)
from cw.dispatch.gating.disk_pressure import (
    _HOST_TMP_EXHAUSTED_REASON,
    _apply_disk_pressure_gate,
    _disk_pressure_warn_line,
    _DiskPressure,
    _emit_disk_pressure_bypass,
    _emit_disk_pressure_skip,
    _record_host_tmp_exhausted_block,
    _reset_host_tmp_exhausted_block,
    _resolve_disk_pressure,
    _resolve_inode_pressure,
    _update_host_tmp_latch,
)
from cw.dispatch.gating.freshness import (
    FRESHNESS_MAIN_BEHIND,
    FRESHNESS_MAIN_DETACHED,
    FRESHNESS_MAIN_DIRTY_CHECKOUT,
    FRESHNESS_MAIN_DIVERGED,
    FRESHNESS_NON_MAIN_HEAD,
    _emit_stale_skip,
    _resolve_freshness,
)
from cw.dispatch.gating.ssh_key import (
    _SSH_KEY_WARN_SENTINEL,
    _apply_ssh_key_gate,
    _emit_ssh_key_bypass,
    _emit_ssh_key_skip,
    _resolve_ssh_key_once,
)
from cw.dispatch.gating.usage_limit import (
    _emit_usage_limit_skip_events,
    _reconcile_usage_limited,
)

__all__ = [
    "FRESHNESS_MAIN_BEHIND",
    "FRESHNESS_MAIN_DETACHED",
    "FRESHNESS_MAIN_DIRTY_CHECKOUT",
    "FRESHNESS_MAIN_DIVERGED",
    "FRESHNESS_NON_MAIN_HEAD",
    "_AVAILABILITY_OUTAGE_REASON",
    "_AVAILABILITY_PROBE_TIMEOUT_SECONDS",
    "_AVAILABILITY_PROBE_TTL_SECONDS",
    "_HOST_TMP_EXHAUSTED_REASON",
    "_SSH_KEY_WARN_SENTINEL",
    "_DiskPressure",
    "_apply_disk_pressure_gate",
    "_apply_ssh_key_gate",
    "_disk_pressure_warn_line",
    "_emit_availability_skip",
    "_emit_disk_pressure_bypass",
    "_emit_disk_pressure_skip",
    "_emit_ssh_key_bypass",
    "_emit_ssh_key_skip",
    "_emit_stale_skip",
    "_emit_usage_limit_skip_events",
    "_invalidate_stale_context_json",
    "_reconcile_usage_limited",
    "_record_availability_block",
    "_record_host_tmp_exhausted_block",
    "_reset_availability_block",
    "_reset_host_tmp_exhausted_block",
    "_resolve_availability",
    "_resolve_availability_once",
    "_resolve_disk_pressure",
    "_resolve_freshness",
    "_resolve_inode_pressure",
    "_resolve_ssh_key_once",
    "_update_host_tmp_latch",
]
