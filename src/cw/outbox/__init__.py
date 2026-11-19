"""The per-client outbox store for prose posts made with no agent present.

RFC 0019 O1 (#3705). An entry is a prose post waiting to reach a tracker: a body
file and a JSON record under ``state_dir()/outbox/<client>/``, keyed by the
idempotency key ``(role, ticket, marker)``. This package is the store, the
per-client drain lease and the drain's sidecar files. It imports no subprocess
and no tracker code: the drain engine that posts through ``gh`` lives in the
sibling module :mod:`cw.outbox_drain` (RFC 0019 O2, #3706), and the dispatch
tick's launcher in :mod:`cw.dispatch.outbox_drain`.

Submodules, each importing its siblings by direct path and never through this
package:

- ``_models`` — enums, caps, errors, validators, the transition tables, the
  persisted ``OutboxEntry`` and the list models. Imports no sibling.
- ``_layout`` — paths, directory creation and atomic record and body IO.
  Imports ``_models``.
- ``_lock`` — ``_outbox_lock``, the leaf ``"outbox"`` lock. Imports ``_layout``
  and ``_models``.
- ``_store`` — ``enqueue``, ``claim``, ``settle``, ``release``, the reads and
  ``peek_pending``. Imports ``_layout``, ``_lock`` and ``_models``.
- ``_lease`` — ``drain_lease`` (the unguarded per-client drain lease),
  ``drain_lease_held``, and the unreachable-marker, held-list and
  progress-counter IO. Imports ``_layout`` and ``_models``.

Tests that patch a name patch it on the submodule that calls it, not here: a
patch on this package rebinds only the re-export.
"""

from cw.outbox._lease import (
    append_held,
    clear_all_markers,
    clear_held,
    clear_marker,
    drain_lease,
    drain_lease_held,
    marked_roles,
    read_held,
    read_marker,
    write_marker,
)
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
    RELEASE_REASONS,
    SETTLE_STATES,
    TICK_SCAN_MAX_RECORDS,
    VERDICT_IN_FLIGHT,
    VERDICT_OWNER_GONE,
    VERDICT_UNKNOWN,
    ClaimantVerdict,
    DrainLeaseHeldError,
    EnqueueResult,
    HeldList,
    OutboxClaimError,
    OutboxEntry,
    OutboxError,
    OutboxListEnvelope,
    OutboxListRow,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    OutboxSchemaError,
    OutboxState,
    OutboxTransition,
    OutboxTransitionError,
    OutboxValidationError,
    PendingPeek,
    UnreachableMarker,
    make_entry_id,
    transition_allowed,
)
from cw.outbox._store import (
    claim,
    claimant_state,
    enqueue,
    list_clients,
    list_entries,
    peek_pending,
    read_parts,
    release,
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
    "RELEASE_REASONS",
    "SETTLE_STATES",
    "TICK_SCAN_MAX_RECORDS",
    "VERDICT_IN_FLIGHT",
    "VERDICT_OWNER_GONE",
    "VERDICT_UNKNOWN",
    "ClaimantVerdict",
    "DrainLeaseHeldError",
    "EnqueueResult",
    "HeldList",
    "OutboxClaimError",
    "OutboxEntry",
    "OutboxError",
    "OutboxListEnvelope",
    "OutboxListRow",
    "OutboxReason",
    "OutboxRecordError",
    "OutboxRole",
    "OutboxSchemaError",
    "OutboxState",
    "OutboxTransition",
    "OutboxTransitionError",
    "OutboxValidationError",
    "PendingPeek",
    "UnreachableMarker",
    "append_held",
    "claim",
    "claimant_state",
    "clear_all_markers",
    "clear_held",
    "clear_marker",
    "drain_lease",
    "drain_lease_held",
    "enqueue",
    "list_clients",
    "list_entries",
    "make_entry_id",
    "marked_roles",
    "peek_pending",
    "read_held",
    "read_marker",
    "read_parts",
    "release",
    "settle",
    "transition_allowed",
    "write_marker",
]
