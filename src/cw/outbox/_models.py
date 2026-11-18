"""Outbox vocabulary: enums, caps, errors, the persisted entry and the list models.

No ``from __future__ import annotations`` here on purpose: pydantic evaluates
the field annotations at runtime, so the runtime-needed imports (``LaunchMarker``,
``AwareDatetime``) are real runtime imports and ruff's TC001 does not fire.

Imports from no sibling submodule.
"""

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal, NamedTuple, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    model_validator,
)

from cw.exceptions import CwError
from cw.models import LaunchMarker

# The persisted entry record (``<id>.json``). A reader refuses a record newer
# than this rather than rewrite it, because a stale process would drop the
# fields it does not know on the rewrite (R24).
OUTBOX_SCHEMA_VERSION = 1
# The ``cw outbox list --json`` envelope and row shape, versioned independently
# of the entry record.
OUTBOX_LIST_SCHEMA_VERSION = 1

# A local copy of the tracker comment limit (R6): this module imports no
# tracker code.
MAX_PART_CHARS = 60_000
MAX_PARTS = 20
MAX_BODY_BYTES = 1_048_576
MAX_MARKER_CHARS = 128
MAX_TICKET_CHARS = 64
MAX_PRODUCER_CHARS = 64
MAX_DETAIL_CHARS = 500
ENTRY_ID_LENGTH = 32
DEFAULT_CLAIM_LIMIT = 1
SHA256_HEX_LENGTH = 64

_CLIENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_TICKET_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
_MARKER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_PRODUCER_RE = re.compile(r"\A[a-z][a-z0-9_-]*\Z")
_ENTRY_ID_RE = re.compile(rf"\A[0-9a-f]{{{ENTRY_ID_LENGTH}}}\Z")


class OutboxError(CwError):
    """Base for every outbox failure."""

    __slots__ = ()


class OutboxValidationError(OutboxError, ValueError):
    """Bad input: a malformed name, a cap violation, a bad limit.

    A ``ValueError`` too, so a name check run inside a pydantic validator
    surfaces as a ``ValidationError`` rather than escaping it.
    """

    __slots__ = ()


class OutboxSchemaError(OutboxError):
    """A record written by a newer cw; it is read-refused and never rewritten."""

    __slots__ = ()


class OutboxRecordError(OutboxError):
    """A corrupt or unreadable record or body, or a wrapped ``OSError``."""

    __slots__ = ()


class OutboxClaimError(OutboxError):
    """The claim token is not live: wrong token, or the entry is not claimed."""

    __slots__ = ()


class OutboxTransitionError(OutboxError):
    """An illegal target state or reason."""

    __slots__ = ()


class OutboxRole(StrEnum):
    """Which tracker role an entry posts to (RFC 0019 D-A2).

    Declared locally: this module does not import the role model that RFC's
    R1 adds in the same wave.
    """

    TICKETS = "tickets"
    KNOWLEDGE = "knowledge"
    CODE_HOST = "code_host"
    CHAT = "chat"


class OutboxState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    UNVERIFIED = "unverified"
    FAILED = "failed"


class OutboxReason(StrEnum):
    POSTED = "posted"
    MARKER_ALREADY_PRESENT = "marker_already_present"
    READBACK_UNVERIFIED = "readback_unverified"
    READBACK_MISMATCH = "readback_mismatch"
    TOOL_UNAVAILABLE = "tool_unavailable"
    TURN_FAILED = "turn_failed"
    BODY_UNREADABLE = "body_unreadable"
    CLAIM_RECOVERED = "claim_recovered"


# The closed reason set per state (R8). ``claimed`` retains the reason the entry
# carried before the claim; ``pending`` carries one only after a recovery.
REASONS_BY_STATE: Mapping[OutboxState, frozenset[OutboxReason]] = {
    OutboxState.DELIVERED: frozenset(
        {OutboxReason.POSTED, OutboxReason.MARKER_ALREADY_PRESENT}
    ),
    OutboxState.UNVERIFIED: frozenset(
        {OutboxReason.READBACK_UNVERIFIED, OutboxReason.READBACK_MISMATCH}
    ),
    OutboxState.FAILED: frozenset(
        {
            OutboxReason.TOOL_UNAVAILABLE,
            OutboxReason.TURN_FAILED,
            OutboxReason.BODY_UNREADABLE,
        }
    ),
    OutboxState.PENDING: frozenset({OutboxReason.CLAIM_RECOVERED}),
    OutboxState.CLAIMED: frozenset(
        {
            OutboxReason.CLAIM_RECOVERED,
            OutboxReason.READBACK_UNVERIFIED,
            OutboxReason.READBACK_MISMATCH,
        }
    ),
}

# The R9 decision table. ``delivered`` and ``failed`` are terminal. The one
# extra edge, ``claimed -> pending``, is the store's own dead-owner recovery
# and is reachable only with ``recovery=True``.
ALLOWED_TRANSITIONS: frozenset[tuple[OutboxState, OutboxState]] = frozenset(
    {
        (OutboxState.PENDING, OutboxState.CLAIMED),
        (OutboxState.UNVERIFIED, OutboxState.CLAIMED),
        (OutboxState.CLAIMED, OutboxState.DELIVERED),
        (OutboxState.CLAIMED, OutboxState.UNVERIFIED),
        (OutboxState.CLAIMED, OutboxState.FAILED),
    }
)
_RECOVERY_EDGE = (OutboxState.CLAIMED, OutboxState.PENDING)


