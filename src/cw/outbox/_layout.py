"""Outbox on-disk layout: paths, directory creation, atomic record and body IO.

``state_dir() / "outbox" / <client>`` holds one ``<id>.body`` and one
``<id>.json`` per entry plus the ``.outbox.lock`` file. The path is composed at
call time, never bound at module level, so the autouse ``tmp_config_dir``
redirects it. The record is the commit point: a body without a record is an
orphan and is ignored.

``atomic_write_text`` does not fsync, on purpose (R22): the store is
mirror-only and ``claim()`` verifies the body's sha256, so a torn or missing
body remains retryable rather than becoming a bad post or a terminal state.

Imports ``_models``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.atomic import atomic_write_text
from cw.config import refuse_real_state_write, state_dir
from cw.outbox._models import (
    OUTBOX_SCHEMA_VERSION,
    OutboxEntry,
    OutboxRecordError,
    OutboxSchemaError,
    check_client,
    check_entry_id,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# R23: the outbox holds prose that may quote ticket context, so it is private
# to the operator account. ``mkstemp`` already yields 0600 files.
DIR_MODE = 0o700
FILE_MODE = 0o600

_OUTBOX_DIRNAME = "outbox"
_LOCK_NAME = ".outbox.lock"
_AUDIT_NAME = ".outbox.audit.jsonl"
_RECORD_SUFFIX = ".json"
_BODY_SUFFIX = ".body"


def outbox_root() -> Path:
    return state_dir() / _OUTBOX_DIRNAME


def client_dir(client: str) -> Path:
    return outbox_root() / check_client(client)


def lock_path(client: str) -> Path:
    return client_dir(client) / _LOCK_NAME


def audit_path(client: str) -> Path:
    return client_dir(client) / _AUDIT_NAME


def record_path(client: str, entry_id: str) -> Path:
    return client_dir(client) / f"{check_entry_id(entry_id)}{_RECORD_SUFFIX}"


def body_path(client: str, entry_id: str) -> Path:
    return client_dir(client) / f"{check_entry_id(entry_id)}{_BODY_SUFFIX}"


def record_exists(client: str, entry_id: str) -> bool:
    return record_path(client, entry_id).is_file()


def ensure_client_dir(client: str) -> Path:
    """Create ``outbox/`` and ``outbox/<client>/`` 0700 and return the latter."""
    directory = client_dir(client)
    root = directory.parent
    refuse_real_state_write(directory)
    try:
        root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        directory.mkdir(mode=DIR_MODE, exist_ok=True)
        # ``mkdir`` honours the umask and skips an existing directory.
        root.chmod(DIR_MODE)
        directory.chmod(DIR_MODE)
    except OSError as exc:
        msg = f"cannot prepare outbox directory {directory}"
        raise OutboxRecordError(msg) from exc
    return directory


def _write(path: Path, text: str) -> None:
    refuse_real_state_write(path)
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        msg = f"cannot write outbox file {path.name}"
        raise OutboxRecordError(msg) from exc


def write_body(client: str, entry_id: str, text: str) -> None:
    _write(body_path(client, entry_id), text)


def write_record(client: str, entry: OutboxEntry) -> None:
    _write(record_path(client, entry.id), entry.model_dump_json())


def read_body_bytes(client: str, entry_id: str) -> bytes:
    """The raw body bytes. Raises the raw ``OSError``; the caller classifies it."""
    return body_path(client, entry_id).read_bytes()


def _parse_record(path: Path, client: str) -> OutboxEntry:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        msg = f"cannot read outbox record {path.name}"
        raise OutboxRecordError(msg) from exc
    if not isinstance(data, dict):
        msg = f"outbox record {path.name} is not a JSON object"
        raise OutboxRecordError(msg)
    if "schema_version" in data and type(data["schema_version"]) is not int:
        msg = f"outbox record {path.name} has a non-integer schema_version"
        raise OutboxRecordError(msg)
    version = data.get("schema_version")
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version > OUTBOX_SCHEMA_VERSION
    ):
        # Checked before ``extra="forbid"`` validation so a newer record is
        # reported as such, and it is never rewritten (R24).
        msg = f"outbox record {path.name} has a newer schema_version than this cw"
        raise OutboxSchemaError(msg)
    try:
        entry = OutboxEntry.model_validate(data)
    except ValidationError as exc:
        msg = f"outbox record {path.name} is invalid"
        raise OutboxRecordError(msg) from exc
    if entry.id != path.stem:
        msg = f"outbox record {path.name} does not match its entry id"
        raise OutboxRecordError(msg)
    if entry.client != client:
        msg = f"outbox record {path.name} belongs to another client"
        raise OutboxRecordError(msg)
    return entry


def read_record(client: str, entry_id: str) -> OutboxEntry:
    """Read one record. Raises on a missing, corrupt or too-new record."""
    return _parse_record(record_path(client, entry_id), client)


def append_audit_event(client: str, event: dict[str, object]) -> None:
    """Durably append one transition event before its record replacement."""
    path = audit_path(client)
    refuse_real_state_write(path)
    fd = -1
    try:
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            fd = -1
            stream.write(json.dumps(event, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(FILE_MODE)
    except OSError as exc:
        msg = f"cannot append outbox audit event {path.name}"
        raise OutboxRecordError(msg) from exc
    finally:
        if fd != -1:
            os.close(fd)


def scan_entries(client: str) -> list[OutboxEntry]:
    """Every readable record of *client*, unordered.

    Pure read, lock-free, and it never creates the client directory. Orphan
    bodies and ``*.tmp`` leftovers do not match ``*.json`` and are ignored. A
    corrupt or too-new record is skipped with a WARNING and never rewritten.
    """
    directory = client_dir(client)
    if not directory.is_dir():
        return []
    entries: list[OutboxEntry] = []
    for path in sorted(directory.glob(f"*{_RECORD_SUFFIX}")):
        try:
            entries.append(_parse_record(path, client))
        except (OutboxRecordError, OutboxSchemaError) as exc:
            logger.warning(
                "skipping outbox record %s (%s)", path.name, type(exc).__name__
            )
    return entries


def list_client_names() -> list[str]:
    """Every client directory present on disk, sorted."""
    root = outbox_root()
    if not root.is_dir():
        return []
    names: list[str] = []
    for path in root.iterdir():
        try:
            check_client(path.name)
        except ValueError:
            continue
        if path.is_dir():
            names.append(path.name)
    return sorted(names)
