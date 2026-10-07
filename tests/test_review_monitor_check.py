"""Characterization tests for the review-monitor ``check`` cycle (#2499).

Drives ``cmd_check`` end to end through the ``FakeCommands`` router (moving to
``review_monitor_lib/check.py`` with ``_complete_terminal_pr`` and
``_derive_check_signals``). The result key set is the JSON contract the
``/review-monitor`` skill consumes, so it is pinned exactly. ``gh pr view``,
``gh review view`` and reviews payloads are hand-authored from the fields the
code reads.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests import _review_monitor_helpers as helpers

if TYPE_CHECKING:
    from pathlib import Path

_ME = "matt-w"
_PR_VIEW = ("gh", "pr", "view", "42", "--json")
_REVIEW_VIEW = ("gh", "review", "view", "42", "--json")
_USER = ("gh", "api", "user")
_REVIEWS = ("gh", "api", "repos/acme/widgets/pulls/42/reviews")
_DIFF = "+++ b/a.py\n@@ -8,0 +9,2 @@\n+x\n+y\n"

_SIGNAL_KEYS = {
    "failing_checks",
    "pending_checks_count",
    "ci_ok",
    "merge_state_status",
    "merge_blocked",
    "attention_state",
    "awaiting_rereview",
    "reviewer_count",
    "needs_local_ping",
    "needs_escalation",
    "auto_fix_ok",
    "auto_fix_blocked_reason",
    "business_minutes_in_state",
    "needs_channel_bump",
    "dm_escalation_reason",
    "head_ref_name",
    "base_ref_name",
    "change_request_source",
    "pending_comment_reviews",
}
_RESULT_KEYS = _SIGNAL_KEYS | {
    "pr_number",
    "pr_state",
    "changed",
    "old_sha",
    "new_sha",
    "role",
    "status",
    "thread_updates",
    "all_addressed",
    "unaddressed",
    "touched_threads",
    "has_delta_diff",
    "slack_channel",
    "slack_ts",
    "slack_last_seen_ts",
    "auto_fix_attempts_today",
    "is_draft",
}


def _pr_view(**fields: Any) -> str:
    view: dict[str, Any] = {
        "state": "OPEN",
        "headRefOid": "deadbeef",
        "headRefName": "feature",
        "baseRefName": "main",
        "isDraft": False,
        "statusCheckRollup": list(helpers.ROLLUP_GREEN),
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "reviewRequests": [{"login": "bob"}],
    }
    view.update(fields)
    return json.dumps(view)


def _thread(tid: str, *comments: tuple[str, str], **fields: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "path": "a.py",
        "line": 10,
        "comments": [
            {"author": {"login": login}, "body": body, "url": f"https://gh/{tid}"}
            for login, body in comments
        ],
        **fields,
    }


def _status(**fields: Any) -> Any:
    return helpers.get("ThreadStatus")(**{"file": "a.py", "line": 10, **fields})


def _fake(
    monkeypatch: pytest.MonkeyPatch,
    pr_view: str,
    threads: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    diff: str = "",
) -> helpers.FakeCommands:
    fake = helpers.FakeCommands()
    fake.add(_PR_VIEW, pr_view)
    fake.add(_USER, _ME)
    fake.add(_REVIEW_VIEW, json.dumps({"threads": threads or []}))
    fake.add(_REVIEWS, json.dumps(reviews or []))
    fake.add(("git", "diff"), diff)
    return fake.install(monkeypatch)


def test_check_unknown_pr_reports_error(review_monitor_state_dir: Path) -> None:
    assert helpers.get("cmd_check")(42, helpers.REPO) == {
        "error": f"PR {helpers.KEY} not found in monitored"
    }


def test_check_merged_completes_and_surfaces_deferred_threads(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads = [_thread("d1", ("bob", "rename"), (_ME, "follow-up in #7"))]
    fake = _fake(monkeypatch, _pr_view(state="MERGED"), threads=threads)
    helpers.seed_prs(helpers.make_pr(thread_status={"d1": _status(deferred=True)}))

    result = helpers.get("cmd_check")(42, helpers.REPO)

    assert result["completed"] is True
    assert (result["pr_state"], result["reason"]) == ("MERGED", "MERGED")
    assert [d["thread_id"] for d in result["deferred_threads"]] == ["d1"]
    assert result["deferred_threads"][0]["deferral_reply"] == "follow-up in #7"
    state = helpers.get("load_state")(helpers.REPO)
    assert state.monitored == {}
    assert state.completed[helpers.KEY]["reason"] == "MERGED"
    assert fake.calls[0] == (
        (
            *_PR_VIEW,
            "headRefOid,headRefName,baseRefName,isDraft,state,mergedAt,closedAt,"
            "statusCheckRollup,mergeStateStatus,reviewDecision,reviewRequests",
        ),
        helpers.REPO,
    )


def test_check_closed_completes_without_deferred_lookup(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(monkeypatch, _pr_view(state="CLOSED"))
    helpers.seed_prs(helpers.make_pr(thread_status={"d1": _status(deferred=True)}))

    result = helpers.get("cmd_check")(42, helpers.REPO)

    assert result == {
        "pr_number": 42,
        "pr_state": "CLOSED",
        "completed": True,
        "reason": "CLOSED",
        "deferred_threads": [],
    }
    assert fake.argvs() == [fake.calls[0][0]]


def test_check_author_new_commit_with_failing_ci(
    review_monitor_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    threads = [_thread("t1", ("bob", "rename"), (_ME, "done"))]
    reviews = [
        {
            "id": 7,
            "state": "COMMENTED",
            "body": "please rename",
            "submitted_at": "2026-09-02T00:00:00Z",
            "user": {"login": "bob"},
        }
    ]
    fake = _fake(
        monkeypatch,
        _pr_view(headRefOid="cafe", statusCheckRollup=list(helpers.ROLLUP_FAILING)),
        threads=threads,
        reviews=reviews,
        diff=_DIFF,
    )
    stale_ref = helpers.get("CommentReviewRef")(
        review_id="3", author="bob", submitted_at="2026-09-01", body="old"
    )
    helpers.seed_prs(
        helpers.make_pr(
            repo_path=str(tmp_path),
            status="ready_to_approve",
            our_threads=["t1"],
            thread_status={"t1": _status()},
            comment_reviews={"3": stale_ref},
            slack_channel="C1",
        )
    )

    result = helpers.get("cmd_check")(42, helpers.REPO)

    assert set(result) == _RESULT_KEYS
    assert result["changed"] is True
    assert (result["old_sha"], result["new_sha"]) == ("deadbeef", "cafe")
    assert result["status"] == "watching"
    assert result["thread_updates"]["t1"]["replied"] is True
    assert result["touched_threads"] == ["t1"]
    assert result["has_delta_diff"] is False
    assert result["attention_state"] == "ci_failing"
    assert result["needs_local_ping"] is True
    assert result["needs_escalation"] is True
    assert [c["name"] for c in result["failing_checks"]] == ["lint"]
    assert result["pending_comment_reviews"] == []
    assert result["awaiting_rereview"] is False
    assert result["auto_fix_ok"] is True
    assert result["slack_channel"] == "C1"
    pr = helpers.stored_pr()
    assert (pr.last_seen_sha, pr.delta_base_sha) == ("cafe", "cafe")
    assert set(pr.comment_reviews) == {"7"}
    assert pr.state_entered_at is not None
    assert ("git", "diff", "deadbeef..cafe") in fake.argvs()


def test_check_author_waiting_on_rereview_surfaces_pending_comment_review(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reviews = [
        {
            "id": 8,
            "state": "COMMENTED",
            "body": "consider a guard",
            "submitted_at": "2026-09-02T00:00:00Z",
            "user": {"login": "bob"},
        }
    ]
    fake = _fake(monkeypatch, _pr_view(), reviews=reviews)
    helpers.seed_prs(
        helpers.make_pr(
            repo_path=str(tmp_path / "gone"),
            last_notified_state="ready_to_approve",
            thread_status={"t1": _status(resolved=True)},
        )
    )

    result = helpers.get("cmd_check")(42, helpers.REPO)

    assert result["changed"] is False
    assert result["status"] == "ready_to_approve"
    assert result["attention_state"] == "ready_to_approve"
    assert result["awaiting_rereview"] is True
    assert result["needs_local_ping"] is False
    assert result["reviewer_count"] == 1
    assert result["pending_comment_reviews"] == [
        {
            "review_id": "8",
            "author": "bob",
            "submitted_at": "2026-09-02T00:00:00Z",
            "body": "consider a guard",
        }
    ]
    assert helpers.stored_pr().awaiting_rereview is True
    assert not any(argv[0] == "git" for argv in fake.argvs())


def test_check_reviewer_delta_is_held_until_acked(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake(monkeypatch, _pr_view(headRefOid="cafe"), diff=_DIFF)
    helpers.seed_prs(
        helpers.make_pr(
            role="reviewer",
            repo_path=str(tmp_path),
            our_threads=["t1"],
            thread_status={"t1": _status()},
        )
    )

    result = helpers.get("cmd_check")(42, helpers.REPO)

    assert set(result) == _RESULT_KEYS | {"delta_diff"}
    assert result["delta_diff"] == _DIFF
    assert result["has_delta_diff"] is True
    assert result["touched_threads"] == ["t1"]
    assert result["attention_state"] is None
    pr = helpers.stored_pr()
    assert (pr.last_seen_sha, pr.delta_base_sha) == ("cafe", "deadbeef")
    assert _REVIEWS not in fake.argvs()


def test_check_reviewer_empty_delta_advances_baseline(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake(monkeypatch, _pr_view(headRefOid="cafe"))
    helpers.seed_prs(helpers.make_pr(role="reviewer", repo_path=str(tmp_path)))

    result = helpers.get("cmd_check")(42, helpers.REPO)

    assert "delta_diff" not in result
    assert result["has_delta_diff"] is False
    assert helpers.stored_pr().delta_base_sha == "cafe"


def test_check_draft_demotes_and_silences_attention(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(monkeypatch, _pr_view(isDraft=True, baseRefName="stack-base"))
    helpers.seed_prs(helpers.make_pr(status="ready_to_approve"))

    result = helpers.get("cmd_check")(42, helpers.REPO)

    assert result["is_draft"] is True
    assert result["status"] == "watching"
    assert result["attention_state"] is None
    assert result["business_minutes_in_state"] == 0
    assert (result["auto_fix_ok"], result["auto_fix_blocked_reason"]) == (
        False,
        "draft stacked on 'stack-base'",
    )


def test_check_unparseable_view_keeps_last_seen_sha(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(monkeypatch, "")
    helpers.seed_prs(helpers.make_pr())

    result = helpers.get("cmd_check")(42, helpers.REPO)

    assert result["pr_state"] == "UNKNOWN"
    assert (result["changed"], result["new_sha"]) == (False, "deadbeef")
    assert result["merge_state_status"] == "UNKNOWN"
    assert (result["head_ref_name"], result["base_ref_name"]) == ("", "")


def test_derive_check_signals_key_set_and_change_request_source(
    review_monitor_state_dir: Path,
) -> None:
    pr = helpers.make_pr(thread_status={"t1": _status()})
    view = {"reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": None}

    signals = helpers.get("_derive_check_signals")(
        pr, view, is_draft=False, has_prior_human_review=True
    )

    assert set(signals) == _SIGNAL_KEYS
    assert signals["attention_state"] == "changes_requested"
    assert signals["change_request_source"] == "inline"
    assert signals["reviewer_count"] == 0
    assert signals["awaiting_rereview"] is False
