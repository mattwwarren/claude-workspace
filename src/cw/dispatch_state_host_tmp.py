"""Per-client worker-tmp inode-exhaustion latch in the dispatch sidecar (#2470).

Persisted in the shared ``dispatch_state.json`` sidecar under the
``"host_tmp_probe"`` key, one entry per client name. Split out of
``cw.dispatch_state`` (#3638) to keep that module under its size ceiling; the
sidecar key, shape and fail-safe behaviour are unchanged.

Imports ``cw.dispatch_state``; that module never imports this one. The state
file path is read at call time as ``dispatch_state.DISPATCH_STATE_FILE``
because the suite isolates state by patching that module attribute.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, NamedTuple

from cw import dispatch_state


class HostTmpProbeCache(NamedTuple):
    """One client's worker-tmp inode-exhaustion latch (#2470).

    Persisted in DISPATCH_STATE_FILE under the ``"host_tmp_probe"`` key, one
    entry per client name. Per-client (like :func:`load_main_drift_latches`
    and :class:`~cw.dispatch_state.OpenPrProbeCache`) rather than fleet-wide
    (like :class:`~cw.dispatch_state.AvailabilityProbeCache`): the disk-pressure
    gate it instruments probes each client's own worktree-base mount, so one
    client's exhausted mount must never clobber or mask another client's entry.
    A NamedTuple for the same transient-state reason as
    :class:`~cw.dispatch_state.AvailabilityProbeCache`.
    """

    probed_at: datetime
    exhausted: bool
    latched: bool  # True once SESSION_NEEDS_ATTENTION has fired for this
    # client's current unbroken run of exhausted probes; False once a
    # subsequent probe is healthy (edge-triggered reset).


# A failed sidecar write must not erase an edge-triggered latch in the current
# process: the next probe would otherwise re-emit SESSION_NEEDS_ATTENTION.
# Include the state-file path because tests, and callers embedding cw, can
# redirect state between independent stores in one process.
_HOST_TMP_PROBE_FALLBACK: dict[tuple[str, str], HostTmpProbeCache] = {}


def _parse_host_tmp_probe_entry(entry: object) -> HostTmpProbeCache | None:
    """Validate one raw ``"host_tmp_probe"`` entry, or None when malformed.

    Per-entry, same reasoning as the open-PR probe parser: one bad client entry
    must not blind the reader to its well-formed siblings.
    """
    if not isinstance(entry, dict):
        return None
    probed_at = entry.get("probed_at")
    exhausted = entry.get("exhausted")
    latched = entry.get("latched")
    if (
        not isinstance(probed_at, str)
        or not isinstance(exhausted, bool)
        or not isinstance(latched, bool)
    ):
        return None
    try:
        parsed_probed_at = datetime.fromisoformat(probed_at)
    except ValueError:
        return None
    return HostTmpProbeCache(
        probed_at=parsed_probed_at, exhausted=exhausted, latched=latched
    )


def load_host_tmp_probe_cache() -> dict[str, HostTmpProbeCache]:
    """Load every per-client worker-tmp inode latch (#2470), keyed by client.

    Returns ``{}`` when the file is absent, unreadable, malformed, or missing
    the ``"host_tmp_probe"`` key. Individual malformed entries are dropped.
    """
    cache: dict[str, HostTmpProbeCache] = {}
    path = dispatch_state.DISPATCH_STATE_FILE
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            raw = None
        if isinstance(raw, dict):
            entries = raw.get("host_tmp_probe")
            if isinstance(entries, dict):
                for client_name, entry in entries.items():
                    parsed = _parse_host_tmp_probe_entry(entry)
                    if parsed is not None:
                        cache[client_name] = parsed
    state_path = str(path)
    cache.update(
        {
            client_name: entry
            for (fallback_path, client_name), entry in _HOST_TMP_PROBE_FALLBACK.items()
            if fallback_path == state_path
        }
    )
    return cache


def save_host_tmp_probe_cache(client_name: str, cache: HostTmpProbeCache) -> bool:
    """Persist one client's worker-tmp inode latch (#2470).

    Read-merge-writes the shared sidecar (other keys preserved, #1157) and
    merges into the existing ``"host_tmp_probe"`` dict so other clients keep
    their own entries. Silently swallows write errors, same posture as
    :func:`~cw.dispatch_state.save_main_drift_latches`. When persistence fails,
    retain a process-local per-client fallback so a set cannot re-fire attention
    on every tick; a later healthy probe replaces that fallback with an
    unlatched entry.

    Returns whether the sidecar write succeeded.
    """

    def _set(payload: dict[str, Any]) -> bool:
        entries = payload.get("host_tmp_probe")
        if not isinstance(entries, dict):
            entries = {}
        entries[client_name] = {
            "probed_at": cache.probed_at.isoformat(),
            "exhausted": cache.exhausted,
            "latched": cache.latched,
        }
        payload["host_tmp_probe"] = entries
        return True

    ok = dispatch_state.mutate_dispatch_state(
        _set,
        failure_msg="dispatch_state: failed to persist host_tmp_probe (client=%s)",
        failure_args=(client_name,),
        exc_info=True,
    )
    key = (str(dispatch_state.DISPATCH_STATE_FILE), client_name)
    if ok:
        _HOST_TMP_PROBE_FALLBACK.pop(key, None)
    else:
        _HOST_TMP_PROBE_FALLBACK[key] = cache
    return ok
