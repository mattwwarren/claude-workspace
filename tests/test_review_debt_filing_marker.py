"""Tests for the review-debt body markers and dedup match (#3041)."""

from __future__ import annotations

from cw.review_debt_filing import debt_marker, pr_marker
from cw.review_debt_filing._marker import (
    body_matches,
    has_pr_marker,
    scope_prefix,
    with_pr_marker,
)

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
