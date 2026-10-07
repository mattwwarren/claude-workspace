"""Characterization tests for review-monitor escalation timing (#2499).

Covers business-minute arithmetic (America/New_York, Mon-Fri 8a-6p), the
state-entered stamp, the per-day auto-fix counter and gate, channel-bump
staleness and DM escalation reasons (moving to
``review_monitor_lib/escalation.py``). Fixed instants use 2026-10-05, a
Monday on EDT (UTC-4), so 8a ET is 12:00Z and 6p ET is 22:00Z.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from freezegun import freeze_time

from tests import _review_monitor_helpers as helpers

_MONDAY_NOON_ET = "2026-10-05T16:00:00+00:00"
_TODAY = "2026-10-05"


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).astimezone(UTC)


@pytest.mark.parametrize(
    ("start", "end", "minutes"),
    [
        ("2026-10-05T13:00:00+00:00", "2026-10-05T15:00:00+00:00", 120),
        ("2026-10-05T10:00:00+00:00", "2026-10-05T23:00:00+00:00", 600),
        ("2026-10-05T21:00:00+00:00", "2026-10-06T13:00:00+00:00", 120),
        ("2026-10-09T21:00:00+00:00", "2026-10-12T13:00:00+00:00", 120),
        ("2026-10-10T14:00:00+00:00", "2026-10-11T20:00:00+00:00", 0),
        ("2026-10-05T15:00:00+00:00", "2026-10-05T13:00:00+00:00", 0),
        ("2026-10-05T13:00:00+00:00", "2026-10-05T13:00:00+00:00", 0),
    ],
    ids=[
        "within-day",
        "clamped-day",
        "overnight",
        "over-weekend",
        "weekend",
        "reversed",
        "empty",
    ],
)
def test_business_minutes_between(start: str, end: str, minutes: int) -> None:
    assert helpers.get("_business_minutes_between")(_utc(start), _utc(end)) == minutes


@freeze_time(_MONDAY_NOON_ET)
def test_today_utc_str_and_auto_fix_counter_reset() -> None:
    pr = helpers.make_pr(auto_fix_attempts_today=2, auto_fix_attempt_date="2026-10-04")

    assert helpers.get("_today_utc_str")() == _TODAY
    assert helpers.get("_auto_fix_attempts_today")(pr) == 0
    helpers.get("_reset_auto_fix_counter_if_stale")(pr)
    assert (pr.auto_fix_attempt_date, pr.auto_fix_attempts_today) == (_TODAY, 0)

    pr.auto_fix_attempts_today = 1
    helpers.get("_reset_auto_fix_counter_if_stale")(pr)
    assert pr.auto_fix_attempts_today == 1
    assert helpers.get("_auto_fix_attempts_today")(pr) == 1


@freeze_time(_MONDAY_NOON_ET)
@pytest.mark.parametrize(
    ("notified", "entered", "attention", "expected"),
    [
        ("ci_failing", "old", None, None),
        ("ci_failing", "old", "merge_blocked", _MONDAY_NOON_ET),
        ("ci_failing", "old", "ci_failing", "old"),
        ("ci_failing", None, "ci_failing", _MONDAY_NOON_ET),
    ],
)
def test_ensure_state_entered_at(
    notified: str, entered: str | None, attention: str | None, expected: str | None
) -> None:
    pr = helpers.make_pr(last_notified_state=notified, state_entered_at=entered)

    helpers.get("_ensure_state_entered_at")(pr, attention)

    assert pr.state_entered_at == expected


@pytest.mark.parametrize(
    ("fixed_at", "entered_at", "expected"),
    [
        (None, "2026-10-05T12:00:00+00:00", False),
        ("2026-10-05T12:00:00+00:00", None, False),
        ("garbage", "2026-10-05T12:00:00+00:00", False),
        ("2026-10-05T13:00:00+00:00", "2026-10-05T12:00:00+00:00", True),
        ("2026-10-05T11:00:00+00:00", "2026-10-05T12:00:00+00:00", False),
    ],
)
def test_auto_fix_already_addressed_state(
    fixed_at: str | None, entered_at: str | None, expected: bool
) -> None:
    pr = helpers.make_pr(last_auto_fix_at=fixed_at, state_entered_at=entered_at)

    assert helpers.get("_auto_fix_already_addressed_state")(pr) is expected


@freeze_time(_MONDAY_NOON_ET)
@pytest.mark.parametrize(
    ("fields", "is_draft", "base", "expected"),
    [
        (
            {"auto_fix_attempts_today": 2, "auto_fix_attempt_date": _TODAY},
            False,
            "main",
            (False, "daily cap reached"),
        ),
        (
            {
                "last_auto_fix_at": "2026-10-05T15:00:00+00:00",
                "state_entered_at": "2026-10-05T14:00:00+00:00",
            },
            False,
            "main",
            (False, "already addressed this state — waiting for reviewer"),
        ),
        ({}, True, "feature/base", (False, "draft stacked on 'feature/base'")),
        ({}, True, "master", (False, "draft")),
        (
            {"auto_fix_attempts_today": 2, "auto_fix_attempt_date": "2026-10-04"},
            False,
            "main",
            (True, None),
        ),
    ],
    ids=["cap", "already-addressed", "stacked-draft", "draft", "allowed"],
)
def test_compute_auto_fix_ok(
    fields: dict[str, object],
    is_draft: bool,
    base: str,
    expected: tuple[bool, str | None],
) -> None:
    pr = helpers.make_pr(**fields)

    assert helpers.get("_compute_auto_fix_ok")(pr, is_draft, base) == expected


@freeze_time(_MONDAY_NOON_ET)
@pytest.mark.parametrize(
    ("entered", "minutes"),
    [(None, 0), ("not-a-date", 0), ("2026-10-05T12:00:00+00:00", 240)],
)
def test_business_minutes_in_state(entered: str | None, minutes: int) -> None:
    pr = helpers.make_pr(state_entered_at=entered)

    assert helpers.get("_business_minutes_in_state")(pr) == minutes


@freeze_time(_MONDAY_NOON_ET)
@pytest.mark.parametrize(
    ("attention", "entered", "last_bump", "expected"),
    [
        ("ci_failing", "2026-10-05T12:00:00+00:00", None, False),
        ("ready_to_approve", "2026-10-05T13:00:00+00:00", None, False),
        ("ready_to_approve", "2026-10-05T12:00:00+00:00", None, True),
        ("ready_to_approve", "2026-10-05T12:00:00+00:00", "garbage", True),
        (
            "ready_to_approve",
            "2026-10-05T12:00:00+00:00",
            "2026-10-05T15:00:00+00:00",
            False,
        ),
        (
            "ready_to_approve",
            "2026-10-05T12:00:00+00:00",
            "2026-10-04T15:00:00+00:00",
            True,
        ),
    ],
    ids=[
        "wrong-state",
        "under-threshold",
        "first-bump",
        "bad-last-bump",
        "cooldown",
        "cooled-down",
    ],
)
def test_needs_channel_bump(
    attention: str, entered: str, last_bump: str | None, expected: bool
) -> None:
    pr = helpers.make_pr(state_entered_at=entered, last_channel_bump_at=last_bump)

    assert helpers.get("_needs_channel_bump")(pr, attention) is expected


@freeze_time(_MONDAY_NOON_ET)
@pytest.mark.parametrize(
    ("fields", "attention", "reason"),
    [
        (
            {"role": "reviewer", "registered_at": "2026-09-01T00:00:00+00:00"},
            None,
            None,
        ),
        (
            {
                "last_escalated_at": "2026-10-05T14:00:00+00:00",
                "registered_at": "2026-09-01T00:00:00+00:00",
            },
            None,
            None,
        ),
        (
            {
                "last_escalated_at": "garbage",
                "registered_at": "2026-09-01T00:00:00+00:00",
            },
            None,
            "week_old",
        ),
        (
            {
                "auto_fix_attempts_today": 2,
                "auto_fix_attempt_date": _TODAY,
                "registered_at": "2026-10-05T10:00:00+00:00",
            },
            "changes_requested",
            "loop",
        ),
        (
            {
                "auto_fix_attempts_today": 2,
                "auto_fix_attempt_date": _TODAY,
                "registered_at": "2026-10-05T10:00:00+00:00",
            },
            "ready_to_approve",
            None,
        ),
        (
            {
                "last_escalated_at": "2026-10-05T07:00:00+00:00",
                "registered_at": "2026-09-28T16:00:00+00:00",
            },
            None,
            "week_old",
        ),
        ({"registered_at": "2026-09-28T16:00:01+00:00"}, None, None),
    ],
    ids=[
        "reviewer",
        "cooldown",
        "bad-escalated-at",
        "loop",
        "cap-but-not-failing",
        "week-old",
        "under-a-week",
    ],
)
def test_dm_escalation_reason(
    fields: dict[str, object], attention: str | None, reason: str | None
) -> None:
    pr = helpers.make_pr(**fields)

    assert helpers.get("_dm_escalation_reason")(pr, attention) == reason


def test_dm_escalation_reason_tolerates_unparseable_registered_at() -> None:
    pr = helpers.make_pr()
    pr.registered_at = "garbage"

    assert helpers.get("_dm_escalation_reason")(pr, None) is None
