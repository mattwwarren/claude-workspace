"""Verify a tracker-IO turn's self-attested result (#3057, RFC 0018 A2).

cw never talks to the tracker itself (ADR-0013), so it cannot confirm a post
independently. The agent reports the ticket it posted to, one id and URL per
part, and the first and last :data:`READBACK_WINDOW` characters of each
stored comment. This module checks that report against the parts cw emitted,
D-7 style: one closed :class:`TrackerIoReason` per failure, first failure
wins, and a :class:`PublicState` saying what may now be public.

Every check is self-attested: the ids and read-back windows are model-echoed,
not proof of delivery. The middle of a part longer than two windows is not
verified at all. Marker survival is recorded, never enforced, because whether
the tracker keeps a hidden HTML comment is unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cw.gh import AGENT_COMMENT_MARKER
from cw.tracker_post import normalize_for_compare, strip_trailing_marker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.tracker_io._prompt import TurnPart, TurnResult


class TrackerIoReason(StrEnum):
    """Why a tracker-IO turn did not end in a verified post. Closed set."""

    # Refused before launch: nothing is public.
    TRACKER_UNSUPPORTED = "tracker_unsupported"
    MCP_SERVER_UNNAMED = "mcp_server_unnamed"
    TRACKER_MCP_NOT_ENABLED = "tracker_mcp_not_enabled"
    PLAN_REFUSED = "plan_refused"
    SCRATCH_UNWRITABLE = "scratch_unwritable"
    CODEX_UNAVAILABLE = "codex_unavailable"
    # The agent reported it could not post.
    TOOL_UNAVAILABLE = "tool_unavailable"
    TURN_REPORTED_FAILURE = "turn_reported_failure"
    # The turn ended without a usable report.
    TURN_TIMEOUT = "turn_timeout"
    TURN_EXIT_NONZERO = "turn_exit_nonzero"
    RESULT_UNPARSEABLE = "result_unparseable"
    # The report does not match what cw emitted.
    TICKET_MISMATCH = "ticket_mismatch"
    IDS_COUNT_MISMATCH = "ids_count_mismatch"
    IDS_OUT_OF_ORDER = "ids_out_of_order"
    ID_MALFORMED = "id_malformed"
    ID_DUPLICATE = "id_duplicate"
    URL_MISMATCH = "url_mismatch"
    READBACK_MISMATCH = "readback_mismatch"
    READBACK_MISSING = "readback_missing"


class PublicState(StrEnum):
    """What a turn may have made public on the ticket."""

    NONE = "none"
    POSSIBLE = "possible"
    CONFIRMED = "confirmed"


_PRE_LAUNCH = frozenset(
    {
        TrackerIoReason.TRACKER_UNSUPPORTED,
        TrackerIoReason.MCP_SERVER_UNNAMED,
        TrackerIoReason.TRACKER_MCP_NOT_ENABLED,
        TrackerIoReason.PLAN_REFUSED,
        TrackerIoReason.SCRATCH_UNWRITABLE,
        TrackerIoReason.CODEX_UNAVAILABLE,
    }
)
_AGENT_REPORTED = frozenset(
    {TrackerIoReason.TOOL_UNAVAILABLE, TrackerIoReason.TURN_REPORTED_FAILURE}
)
_NO_REPORT = frozenset(
    {
        TrackerIoReason.TURN_TIMEOUT,
        TrackerIoReason.TURN_EXIT_NONZERO,
        TrackerIoReason.RESULT_UNPARSEABLE,
    }
)
_STATUS_REASON = {
    "tool_unavailable": TrackerIoReason.TOOL_UNAVAILABLE,
    "failed": TrackerIoReason.TURN_REPORTED_FAILURE,
}
# A tracker comment id cw accepts from the agent (Linear ids are UUIDs).
_COMMENT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class PostedComment:
    """One comment the agent reports it created (1-based *index*)."""

    index: int
    comment_id: str
    url: str


@dataclass(frozen=True)
class VerifyOutcome:
    """The verification of one turn result."""

    reason: TrackerIoReason | None
    public_state: PublicState
    posted: tuple[PostedComment, ...]
    marker_survived: bool | None

    @property
    def verified(self) -> bool:
        """True when every check passed."""
        return self.reason is None


def public_state_for(
    reason: TrackerIoReason | None, *, parts_returned: int
) -> PublicState:
    """Classify *reason* given how many parts the agent reported."""
    if reason is None:
        return PublicState.CONFIRMED
    if reason in _PRE_LAUNCH:
        return PublicState.NONE
    if reason in _NO_REPORT:
        return PublicState.POSSIBLE
    if parts_returned > 0:
        return PublicState.CONFIRMED
    # An agent-reported failure with no ids is NONE (still unconfirmed: it is
    # not proof nothing was posted); a mismatch with no ids proves nothing.
    return PublicState.NONE if reason in _AGENT_REPORTED else PublicState.POSSIBLE


def _check_status(_t: str, _e: Sequence[str], r: TurnResult) -> TrackerIoReason | None:
    return _STATUS_REASON.get(r.status)


def _check_ticket(t: str, _e: Sequence[str], r: TurnResult) -> TrackerIoReason | None:
    return None if r.ticket_id == t else TrackerIoReason.TICKET_MISMATCH


def _check_count(_t: str, e: Sequence[str], r: TurnResult) -> TrackerIoReason | None:
    return None if len(r.parts) == len(e) else TrackerIoReason.IDS_COUNT_MISMATCH


def _check_order(_t: str, _e: Sequence[str], r: TurnResult) -> TrackerIoReason | None:
    indices = [part.index for part in r.parts]
    expected = list(range(1, len(r.parts) + 1))
    return None if indices == expected else TrackerIoReason.IDS_OUT_OF_ORDER


def _check_ids(_t: str, _e: Sequence[str], r: TurnResult) -> TrackerIoReason | None:
    ids = [part.comment_id for part in r.parts]
    if any(_COMMENT_ID_RE.fullmatch(i) is None for i in ids):
        return TrackerIoReason.ID_MALFORMED
    return None if len(set(ids)) == len(ids) else TrackerIoReason.ID_DUPLICATE


def _check_urls(t: str, _e: Sequence[str], r: TurnResult) -> TrackerIoReason | None:
    for part in r.parts:
        if not part.url:
            continue
        if part.comment_id not in part.url:
            return TrackerIoReason.URL_MISMATCH
        if t.lower() not in part.url.lower():
            return TrackerIoReason.TICKET_MISMATCH
    return None


def _comparable(text: str) -> str:
    return normalize_for_compare(strip_trailing_marker(text))


def _readback_reason(part: TurnPart, emitted: str) -> TrackerIoReason | None:
    """Head is a prefix, tail a suffix, of the emitted part (marker-stripped)."""
    head = _comparable(part.readback_head)
    tail = _comparable(part.readback_tail)
    if not head or not tail:
        return TrackerIoReason.READBACK_MISSING
    want = _comparable(emitted)
    if want.startswith(head) and want.endswith(tail):
        return None
    return TrackerIoReason.READBACK_MISMATCH


def _check_readback(_t: str, e: Sequence[str], r: TurnResult) -> TrackerIoReason | None:
    for part, emitted in zip(r.parts, e, strict=True):
        reason = _readback_reason(part, emitted)
        if reason is not None:
            return reason
    return None


# Checks a..h in order; the first failure is the outcome's reason.
_CHECKS: tuple[
    Callable[[str, Sequence[str], TurnResult], TrackerIoReason | None], ...
] = (
    _check_status,
    _check_ticket,
    _check_count,
    _check_order,
    _check_ids,
    _check_urls,
    _check_readback,
)


def _marker_survived(result: TurnResult) -> bool | None:
    """Whether every stored tail still ends with the marker; ``None`` if no parts."""
    if not result.parts:
        return None
    return all(
        part.readback_tail.rstrip().endswith(AGENT_COMMENT_MARKER)
        for part in result.parts
    )


def verify_turn_result(
    ticket_id: str, emitted: Sequence[str], result: TurnResult | None
) -> VerifyOutcome:
    """Check *result* against the *emitted* marked parts for *ticket_id*.

    ``None`` (an unparseable result) is ``RESULT_UNPARSEABLE``/``POSSIBLE``.
    """
    if result is None:
        unparseable = TrackerIoReason.RESULT_UNPARSEABLE
        return VerifyOutcome(
            unparseable, public_state_for(unparseable, parts_returned=0), (), None
        )
    reason = next(
        (
            found
            for check in _CHECKS
            if (found := check(ticket_id, emitted, result)) is not None
        ),
        None,
    )
    posted = tuple(
        PostedComment(part.index, part.comment_id, part.url) for part in result.parts
    )
    return VerifyOutcome(
        reason,
        public_state_for(reason, parts_returned=len(result.parts)),
        posted,
        _marker_survived(result),
    )
