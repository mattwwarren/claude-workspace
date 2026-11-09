"""Tests for :func:`cw.review_debt_filing.link_pr` (#3041, R11)."""

from __future__ import annotations

import pytest

from cw.review_debt_filing import link_pr, pr_marker
from tests._fake_gh import FakeGh
from tests._review_debt_helpers import _debt_record

_PR = "https://github.com/owner/repo/pull/9"


def _gh_with(number: int, body: str = "debt body\n") -> FakeGh:
    gh = FakeGh()
    gh.bodies[number] = body
    return gh


def test_adds_the_marker_posts_one_comment_and_sets_pr_url() -> None:
    gh = _gh_with(101)
    result = link_pr([_debt_record()], _PR, gh)
    assert gh.bodies[101] == f"debt body\n{pr_marker(_PR)}\n"
    assert len(gh.comments["101"]) == 1
    assert _PR in gh.comments["101"][0]
    assert [r.pr_url for r in result.records] == [_PR]
    assert (result.linked, result.already_linked, result.failed) == (1, 0, 0)
    assert all(snapshot == () for snapshot in gh.lock_snapshots)


def test_a_second_run_is_a_no_op() -> None:
    gh = _gh_with(101)
    first = link_pr([_debt_record()], _PR, gh)
    calls = len(gh.calls)
    second = link_pr(first.records, _PR, gh)
    assert len(gh.calls) == calls
    assert (second.linked, second.already_linked) == (0, 1)
    assert second.records == []


@pytest.mark.parametrize("source", ["create", "reuse"])
def test_the_create_and_reuse_paths_share_one_code_path(source: str) -> None:
    """Both finalize paths hand ``link_pr`` a PR URL; only its source differs."""
    pr = f"https://github.com/owner/repo/pull/{1 if source == 'create' else 2}"
    gh = _gh_with(101)
    result = link_pr([_debt_record()], pr, gh)
    assert result.linked == 1
    assert pr_marker(pr) in gh.bodies[101]


def test_a_body_already_carrying_the_marker_gets_no_edit_or_comment() -> None:
    gh = _gh_with(101, f"body\n{pr_marker(_PR)}\n")
    result = link_pr([_debt_record()], _PR, gh)
    assert gh.comments == {}
    assert not any(c.startswith("update_issue_body") for c in gh.calls)
    assert (result.already_linked, [r.pr_url for r in result.records]) == (1, [_PR])


@pytest.mark.parametrize("fail", ["fetch", "update", "comment"])
def test_a_gh_failure_degrades(fail: str) -> None:
    gh = _gh_with(101)
    if fail == "fetch":
        gh.fail_fetch_body = {"101"}
    elif fail == "update":
        gh.fail_update_issue_body_at = {0}
    else:
        gh.fail_post_comment = {"101"}
    result = link_pr([_debt_record()], _PR, gh)
    assert (result.failed, result.records) == (1, [])


def test_emitted_and_linear_records_are_skipped() -> None:
    gh = FakeGh()
    records = [
        _debt_record(mode="emitted", ticket_id=None, ticket_url=None),
        _debt_record(tracker="linear", mode="emitted", ticket_id="ENG-1"),
        _debt_record(ticket_id="#7"),
    ]
    result = link_pr(records, _PR, gh)
    assert gh.calls == []
    assert result.skipped == len(records)
