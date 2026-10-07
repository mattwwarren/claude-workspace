"""Characterization tests for review-monitor nudges and notifications (#2499).

Covers the nudge gate (cooldown, grace period, fail-closed activity check),
the drain-time ``record-*`` / ``mark-*`` recorders, the auto-fix and
channel-bump counters, ``pending-channel-bumps`` and ``catchup`` (moving to
``review_monitor_lib/notify.py``). ``gh pr view --json updatedAt`` output is
hand-authored from that one documented field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from tests import _review_monitor_helpers as helpers

_PR_VIEW = ("gh", "pr", "view", "42", "--json", "updatedAt")


def _activity(monkeypatch: pytest.MonkeyPatch, raw: str) -> helpers.FakeCommands:
    return helpers.FakeCommands().add(_PR_VIEW, raw).install(monkeypatch)


def _updated(**delta: float) -> str:
    return json.dumps({"updatedAt": helpers.ago(**delta)})


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "could not fetch PR activity — skipping nudge to be safe"),
        ("not json", "could not parse PR activity — skipping nudge to be safe"),
        ("{}", "could not parse PR activity — skipping nudge to be safe"),
        (
            '{"updatedAt": "yesterday"}',
            "could not parse PR activity — skipping nudge to be safe",
        ),
    ],
)
def test_activity_check_fails_closed(
    review_monitor_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    reason: str,
) -> None:
    fake = _activity(monkeypatch, raw)

    assert helpers.get("_nudge_activity_check")(42, helpers.REPO) == {
        "allowed": False,
        "reason": reason,
    }
    assert fake.calls == [(_PR_VIEW, helpers.REPO)]


def test_activity_check_blocks_recent_and_passes_stale(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = helpers.get("_nudge_activity_check")

    _activity(monkeypatch, _updated(hours=1))
    assert check(42, helpers.REPO) == {
        "allowed": False,
        "reason": "PR has recent activity",
    }
    _activity(monkeypatch, _updated(hours=30))
    assert check(42, helpers.REPO) is None


def test_nudge_ok_unknown_pr(review_monitor_state_dir: Path) -> None:
    assert helpers.get("cmd_nudge_ok")(42, helpers.REPO) == {
        "allowed": False,
        "reason": f"PR {helpers.KEY} not found in monitored",
    }


@pytest.mark.parametrize(
    ("fields", "reason_prefix"),
    [
        ({"last_nudge_at": "1h", "registered_at": "48h"}, "cooldown active, "),
        ({"registered_at": "1h"}, "grace period — registered "),
    ],
)
def test_nudge_ok_blocked_before_activity_check(
    review_monitor_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, str],
    reason_prefix: str,
) -> None:
    fake = helpers.FakeCommands().install(monkeypatch)
    helpers.seed_prs(
        helpers.make_pr(
            **{k: helpers.ago(hours=int(v[:-1])) for k, v in fields.items()}
        )
    )

    result = helpers.get("cmd_nudge_ok")(42, helpers.REPO)

    assert result["allowed"] is False
    assert result["reason"].startswith(reason_prefix)
    assert fake.calls == []


def test_nudge_ok_surfaces_activity_block(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activity(monkeypatch, _updated(hours=2))
    helpers.seed_prs(helpers.make_pr(registered_at=helpers.ago(hours=48)))

    assert helpers.get("cmd_nudge_ok")(42, helpers.REPO) == {
        "allowed": False,
        "reason": "PR has recent activity",
    }


def test_nudge_ok_allows_never_nudged_pr_with_unparseable_registration(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activity(monkeypatch, _updated(hours=30))
    pr = helpers.make_pr()
    pr.registered_at = "garbage"
    helpers.seed_prs(pr)

    assert helpers.get("cmd_nudge_ok")(42, helpers.REPO) == {
        "allowed": True,
        "reason": "never nudged",
    }


@freeze_time("2026-10-05T16:00:00+00:00")
def test_nudge_ok_allows_after_cooldown(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activity(monkeypatch, _updated(hours=30))
    helpers.seed_prs(
        helpers.make_pr(
            registered_at=helpers.ago(hours=72), last_nudge_at=helpers.ago(hours=30)
        )
    )

    assert helpers.get("cmd_nudge_ok")(42, helpers.REPO) == {
        "allowed": True,
        "reason": "last nudge was 1 day, 6:00:00 ago",
    }


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("cmd_record_nudge", ()),
        ("cmd_mark_notified", ("ci_failing",)),
        ("cmd_mark_escalated", ()),
    ],
)
def test_void_recorders_warn_on_unknown_pr(
    review_monitor_state_dir: Path,
    caplog: pytest.LogCaptureFixture,
    command: str,
    args: tuple[str, ...],
) -> None:
    with caplog.at_level("WARNING"):
        assert helpers.get(command)(42, helpers.REPO, *args) is None

    assert any(
        command in r.message and helpers.KEY in r.message for r in caplog.records
    )
    assert not helpers.get("state_path_for_repo")(helpers.REPO).exists()


@freeze_time("2026-10-05T16:00:00+00:00")
def test_drain_time_recorders_stamp_and_count(review_monitor_state_dir: Path) -> None:
    now = "2026-10-05T16:00:00+00:00"
    helpers.seed_prs(helpers.make_pr(nudge_count=1, escalation_count=2))

    helpers.get("cmd_record_nudge")(42, helpers.REPO)
    helpers.get("cmd_mark_notified")(42, helpers.REPO, "ready_to_approve")
    helpers.get("cmd_mark_escalated")(42, helpers.REPO)

    pr = helpers.stored_pr()
    assert (pr.last_nudge_at, pr.nudge_count) == (now, 2)
    assert (pr.last_notified_state, pr.last_notified_at) == ("ready_to_approve", now)
    assert (pr.last_escalated_at, pr.escalation_count) == (now, 3)


@pytest.mark.parametrize("command", ["cmd_record_auto_fix", "cmd_record_channel_bump"])
def test_counters_report_unknown_pr(
    review_monitor_state_dir: Path, command: str
) -> None:
    assert helpers.get(command)(42, helpers.REPO) == {
        "error": f"PR {helpers.KEY} not found in monitored"
    }


@freeze_time("2026-10-05T16:00:00+00:00")
def test_record_auto_fix_counts_toward_daily_cap(
    review_monitor_state_dir: Path,
) -> None:
    helpers.seed_prs(
        helpers.make_pr(auto_fix_attempts_today=5, auto_fix_attempt_date="2026-10-04")
    )
    record = helpers.get("cmd_record_auto_fix")

    assert record(42, helpers.REPO) == {
        "attempts_today": 1,
        "remaining": 1,
        "capped": False,
    }
    assert record(42, helpers.REPO) == {
        "attempts_today": 2,
        "remaining": 0,
        "capped": True,
    }
    assert record(42, helpers.REPO)["remaining"] == 0
    pr = helpers.stored_pr()
    assert pr.last_auto_fix_at == "2026-10-05T16:00:00+00:00"
    assert pr.auto_fix_attempt_date == "2026-10-05"


@freeze_time("2026-10-05T16:00:00+00:00")
def test_record_channel_bump_stamps_and_counts(review_monitor_state_dir: Path) -> None:
    helpers.seed_prs(helpers.make_pr(channel_bump_count=1))

    assert helpers.get("cmd_record_channel_bump")(42, helpers.REPO) == {
        "last_channel_bump_at": "2026-10-05T16:00:00+00:00",
        "channel_bump_count": 2,
    }
    assert helpers.stored_pr().channel_bump_count == 2


def _stale_ready_pr(number: int, repo: str = helpers.REPO, **fields: Any) -> Any:
    defaults: dict[str, Any] = {
        "repo": repo,
        "pr_number": number,
        "status": "ready_to_approve",
        "state_entered_at": "2026-10-05T12:00:00+00:00",
    }
    return helpers.make_pr(**{**defaults, **fields})


@freeze_time("2026-10-05T16:00:00+00:00")
def test_pending_channel_bumps_lists_stale_author_prs_across_repos(
    review_monitor_state_dir: Path,
) -> None:
    helpers.seed_prs(
        _stale_ready_pr(2),
        _stale_ready_pr(1, last_channel_bump_at="2026-10-04T12:00:00+00:00"),
        _stale_ready_pr(3, role="reviewer"),
        _stale_ready_pr(4, status="watching"),
        _stale_ready_pr(5, state_entered_at="2026-10-05T15:00:00+00:00"),
    )
    helpers.seed_prs(_stale_ready_pr(7, repo="zeta/app"))

    assert helpers.get("cmd_pending_channel_bumps")() == [
        {
            "repo": helpers.REPO,
            "pr_number": 1,
            "business_minutes_in_state": 240,
            "last_channel_bump_at": "2026-10-04T12:00:00+00:00",
        },
        {
            "repo": helpers.REPO,
            "pr_number": 2,
            "business_minutes_in_state": 240,
            "last_channel_bump_at": None,
        },
        {
            "repo": "zeta/app",
            "pr_number": 7,
            "business_minutes_in_state": 240,
            "last_channel_bump_at": None,
        },
    ]


@freeze_time("2026-10-05T16:00:00+00:00")
def test_catchup_marks_only_unnotified_author_attention_prs(
    review_monitor_state_dir: Path,
) -> None:
    helpers.seed_prs(
        helpers.make_pr(pr_number=1, status="ready_to_approve"),
        helpers.make_pr(pr_number=2, status="ready_to_approve", role="reviewer"),
        helpers.make_pr(
            pr_number=3, status="ready_to_approve", last_notified_state="ci_failing"
        ),
        helpers.make_pr(pr_number=4, status="watching"),
    )
    helpers.seed_prs(helpers.make_pr(repo="zeta/app", pr_number=9))
    quiet_state = helpers.get("state_path_for_repo")("zeta/app")
    quiet_before = quiet_state.read_bytes()

    assert helpers.get("cmd_catchup")() == {"marked": ["acme/widgets#1"]}

    marked = helpers.stored_pr("acme/widgets#1")
    assert marked.last_notified_state == "ready_to_approve"
    assert marked.last_notified_at == "2026-10-05T16:00:00+00:00"
    assert helpers.stored_pr("acme/widgets#3").last_notified_state == "ci_failing"
    assert quiet_state.read_bytes() == quiet_before
