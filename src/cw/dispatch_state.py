"""Dispatch-loop sidecar state persisted alongside sessions.json (#1322).

The ``dispatch_state.json`` sidecar holds transient dispatch-loop coordination
state — usage-limit backoff expiry, the fleet-wide gh-availability probe cache
(RFC 0011 A5), and the per-client main-checkout-drift attention latches (#1258).
Extracted from ``cw.config``; depends on ``cw.config`` for the state-dir
accessors and the #1017 write guard, one-directional (``cw.config`` never
imports this module).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from cw.atomic import atomic_write_text
from cw.config import STATE_DIR, refuse_real_state_write, state_dir

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

# Why: config.py's own convention (see its "Path accessors" comment) is
# "never `from cw.config import STATE_DIR` in a consumer; always call the
# accessor" so a monkeypatch of `cw.config.STATE_DIR` reaches every consumer.
# These two constants are the deliberate, precedented exception: they are
# also the direct monkeypatch targets tests patch on this module
# (`cw.dispatch_state.DISPATCH_STATE_FILE`/`DISPATCH_STATE_LOCK`), so a
# frozen import-time snapshot of STATE_DIR is safe today. It would silently
# stop tracking a later `cw.config.STATE_DIR` reassignment, unlike an
# accessor call.
DISPATCH_STATE_FILE = STATE_DIR / "dispatch_state.json"
DISPATCH_STATE_LOCK = STATE_DIR / ".dispatch_state.lock"


def dispatch_state_lock_file() -> Path:
    return DISPATCH_STATE_LOCK


class AvailabilityProbeCache(NamedTuple):
    """Fleet-wide TTL-cached gh-availability probe result + outage latch.

    Persisted in DISPATCH_STATE_FILE under the ``"availability_probe"`` key
    (RFC 0011 A5). A NamedTuple, not a pydantic BaseModel: transient,
    TTL-bounded runtime state with no durability/migration contract — the
    dispatch loop re-probes on every TTL expiry, so a shape drift self-heals
    within one TTL window rather than needing a schema version.
    """

    probed_at: datetime
    available: bool
    latched: bool  # True once SESSION_NEEDS_ATTENTION has fired for the
    # current unbroken run of failures; False once a subsequent fresh probe
    # succeeds (edge-triggered reset).


@contextlib.contextmanager
def dispatch_state_lock() -> Iterator[None]:
    """Acquire an exclusive file lock over the DISPATCH_STATE_FILE write window.

    Mirror of ``concurrency_override_lock()``/``clients_lock()``. Hold this
    across every load→mutate→write sequence in ``save_usage_limited_until``,
    ``save_availability_probe_cache``, and ``save_main_drift_latches`` so
    concurrent ``cw`` processes cannot clobber each other's edits (lost
    update, #1256). The lock is advisory (``fcntl.flock``) and per-open-fd,
    so sequential re-acquisitions in the same process are safe.
    Do NOT nest: acquiring while already holding will deadlock.

    Lock ordering: ``sessions_lock()`` → ``dispatch_state_lock()`` is
    permitted and occurs today, via ``save_main_drift_latches`` called from
    inside ``reconcile/core.py``'s ``with sessions_lock():`` block through
    ``_act_on_main_drift_candidates`` (``main_drift.py:162``). The reverse
    ordering is forbidden and never occurs in the current call graph. The one
    pathological cross-lock path (#1228, the RFC 0010 P4 review-recipe act
    phase re-entering ``reconcile()`` from inside ``sessions_lock()``) fails
    first at its own ``reconcile()`` call (``dispatch.py:1491``) with
    ``SessionsLockReentryError``, before it can reach any
    ``dispatch_state_lock()`` acquisition.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    lock_path = dispatch_state_lock_file()
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _load_dispatch_state_raw() -> dict[str, Any]:
    """Read DISPATCH_STATE_FILE as a dict, or ``{}`` if absent/corrupt/unreadable.

    Shared read-side of the read-merge-write save helpers
    (``save_usage_limited_until`` / ``save_availability_probe_cache``) so that
    neither clobbers the other's key in the shared sidecar (#1157). A corrupt
    or non-object existing file is treated as empty rather than raising.
    """
    path = DISPATCH_STATE_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_usage_limited_until() -> datetime | None:
    """Load the persisted usage-limit backoff expiry from DISPATCH_STATE_FILE.

    Returns None when the file is absent, unreadable, malformed, or the stored
    timestamp is already in the past (so a stale backoff from a previous loop
    run never silently re-blocks a fresh loop start).
    """
    path = DISPATCH_STATE_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        ts = raw.get("usage_limited_until")
        if not isinstance(ts, str):
            return None
        dt = datetime.fromisoformat(ts)
        return dt if dt > datetime.now(UTC) else None
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def save_usage_limited_until(dt: datetime | None) -> None:
    """Persist (or clear) the usage-limit backoff expiry to DISPATCH_STATE_FILE.

    Writes ``{"usage_limited_until": "<iso>"}`` when *dt* is set; writes
    ``{"usage_limited_until": null}`` to clear it.  Read-merge-writes the
    shared sidecar so the ``availability_probe`` key (RFC 0011 A5) is
    preserved rather than clobbered (#1157).  Creates STATE_DIR if needed.
    Silently swallows write errors — a failed persist just means the next loop
    start won't honour the backoff (acceptable degradation).
    """
    try:
        refuse_real_state_write(DISPATCH_STATE_FILE)
        DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            payload["usage_limited_until"] = dt.isoformat() if dt is not None else None
            atomic_write_text(DISPATCH_STATE_FILE, json.dumps(payload))
    except OSError:
        logger.warning("dispatch_state: failed to persist usage_limited_until")


