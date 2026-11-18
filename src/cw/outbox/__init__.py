"""The per-client outbox store for prose posts made with no agent present.

RFC 0019 O1 (#3705). An entry is a prose post waiting to reach a tracker: a body
file and a JSON record under ``state_dir()/outbox/<client>/``, keyed by the
idempotency key ``(role, ticket, marker)``. This package is the store only: no
drain, no producers, no events. ``cw outbox list`` is the one command.

Submodules, each importing its siblings by direct path and never through this
package:

- ``_models`` — enums, caps, errors, validators, the transition tables, the
  persisted ``OutboxEntry`` and the list models. Imports no sibling.
- ``_layout`` — paths, directory creation and atomic record and body IO.
  Imports ``_models``.
- ``_lock`` — ``_outbox_lock``, the leaf ``"outbox"`` lock. Imports ``_layout``
  and ``_models``.
- ``_store`` — ``enqueue``, ``claim``, ``settle`` and the reads. Imports every
  submodule above.

Tests that patch a name patch it on the submodule that calls it, not here: a
patch on this package rebinds only the re-export.
"""

from cw.outbox._models import (
    ALLOWED_TRANSITIONS,
    DEFAULT_CLAIM_LIMIT,
    MAX_BODY_BYTES,
    MAX_DETAIL_CHARS,
    MAX_MARKER_CHARS,
    MAX_PART_CHARS,
    MAX_PARTS,
    MAX_PRODUCER_CHARS,
    MAX_TICKET_CHARS,
    OUTBOX_LIST_SCHEMA_VERSION,
    OUTBOX_SCHEMA_VERSION,
    REASONS_BY_STATE,
    EnqueueResult,
    OutboxClaimError,
    OutboxEntry,
    OutboxError,
    OutboxHistoryFullError,
    OutboxListEnvelope,
    OutboxListRow,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    OutboxSchemaError,
    OutboxState,
    OutboxTransitionError,
    OutboxValidationError,
    make_entry_id,
    transition_allowed,
)
from cw.outbox._store import (
    claim,
    claimant_state,
    enqueue,
    list_clients,
    list_entries,
    read_parts,
    settle,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_CLAIM_LIMIT",
    "MAX_BODY_BYTES",
    "MAX_DETAIL_CHARS",
    "MAX_MARKER_CHARS",
    "MAX_PARTS",
    "MAX_PART_CHARS",
    "MAX_PRODUCER_CHARS",
    "MAX_TICKET_CHARS",
    "OUTBOX_LIST_SCHEMA_VERSION",
    "OUTBOX_SCHEMA_VERSION",
    "REASONS_BY_STATE",
    "EnqueueResult",
    "OutboxClaimError",
    "OutboxEntry",
    "OutboxError",
    "OutboxHistoryFullError",
    "OutboxListEnvelope",
    "OutboxListRow",
    "OutboxReason",
    "OutboxRecordError",
    "OutboxRole",
    "OutboxSchemaError",
    "OutboxState",
    "OutboxTransitionError",
    "OutboxValidationError",
    "claim",
    "claimant_state",
    "enqueue",
    "list_clients",
    "list_entries",
    "make_entry_id",
    "read_parts",
    "settle",
    "transition_allowed",
]
