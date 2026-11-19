"""The before/after snapshot comparator for a ``per_server`` turn (O2b, P1.6)."""

from __future__ import annotations

import pytest

from cw.tracker_io import IssueSnapshot, compare_snapshots
from cw.tracker_io._prompt import TurnStatus
from cw.tracker_io._snapshot_check import (
    SNAPSHOT_SCHEMA,
    SnapshotComment,
    markers_present,
    parse_snapshot,
)

_TICKET = "ENG-1"
_M1 = "<!-- cw-outbox:m:1/2 -->"
_M2 = "<!-- cw-outbox:m:2/2 -->"

Comments = tuple[tuple[str, tuple[str, ...]], ...]


def _snap(
    *,
    status: TurnStatus = "ok",
    ticket_id: str = _TICKET,
    comments: Comments = (),
    state: str = "Todo",
    assignee: str = "",
    labels: tuple[str, ...] = (),
) -> IssueSnapshot:
    return IssueSnapshot(
        status=status,
        detail="",
        ticket_id=ticket_id,
        comments=tuple(
            SnapshotComment(id=cid, marker_lines=lines) for cid, lines in comments
        ),
        state=state,
        assignee=assignee,
        labels=labels,
    )


class TestParse:
    def test_valid_document_parses(self) -> None:
        doc = (
            '{"status":"ok","detail":"","ticket_id":"ENG-1",'
            '"comments":[{"id":"c1","marker_lines":["x"]}],'
            '"state":"Todo","assignee":"","labels":["a"]}'
        )

        parsed = parse_snapshot(doc)

        assert parsed is not None
        assert parsed.comments == (SnapshotComment(id="c1", marker_lines=("x",)),)
        assert markers_present(parsed) == ("x",)
        assert parsed.labels == ("a",)

    def test_extra_key_is_rejected(self) -> None:
        doc = (
            '{"status":"ok","detail":"","ticket_id":"ENG-1","comments":[],'
            '"state":"","assignee":"","labels":[],"extra":1}'
        )

        assert parse_snapshot(doc) is None

    def test_comment_without_marker_lines_key_is_rejected(self) -> None:
        doc = (
            '{"status":"ok","detail":"","ticket_id":"ENG-1",'
            '"comments":[{"id":"c1"}],"state":"","assignee":"","labels":[]}'
        )

        assert parse_snapshot(doc) is None

    def test_bad_status_is_rejected(self) -> None:
        doc = (
            '{"status":"maybe","detail":"","ticket_id":"ENG-1","comments":[],'
            '"state":"","assignee":"","labels":[]}'
        )

        assert parse_snapshot(doc) is None

    def test_absent_content_is_none(self) -> None:
        assert parse_snapshot(None) is None
        assert parse_snapshot("") is None

    def test_schema_is_strict_and_requires_every_key(self) -> None:
        assert SNAPSHOT_SCHEMA["additionalProperties"] is False
        assert set(SNAPSHOT_SCHEMA["required"]) == {
            "status",
            "detail",
            "ticket_id",
            "comments",
            "state",
            "assignee",
            "labels",
        }


class TestAccepted:
    def test_delta_is_exactly_the_expected_markers(self) -> None:
        before = _snap(comments=(("c0", ()),))
        after = _snap(comments=(("c0", ()), ("c1", (_M1,)), ("c2", (_M2,))))

        assert (
            compare_snapshots(
                before, after, ticket_id=_TICKET, expected_markers=(_M1, _M2)
            )
            is None
        )

    def test_empty_delta_with_empty_expected_set_is_accepted(self) -> None:
        before = _snap(comments=(("c0", (_M1,)),))
        after = _snap(comments=(("c0", (_M1,)),))

        assert (
            compare_snapshots(before, after, ticket_id=_TICKET, expected_markers=())
            is None
        )

    def test_labels_in_a_different_order_are_accepted(self) -> None:
        before = _snap(labels=("a", "b"))
        after = _snap(labels=("b", "a"))

        assert (
            compare_snapshots(before, after, ticket_id=_TICKET, expected_markers=())
            is None
        )


