"""One-time backup of a legacy-schema sessions.json, taken at save time (#2951).

The read path (``cw.config.load_state``) must never write. The backup of the
pre-migration bytes therefore lives here and is called from
``cw.config.save_state`` immediately before the first overwrite. The decision
comes from the **on-disk** file, not from process state, so it holds for any
writer regardless of whether it loaded first.

Callers must hold ``sessions_lock`` and must run ``refuse_real_state_write``
before calling ``backup_legacy_state``; the backup is a sibling of the state
file, so the real-state guard covers it.

The on-disk ``schema_version`` is read with an anchored head-peek. Every
cw-written file starts ``{`` then ``"schema_version"`` because it is the first
field of ``CwState``, so the peek decides without a full parse on the hot path.
Anything the peek cannot decide falls back to the full-parse semantics that
``load_state`` always used.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
from typing import TYPE_CHECKING

from cw.models import CW_STATE_SCHEMA_VERSION

if TYPE_CHECKING:
    from pathlib import Path

_PEEK_BYTES = 256
_SCHEMA_RE = re.compile(rb'\s*\{\s*"schema_version"\s*:\s*(\d+)\s*[,}]')


def _parse_schema_version(path: Path) -> int | None:
    """Full-parse fallback. None means the file is not a legacy candidate."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return int(raw.get("schema_version") or 0)
    except (TypeError, ValueError):
        return None


def on_disk_schema_version(path: Path) -> int | None:
    """Return the schema version recorded in the file at ``path``.

    None when the file is absent, unparsable, not a JSON object, or carries a
    non-numeric ``schema_version``. A missing or null key reads as 0.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(_PEEK_BYTES)
    except FileNotFoundError:
        return None
    match = _SCHEMA_RE.match(head)
    if match is not None:
        return int(match.group(1))
    return _parse_schema_version(path)


def backup_legacy_state(path: Path) -> None:
    """Copy the pre-migration ``sessions.json`` to its backup, at most once.

    No-op when the backup already exists, the file is absent, or the on-disk
    schema is current or newer. The copy goes to a temp sibling first and is
    moved into place with ``os.replace``, so the final name only ever holds a
    complete copy. Any failure removes the temp and propagates the ``OSError``,
    so the caller never overwrites the legacy bytes without a backup. A stale
    temp from a killed run is overwritten by the next ``copy2``; callers hold
    ``sessions_lock``, which keeps the fixed temp name single-writer.
    """
    backup = path.parent / f".{path.name}.0.x-backup"
    if backup.exists():
        return
    version = on_disk_schema_version(path)
    if version is None or version >= CW_STATE_SCHEMA_VERSION:
        return
    tmp = backup.with_name(backup.name + ".tmp")
    try:
        shutil.copy2(path, tmp)
        tmp.replace(backup)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
