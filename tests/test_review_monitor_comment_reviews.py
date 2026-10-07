"""Characterization tests for review-monitor comment-review tracking (#2499).

Covers bot detection, engaged-reviewer detection, comment-review collection
and refresh, the fallback pending-review surface and
``cmd_mark_comment_review`` (moving to
``review_monitor_lib/comment_reviews.py``). Reviews payloads are
hand-authored from documented REST fields (``id``, ``state``, ``body``,
``submitted_at``, ``user.login``); see ``tests/test_review_monitor.py`` for
the blank-body reconstruction cases.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests import _review_monitor_helpers as helpers

_ME = "matt-w"
_REVIEWS_ROUTE = ("gh", "api", "repos/acme/widgets/pulls/42/reviews")
_COMMENTS_ROUTE = ("gh", "api", "repos/acme/widgets/pulls/42/comments")


def _review(
    rid: int,
    login: str,
    state: str = "COMMENTED",
    body: str = "please fix",
    at: str = "2026-09-02",
) -> dict[str, Any]:
    return {
        "id": rid,
        "state": state,
        "body": body,
        "submitted_at": at,
        "user": {"login": login},
    }


def _ref(rid: str, at: str = "2026-09-02", classification: str = "unclassified") -> Any:
    return helpers.get("CommentReviewRef")(
        review_id=rid,
        author="bob",
        submitted_at=at,
        body=f"body {rid}",
        classification=classification,
    )


@pytest.mark.parametrize(
    ("login", "is_bot"),
    [
        ("", False),
        ("alice", False),
        ("SonarCloud[bot]", False),
        ("sonarqube", False),
        ("Dependabot", True),
        ("coderabbitai", True),
        ("renovate[bot]", True),
        ("sourcery-ai", True),
        ("acme-bot", True),
        ("helper-ai", True),
    ],
)
def test_is_bot_login(login: str, is_bot: bool) -> None:
    assert helpers.get("is_bot_login")(login) is is_bot


@pytest.mark.parametrize(
    ("reviews", "engaged"),
    [
        ([], False),
        ([_review(1, "bob", "APPROVED")], False),
        ([_review(1, _ME)], False),
        ([_review(1, "dependabot[bot]", "CHANGES_REQUESTED")], False),
        ([{"id": 1, "state": "COMMENTED", "user": None}], False),
        ([_review(1, "bob", "CHANGES_REQUESTED")], True),
        ([_review(1, "bob")], True),
    ],
)
def test_has_engaged_human_reviewer(
    reviews: list[dict[str, Any]], engaged: bool
) -> None:
    assert helpers.get("_has_engaged_human_reviewer")(reviews, _ME) is engaged


def test_collect_new_comment_reviews_filters_and_truncates() -> None:
    pr = helpers.make_pr(comment_reviews={"5": _ref("5", classification="neutral")})
    reviews = [
        _review(1, "bob", state="APPROVED"),
        _review(2, "coderabbitai"),
        {"id": 3, "state": "COMMENTED", "body": "x", "user": {}},
        _review(4, "bob", at="2026-09-01"),
        _review(5, "bob", body="new text"),
        _review(6, "carol", body="  "),
        _review(7, "carol", body="y" * 2500),
    ]

    helpers.get("_collect_new_comment_reviews")(pr, reviews, "2026-09-01")

    assert set(pr.comment_reviews) == {"5", "7"}
    assert pr.comment_reviews["5"].classification == "neutral"
    assert pr.comment_reviews["5"].body == "body 5"
    assert pr.comment_reviews["7"].body == "y" * 2000
    assert pr.comment_reviews["7"].author == "carol"


def test_refresh_comment_reviews_unparseable_response_changes_nothing(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers.FakeCommands().add(_REVIEWS_ROUTE, "").install(monkeypatch)
    pr = helpers.make_pr(comment_reviews={"1": _ref("1")})

    engaged = helpers.get("_refresh_comment_reviews")(
        pr, helpers.REPO, 42, sha_changed=True, our_username=_ME
    )

    assert engaged is False
    assert set(pr.comment_reviews) == {"1"}


def test_refresh_comment_reviews_push_clears_then_recollects(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = [_review(2, "bob", at="2026-09-03")]
    helpers.FakeCommands().add(_REVIEWS_ROUTE, json.dumps(reviews)).install(monkeypatch)
    pr = helpers.make_pr(comment_reviews={"1": _ref("1")})

    engaged = helpers.get("_refresh_comment_reviews")(
        pr, helpers.REPO, 42, sha_changed=True, our_username=_ME
    )

    assert engaged is True
    assert set(pr.comment_reviews) == {"2"}


def test_refresh_comment_reviews_formal_review_prunes_older_entries(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = [
        _review(9, "bob", state="APPROVED", at="2026-09-05"),
        {"id": 10, "state": "CHANGES_REQUESTED", "submitted_at": None, "user": None},
    ]
    helpers.FakeCommands().add(_REVIEWS_ROUTE, json.dumps(reviews)).install(monkeypatch)
    pr = helpers.make_pr(
        comment_reviews={
            "old": _ref("old", at="2026-09-04"),
            "new": _ref("new", at="2026-09-06"),
        }
    )

    engaged = helpers.get("_refresh_comment_reviews")(
        pr, helpers.REPO, 42, sha_changed=False, our_username=_ME
    )

    assert engaged is False
    assert set(pr.comment_reviews) == {"new"}


def test_refresh_comment_reviews_fetches_inline_bodies_for_own_blank_review(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = [_review(100, _ME, body="")]
    comments = [{"pull_request_review_id": 100, "line": 3, "body": "inline note"}]
    fake = helpers.FakeCommands()
    fake.add(_REVIEWS_ROUTE, json.dumps(reviews))
    fake.add(_COMMENTS_ROUTE, json.dumps(comments))
    fake.install(monkeypatch)
    pr = helpers.make_pr()

    engaged = helpers.get("_refresh_comment_reviews")(
        pr, helpers.REPO, 42, sha_changed=False, our_username=_ME
    )

    assert engaged is False
    assert pr.comment_reviews["100"].body == "inline note"
    assert fake.argvs() == [_REVIEWS_ROUTE, _COMMENTS_ROUTE]


def test_fetch_inline_bodies_skips_reviewless_and_blank_comments(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comments = [
        {"pull_request_review_id": None, "line": 1, "body": "orphan"},
        {"pull_request_review_id": 7, "line": 2, "body": "   "},
        {"pull_request_review_id": 7, "line": 3, "body": None},
        {"pull_request_review_id": 7, "line": 4, "body": " kept "},
    ]
    helpers.FakeCommands().add(_COMMENTS_ROUTE, json.dumps(comments)).install(
        monkeypatch
    )

    assert helpers.get("_fetch_inline_comment_bodies_by_review")(helpers.REPO, 42) == {
        "7": ["kept"]
    }


_NO_SIGNAL: dict[str, Any] = {
    "unaddressed_count": 0,
    "review_decision": "",
    "merge_blocked": False,
    "merge_state_status": "CLEAN",
    "ci_ok": True,
}


@pytest.mark.parametrize(
    "override",
    [
        {"merge_blocked": True, "merge_state_status": "DIRTY"},
        {"merge_blocked": True, "merge_state_status": "BEHIND"},
        {"ci_ok": False},
        {"unaddressed_count": 1},
        {"review_decision": "CHANGES_REQUESTED"},
    ],
)
def test_pending_comment_reviews_suppressed_by_higher_signal(
    override: dict[str, Any],
) -> None:
    pr = helpers.make_pr(comment_reviews={"1": _ref("1")})

    pending = helpers.get("_collect_pending_comment_reviews")(
        pr, **{**_NO_SIGNAL, **override}
    )

    assert pending == []


def test_pending_comment_reviews_lists_unclassified_for_author_only() -> None:
    reviews = {"1": _ref("1"), "2": _ref("2", classification="neutral")}
    author_pr = helpers.make_pr(comment_reviews=reviews)
    reviewer_pr = helpers.make_pr(role="reviewer", comment_reviews=reviews)
    blocked = {**_NO_SIGNAL, "merge_blocked": True, "merge_state_status": "BLOCKED"}
    collect = helpers.get("_collect_pending_comment_reviews")

    assert collect(author_pr, **blocked) == [
        {
            "review_id": "1",
            "author": "bob",
            "submitted_at": "2026-09-02",
            "body": "body 1",
        }
    ]
    assert collect(reviewer_pr, **_NO_SIGNAL) == []


def test_mark_comment_review_rejects_invalid_classification(
    review_monitor_state_dir: Path,
) -> None:
    result = helpers.get("cmd_mark_comment_review")(42, helpers.REPO, "1", "maybe")

    assert result == {"error": "invalid classification 'maybe'"}


def test_mark_comment_review_reports_missing_pr_and_review(
    review_monitor_state_dir: Path,
) -> None:
    mark = helpers.get("cmd_mark_comment_review")

    assert mark(42, helpers.REPO, "1", "neutral") == {
        "error": f"PR {helpers.KEY} not found in monitored"
    }
    helpers.seed_prs(helpers.make_pr())
    assert mark(42, helpers.REPO, "1", "neutral") == {
        "error": f"review_id 1 not tracked on {helpers.KEY}"
    }


def test_mark_comment_review_persists_verdict(review_monitor_state_dir: Path) -> None:
    helpers.seed_prs(helpers.make_pr(comment_reviews={"1": _ref("1")}))

    result = helpers.get("cmd_mark_comment_review")(
        42, helpers.REPO, "1", "requests_changes"
    )

    assert result == {
        "ok": True,
        "pr_number": 42,
        "review_id": "1",
        "classification": "requests_changes",
    }
    ref = helpers.stored_pr().comment_reviews["1"]
    assert ref.classification == "requests_changes"
    assert ref.classified_at