class TestRejected:
    @pytest.mark.parametrize(
        ("before", "after", "expected", "fragment"),
        [
            pytest.param(
                _snap(),
                _snap(comments=(("c1", (_M1,)),)),
                (),
                "unexpected marker",
                id="extra-marker",
            ),
            pytest.param(
                _snap(),
                _snap(comments=(("c1", ()),)),
                (_M1,),
                "carries 0 marker lines",
                id="expected-marker-stripped",
            ),
            pytest.param(
                _snap(),
                _snap(comments=(("c1", (_M1,)), ("c2", ()))),
                (_M1,),
                "carries 0 marker lines",
                id="extra-unmarked-comment",
            ),
            pytest.param(
                _snap(),
                _snap(comments=(("c1", (_M1, _M2)),)),
                (_M1, _M2),
                "carries 2 marker lines",
                id="one-comment-two-markers",
            ),
            pytest.param(
                _snap(),
                _snap(comments=(("c1", (_M1,)), ("c2", (_M1,)))),
                (_M1,),
                "repeats marker lines",
                id="marker-on-two-comments",
            ),
            pytest.param(
                _snap(comments=(("c0", ()),)),
                _snap(),
                (),
                "comment ids removed",
                id="removed-comment-id",
            ),
            pytest.param(
                _snap(comments=(("c0", (_M1,)),)),
                _snap(comments=(("c0", ()),)),
                (),
                "marker lines removed",
                id="removed-marker",
            ),
            pytest.param(
                _snap(comments=(("c0", ()),)),
                _snap(comments=(("c0", (_M1,)), ("c1", (_M2,)))),
                (_M1,),
                "marker lines added to an existing comment",
                id="marker-added-to-existing-comment-plus-new-comment",
            ),
            pytest.param(
                _snap(state="Todo"),
                _snap(state="Done"),
                (),
                "state changed",
                id="state",
            ),
            pytest.param(
                _snap(assignee="a"),
                _snap(assignee="b"),
                (),
                "assignee changed",
                id="assignee",
            ),
            pytest.param(
                _snap(labels=("a",)),
                _snap(labels=("b",)),
                (),
                "labels changed",
                id="labels",
            ),
        ],
    )
    def test_each_change_gets_its_own_reason(
        self,
        before: IssueSnapshot,
        after: IssueSnapshot,
        expected: tuple[str, ...],
        fragment: str,
    ) -> None:
        reason = compare_snapshots(
            before, after, ticket_id=_TICKET, expected_markers=expected
        )

        assert reason is not None
        assert fragment in reason

    def test_repeated_comment_id_in_one_read_is_rejected(self) -> None:
        dup = _snap(comments=(("c1", ()), ("c1", ())))

        reason = compare_snapshots(dup, _snap(), ticket_id=_TICKET, expected_markers=())

        assert reason is not None
        assert "repeats comment ids" in reason

    def test_repeated_expected_marker_is_rejected(self) -> None:
        after = _snap(comments=(("c1", (_M1,)),))

        reason = compare_snapshots(
            _snap(), after, ticket_id=_TICKET, expected_markers=(_M1, _M1)
        )

        assert reason is not None
        assert "repeat" in reason

    @pytest.mark.parametrize(
        "status", ["failed", "tool_unavailable", "already_present"]
    )
    def test_non_ok_status_on_either_side_is_rejected(self, status: TurnStatus) -> None:
        bad = _snap(status=status)
        good = _snap()

        assert "snapshot status" in (
            compare_snapshots(bad, good, ticket_id=_TICKET, expected_markers=()) or ""
        )
        assert "snapshot status" in (
            compare_snapshots(good, bad, ticket_id=_TICKET, expected_markers=()) or ""
        )

    def test_ticket_id_mismatch_is_rejected(self) -> None:
        reason = compare_snapshots(
            _snap(ticket_id="ENG-9"),
            _snap(),
            ticket_id=_TICKET,
            expected_markers=(),
        )

        assert reason is not None
        assert "ticket id" in reason
