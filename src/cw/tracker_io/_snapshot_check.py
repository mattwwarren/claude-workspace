"""The before/after snapshot the courier compares for a ``per_server`` turn (O2b).

A plugin app's full tool surface is not narrowed by cw, so the snapshot delta
is the guard: a snapshot of the one ticket is read before and after the post
turn, and only the expected marker comments may appear between them. The
comparator is pure; the reasons it returns are fixed strings so a failed delta
is diagnosable from the event alone (R8, R24).
"""

from __future__ import annotations

from collections import Counter
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


def _duplicate_reason(snapshot: IssueSnapshot, label: str) -> str | None:
    """A read may not repeat a comment id or a marker line: that is inconsistent."""
    dup_ids = sorted(k for k, n in Counter(snapshot.comment_ids).items() if n > 1)
    if dup_ids:
        return f"{label} snapshot repeats comment ids: {dup_ids}"
    dup_markers = sorted(k for k, n in Counter(snapshot.markers).items() if n > 1)
    if dup_markers:
        return f"{label} snapshot repeats marker lines: {dup_markers}"
    return None


def _multiset_removed(before: Counter[str], after: Counter[str]) -> list[str]:
    """Items present before the turn and absent (by count) after it."""
    return sorted((before - after).keys())


def _delta_reason(
    before: IssueSnapshot, after: IssueSnapshot, expected_markers: Sequence[str]
) -> str | None:
    """Only the expected markers, one new comment id each, may appear.

    Comparison is by multiset count, never by set: a set collapses repeats and
    hides a removal or an extra copy. Duplicates within a single read are
    rejected first, so the counts below are over distinct per-read values.
    """
    for snapshot, label in ((before, "before"), (after, "after")):
        duplicate = _duplicate_reason(snapshot, label)
        if duplicate is not None:
            return duplicate
    return (
        _removed_reason(before, after)
        or _marker_reason(before, after, expected_markers)
        or _new_id_reason(before, after, expected_markers)
    )


def _removed_reason(before: IssueSnapshot, after: IssueSnapshot) -> str | None:
    """No comment id or marker line the turn started with may disappear."""
    removed_ids = _multiset_removed(
        Counter(before.comment_ids), Counter(after.comment_ids)
    )
    if removed_ids:
        return f"comment ids removed: {removed_ids}"
    removed_markers = _multiset_removed(Counter(before.markers), Counter(after.markers))
    if removed_markers:
        return f"marker lines removed: {removed_markers}"
    return None


def _marker_reason(
    before: IssueSnapshot, after: IssueSnapshot, expected_markers: Sequence[str]
) -> str | None:
    """The new marker lines are exactly the expected set, each expected once."""
    expected = set(expected_markers)
    new_markers = set(after.markers) - set(before.markers)
    missing = expected - new_markers
    if missing:
        return f"expected marker lines missing after the turn: {sorted(missing)}"
    unexpected = new_markers - expected
    if unexpected:
        return f"unexpected marker lines appeared: {sorted(unexpected)}"
    if len(expected_markers) != len(expected):
        return "expected marker lines repeat: the turn expects each marker once"
    return None


def _new_id_reason(
    before: IssueSnapshot, after: IssueSnapshot, expected_markers: Sequence[str]
) -> str | None:
    """Exactly one new comment id per expected marker line."""
    new_ids = Counter(after.comment_ids) - Counter(before.comment_ids)
    added = sum(new_ids.values())
    if added != len(expected_markers):
        return (
            f"{added} new comment ids for {len(expected_markers)} expected marker lines"
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
