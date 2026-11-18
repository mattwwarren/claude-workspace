"""Ticket snapshot store (RFC 0019 T1, GitHub #3711).

A digest-keyed, content-addressed copy of a ticket as the tracker reported it
at intake, kept under ``state_dir()/ticket_snapshots/<client>/<digest>.json``.
Dispatch re-renders ``.cw/context.json`` from it on every spawn whose row has a
digest. T1 adds the store, the write paths and the render; no binding reader
consumes the store yet (T2).

Submodule DAG (patch the submodule, not this package):

- ``_models``: models, caps, digest, failure vocabulary (no cw I/O).
- ``_store``: content-addressed read and write (uses ``_models``).
- ``_render``: ``.cw/context.json`` payload and write (uses ``_store``).
- ``_fetch``: GitHub fetch and stdin document into a snapshot (uses ``_models``).
- ``_record``: store write, event and row stamp under ``dev_queue_lock``.

Threat model and trust: see ``_models``. Nothing here evaluates trust.
"""

from cw.ticket_snapshot._fetch import (
    StdinComment,
    StdinDocument,
    snapshot_from_github,
    snapshot_from_stdin,
)
from cw.ticket_snapshot._models import (
    MAX_COMMENT_BODY_BYTES,
    MAX_COMMENTS,
    MAX_SNAPSHOT_BYTES,
    SNAPSHOT_SCHEMA_VERSION,
    RenderFailureCause,
    SnapshotComment,
    SnapshotError,
    SnapshotFailureCause,
    SnapshotRefusedError,
    SnapshotUntrustedError,
    TicketSnapshot,
    normalize_ticket_id,
)
from cw.ticket_snapshot._record import (
    SnapshotRecord,
    record_snapshot,
    record_snapshot_from_github,
)
from cw.ticket_snapshot._render import (
    render_context,
    render_for_task,
    write_context_json,
)
from cw.ticket_snapshot._store import read, snapshot_path, write_snapshot

__all__ = [
    "MAX_COMMENTS",
    "MAX_COMMENT_BODY_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "SNAPSHOT_SCHEMA_VERSION",
    "RenderFailureCause",
    "SnapshotComment",
    "SnapshotError",
    "SnapshotFailureCause",
    "SnapshotRecord",
    "SnapshotRefusedError",
    "SnapshotUntrustedError",
    "StdinComment",
    "StdinDocument",
    "TicketSnapshot",
    "normalize_ticket_id",
    "read",
    "record_snapshot",
    "record_snapshot_from_github",
    "render_context",
    "render_for_task",
    "snapshot_from_github",
    "snapshot_from_stdin",
    "snapshot_path",
    "write_context_json",
    "write_snapshot",
]
