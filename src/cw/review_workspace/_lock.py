"""The exclusive workspace lock.

``_workspace_lock`` nests a non-blocking ``fcntl.flock`` inside
``lock_guard("review_workspace", ...)``; the lock name stays the literal the
flat module used. Imports ``_constants`` and ``_primitives``.
"""

from __future__ import annotations

import contextlib
import fcntl
from typing import TYPE_CHECKING

from cw._lock_guard import LockRank, lock_guard
from cw.review_workspace._constants import _LOCK_PATH
from cw.review_workspace._primitives import _ensure_cw, _error, _kind

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextlib.contextmanager
def _workspace_lock(root: Path) -> Iterator[None]:
    _ensure_cw(root)
    lock_path = root / _LOCK_PATH
    with lock_guard("review_workspace", lock_path, LockRank.STATE):
        if _kind(lock_path) not in {"absent", "file"}:
            _error_message = f"review workspace lock is not a regular file: {lock_path}"
            raise _error(_error_message)
        try:
            with lock_path.open("a+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    _error_message = (
                        f"review workspace is locked by another process: {lock_path}"
                    )
                    raise _error(_error_message) from exc
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError as exc:
            _error_message = f"cannot lock review workspace {lock_path}: {exc}"
            raise _error(_error_message) from exc
