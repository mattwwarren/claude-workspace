"""The per-client drain lease and the drain's two sidecar files (#3706).

``drain_lease`` is a long-held lease on ``outbox/<client>/drain.lock``: a
drain holds it for its whole run, across ``gh`` calls, so it is deliberately
NOT the rank-LEAF ``outbox`` lock (which guards only short record writes) and
sits outside ``lock_guard``. It is taken with one non-blocking
``try_flock_until`` attempt and fails fast with ``DrainLeaseHeldError``; it
cannot hang (ADR-0019, "Unguarded by design"). ``drain_lease_held`` is the
dispatch tick's non-blocking probe; it never creates a file.

The sidecars carry no timestamp (ADR-0014): no age or wall clock drives any
decision.

- ``drain.unreachable.<role>`` (``UnreachableMarker``): a binding-level fault
  episode for one role. A corrupt or unreadable marker counts as set.
- ``drain.held`` (``HeldList``): entries held after a per-item failure. A
  missing, corrupt or unreadable file reads as empty; the next hold rewrites
  it.
- ``drain.progress``: a monotonic count of entries that left ``pending`` under
  a drain (settled, or skipped as unreadable). The tick compares it across
  launches, because its bounded record scan cannot see progress made outside
  its window. A missing, corrupt or unreadable file reads as 0.

The drain is the only writer, under the lease; the tick reads lock-free.
Imports ``_layout`` and ``_models``; no subprocess, no tracker code.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
from typing import TYPE_CHECKING

from cw._flock import try_flock_until
from cw.outbox._layout import (
    _write,
    client_dir,
    drain_lock_path,
    ensure_client_dir,
    held_path,
    progress_path,
    unreachable_path,
)
from cw.outbox._models import (
    DrainLeaseHeldError,
    HeldList,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    UnreachableMarker,
    check_client,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def drain_lease(client: str) -> Iterator[None]:
    """Hold *client*'s drain lease for the block, or raise ``DrainLeaseHeldError``.

    Unguarded by design (see the module docstring and ADR-0019).
    """
    check_client(client)
    ensure_client_dir(client)
    path = drain_lock_path(client)
    try:
        handle = path.open("a", encoding="utf-8")
    except OSError as exc:
        msg = f"cannot open outbox drain lock {path.name}"
        raise OutboxRecordError(msg) from exc
    with handle:
        if not try_flock_until(handle, timeout_s=0, poll_interval_s=0.0):
            msg = f"another outbox drain of client {client} is running"
            raise DrainLeaseHeldError(msg)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


def drain_lease_held(client: str) -> bool:
    """Whether another process holds *client*'s drain lease. Creates nothing.

    ``False`` when the lock file is absent. The probe briefly takes the lock
    itself, so a drain starting at that instant sees it held and exits 75; the
    tick's relaunch bound covers that.
    """
    path = drain_lock_path(client)
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def read_marker(client: str, role: OutboxRole) -> UnreachableMarker | None:
    """*role*'s unreachable marker, or ``None`` when unset.

    A corrupt or unreadable marker counts as set: it reads as a fresh episode
    (``transient_failure``, nothing announced).
    """
    path = unreachable_path(client, role)
    # A ValueError is a corrupt file: invalid UTF-8 (UnicodeDecodeError) or
    # invalid JSON / shape (pydantic's ValidationError).
    try:
        text = _read_text(path)
        if text is None:
            return None
        marker = UnreachableMarker.model_validate_json(text)
    except (OSError, ValueError):
        logger.warning("outbox: unreadable drain marker %s counts as set", path.name)
        return UnreachableMarker(role=role, reason=OutboxReason.TRANSIENT_FAILURE)
    if marker.role is not role:
        return UnreachableMarker(role=role, reason=marker.reason)
    return marker


def write_marker(client: str, marker: UnreachableMarker) -> None:
    ensure_client_dir(client)
    _write(unreachable_path(client, marker.role), marker.model_dump_json())


def marked_roles(client: str) -> frozenset[OutboxRole]:
    """Every role whose marker file exists (a corrupt one included)."""
    return frozenset(
        role for role in OutboxRole if unreachable_path(client, role).exists()
    )


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        msg = f"cannot remove outbox drain file {path.name}"
        raise OutboxRecordError(msg) from exc


def clear_marker(client: str, role: OutboxRole) -> None:
    _unlink(unreachable_path(client, role))


def clear_all_markers(client: str) -> None:
    if not client_dir(client).is_dir():
        return
    for role in OutboxRole:
        clear_marker(client, role)


def read_held(client: str) -> frozenset[str]:
    """The held entry ids; a missing, corrupt or unreadable file reads empty."""
    path = held_path(client)
    try:
        text = _read_text(path)
        if text is None:
            return frozenset()
        return frozenset(HeldList.model_validate_json(text).entry_ids)
    except (OSError, ValueError):
        logger.warning("outbox: unreadable held list %s reads as empty", path.name)
        return frozenset()


def append_held(client: str, entry_id: str) -> None:
    """Add *entry_id* to the held list (idempotent), in one atomic write."""
    ensure_client_dir(client)
    path = held_path(client)
    try:
        text = _read_text(path)
        current = [] if text is None else HeldList.model_validate_json(text).entry_ids
    except (OSError, ValueError):
        current = []
    if entry_id in current:
        return
    _write(path, HeldList(entry_ids=[*current, entry_id]).model_dump_json())


def clear_held(client: str) -> None:
    _unlink(held_path(client))


def read_progress(client: str) -> int:
    """How many entries have left ``pending`` under a drain; 0 when unreadable."""
    path = progress_path(client)
    try:
        text = _read_text(path)
        return 0 if text is None else max(0, int(text.strip()))
    except (OSError, ValueError):
        logger.warning("outbox: unreadable drain progress %s reads as 0", path.name)
        return 0


def add_progress(client: str, count: int) -> None:
    """Add *count* to the progress counter (the drain, under its lease, only)."""
    if count <= 0:
        return
    ensure_client_dir(client)
    _write(progress_path(client), str(read_progress(client) + count))
