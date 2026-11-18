"""The outbox store: enqueue, list, claim (with recovery), settle.

Every public mutating function takes the client's outbox lock. ``_claim_locked``
and ``_recover_locked`` run under it and never call a public function, because
re-entry of a guarded lock raises. ``list_entries``, ``list_clients``,
``claimant_state`` and ``read_parts`` are lock-free reads.

Nothing here compares ``claimed_at`` against a threshold (ADR-0014 invariant
1): a claim is recovered only on positive evidence that its owner process is
gone, and a claim whose owner cannot be judged stays claimed until an operator
acts.

Imports ``_layout``, ``_lock`` and ``_models``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.launch_marker import (
    LaunchOwnerState,
    current_launch_marker,
    observed_launch_owner_state,
)
from cw.outbox import _layout as layout
from cw.outbox._lock import _outbox_lock
from cw.outbox._models import (
    DEFAULT_CLAIM_LIMIT,
    MAX_BODY_BYTES,
    MAX_DETAIL_CHARS,
    MAX_PART_CHARS,
    MAX_PARTS,
    OUTBOX_SCHEMA_VERSION,
    REASONS_BY_STATE,
    EnqueueResult,
    OutboxClaimError,
    OutboxEntry,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    OutboxState,
    OutboxTransitionError,
    OutboxValidationError,
    check_client,
    check_entry_id,
    check_marker,
    check_producer,
    check_ticket,
    make_entry_id,
    transition_allowed,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models import LaunchMarker


class _Body(NamedTuple):
    text: str
    sha256: str
    size: int
    spans: list[tuple[int, int]]


def _now() -> datetime:
    return datetime.now(UTC)


def _plan_body(parts: Sequence[str]) -> _Body:
    """Validate the caps and lay the parts out as one body with byte spans."""
    if not 1 <= len(parts) <= MAX_PARTS:
        msg = f"an outbox entry has 1 to {MAX_PARTS} parts, got {len(parts)}"
        raise OutboxValidationError(msg)
    spans: list[tuple[int, int]] = []
    cursor = 0
    for part in parts:
        if not part or len(part) > MAX_PART_CHARS:
            msg = f"each outbox part must be 1 to {MAX_PART_CHARS} characters"
            raise OutboxValidationError(msg)
        try:
            size = len(part.encode("utf-8"))
        except UnicodeEncodeError as exc:
            msg = "an outbox part is not encodable as UTF-8"
            raise OutboxValidationError(msg) from exc
        spans.append((cursor, cursor + size))
        cursor += size
    if cursor > MAX_BODY_BYTES:
        msg = f"an outbox body is at most {MAX_BODY_BYTES} bytes, got {cursor}"
        raise OutboxValidationError(msg)
    text = "".join(parts)
    return _Body(text, hashlib.sha256(text.encode("utf-8")).hexdigest(), cursor, spans)


def _coerce_role(role: OutboxRole | str) -> OutboxRole:
    try:
        return OutboxRole(role)
    except ValueError as exc:
        msg = "unknown outbox role"
        raise OutboxValidationError(msg) from exc


def _require_transition(
    frm: OutboxState, to: OutboxState, *, recovery: bool = False
) -> None:
    if not transition_allowed(frm, to, recovery=recovery):
        msg = f"illegal outbox transition {frm.value} -> {to.value}"
        raise OutboxTransitionError(msg)


def _rebuild(entry: OutboxEntry, **updates: object) -> OutboxEntry:
    """A validated copy of *entry* with *updates* applied."""
    data = entry.model_dump()
    data.update(updates)
    return OutboxEntry.model_validate(data)


def _apply_claim(
    entry: OutboxEntry, *, claimant: LaunchMarker | None, now: datetime
) -> OutboxEntry:
    return _rebuild(
        entry,
        state=OutboxState.CLAIMED,
        claim_token=uuid.uuid4().hex,
        claimant=claimant,
        claimed_at=now,
        claimed_from=entry.state,
        updated_at=now,
    )


def _apply_settle(
    entry: OutboxEntry,
    *,
    state: OutboxState,
    reason: OutboxReason,
    detail: str | None,
    now: datetime,
) -> OutboxEntry:
    return _rebuild(
        entry,
        state=state,
        reason=reason,
        detail=detail,
        claim_token=None,
        claimant=None,
        claimed_at=None,
        claimed_from=None,
        updated_at=now,
    )


def _audit_transition(
    client: str,
    before: OutboxEntry,
    after: OutboxEntry,
    *,
    initiator: str,
    now: datetime,
    reason: OutboxReason | None = None,
) -> None:
    """Append the audit record after the state record has been persisted."""
    claimant = after.claimant or before.claimant
    claim_token = after.claim_token or before.claim_token
    effective_reason = reason or after.reason or before.reason
    layout.append_audit_event(
        client,
        {
            "event": "transition",
            "entry_id": after.id,
            "client": client,
            "before_state": before.state.value,
            "after_state": after.state.value,
            "initiator": initiator,
            "timestamp": now.isoformat(),
            "claimant": claimant.model_dump(mode="json") if claimant else None,
            "claim_token": claim_token,
            "reason": effective_reason.value if effective_reason else None,
        },
    )


def _existing_entry(client: str, entry_id: str) -> OutboxEntry | None:
    if not layout.record_exists(client, entry_id):
        return None
    return layout.read_record(client, entry_id)


def enqueue(
    client: str,
    *,
    role: OutboxRole,
    ticket: str,
    marker: str,
    producer: str,
    parts: Sequence[str],
    redacted: bool,
) -> EnqueueResult:
    """Store a prose post for *client*, idempotently on ``(role, ticket, marker)``.

    The body is stored verbatim: the marker is neither appended nor required in
    it (embedding it in the posted text is the producer's or courier's job). A
    duplicate returns the existing entry unchanged with ``created=False`` and
    never overwrites or raises (D-B1). The body is written first and the record
    last: the record is the commit point.
    """
    check_client(client)
    role = _coerce_role(role)
    check_ticket(ticket)
    check_marker(marker)
    check_producer(producer)
    body = _plan_body(parts)
    now = _now()
    entry = OutboxEntry(
        schema_version=OUTBOX_SCHEMA_VERSION,
        id=make_entry_id(role, ticket, marker),
        client=client,
        role=role,
        ticket=ticket,
        marker=marker,
        producer=producer,
        redacted=redacted,
        parts=body.spans,
        body_sha256=body.sha256,
        body_bytes=body.size,
        state=OutboxState.PENDING,
        created_at=now,
        updated_at=now,
    )
    with _outbox_lock(client):
        existing = _existing_entry(client, entry.id)
        if existing is not None:
            return EnqueueResult(existing, False, existing.body_sha256 == body.sha256)
        layout.write_body(client, entry.id, body.text)
        layout.write_record(client, entry)
    return EnqueueResult(entry, True, True)


def list_entries(
    client: str, *, state: OutboxState | None = None, limit: int | None = None
) -> list[OutboxEntry]:
    """Entries of *client*, newest first. A pure read: no body read, no recovery.

    The *state* filter is applied before *limit*.
    """
    if limit is not None and limit < 1:
        msg = "limit must be at least 1"
        raise OutboxValidationError(msg)
    entries = layout.scan_entries(client)
    if state is not None:
        entries = [e for e in entries if e.state is state]
    entries.sort(key=lambda e: (e.created_at, e.id), reverse=True)
    return entries if limit is None else entries[:limit]


def list_clients() -> list[str]:
    """Every client directory present on disk, sorted."""
    return layout.list_client_names()


def claimant_state(entry: OutboxEntry) -> LaunchOwnerState | None:
    """The verdict on a claimed entry's claimant; ``None`` unless claimed.

    Performs no recovery.
    """
    if entry.state is not OutboxState.CLAIMED:
        return None
    return observed_launch_owner_state(entry.claimant)


def read_parts(entry: OutboxEntry) -> list[str]:
    """Read the body, verify its sha256 and size, and split it into the parts."""
    try:
        data = layout.read_body_bytes(entry.client, entry.id)
    except OSError as exc:
        msg = f"cannot read the body of outbox entry {entry.id}"
        raise OutboxRecordError(msg) from exc
    if len(data) != entry.body_bytes or hashlib.sha256(data).hexdigest() != (
        entry.body_sha256
    ):
        msg = f"the body of outbox entry {entry.id} does not match its record"
        raise OutboxRecordError(msg)
    try:
        return [data[start:end].decode("utf-8") for start, end in entry.parts]
    except UnicodeDecodeError as exc:
        msg = f"the body of outbox entry {entry.id} is not valid UTF-8"
        raise OutboxRecordError(msg) from exc


def _recovered(entry: OutboxEntry, now: datetime) -> OutboxEntry:
    """*entry*, a claimed one whose owner is gone, restored to its prior state.

    Restores to ``claimed_from`` (never blindly to pending): an unverified
    entry returns to unverified so it is verified rather than re-posted.
    """
    # ``claimed_from`` is always set while claimed (the model enforces it).
    target = entry.claimed_from or OutboxState.PENDING
    _require_transition(OutboxState.CLAIMED, target, recovery=True)
    reason = (
        OutboxReason.CLAIM_RECOVERED if target is OutboxState.PENDING else entry.reason
    )
    return _rebuild(
        entry,
        state=target,
        reason=reason,
        claim_token=None,
        claimant=None,
        claimed_at=None,
        claimed_from=None,
        updated_at=now,
    )


def _recover_locked(client: str) -> None:
    """Restore every claimed entry whose owner is provably gone. Lock held."""
    for entry in layout.scan_entries(client):
        if entry.state is not OutboxState.CLAIMED:
            continue
        if observed_launch_owner_state(entry.claimant) is not (
            LaunchOwnerState.OWNER_GONE
        ):
            continue
        now = _now()
        recovered = _recovered(entry, now)
        layout.write_record(client, recovered)
        _audit_transition(client, entry, recovered, initiator="recovery", now=now)


def _body_intact(client: str, entry: OutboxEntry) -> bool:
    """Whether the body matches its record.

    A missing file or a sha256/size mismatch is ``False`` (a torn or absent
    body). Any other ``OSError`` is transient: it raises and settles nothing.
    A false result remains retryable; repairing the body lets a later claim
    succeed.
    """
    try:
        data = layout.read_body_bytes(client, entry.id)
    except FileNotFoundError:
        return False
    except OSError as exc:
        msg = f"cannot read the body of outbox entry {entry.id}"
        raise OutboxRecordError(msg) from exc
    return (
        len(data) == entry.body_bytes
        and hashlib.sha256(data).hexdigest() == entry.body_sha256
    )


def _claim_locked(
    client: str, *, state: OutboxState, limit: int, claimant: LaunchMarker | None
) -> list[OutboxEntry]:
    """Claim up to *limit* oldest entries in *state*. Lock held.

    Verify-then-claim: every body this call will return is verified before any
    claim record is written, so a transient read error raises with nothing
    claimed. An unreadable body remains in its prior retryable state and does
    not count toward *limit*.
    """
    candidates = sorted(
        (e for e in layout.scan_entries(client) if e.state is state),
        key=lambda e: (e.created_at, e.id),
    )
    selected: list[OutboxEntry] = []
    for entry in candidates:
        if len(selected) == limit:
            break
        if _body_intact(client, entry):
            selected.append(entry)
    claimed: list[OutboxEntry] = []
    for entry in selected:
        record = _apply_claim(entry, claimant=claimant, now=_now())
        layout.write_record(client, record)
        _audit_transition(
            client, entry, record, initiator="claim", now=record.updated_at
        )
        claimed.append(record)
    return claimed


def claim(
    client: str,
    *,
    state: OutboxState = OutboxState.PENDING,
    limit: int = DEFAULT_CLAIM_LIMIT,
) -> list[OutboxEntry]:
    """Claim up to *limit* oldest entries in *state* (``pending`` or ``unverified``).

    Recovery of dead-owner claims runs first, inside the same lock hold. The
    claimant marker is read before the lock is taken.
    """
    _require_transition(state, OutboxState.CLAIMED)
    if limit < 1:
        msg = "limit must be at least 1"
        raise OutboxValidationError(msg)
    claimant = current_launch_marker()
    with _outbox_lock(client):
        _recover_locked(client)
        return _claim_locked(client, state=state, limit=limit, claimant=claimant)


def _validate_settle_target(
    state: OutboxState, reason: OutboxReason, detail: str | None
) -> None:
    _require_transition(OutboxState.CLAIMED, state)
    if reason not in REASONS_BY_STATE[state]:
        msg = f"reason {reason.value} is not allowed for state {state.value}"
        raise OutboxTransitionError(msg)
    if detail is not None and len(detail) > MAX_DETAIL_CHARS:
        msg = f"detail is longer than {MAX_DETAIL_CHARS} characters"
        raise OutboxValidationError(msg)


def settle(
    client: str,
    entry_id: str,
    *,
    claim_token: str,
    state: OutboxState,
    reason: OutboxReason,
    detail: str | None = None,
) -> OutboxEntry:
    """Settle a claimed entry as *state* with *reason*, if *claim_token* is live.

    A late settle from a recovered claimant, a wrong token, an unclaimed entry
    and an unknown entry all raise ``OutboxClaimError`` and overwrite nothing.
    """
    check_entry_id(entry_id)
    _validate_settle_target(state, reason, detail)
    with _outbox_lock(client):
        entry = _existing_entry(client, entry_id)
        if entry is None:
            msg = f"no outbox entry {entry_id}"
            raise OutboxClaimError(msg)
        if entry.state is not OutboxState.CLAIMED or entry.claim_token != claim_token:
            msg = f"the claim on outbox entry {entry_id} is not live"
            raise OutboxClaimError(msg)
        settled = _apply_settle(
            entry, state=state, reason=reason, detail=detail, now=_now()
        )
        layout.write_record(client, settled)
        _audit_transition(
            client, entry, settled, initiator="settle", now=settled.updated_at
        )
    return settled
