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
    MAX_TRANSITIONS,
    OUTBOX_SCHEMA_VERSION,
    REASONS_BY_STATE,
    VERDICT_OWNER_GONE,
    EnqueueResult,
    OutboxClaimError,
    OutboxEntry,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    OutboxState,
    OutboxTransition,
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
    from cw.outbox._models import ClaimantVerdict, TransitionInitiator


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


class _History(NamedTuple):
    transitions: list[OutboxTransition]
    dropped: int


def _append_transition(
    entry: OutboxEntry,
    *,
    to_state: OutboxState,
    reason: OutboxReason | None,
    detail: str | None,
    initiator: TransitionInitiator,
    now: datetime,
    claimant: LaunchMarker | None,
    claimant_verdict: ClaimantVerdict | None = None,
) -> _History:
    """*entry*'s transitions plus one item for this change, as a ring buffer.

    The item travels in the same record write as the state it describes (22a),
    so the audit trail needs no separate file. Past ``MAX_TRANSITIONS`` the
    oldest items are dropped and counted in ``dropped``; the cap never refuses
    or fails a transition.
    """
    item = OutboxTransition(
        from_state=entry.state,
        to_state=to_state,
        reason=reason,
        detail=detail,
        at=now,
        initiator=initiator,
        claimant=claimant,
        claimant_verdict=claimant_verdict,
    )
    items = [*entry.transitions, item]
    overflow = max(0, len(items) - MAX_TRANSITIONS)
    return _History(items[overflow:], entry.transitions_dropped + overflow)


def _apply_claim(
    entry: OutboxEntry, *, claimant: LaunchMarker | None, now: datetime
) -> OutboxEntry:
    history = _append_transition(
        entry,
        to_state=OutboxState.CLAIMED,
        reason=entry.reason,
        detail=None,
        initiator="claim",
        now=now,
        claimant=claimant,
    )
    return _rebuild(
        entry,
        state=OutboxState.CLAIMED,
        claim_token=uuid.uuid4().hex,
        claimant=claimant,
        claimed_at=now,
        claimed_from=entry.state,
        transitions=history.transitions,
        transitions_dropped=history.dropped,
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
    history = _append_transition(
        entry,
        to_state=state,
        reason=reason,
        detail=detail,
        initiator="settle",
        now=now,
        claimant=entry.claimant,
    )
    return _rebuild(
        entry,
        state=state,
        reason=reason,
        detail=detail,
        claim_token=None,
        claimant=None,
        claimed_at=None,
        claimed_from=None,
        transitions=history.transitions,
        transitions_dropped=history.dropped,
        updated_at=now,
    )


def _settled_unreadable(
    entry: OutboxEntry, *, claimant: LaunchMarker | None, now: datetime
) -> OutboxEntry:
    """*entry* claimed by this store and settled ``failed/body_unreadable``.

    The store settles on its own only for a missing or corrupt body (22b).
    Both steps land in one record write: ``pending|unverified -> claimed ->
    failed``, so the transition table is never widened to allow a direct edge.
    """
    claimed = _apply_claim(entry, claimant=claimant, now=now)
    return _apply_settle(
        claimed,
        state=OutboxState.FAILED,
        reason=OutboxReason.BODY_UNREADABLE,
        detail=None,
        now=now,
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
        transitions=[
            OutboxTransition(
                from_state=None,
                to_state=OutboxState.PENDING,
                at=now,
                initiator="enqueue",
            )
        ],
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
    history = _append_transition(
        entry,
        to_state=target,
        reason=reason,
        detail=None,
        initiator="recovery",
        now=now,
        claimant=entry.claimant,
        # Recovery runs only on OWNER_GONE (see _recover_locked).
        claimant_verdict=VERDICT_OWNER_GONE,
    )
    return _rebuild(
        entry,
        state=target,
        reason=reason,
        claim_token=None,
        claimant=None,
        claimed_at=None,
        claimed_from=None,
        transitions=history.transitions,
        transitions_dropped=history.dropped,
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
        layout.write_record(client, _recovered(entry, _now()))


def _body_intact(client: str, entry: OutboxEntry) -> bool:
    """Whether the body matches its record.

    A missing file or a sha256/size mismatch is ``False`` (a torn or absent
    body): the claim then settles the entry ``failed/body_unreadable`` (22b). Any
    other ``OSError`` is transient: it raises and settles nothing.
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

    Each entry is its own atomic record write (22d), so the transition item and
    the state change commit together per entry. An entry whose write or body
    read fails is left exactly as it was. When an earlier entry already
    committed, the committed claims are returned and the failure stops the
    batch; when nothing committed, the failure propagates. A missing or corrupt
    body is settled ``failed/body_unreadable`` by this store (22b) and does not
    count toward *limit*; the claim continues with the next entry.
    """
    candidates = sorted(
        (e for e in layout.scan_entries(client) if e.state is state),
        key=lambda e: (e.created_at, e.id),
    )
    now = _now()
    claimed: list[OutboxEntry] = []
    for entry in candidates:
        if len(claimed) == limit:
            break
        try:
            if _body_intact(client, entry):
                record = _apply_claim(entry, claimant=claimant, now=now)
                layout.write_record(client, record)
                claimed.append(record)
            else:
                layout.write_record(
                    client, _settled_unreadable(entry, claimant=claimant, now=now)
                )
        except OutboxRecordError:
            if not claimed:
                raise
            break
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
    return settled
