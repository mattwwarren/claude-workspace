"""The shared best-effort enqueue for codex prose producers (RFC 0019 O3, #3707).

Every codex producer that used to post a ticket comment inline (the verdict
comment, the PLAN plan and park comments, the FINALIZE PR link) now calls
:func:`enqueue_prose` instead. The helper owns ticket normalisation, the
``except`` around :func:`cw.outbox.enqueue` (and nothing else), and the log
line, so the producers do not each repeat them.

The call never raises a store fault and never waits on a row: a failed enqueue
returns an :class:`EnqueueOutcome` whose ``skipped_reason`` the producer turns
into one ``review.verdict_comment_skipped`` event. Delivery is the drain's job
(O2), not this module's.

This module imports no tracker code and no ``cw.tracker_io``; the outbox
package itself imports none of it either.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Final, NamedTuple

from cw.outbox import (
    MAX_BODY_BYTES,
    MAX_PART_CHARS,
    MAX_PARTS,
    MAX_TICKET_CHARS,
    OutboxError,
    OutboxRole,
    enqueue,
    list_entries,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_log = logging.getLogger(__name__)

#: The skipped reason when the store refused or failed the enqueue.
SKIP_ENQUEUE_FAILED: Final = "outbox_enqueue_failed"
#: The skipped reason when the text exceeds the outbox's part or body caps.
SKIP_BODY_TOO_LONG: Final = "body_too_long"
#: The skipped reason when an entry with this marker already exists.
SKIP_ALREADY_POSTED: Final = "already_posted"

_MARKER_DIGEST_CHARS: Final = 16


class EnqueueOutcome(NamedTuple):
    """What :func:`enqueue_prose` did: the entry (if any) and why nothing was added."""

    entry_id: str | None
    created: bool
    skipped_reason: str | None


def content_marker(prefix: str, text: str, *, chars: int = _MARKER_DIGEST_CHARS) -> str:
    """``<prefix>-<sha256(text)[:chars]>``: stable for identical text, else distinct."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:chars]}"


def try_content_marker(
    prefix: str, text: str, *, chars: int = _MARKER_DIGEST_CHARS
) -> str | None:
    """Return a marker, or ``None`` when *text* is not valid UTF-8."""
    try:
        return content_marker(prefix, text, chars=chars)
    except UnicodeEncodeError:
        return None


def entry_state_label(client: str, entry_id: str) -> str:
    """The stored state of entry *entry_id*, else ``unknown``.

    A read-only lookup for the duplicate echo; a missing entry or a store fault
    reads as ``unknown`` and never raises.
    """
    try:
        for entry in list_entries(client):
            if entry.id == entry_id:
                return str(entry.state.value)
    except (OutboxError, OSError) as exc:
        _log.warning("outbox_state_lookup_failed entry=%s: %s", entry_id, exc)
    return "unknown"


def _body_too_long(parts: Sequence[str]) -> bool:
    if len(parts) > MAX_PARTS:
        return True
    if any(not part or len(part) > MAX_PART_CHARS for part in parts):
        return True
    return sum(len(part.encode("utf-8")) for part in parts) > MAX_BODY_BYTES


def enqueue_prose(
    client: str,
    ticket: str,
    producer: str,
    marker: str,
    parts: Sequence[str],
    *,
    operator_authored: bool = False,
    redacted: bool = False,
) -> EnqueueOutcome:
    """Enqueue *parts* for *client*'s *ticket*, idempotently on *marker*.

    Operator-visible outcomes are returned, never raised: a duplicate is
    ``already_posted`` with the existing entry's id, an oversize body is
    ``body_too_long``, and any other store or validation fault (including an
    overlong or malformed ticket) is ``outbox_enqueue_failed``.
    """
    ticket_id = ticket.lstrip("#")
    try:
        ticket_id.encode("utf-8")
        marker.encode("utf-8")
        for part in parts:
            part.encode("utf-8")
    except UnicodeEncodeError as exc:
        _log.warning("outbox_enqueue_failed ticket=%s: %s", ticket_id, exc)
        return EnqueueOutcome(None, False, SKIP_ENQUEUE_FAILED)
    if _body_too_long(parts):
        return EnqueueOutcome(None, False, SKIP_BODY_TOO_LONG)
    if len(ticket_id) > MAX_TICKET_CHARS:
        _log.warning("outbox_enqueue_failed ticket=%s: ticket too long", ticket_id)
        return EnqueueOutcome(None, False, SKIP_ENQUEUE_FAILED)
    try:
        result = enqueue(
            client,
            role=OutboxRole.TICKETS,
            ticket=ticket_id,
            marker=marker,
            producer=producer,
            parts=parts,
            redacted=redacted,
            operator_authored=operator_authored,
        )
    except (OutboxError, OSError) as exc:
        _log.warning("outbox_enqueue_failed ticket=%s: %s", ticket_id, exc)
        return EnqueueOutcome(None, False, SKIP_ENQUEUE_FAILED)
    if not result.created:
        return EnqueueOutcome(result.entry.id, False, SKIP_ALREADY_POSTED)
    return EnqueueOutcome(result.entry.id, True, None)
