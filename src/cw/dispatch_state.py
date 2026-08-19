"""Dispatch-loop sidecar state persisted alongside sessions.json (#1322).

The ``dispatch_state.json`` sidecar holds transient dispatch-loop coordination
state — usage-limit backoff expiry, the fleet-wide gh-availability probe cache
(RFC 0011 A5), the per-client main-checkout-drift attention latches (#1258),
a per-(client, ticket) marker recording an in-flight blocking review, so
``cw dev-queue status``/``cw doctor`` can distinguish a dead dispatch loop from
one legitimately busy (#1742), and the per-(client, ticket) open-PR probe cache
backing the pre-dispatch stale-dispatch gate (#1862).
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


class ExecutorBlockedMarker(NamedTuple):
    """One in-flight, loop-blocking executor review (#1742).

    Persisted in DISPATCH_STATE_FILE under the ``"executor_blocked"`` key,
    one entry per ``f"{client}/{ticket_id}"``. A NamedTuple, not a pydantic
    BaseModel, for the same reason as :class:`AvailabilityProbeCache`:
    transient runtime state with no durability or migration contract.

    Deliberately non-durable. It is written when a review starts and cleared
    in a ``finally:`` when it ends, so the only way one survives its owning
    work is a process death — and the review itself dies with that process
    (it runs on a daemon thread). ``clear_all_executor_blocked_markers()``
    at dispatch-loop boot therefore closes the whole crash window, which is
    why there is no schema version and no per-entry expiry ceiling.

    ``executor`` is free text ("codex" today) so a future non-codex blocking
    executor needs no schema change. ``reviewer_role`` is reserved for
    per-role granularity and is ``None`` at whole-review granularity.
    """

    client: str
    ticket_id: str
    executor: str
    reviewer_role: str | None
    started_at: datetime
    session_id: str


class OpenPrProbeCache(NamedTuple):
    """One per-(client, ticket) TTL-cached open-PR probe result (#1862).

    Persisted in DISPATCH_STATE_FILE under the ``"open_pr_probe"`` key, one
    entry per ``f"{client}/{ticket_id}"``. A NamedTuple, not a pydantic
    BaseModel, for the same reason as :class:`AvailabilityProbeCache`:
    transient, TTL-bounded runtime state with no durability or migration
    contract -- the dispatch loop re-probes on every TTL expiry, so a shape
    drift self-heals within one TTL window.

    Per-ticket (like :class:`ExecutorBlockedMarker`) rather than fleet-wide
    (like :class:`AvailabilityProbeCache`): the question it answers -- "does
    *this* ticket's branch already have an open PR" -- is per-ticket by
    construction.

    Only *reliable* verdicts are cached. A transient ``gh`` failure or an
    absent ``gh`` binary is never written here, so an unreliable reading can
    never latch into a persisted false negative -- see
    ``cw.dispatch.pr_gate.resolve_stale_pr_ticket_ids``.
    """

    probed_at: datetime
    has_open_pr: bool


def _executor_blocked_key(client: str, ticket_id: str) -> str:
    """Composite sidecar key for one (client, ticket) marker.

    Mirrors the ``f"{client}/{lane}"`` composite-key convention already used
    for ``ClientConcurrencyOverride.lanes``.
    """
    return f"{client}/{ticket_id}"


def _open_pr_probe_key(client: str, ticket_id: str) -> str:
    """Composite sidecar key for one (client, ticket) open-PR probe entry.

    Same ``f"{client}/{ticket_id}"`` shape as :func:`_executor_blocked_key`,
    kept as its own function rather than shared: the two sidecar keys index
    independent dicts and are free to diverge without one silently reshaping
    the other's on-disk layout.
    """
    return f"{client}/{ticket_id}"


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


def _parse_executor_blocked_entry(entry: object) -> ExecutorBlockedMarker | None:
    """Validate one raw ``"executor_blocked"`` entry, or None when malformed.

    Per-entry rather than all-or-nothing (unlike ``load_main_drift_latches``):
    several concurrent reviews share this key, so one bad entry must not
    blind the reader to every live marker beside it.
    """
    if not isinstance(entry, dict):
        return None
    client = entry.get("client")
    ticket_id = entry.get("ticket_id")
    executor = entry.get("executor")
    reviewer_role = entry.get("reviewer_role")
    started_at = entry.get("started_at")
    session_id = entry.get("session_id")
    if (
        not isinstance(client, str)
        or not isinstance(ticket_id, str)
        or not isinstance(executor, str)
        or not isinstance(session_id, str)
        or not isinstance(started_at, str)
        or not (reviewer_role is None or isinstance(reviewer_role, str))
    ):
        return None
    try:
        parsed_started_at = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return ExecutorBlockedMarker(
        client=client,
        ticket_id=ticket_id,
        executor=executor,
        reviewer_role=reviewer_role,
        started_at=parsed_started_at,
        session_id=session_id,
    )


def load_executor_blocked_markers() -> dict[str, ExecutorBlockedMarker]:
    """Load every in-flight executor-blocked marker (#1742).

    Returns ``{}`` when the file is absent, unreadable, malformed, or missing
    the ``"executor_blocked"`` key. Individual malformed entries are dropped
    rather than raised on or poisoning their well-formed siblings.
    """
    path = DISPATCH_STATE_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("executor_blocked")
    if not isinstance(entries, dict):
        return {}
    markers: dict[str, ExecutorBlockedMarker] = {}
    for key, entry in entries.items():
        marker = _parse_executor_blocked_entry(entry)
        if marker is not None:
            markers[key] = marker
    return markers


def save_executor_blocked_marker(marker: ExecutorBlockedMarker) -> None:
    """Record one in-flight executor-blocked marker (#1742).

    Read-merge-writes the shared sidecar so the other keys are preserved
    rather than clobbered (#1157), and merges into the existing
    ``"executor_blocked"`` dict so concurrent reviews on other clients keep
    their own entries. Silently swallows write errors — a failed persist just
    means ``cw dev-queue status`` reports ``[STALE]`` instead of ``[BLOCKED]``
    for this review (acceptable degradation: legibility, not correctness).
    """
    try:
        refuse_real_state_write(DISPATCH_STATE_FILE)
        DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            entries = payload.get("executor_blocked")
            if not isinstance(entries, dict):
                entries = {}
            entries[_executor_blocked_key(marker.client, marker.ticket_id)] = {
                "client": marker.client,
                "ticket_id": marker.ticket_id,
                "executor": marker.executor,
                "reviewer_role": marker.reviewer_role,
                "started_at": marker.started_at.isoformat(),
                "session_id": marker.session_id,
            }
            payload["executor_blocked"] = entries
            atomic_write_text(DISPATCH_STATE_FILE, json.dumps(payload))
    except OSError:
        logger.warning(
            "dispatch_state: failed to persist executor_blocked marker (%s/%s)",
            marker.client,
            marker.ticket_id,
            exc_info=True,
        )


def clear_executor_blocked_marker(client: str, ticket_id: str) -> None:
    """Drop one in-flight executor-blocked marker; no-op when absent (#1742).

    Called from a ``finally:`` on the review path, so it must never raise:
    a swallowed failure leaves a stale marker that the next process boot's
    ``clear_all_executor_blocked_markers()`` sweeps up anyway.
    """
    try:
        refuse_real_state_write(DISPATCH_STATE_FILE)
        DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            entries = payload.get("executor_blocked")
            if not isinstance(entries, dict):
                return
            entries.pop(_executor_blocked_key(client, ticket_id), None)
            payload["executor_blocked"] = entries
            atomic_write_text(DISPATCH_STATE_FILE, json.dumps(payload))
    except OSError:
        logger.warning(
            "dispatch_state: failed to clear executor_blocked marker (%s/%s)",
            client,
            ticket_id,
            exc_info=True,
        )


def clear_all_executor_blocked_markers() -> None:
    """Wipe every executor-blocked marker — dispatch-loop boot only (#1742).

    Any marker still on disk at process start is orphaned by construction:
    the review that wrote it ran on a daemon thread that died with the prior
    process. This is the crash-durability story for a deliberately
    non-durable marker, and the reason it carries no schema version.
    """
    try:
        refuse_real_state_write(DISPATCH_STATE_FILE)
        DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            payload["executor_blocked"] = {}
            atomic_write_text(DISPATCH_STATE_FILE, json.dumps(payload))
    except OSError:
        logger.warning(
            "dispatch_state: failed to clear executor_blocked markers at boot",
            exc_info=True,
        )


def _parse_open_pr_probe_entry(entry: object) -> OpenPrProbeCache | None:
    """Validate one raw ``"open_pr_probe"`` entry, or None when malformed.

    Per-entry rather than all-or-nothing, on the same reasoning as
    :func:`_parse_executor_blocked_entry`: many tickets share this key, so one
    bad entry must not blind the reader to every valid sibling beside it (which
    would silently re-probe the whole queue on every tick).
    """
    if not isinstance(entry, dict):
        return None
    probed_at = entry.get("probed_at")
    has_open_pr = entry.get("has_open_pr")
    if not isinstance(probed_at, str) or not isinstance(has_open_pr, bool):
        return None
    try:
        parsed_probed_at = datetime.fromisoformat(probed_at)
    except ValueError:
        return None
    return OpenPrProbeCache(probed_at=parsed_probed_at, has_open_pr=has_open_pr)


def load_open_pr_probe_cache() -> dict[str, OpenPrProbeCache]:
    """Load every per-ticket open-PR probe entry (#1862).

    Returns ``{}`` when the file is absent, unreadable, malformed, or missing
    the ``"open_pr_probe"`` key. Individual malformed entries are dropped
    rather than raised on or poisoning their well-formed siblings.

    TTL expiry is deliberately NOT applied here (unlike
    :func:`load_usage_limited_until`, which drops a lapsed deadline): the TTL
    window is a caller-side tuning knob, and the caller
    (``cw.dispatch.pr_gate``) accepts a ``ttl_seconds`` override, so this
    reader stays a plain deserializer.
    """
    path = DISPATCH_STATE_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("open_pr_probe")
    if not isinstance(entries, dict):
        return {}
    cache: dict[str, OpenPrProbeCache] = {}
    for key, entry in entries.items():
        parsed = _parse_open_pr_probe_entry(entry)
        if parsed is not None:
            cache[key] = parsed
    return cache


def _write_open_pr_probe_entries(keyed_entries: dict[str, OpenPrProbeCache]) -> None:
    """Read-merge-write one or more ``"open_pr_probe"`` entries in a single lock.

    Shared by :func:`save_open_pr_probe_entry` (one entry) and
    :func:`save_open_pr_probe_entries` (a batch) so a caller probing several
    tickets in one pass pays one lock-acquire + full-file read + full-file
    write instead of one per ticket (#1862 perf follow-up). Silently swallows
    write errors -- a failed persist just means the next tick re-probes these
    tickets (acceptable degradation: extra ``gh pr list`` calls, never a wrong
    gating decision).
    """
    if not keyed_entries:
        return
    try:
        refuse_real_state_write(DISPATCH_STATE_FILE)
        DISPATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with dispatch_state_lock():
            payload = _load_dispatch_state_raw()
            entries = payload.get("open_pr_probe")
            if not isinstance(entries, dict):
                entries = {}
            for key, entry in keyed_entries.items():
                entries[key] = {
                    "probed_at": entry.probed_at.isoformat(),
                    "has_open_pr": entry.has_open_pr,
                }
            payload["open_pr_probe"] = entries
            atomic_write_text(DISPATCH_STATE_FILE, json.dumps(payload))
    except OSError:
        logger.warning(
            "dispatch_state: failed to persist %d open_pr_probe entry/entries (%s)",
            len(keyed_entries),
            sorted(keyed_entries),
            exc_info=True,
        )


def save_open_pr_probe_entry(
    client: str, ticket_id: str, entry: OpenPrProbeCache
) -> None:
    """Record one per-ticket open-PR probe result (#1862).

    Read-merge-writes the shared sidecar so the other keys are preserved rather
    than clobbered (#1157), and merges into the existing ``"open_pr_probe"``
    dict so concurrently-probed tickets keep their own entries.
    """
    _write_open_pr_probe_entries({_open_pr_probe_key(client, ticket_id): entry})


def save_open_pr_probe_entries(
    client: str, entries_by_ticket_id: dict[str, OpenPrProbeCache]
) -> None:
    """Record several per-ticket open-PR probe results for *client* in one write.

    Batched sibling of :func:`save_open_pr_probe_entry` (#1862 perf follow-up):
    a single tick can newly-probe many candidates, and persisting each with its
    own lock-acquire + full-file read + full-file write scales lock contention
    and I/O linearly with backlog size for no benefit -- the whole point of the
    read-merge-write pattern is that one lock window can absorb any number of
    key changes. No-op when *entries_by_ticket_id* is empty (never acquires the
    lock for a call with nothing to persist).
    """
    _write_open_pr_probe_entries(
        {
            _open_pr_probe_key(client, ticket_id): entry
            for ticket_id, entry in entries_by_ticket_id.items()
        }
    )
