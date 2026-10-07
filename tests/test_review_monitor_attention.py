"""Characterization tests for review-monitor attention signals (#2499).

Covers ``_summarize_status_checks`` (CheckRun and StatusContext shapes),
``_resolve_change_request_source``, every row of the
``_compute_attention_state`` precedence chain and
``_compute_needs_escalation`` (moving to ``review_monitor_lib/attention.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests import _review_monitor_helpers as helpers


@pytest.mark.parametrize(
    ("rollup", "failing_names", "pending_count"),
    [
        (helpers.ROLLUP_GREEN, [], 0),
        (helpers.ROLLUP_FAILING, ["lint"], 0),
        (helpers.ROLLUP_PENDING, [], 2),
        (helpers.ROLLUP_CANCELLED_ONLY, ["check"], 0),
        (helpers.ROLLUP_STATUS_CONTEXT_FAIL, ["sonar"], 0),
        ((helpers.status_context("FAILURE", context="legacy"),), ["legacy"], 0),
        ((helpers.checkrun("completed", "neutral"),), [], 0),
        (({"__typename": "CheckRun", "status": None},), [], 0),
        (({"state": None},), [], 0),
    ],
)
def test_summarize_status_checks(
    rollup: tuple[dict[str, Any], ...], failing_names: list[str], pending_count: int
) -> None:
    summary = helpers.get("_summarize_status_checks")(list(rollup))

    assert [f["name"] for f in summary["failing"]] == failing_names
    assert summary["pending_count"] == pending_count
    assert summary["ok"] is (not failing_names)


def test_summarize_status_checks_failure_record_shapes() -> None:
    summary = helpers.get("_summarize_status_checks")(
        [helpers.ROLLUP_FAILING[0], helpers.ROLLUP_STATUS_CONTEXT_FAIL[0]]
    )

    assert summary["failing"] == [
        {
            "workflow": "wf",
            "name": "lint",
            "conclusion": "FAILURE",
            "url": "https://ci/1",
        },
        {"workflow": "", "name": "sonar", "conclusion": "ERROR", "url": "https://ci/2"},
    ]


@pytest.mark.parametrize(
    ("state", "unaddressed", "decision", "actionable", "source"),
    [
        ("ci_failing", 3, "CHANGES_REQUESTED", True, None),
        ("changes_requested", 1, "CHANGES_REQUESTED", True, "inline"),
        ("changes_requested", 0, "CHANGES_REQUESTED", True, "formal"),
        ("changes_requested", 0, "", True, "comment"),
        ("changes_requested", 0, "", False, None),
    ],
)
def test_resolve_change_request_source(
    state: str, unaddressed: int, decision: str, actionable: bool, source: str | None
) -> None:
    assert (
        helpers.get("_resolve_change_request_source")(
            state, unaddressed, decision, actionable
        )
        == source
    )


_QUIET: dict[str, Any] = {
    "role": "author",
    "status": "watching",
    "ci_ok": True,
    "merge_blocked": False,
}


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"role": "reviewer", "ci_ok": False}, None),
        ({"is_draft": True, "ci_ok": False}, None),
        (
            {"merge_blocked": True, "merge_state_status": "DIRTY", "ci_ok": False},
            "merge_blocked",
        ),
        ({"merge_blocked": True, "merge_state_status": "BEHIND"}, "merge_blocked"),
        ({"ci_ok": False, "unaddressed_count": 2}, "ci_failing"),
        (
            {"unaddressed_count": 1, "has_actionable_comment_review": True},
            "changes_requested",
        ),
        ({"review_decision": "CHANGES_REQUESTED"}, "changes_requested"),
        (
            {"has_actionable_comment_review": True, "reviewer_count": 0},
            "changes_requested",
        ),
        ({"review_decision": "REVIEW_REQUIRED", "reviewer_count": 0}, "no_reviewer"),
        ({"review_decision": "REVIEW_REQUIRED"}, None),
        ({"review_decision": "REVIEW_REQUIRED", "reviewer_count": 1}, None),
        (
            {"merge_blocked": True, "merge_state_status": "BLOCKED"},
            "ready_to_approve",
        ),
        ({"status": "ready_to_approve"}, "ready_to_approve"),
        ({}, None),
    ],
)
def test_compute_attention_state_precedence(
    override: dict[str, Any], expected: str | None
) -> None:
    assert helpers.get("_compute_attention_state")(**{**_QUIET, **override}) == expected


def test_needs_escalation_never_without_state_or_for_no_reviewer() -> None:
    needs = helpers.get("_compute_needs_escalation")
    pr = helpers.make_pr()

    assert needs(pr, None) is False
    assert needs(pr, "no_reviewer") is False


@pytest.mark.parametrize("state", ["ci_failing", "merge_blocked"])
def test_needs_escalation_immediate_states_fire_once_per_state(state: str) -> None:
    needs = helpers.get("_compute_needs_escalation")
    pr = helpers.make_pr(
        last_notified_state=state, last_notified_at=helpers.ago(hours=2)
    )

    assert needs(pr, state) is True
    pr.last_escalated_at = helpers.ago(hours=1)
    assert needs(pr, state) is False


@pytest.mark.parametrize(
    ("notified_state", "notified_minutes_ago", "expected"),
    [
        (None, None, False),
        ("ready_to_approve", None, False),
        ("ci_failing", 60, False),
        ("ready_to_approve", 5, False),
        ("ready_to_approve", 20, True),
    ],
)
def test_needs_escalation_ready_to_approve_waits_for_grace(
    notified_state: str | None, notified_minutes_ago: int | None, expected: bool
) -> None:
    pr = helpers.make_pr(
        last_notified_state=notified_state,
        last_notified_at=(
            None
            if notified_minutes_ago is None
            else helpers.ago(minutes=notified_minutes_ago)
        ),
    )

    assert helpers.get("_compute_needs_escalation")(pr, "ready_to_approve") is expected


def test_needs_escalation_new_state_after_old_escalation_fires() -> None:
    pr = helpers.make_pr(
        last_notified_state="ci_failing",
        last_notified_at=helpers.ago(hours=3),
        last_escalated_at=helpers.ago(hours=2),
    )

    assert helpers.get("_compute_needs_escalation")(pr, "merge_blocked") is True
