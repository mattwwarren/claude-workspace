"""The per-client outbox lock.

``_outbox_lock`` is a leaf lock (ADR-0019): it wraps a plain exclusive
``fcntl.flock`` in ``lock_guard("outbox", ..., LockRank.LEAF)``, keyed per client
directory. It nests legally under ``sessions_lock`` and ``dev_queue_lock`` and
nothing may be acquired while it is held. It covers file IO and ``/proc`` or
psutil reads only: no subprocess, network or tracker call runs under it (R17).

Imports ``_layout`` and ``_models``.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from typing import TYPE_CHECKING

from cw._lock_guard import LockRank, lock_guard
from cw.outbox._layout import FILE_MODE, ensure_client_dir, lock_path
from cw.outbox._models import OutboxRecordError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _open_lock_fd(path: Path) -> int:
    try:
        return os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    except OSError as exc:
        msg = f"cannot open outbox lock file {path.name}"
        raise OutboxRecordError(msg) from exc


@contextlib.contextmanager
def _outbox_lock(client: str) -> Iterator[None]:
    """Hold the exclusive outbox lock of *client*. Rank LEAF; see ADR-0019."""
    path = lock_path(client)
    with lock_guard("outbox", path, LockRank.LEAF):
        ensure_client_dir(client)
        fd = _open_lock_fd(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
