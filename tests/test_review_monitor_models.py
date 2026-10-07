"""Characterization tests for the review-monitor data models (#2499).

Covers ``ThreadStatus``, ``CommentReviewRef``, ``MonitoredPR`` and
``MonitorState`` as they exist in ``.claude/scripts/review_monitor.py`` (moving
to ``review_monitor_lib/models.py``). ``MonitoredPR.to_dict()``'s key set is
the persisted JSON schema of every state file, so it is pinned exactly.
"""

from __future__ import annotations

import pytest

from tests import _review_monitor_helpers as helpers

_PERSISTED_PR_KEYS = {
    "role",
    "repo",
    "repo_path",
    "pr_number",
    "last_seen_sha",
    "delta_base_sha",
    "registered_at",
    "last_checked_at",
    "last_nudge_at",
    "nudge_count",
    "our_review_id",
    "our_threads",
    "thread_status",
    "delta_findings",
    "status",
    "slack_channel",
    "slack_ts",
    "slack_last_seen_ts",
    "last_notified_state",
    "last_notified_at",
    "last_escalated_at",
    "escalation_count",
    "auto_fix_attempts_today",
    "auto_fix_attempt_date",
    "last_auto_fix_at",
    "last_channel_bump_at",
    "channel_bump_count",
    "state_entered_at",
    "comment_reviews",
    "awaiting_rereview",
}


def _thread(**fields: object) -> object:
    return helpers.get("ThreadStatus")(**{"file": "a.py", "line": 3, **fields})


@pytest.mark.parametrize(
    ("fields", "addressed"),
    [
        ({}, False),
        ({"deferred": True}, False),
        ({"resolved": True}, True),
        ({"replied": True}, True),
        ({"code_changed": True}, True),
    ],
)
def test_thread_is_addressed_ignores_deferral_alone(
    fields: dict[str, bool], addressed: bool
) -> None:
    assert _thread(**fields).is_addressed is addressed


def test_thread_status_round_trips_and_defaults_missing_flags() -> None:
    thread_cls = helpers.get("ThreadStatus")
    original = _thread(line=None, resolved=True, deferred=True)

    assert thread_cls.from_dict(original.to_dict()) == original
    assert thread_cls.from_dict({"file": "b.py", "line": 7}) == thread_cls(
        file="b.py", line=7
    )


def test_comment_review_ref_round_trips_with_defaults() -> None:
    ref_cls = helpers.get("CommentReviewRef")
    minimal = {"review_id": "9", "author": "bob", "submitted_at": "t", "body": "b"}

    ref = ref_cls.from_dict(minimal)

    assert ref.classification == "unclassified"
    assert ref.classified_at is None
    assert ref_cls.from_dict(ref.to_dict()) == ref


def test_post_init_stamps_timestamps_and_defaults_delta_base() -> None:
    pr = helpers.make_pr()

    assert pr.registered_at
    assert pr.last_checked_at == pr.registered_at
    assert pr.delta_base_sha == "deadbeef"


def test_post_init_keeps_explicit_values() -> None:
    pr = helpers.make_pr(
        registered_at="2026-01-01T00:00:00+00:00",
        last_checked_at="2026-01-02T00:00:00+00:00",
        delta_base_sha="base",
    )

    assert pr.registered_at == "2026-01-01T00:00:00+00:00"
    assert pr.last_checked_at == "2026-01-02T00:00:00+00:00"
    assert pr.delta_base_sha == "base"


def test_thread_rollups_report_unaddressed_ids() -> None:
    pr = helpers.make_pr(
        thread_status={"t1": _thread(resolved=True), "t2": _thread()},
    )

    assert pr.all_threads_addressed() is False
    assert pr.unaddressed_threads() == ["t2"]
    assert helpers.make_pr().all_threads_addressed() is True


def test_monitored_pr_to_dict_key_set_is_the_persisted_schema() -> None:
    assert set(helpers.make_pr().to_dict()) == _PERSISTED_PR_KEYS


def test_monitored_pr_round_trips_nested_records() -> None:
    ref = helpers.get("CommentReviewRef")(
        review_id="5", author="bob", submitted_at="t", body="please fix"
    )
    pr = helpers.make_pr(
        thread_status={"t1": _thread(replied=True)},
        comment_reviews={"5": ref},
        our_threads=["t1"],
        nudge_count=2,
        awaiting_rereview=True,
    )

    restored = helpers.get("MonitoredPR").from_dict(pr.to_dict())

    assert restored == pr


def test_monitored_pr_from_minimal_dict_uses_field_defaults() -> None:
    restored = helpers.get("MonitoredPR").from_dict(
        {
            "role": "reviewer",
            "repo": helpers.REPO,
            "repo_path": "/r",
            "pr_number": 7,
            "last_seen_sha": "abc",
        }
    )

    assert restored.status == "watching"
    assert restored.delta_base_sha == "abc"
    assert restored.thread_status == {}
    assert restored.comment_reviews == {}
    assert restored.awaiting_rereview is False


def test_complete_pr_moves_entry_with_reason_and_timestamp() -> None:
    state = helpers.get("MonitorState")(
        monitored={helpers.KEY: helpers.make_pr()}, completed={}
    )

    state.complete_pr(helpers.KEY, "merged")

    assert state.monitored == {}
    entry = state.completed[helpers.KEY]
    assert entry["reason"] == "merged"
    assert entry["completed_at"]
    assert entry["pr_number"] == helpers.PR_NUMBER


def test_complete_pr_on_missing_key_warns_and_changes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = helpers.get("MonitorState")(monitored={}, completed={})

    with caplog.at_level("WARNING"):
        state.complete_pr("acme/widgets#9", "merged")

    assert state.completed == {}
    assert any("acme/widgets#9" in r.message for r in caplog.records)


def test_monitor_state_round_trips() -> None:
    state_cls = helpers.get("MonitorState")
    state = state_cls(
        monitored={helpers.KEY: helpers.make_pr()},
        completed={"acme/widgets#1": {"reason": "closed"}},
    )

    assert state_cls.from_dict(state.to_dict()) == state
    assert state_cls.from_dict({}) == state_cls(monitored={}, completed={})
