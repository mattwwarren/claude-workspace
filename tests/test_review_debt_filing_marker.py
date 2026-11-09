"""Tests for the review-debt body markers and dedup match (#3041)."""

from __future__ import annotations

import pytest

from cw.review_debt_filing import debt_marker, pr_marker
from cw.review_debt_filing._marker import (
    body_matches,
    has_pr_marker,
    parse_debt_key,
    parse_pr_markers,
    queue_ticket_ref,
    scope_prefix,
    with_pr_marker,
)
from tests._review_debt_helpers import _debt_body

_KEY = "f" * 64
_PR = "https://github.com/owner/repo/pull/7"


def _body(ref: str = "#3041", key: str = _KEY) -> str:
    return f"{scope_prefix(ref)} `dev/3041`.\n\ntext\n\n{debt_marker(key)}\n"


def test_markers_have_the_documented_shape() -> None:
    assert debt_marker(_KEY) == f"<!-- cw-review-debt: {_KEY} -->"
    assert pr_marker(_PR) == f"<!-- cw-review-debt-pr: {_PR} -->"


def test_a_body_for_the_same_key_and_ticket_matches() -> None:
    assert body_matches(_body(), _KEY, "#3041")


def test_the_same_key_under_another_queue_ticket_is_a_miss() -> None:
    assert not body_matches(_body(ref="#9999"), _KEY, "#3041")


def test_another_key_is_a_miss() -> None:
    assert not body_matches(_body(key="e" * 64), _KEY, "#3041")


def test_an_empty_body_is_a_miss() -> None:
    assert not body_matches("", _KEY, "#3041")


def test_a_marker_mentioned_inline_is_not_a_match() -> None:
    body = f"{scope_prefix('#3041')} x.\nsee {debt_marker(_KEY)} above\n"
    assert not body_matches(body, _KEY, "#3041")


def test_pr_marker_round_trip() -> None:
    body = "line one\n\n"
    linked = with_pr_marker(body, _PR)
    assert linked == f"line one\n{pr_marker(_PR)}\n"
    assert has_pr_marker(linked, _PR)
    assert not has_pr_marker(body, _PR)
    assert not has_pr_marker(linked, "https://github.com/owner/repo/pull/8")


_PR2 = "https://github.com/owner/repo/pull/8"


class TestParsePrMarkers:
    def test_none(self) -> None:
        assert parse_pr_markers(_body()) == []
        assert parse_pr_markers("") == []

    def test_one(self) -> None:
        assert parse_pr_markers(with_pr_marker(_body(), _PR)) == [_PR]

    def test_several_keep_body_order(self) -> None:
        body = with_pr_marker(with_pr_marker(_body(), _PR2), _PR)
        assert parse_pr_markers(body) == [_PR2, _PR]

    def test_duplicate_lines_are_deduped(self) -> None:
        body = with_pr_marker(with_pr_marker(_body(), _PR), _PR)
        assert parse_pr_markers(body) == [_PR]

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert parse_pr_markers(f"x\n   {pr_marker(_PR)}  \t\n") == [_PR]

    def test_unterminated_or_malformed_markers_are_ignored(self) -> None:
        body = (
            "<!-- cw-review-debt-pr: https://github.com/o/r/pull/1\n"
            "<!-- cw-review-debt-pr:https://github.com/o/r/pull/2 -->\n"
            "<!-- cw-review-debt-pr: https://github.com/o/r/pull/3 --\n"
        )
        assert parse_pr_markers(body) == []

    def test_a_marker_inside_prose_is_ignored(self) -> None:
        assert parse_pr_markers(f"see {pr_marker(_PR)} here\n") == []

    @pytest.mark.parametrize(
        "line", ["<!-- cw-review-debt-pr:  -->", "<!-- cw-review-debt-pr: -->"]
    )
    def test_an_empty_url_is_ignored(self, line: str) -> None:
        assert parse_pr_markers(f"x\n{line}\n") == []

    def test_the_key_marker_is_not_a_pr_marker(self) -> None:
        assert parse_pr_markers(debt_marker(_KEY)) == []


class TestParseDebtKey:
    def test_parses(self) -> None:
        assert parse_debt_key(_body()) == _KEY

    def test_absent(self) -> None:
        assert parse_debt_key("no marker here\n") is None

    @pytest.mark.parametrize(
        "line",
        [
            f"<!-- cw-review-debt: {_KEY}",
            "<!-- cw-review-debt:  -->",
            f"inline {debt_marker(_KEY)}",
        ],
    )
    def test_malformed(self, line: str) -> None:
        assert parse_debt_key(f"first\n{line}\n") is None

    def test_a_pr_marker_is_not_a_key(self) -> None:
        assert parse_debt_key(pr_marker(_PR)) is None


class TestQueueTicketRef:
    def test_parses_the_scope_line(self) -> None:
        assert queue_ticket_ref(_body()) == "3041"
        assert queue_ticket_ref(_debt_body(_KEY, queue_ticket="77")) == "77"

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "Some other first line\n",
            f"x\n{scope_prefix('#3041')} `dev/3041`.\n",
            f"{scope_prefix('ABC-12')} `dev/x`.\n",
            "Deferred during review of #3041 on a branch\n",
        ],
    )
    def test_none_on_another_first_line(self, body: str) -> None:
        assert queue_ticket_ref(body) is None
