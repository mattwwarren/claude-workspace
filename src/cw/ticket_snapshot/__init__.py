"""Ticket snapshot store (RFC 0019 T1, GitHub #3711).

A digest-keyed, content-addressed copy of a ticket as the tracker reported it
at intake, kept under ``state_dir()/ticket_snapshots/<client>/<digest>.json``.
Dispatch re-renders ``.cw/context.json`` from it on every spawn whose row has a
digest. T1 adds the store, the write paths and the render; T2 (#3712) adds
the trust evaluator, the stage-entry refresh and the plan-of-record recovery.

Submodule DAG (patch the submodule, not this package):

- ``_models``: models, caps, digest, failure vocabulary (no cw I/O).
- ``_store``: content-addressed read and write (uses ``_models``).
- ``_render``: ``.cw/context.json`` payload and write (uses ``_store``).
- ``_fetch``: GitHub fetch and stdin document into a snapshot (uses ``_models``).
- ``_record``: store write, author-conflict flag, event and row stamp under
  ``dev_queue_lock``.
- ``_refresh``: stage-entry refresh and current-row fallback (uses ``_record``).
- ``_binding``: the one trust evaluator and ``SnapshotContext`` (uses
  ``_refresh``).
- ``_plan``: plan-of-record recovery (uses ``_binding``). NOT re-exported here:
  it imports ``cw.codex_plan._post``, whose package imports this one.

Threat model: see ``_models``. Trust: see ``_binding``.
"""

from cw.ticket_snapshot._binding import (
    DEFAULT_SNAPSHOT_CONTEXT,
    Purpose,
    SnapshotContext,
    bound_comments,
    context_for_client,
)
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
    digest_or_none,
    normalize_ticket_id,
)
from cw.ticket_snapshot._record import (
    SnapshotRecord,
    flag_author_conflicts,
    record_snapshot,
    record_snapshot_from_github,
)
from cw.ticket_snapshot._refresh import (
    REFRESH_REASONS,
    RefreshError,
    RefreshReason,
    read_current,
    read_for_stage,
    refresh_snapshot,
    snapshot_for_stage,
)
from cw.ticket_snapshot._render import (
    render_context,
    render_for_task,
    write_context_json,
)
from cw.ticket_snapshot._store import read, snapshot_path, write_snapshot

__all__ = [
    "DEFAULT_SNAPSHOT_CONTEXT",
    "MAX_COMMENTS",
    "MAX_COMMENT_BODY_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "REFRESH_REASONS",
    "SNAPSHOT_SCHEMA_VERSION",
    "Purpose",
    "RefreshError",
    "RefreshReason",
    "RenderFailureCause",
    "SnapshotComment",
    "SnapshotContext",
    "SnapshotError",
    "SnapshotFailureCause",
    "SnapshotRecord",
    "SnapshotRefusedError",
    "SnapshotUntrustedError",
    "StdinComment",
    "StdinDocument",
    "TicketSnapshot",
    "bound_comments",
    "context_for_client",
    "digest_or_none",
    "flag_author_conflicts",
    "normalize_ticket_id",
    "read",
    "read_current",
    "read_for_stage",
    "record_snapshot",
    "record_snapshot_from_github",
    "refresh_snapshot",
    "render_context",
    "render_for_task",
    "snapshot_for_stage",
    "snapshot_from_github",
    "snapshot_from_stdin",
    "snapshot_path",
    "write_context_json",
    "write_snapshot",
]
