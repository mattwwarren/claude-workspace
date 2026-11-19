"""The before/after snapshot comparator for a ``per_server`` turn (O2b, P1.6)."""

from __future__ import annotations

import pytest

from cw.tracker_io import IssueSnapshot, compare_snapshots
from cw.tracker_io._prompt import TurnStatus
from cw.tracker_io._snapshot_check import SNAPSHOT_SCHEMA, parse_snapshot

_TICKET = "ENG-1"
_M1 = "<!-- cw-outbox:m:1/2 -->"
_M2 = "<!-- cw-outbox:m:2/2 -->"


def _snap(
    *,
    status: TurnStatus = "ok",
    ticket_id: str = _TICKET,
    comment_ids: tuple[str, ...] = (),
    markers: tuple[str, ...] = (),
    state: str = "Todo",
    assignee: str = "",
    labels: tuple[str, ...] = (),
) -> IssueSnapshot:
    return IssueSnapshot(
        status=status,
        detail="",
        ticket_id=ticket_id,
        comment_ids=comment_ids,
        markers=markers,
        state=state,
        assignee=assignee,
        labels=labels,
    )


class TestParse:
    def test_valid_document_parses(self) -> None:
        doc = (
            '{"status":"ok","detail":"","ticket_id":"ENG-1","comment_ids":["c1"],'
            '"markers":[],"state":"Todo","assignee":"","labels":["a"]}'
        )

        parsed = parse_snapshot(doc)

        assert parsed is not None
        assert parsed.comment_ids == ("c1",)
        assert parsed.labels == ("a",)

    def test_extra_key_is_rejected(self) -> None:
        doc = (
            '{"status":"ok","detail":"","ticket_id":"ENG-1","comment_ids":[],'
            '"markers":[],"state":"","assignee":"","labels":[],"extra":1}'
        )

        assert parse_snapshot(doc) is None

    def test_bad_status_is_rejected(self) -> None:
        doc = (
            '{"status":"maybe","detail":"","ticket_id":"ENG-1","comment_ids":[],'
            '"markers":[],"state":"","assignee":"","labels":[]}'
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
            "comment_ids",
            "markers",
            "state",
            "assignee",
            "labels",
        }


class TestAccepted:
    def test_delta_is_exactly_the_expected_markers(self) -> None:
        before = _snap(comment_ids=("c0",), markers=())
        after = _snap(comment_ids=("c0", "c1", "c2"), markers=(_M1, _M2))

        assert (
            compare_snapshots(
                before, after, ticket_id=_TICKET, expected_markers=(_M1, _M2)
            )
            is None
        )

    def test_empty_delta_with_empty_expected_set_is_accepted(self) -> None:
        before = _snap(comment_ids=("c0",), markers=(_M1,))
        after = _snap(comment_ids=("c0",), markers=(_M1,))

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
                _snap(comment_ids=("c1",), markers=(_M1,)),
                (),
                "unexpected marker",
                id="extra-marker",
            ),
            pytest.param(
                _snap(),
                _snap(comment_ids=("c1",)),
                (_M1,),
                "missing after the turn",
                id="expected-marker-stripped",
            ),
            pytest.param(
                _snap(),
                _snap(comment_ids=("c1", "c2"), markers=(_M1,)),
                (_M1,),
                "new comment ids",
                id="extra-comment-id",
            ),
            pytest.param(
                _snap(comment_ids=("c0",)),
                _snap(),
                (),
                "comment ids removed",
                id="removed-comment-id",
            ),
            pytest.param(
                _snap(markers=(_M1,)),
                _snap(),
                (),
                "marker lines removed",
                id="removed-marker",
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