def load_usage_limit_armed_at() -> datetime | None:
    """Load the persisted usage-limit backoff arm timestamp (#1343).

    Unlike :func:`load_usage_limited_until`, this is a historical marker, not
    a forward-looking deadline: it is NOT expiry-checked against "now", so it
    remains readable after the window it armed has lapsed -- needed so the
    dispatch loop can read an exact ``detected_at`` at the moment it notices
    the armed->cleared transition. Returns None when the file is absent,
    unreadable, malformed, or the key is missing.
    """
    path = DISPATCH_STATE_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        ts = raw.get("usage_limit_armed_at")
        if not isinstance(ts, str):
            return None
        return datetime.fromisoformat(ts)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def save_usage_limit_armed_at(dt: datetime | None) -> None:
    """Persist (or clear) the usage-limit backoff arm timestamp (#1343).

    Writes ``{"usage_limit_armed_at": "<iso>"}`` when *dt* is set; writes
    ``{"usage_limit_armed_at": null}`` to clear it. Read-merge-writes the
    shared sidecar so the other keys (``usage_limited_until``,
    ``availability_probe``, ``main_drift_latches``) are preserved rather than
    clobbered (#1157). Creates STATE_DIR if needed. Silently swallows write
    errors -- a failed persist just means a later ``dispatch.usage_limit_cleared``
    event degrades ``detected_at`` to null rather than being suppressed
    (acceptable degradation).
    """
    try:
        refuse_real_state_write(DISPATCH_STATE_FILE)
        DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            payload["usage_limit_armed_at"] = dt.isoformat() if dt is not None else None
            atomic_write_text(DISPATCH_STATE_FILE, json.dumps(payload))
    except OSError:
        logger.warning("dispatch_state: failed to persist usage_limit_armed_at")


def load_availability_probe_cache() -> AvailabilityProbeCache | None:
    """Load the fleet-wide gh-availability probe cache from DISPATCH_STATE_FILE.

    Returns None when the file is absent, unreadable, malformed, missing the
    ``"availability_probe"`` key, or storing a malformed entry shape (RFC 0011
    A5). Tolerates other keys (e.g. ``usage_limited_until``) sharing the file.
    """
    path = DISPATCH_STATE_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        entry = raw.get("availability_probe")
        if not isinstance(entry, dict):
            return None
        probed_at = entry.get("probed_at")
        available = entry.get("available")
        latched = entry.get("latched")
        if (
            not isinstance(probed_at, str)
            or not isinstance(available, bool)
            or not isinstance(latched, bool)
        ):
            return None
        return AvailabilityProbeCache(
            probed_at=datetime.fromisoformat(probed_at),
            available=available,
            latched=latched,
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def save_availability_probe_cache(cache: AvailabilityProbeCache) -> None:
    """Persist the fleet-wide gh-availability probe cache to DISPATCH_STATE_FILE.

    Read-merge-writes the shared sidecar so the ``usage_limited_until`` key is
    preserved rather than clobbered (#1157).  Creates STATE_DIR if needed.
    Silently swallows write errors — a failed persist just means the next tick
    re-probes rather than reading the cache (acceptable degradation).
    """
    try:
        refuse_real_state_write(DISPATCH_STATE_FILE)
        DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            payload["availability_probe"] = {
                "probed_at": cache.probed_at.isoformat(),
                "available": cache.available,
                "latched": cache.latched,
            }
            atomic_write_text(DISPATCH_STATE_FILE, json.dumps(payload))
    except OSError:
        logger.warning("dispatch_state: failed to persist availability_probe")


def load_main_drift_latches() -> dict[str, bool]:
    """Load the per-client main-checkout-drift attention latches (#1258).

    Returns ``{}`` when the file is absent, unreadable, malformed, missing
    the ``"main_drift_latches"`` key, or storing a non-dict / wrong-value-type
    entry. Mirrors :func:`load_availability_probe_cache`'s fail-safe shape;
    tolerates other keys (e.g. ``availability_probe``) sharing the file.
    """
    path = DISPATCH_STATE_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        entry = raw.get("main_drift_latches")
        if not isinstance(entry, dict):
            return {}
        if not all(
            isinstance(key, str) and isinstance(value, bool)
            for key, value in entry.items()
        ):
            return {}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}
    else:
        return entry


def save_main_drift_latches(latches: dict[str, bool]) -> None:
    """Persist the per-client main-checkout-drift attention latches (#1258).

    Read-merge-writes the shared sidecar so ``usage_limited_until`` and
    ``availability_probe`` are preserved rather than clobbered.  Creates
    STATE_DIR if needed.  Silently swallows write errors, same posture as
    ``save_availability_probe_cache`` — acceptable degradation: a failed
    persist on a *set* just risks one extra re-fire next tick, but a failed
    persist on a *reset* leaves the on-disk latch stale (still ``True``),
    which can suppress one genuine future re-arm until a later tick's write
    succeeds. Bounded and self-healing, not silent data loss: the merge is
    per-tick (this function is called at most once per ``reconcile()``, with
    the full latch dict), so a lost update only ever costs one client's one
    flip, never another client's state.
    """
    try:
        refuse_real_state_write(DISPATCH_STATE_FILE)
        DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Why: file-locked (dispatch_state_lock, #1256) — reconcile() runs
        # from independent, short-lived processes (cw status/list/start,
        # every dispatch_tick), so concurrent read-merge-writes for
        # different clients previously could race and lose one flip. The
        # lock serializes this critical section against the other two
        # DISPATCH_STATE_FILE writers, closing that race.
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            payload["main_drift_latches"] = latches
            atomic_write_text(DISPATCH_STATE_FILE, json.dumps(payload))
    except OSError:
        logger.warning(
            "dispatch_state: failed to persist main_drift_latches (clients=%s)",
            sorted(latches),
            exc_info=True,
        )
