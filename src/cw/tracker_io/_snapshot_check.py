"""The before/after snapshot the courier compares for a ``per_server`` turn (O2b).

A plugin app's full tool surface is not narrowed by cw, so the snapshot delta
is the guard: a snapshot of the one ticket is read before and after the post
turn, and only the expected marker comments may appear between them. The
comparator is pure; the reasons it returns are fixed strings so a failed delta
is diagnosable from the event alone (R8, R24).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from cw.openai_strict_schema import to_openai_strict_schema
from cw.tracker_io._prompt import TurnStatus

if TYPE_CHECKING:
    from collections.abc import Sequence


class IssueSnapshot(BaseModel):
    """One read of a ticket: its comments, the markers among them, and its fields."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: TurnStatus
    detail: str
    ticket_id: str
    comment_ids: tuple[str, ...]
    markers: tuple[str, ...]
    state: str
    assignee: str
    labels: tuple[str, ...]


#: The ``--output-schema`` document for a snapshot turn (every key required).
SNAPSHOT_SCHEMA = to_openai_strict_schema(IssueSnapshot.model_json_schema())


def parse_snapshot(content: str | None) -> IssueSnapshot | None:
    """The parsed snapshot document, or ``None`` when it is absent or invalid."""
    if not content:
        return None
    try:
        return IssueSnapshot.model_validate_json(content)
    except ValidationError:
        return None


def _identity_reason(
    before: IssueSnapshot, after: IssueSnapshot, ticket_id: str
) -> str | None:
    """Both reads must be ``ok`` and name the expected ticket."""
    for label, snapshot in (("before", before), ("after", after)):
        if snapshot.status != "ok":
            return f"{label} snapshot status is {snapshot.status!r}"
        if snapshot.ticket_id != ticket_id:
            return (
                f"{label} snapshot ticket id {snapshot.ticket_id!r} "
                f"is not {ticket_id!r}"
            )
    return None


def _field_reason(before: IssueSnapshot, after: IssueSnapshot) -> str | None:
    """State, assignee and sorted labels must not change across the turn."""
    if before.state != after.state:
        return f"issue state changed from {before.state!r} to {after.state!r}"
    if before.assignee != after.assignee:
        return "issue assignee changed"
    if sorted(before.labels) != sorted(after.labels):
        return "issue labels changed"
    return None


def _delta_reason(
    before: IssueSnapshot, after: IssueSnapshot, expected_markers: Sequence[str]
) -> str | None:
    """Only the expected markers, one new comment id each, may appear."""
    before_ids, after_ids = set(before.comment_ids), set(after.comment_ids)
    removed_ids = before_ids - after_ids
    if removed_ids:
        return f"comment ids removed: {sorted(removed_ids)}"
    before_markers, after_markers = set(before.markers), set(after.markers)
    removed_markers = before_markers - after_markers
    if removed_markers:
        return f"marker lines removed: {sorted(removed_markers)}"
    expected = set(expected_markers)
    new_markers = after_markers - before_markers
    missing = expected - new_markers
    if missing:
        return f"expected marker lines missing after the turn: {sorted(missing)}"
    unexpected = new_markers - expected
    if unexpected:
        return f"unexpected marker lines appeared: {sorted(unexpected)}"
    new_ids = after_ids - before_ids
    if len(new_ids) != len(expected):
        return (
            f"{len(new_ids)} new comment ids for {len(expected)} expected marker lines"
        )
    return None


def compare_snapshots(
    before: IssueSnapshot,
    after: IssueSnapshot,
    *,
    ticket_id: str,
    expected_markers: Sequence[str],
) -> str | None:
    """``None`` when the delta is exactly *expected_markers*; else a reason.

    Accepted: one new comment id per expected marker, the expected markers
    newly present, and the state, assignee and sorted labels unchanged. Any
    removed comment or marker, any unexpected new marker or comment, or a
    changed field is rejected with its own reason.
    """
    return (
        _identity_reason(before, after, ticket_id)
        or _field_reason(before, after)
        or _delta_reason(before, after, expected_markers)
    )
