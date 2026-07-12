"""Atomic file writing utilities.

State files in ``cw`` are read without a lock (``load_dev_queue``,
``load_state``, etc.). Writers previously used ``Path.write_text``, which
opens with ``O_TRUNC`` and can expose an empty or partial file to any
concurrent reader. ``atomic_write_text`` writes to a sibling temp file
and atomically renames it into place so readers always observe either
the prior complete file or the new complete file.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_BACKUP_KEEP = 5


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically via a unique temp file + ``Path.replace``.

    The temp file is created with ``mkstemp`` in the same directory as
    *path* so the final rename stays on one filesystem. A unique temp
    name per call is required because concurrent writers (possible when
    the outer lock is advisory or absent) would otherwise race on a
    shared temp name and one ``Path.replace`` would fail with ``ENOENT``.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp_name).replace(path)
    except BaseException:
        # Remove the temp file if the rename didn't consume it.
        with contextlib.suppress(FileNotFoundError):
            Path(tmp_name).unlink()
        raise


def rotate_backup(path: Path, *, keep: int = _DEFAULT_BACKUP_KEEP) -> None:
    """Snapshot *path* to a timestamped backup sibling before it is overwritten.

    No-op if *path* doesn't exist yet (nothing to back up on the first write).
    Backups are named ``<name>.bak-<time_ns>`` so concurrent writers never
    collide. Keeps only the *keep* most recent snapshots (by mtime); older
    ones are pruned. Best-effort: any OSError during snapshot or prune is
    logged and swallowed — a failed backup must never block the primary
    write. See GitHub #1017 (dev_queue.json write-ahead backup rotation).
    """
    if not path.exists():
        return
    backup = path.parent / f"{path.name}.bak-{time.time_ns()}"
    try:
        shutil.copy2(path, backup)
    except OSError:
        logger.warning("rotate_backup: failed to snapshot %s", path)
        return
    existing = sorted(
        path.parent.glob(f"{path.name}.bak-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in existing[keep:]:
        with contextlib.suppress(OSError):
            stale.unlink()
