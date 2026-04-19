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
import os
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically via a unique temp file + ``os.replace``.

    The temp file is created with ``mkstemp`` in the same directory as
    *path* so the final rename stays on one filesystem. A unique temp
    name per call is required because concurrent writers (possible when
    the outer lock is advisory or absent) would otherwise race on a
    shared temp name and one ``os.replace`` would fail with ``ENOENT``.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        # Remove the temp file if the rename didn't consume it.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