def transition_allowed(
    frm: OutboxState, to: OutboxState, *, recovery: bool = False
) -> bool:
    """Whether ``frm -> to`` is a legal edge; *recovery* adds the store-only one."""
    if (frm, to) in ALLOWED_TRANSITIONS:
        return True
    return recovery and (frm, to) == _RECOVERY_EDGE


def _check(pattern: re.Pattern[str], value: str, what: str, limit: int | None) -> str:
    if limit is not None and len(value) > limit:
        msg = f"outbox {what} is longer than {limit} characters"
        raise OutboxValidationError(msg)
    if not pattern.match(value):
        msg = f"outbox {what} is not valid"
        raise OutboxValidationError(msg)
    return value


def check_client(client: str) -> str:
    return _check(_CLIENT_RE, client, "client name", None)


def check_ticket(ticket: str) -> str:
    return _check(_TICKET_RE, ticket, "ticket", MAX_TICKET_CHARS)


def check_marker(marker: str) -> str:
    return _check(_MARKER_RE, marker, "idempotency key marker", MAX_MARKER_CHARS)


def check_producer(producer: str) -> str:
    return _check(_PRODUCER_RE, producer, "producer", MAX_PRODUCER_CHARS)


def check_entry_id(entry_id: str) -> str:
    return _check(_ENTRY_ID_RE, entry_id, "entry id", None)


def make_entry_id(role: OutboxRole, ticket: str, marker: str) -> str:
    """The content-hash id of the idempotency key ``(role, ticket, marker)``.

    The client is not part of it: a client is a directory.
    """
    key = f"{role.value}\0{ticket}\0{marker}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:ENTRY_ID_LENGTH]


def _check_spans(parts: list[tuple[int, int]], body_bytes: int) -> None:
    if not 1 <= len(parts) <= MAX_PARTS:
        msg = f"an outbox entry has 1 to {MAX_PARTS} parts, got {len(parts)}"
        raise ValueError(msg)
    cursor = 0
    for start, end in parts:
        if start != cursor or end < start:
            msg = "outbox part spans must be contiguous and start at byte 0"
            raise ValueError(msg)
        cursor = end
    if cursor != body_bytes:
        msg = "outbox part spans must end at body_bytes"
        raise ValueError(msg)


class OutboxEntry(BaseModel):
    """One persisted outbox record (``<id>.json``); the body lives beside it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt = OUTBOX_SCHEMA_VERSION
    id: str
    client: str
    role: OutboxRole
    ticket: str
    marker: str
    producer: str
    redacted: bool
    # Contiguous ``[start, end)`` byte spans of the parts inside the one body.
    parts: list[tuple[int, int]]
    body_sha256: str
    body_bytes: int = Field(ge=0)
    state: OutboxState
    reason: OutboxReason | None = None
    detail: str | None = Field(default=None, max_length=MAX_DETAIL_CHARS)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    claim_token: str | None = None
    # The claimer's launch marker; ``None`` reads as ``UNKNOWN`` and is never
    # recovered (R14).
    claimant: LaunchMarker | None = None
    # Display only. No decision may compare it against a threshold (ADR-0014).
    claimed_at: AwareDatetime | None = None
    claimed_from: OutboxState | None = None

    def _check_claim_fields(self) -> None:
        claimed = self.state is OutboxState.CLAIMED
        fields = (self.claim_token, self.claimed_at, self.claimed_from)
        present = [f is not None for f in fields]
        if (claimed and not all(present)) or (not claimed and any(present)):
            msg = "claim_token, claimed_at and claimed_from are set iff claimed"
            raise ValueError(msg)
        if not claimed and self.claimant is not None:
            msg = "claimant must be empty unless state is claimed"
            raise ValueError(msg)
        allowed_from = {OutboxState.PENDING, OutboxState.UNVERIFIED}
        if claimed and self.claimed_from not in allowed_from:
            msg = "claimed_from must be pending or unverified"
            raise ValueError(msg)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        check_entry_id(self.id)
        check_client(self.client)
        check_ticket(self.ticket)
        check_marker(self.marker)
        check_producer(self.producer)
        if not re.fullmatch(rf"[0-9a-f]{{{SHA256_HEX_LENGTH}}}", self.body_sha256):
            msg = "body_sha256 must be a sha256 hex digest"
            raise ValueError(msg)
        _check_spans(self.parts, self.body_bytes)
        self._check_claim_fields()
        if self.reason is not None and self.reason not in REASONS_BY_STATE[self.state]:
            msg = f"reason {self.reason.value} is not allowed in state {self.state}"
            raise ValueError(msg)
        return self


class EnqueueResult(NamedTuple):
    """What ``enqueue`` did: the stored entry and whether this call created it.

    ``body_matches`` is whether the offered body equals the stored one, which
    only differs from ``True`` on a duplicate (``created`` is ``False``).
    """

    entry: OutboxEntry
    created: bool
    body_matches: bool


class OutboxListRow(BaseModel):
    """One ``cw outbox list --json`` row. Omits ``claim_token``: a capability."""

    id: str
    client: str
    role: OutboxRole
    ticket: str
    marker: str
    producer: str
    state: OutboxState
    reason: OutboxReason | None
    detail: str | None
    created_at: datetime
    updated_at: datetime
    claimed_at: datetime | None
    claimant_verdict: Literal["in_flight", "owner_gone", "unknown"] | None
    part_count: int
    body_bytes: int


class OutboxListEnvelope(BaseModel):
    """The ``cw outbox list --json`` payload."""

    schema_version: int = OUTBOX_LIST_SCHEMA_VERSION
    entries: list[OutboxListRow]
    limit: int
    truncated: bool
