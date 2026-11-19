"""The before/after snapshot the courier compares for a ``per_server`` turn (O2b).

A plugin app's full tool surface is not narrowed by cw, so the snapshot delta
is the guard: a snapshot of the one ticket is read before and after the post
turn, and only the expected marker comments may appear between them. The
comments are paired with the marker lines each one carries, so a new comment
is accepted only when it carries exactly one expected marker, and an existing
comment may not gain or lose one. The comparator is pure; the reasons it
returns are fixed strings so a failed delta is diagnosable from the event
alone (R8, R24).
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from cw.openai_strict_schema import to_openai_strict_schema
from cw.tracker_io._prompt import TurnStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class SnapshotComment(BaseModel):
    """One comment on the ticket and the marker lines it itself contains."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    marker_lines: tuple[str, ...]


class IssueSnapshot(BaseModel):
    """One read of a ticket: its comments with their markers, and its fields."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: TurnStatus
    detail: str
    ticket_id: str
    comments: tuple[SnapshotComment, ...]
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


def markers_present(snapshot: IssueSnapshot) -> tuple[str, ...]:
    """Every marker line on the ticket, across all its comments."""
    return tuple(line for comment in snapshot.comments for line in comment.marker_lines)


def _by_id(snapshot: IssueSnapshot) -> dict[str, tuple[str, ...]]:
    """Comment id to its marker lines. Callers reject duplicate ids first."""
    return {comment.id: comment.marker_lines for comment in snapshot.comments}


def _duplicate_reason(snapshot: IssueSnapshot, label: str) -> str | None:
    """A read may not repeat a comment id or a marker line: that is inconsistent."""
    ids = Counter(comment.id for comment in snapshot.comments)
    dup_ids = sorted(k for k, n in ids.items() if n > 1)
    if dup_ids:
        return f"{label} snapshot repeats comment ids: {dup_ids}"
    lines = Counter(
        line for comment in snapshot.comments for line in comment.marker_lines
    )
    dup_markers = sorted(k for k, n in lines.items() if n > 1)
    if dup_markers:
        return f"{label} snapshot repeats marker lines: {dup_markers}"
    return None


def _removed_reason(
    before: Mapping[str, tuple[str, ...]], after: Mapping[str, tuple[str, ...]]
) -> str | None:
    """No comment the turn started with may disappear, nor lose a marker line."""
    removed_ids = sorted(cid for cid in before if cid not in after)
    if removed_ids:
        return f"comment ids removed: {removed_ids}"
    lost = sorted(
        line
        for cid, lines in before.items()
        for line in lines
        if line not in after[cid]
    )
    if lost:
        return f"marker lines removed: {lost}"
    return None


def _existing_reason(
    before: Mapping[str, tuple[str, ...]], after: Mapping[str, tuple[str, ...]]
) -> str | None:
    """A comment present before the turn may not gain a marker line in it."""
    gained = sorted(
        line
        for cid, lines in after.items()
        if cid in before
        for line in lines
        if line not in before[cid]
    )
    if gained:
        return f"marker lines added to an existing comment: {gained}"
    return None


def _pairing_reason(
    before: Mapping[str, tuple[str, ...]],
    after: Mapping[str, tuple[str, ...]],
    expected_markers: Sequence[str],
) -> str | None:
    """Each new comment carries exactly one expected marker; each expected once."""
    expected = set(expected_markers)
    if len(expected_markers) != len(expected):
        return "expected marker lines repeat: the turn expects each marker once"
    carried: Counter[str] = Counter()
    for cid, lines in after.items():
        if cid in before:
            continue
        if len(lines) != 1:
            return f"new comment {cid!r} carries {len(lines)} marker lines, expected 1"
        (line,) = lines
        if line not in expected:
            return f"unexpected marker line appeared: {line!r}"
        carried[line] += 1
    missing = sorted(expected - set(carried))
    if missing:
        return f"expected marker lines missing after the turn: {missing}"
    return None


def _delta_reason(
    before: IssueSnapshot, after: IssueSnapshot, expected_markers: Sequence[str]
) -> str | None:
    """Only the expected markers, each on one new comment, may appear.

    Duplicates within a single read are rejected first. The comparison is by
    comment id and by the marker lines each comment carries, so an edit that
    adds a marker to an existing comment is caught even when the new comment
    count still matches.
    """
    for snapshot, label in ((before, "before"), (after, "after")):
        duplicate = _duplicate_reason(snapshot, label)
        if duplicate is not None:
            return duplicate
    old, new = _by_id(before), _by_id(after)
    return (
        _removed_reason(old, new)
        or _existing_reason(old, new)
        or _pairing_reason(old, new, expected_markers)
    )


def compare_snapshots(
    before: IssueSnapshot,
    after: IssueSnapshot,
    *,
    ticket_id: str,
    expected_markers: Sequence[str],
) -> str | None:
    """``None`` when the delta is exactly *expected_markers*; else a reason.

    Accepted: each expected marker on exactly one new comment, nothing removed,
    no existing comment gaining a marker, and the state, assignee and sorted
    labels unchanged. Any other new comment, or a changed field, is rejected
    with its own reason.
    """
    return (
        _identity_reason(before, after, ticket_id)
        or _field_reason(before, after)
        or _delta_reason(before, after, expected_markers)
    )
