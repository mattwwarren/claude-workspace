"""Digest-keyed snapshot store: ``state_dir()/ticket_snapshots/<client>/<digest>.json``.

Content-addressed and lock-free: a write of identical bytes is a no-op, and a
file is never rewritten. The read path checks, in order, that the bytes hash
to the row digest, that the file's ``schema_version`` is not newer than this
cw, that the content parses and validates, and that its ``ticket_id`` and
``client`` match the row. Any failure raises :class:`SnapshotUntrustedError`
with a typed ``reason``; the file is never repaired or rewritten.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from cw.atomic import atomic_write_text
from cw.config import refuse_real_state_write, state_dir
from cw.ticket_snapshot._models import (
    _DIGEST_RE,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotUntrustedError,
    TicketSnapshot,
    canonical_json,
    check_client,
    check_digest,
    digest_of,
    enforce_caps,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask

SNAPSHOT_DIR_NAME = "ticket_snapshots"
DIR_MODE = 0o700
FILE_MODE = 0o600


def snapshot_path(client: str, digest: str) -> Path:
    """The store path, composed at call time so a test's state-dir swap moves it."""
    return (
        state_dir()
        / SNAPSHOT_DIR_NAME
        / check_client(client)
        / f"{check_digest(digest)}.json"
    )


def _ensure_dir(directory: Path) -> None:
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    directory.chmod(DIR_MODE)


def write_snapshot(snapshot: TicketSnapshot) -> str:
    """Write *snapshot* content-addressed and return its digest. Takes no lock."""
    enforce_caps(snapshot)
    text = canonical_json(snapshot)
    digest = digest_of(text)
    path = snapshot_path(snapshot.client, digest)
    refuse_real_state_write(path)
    _ensure_dir(path.parent.parent)
    _ensure_dir(path.parent)
    if not path.exists():
        atomic_write_text(path, text)
    path.chmod(FILE_MODE)
    return digest


def read(task: TicketTask) -> TicketSnapshot | None:
    """Return the row's stored snapshot, or None when the row has no digest.

    Raises :class:`SnapshotUntrustedError` (``missing``, ``mismatch``,
    ``invalid`` or ``too_new``). Any other OSError propagates raw; the spawn
    seam classifies it as ``render_error``.
    """
    digest = task.snapshot_digest
    if digest is None:
        return None
    if not _DIGEST_RE.match(digest):
        msg = "row snapshot digest is not a sha256 hex digest"
        raise SnapshotUntrustedError(msg, reason="invalid")
    path = snapshot_path(task.client, digest)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        msg = "snapshot file is missing"
        raise SnapshotUntrustedError(msg, reason="missing") from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        msg = "snapshot bytes do not hash to the row digest"
        raise SnapshotUntrustedError(msg, reason="mismatch")
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        msg = "snapshot is not valid UTF-8"
        raise SnapshotUntrustedError(msg, reason="invalid") from exc
    except ValueError as exc:
        msg = "snapshot is not valid JSON"
        raise SnapshotUntrustedError(msg, reason="invalid") from exc
    if not isinstance(data, dict):
        msg = "snapshot is not a JSON object"
        raise SnapshotUntrustedError(msg, reason="invalid")
    version = data.get("schema_version")
    if isinstance(version, int) and version > SNAPSHOT_SCHEMA_VERSION:
        msg = "snapshot schema_version is newer than this cw reads"
        raise SnapshotUntrustedError(msg, reason="too_new")
    try:
        snapshot = TicketSnapshot.model_validate(data)
    except ValidationError as exc:
        msg = "snapshot failed validation"
        raise SnapshotUntrustedError(msg, reason="invalid") from exc
    if snapshot.ticket_id != task.ticket_id or snapshot.client != task.client:
        msg = "snapshot ticket_id or client does not match the row"
        raise SnapshotUntrustedError(msg, reason="mismatch")
    return snapshot
