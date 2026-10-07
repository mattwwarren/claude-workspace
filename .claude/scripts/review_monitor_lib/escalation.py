"""Business-hours math, auto-fix gating, channel bumps and DM escalation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from review_monitor_lib.models import MonitoredPR


# Business-hour configuration for stale-PR channel bumps
BUSINESS_TZ = ZoneInfo("America/New_York")
BUSINESS_START_HOUR = 8  # 8a ET
BUSINESS_END_HOUR = 18  # 6p ET
FIRST_WEEKEND_WEEKDAY = 5  # datetime.weekday(): Mon=0..Fri=4, Sat=5, Sun=6
STALE_REVIEW_THRESHOLD_MIN = 240  # 4 business hours
CHANNEL_BUMP_COOLDOWN = timedelta(hours=24)
AUTO_FIX_DAILY_CAP = 2

# Minimum wall-clock time between DM escalations for the same PR.
# Without this, _dm_escalation_reason returns "week_old"/"loop" on every cycle
# and the skill fires a DM each time. 4h matches the user-requested cadence.
DM_ESCALATION_COOLDOWN = timedelta(hours=4)


def _today_utc_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _business_minutes_between(start: datetime, end: datetime) -> int:
    """Return whole minutes inside Mon-Fri
    ``BUSINESS_START_HOUR``..``BUSINESS_END_HOUR`` ET."""
    if end <= start:
        return 0
    start_local = start.astimezone(BUSINESS_TZ)
    end_local = end.astimezone(BUSINESS_TZ)
    total = 0
    cursor = start_local
    while cursor < end_local:
        day_start = cursor.replace(
            hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0
        )
        day_end = cursor.replace(
            hour=BUSINESS_END_HOUR, minute=0, second=0, microsecond=0
        )
        if cursor.weekday() < FIRST_WEEKEND_WEEKDAY:
            window_start = max(cursor, day_start)
            window_end = min(end_local, day_end)
            if window_end > window_start:
                total += int((window_end - window_start).total_seconds() // 60)
        next_day = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cursor = next_day
    return total


def _ensure_state_entered_at(pr: MonitoredPR, attention_state: str | None) -> None:
    """Stamp ``state_entered_at`` whenever the attention_state changes."""
    if attention_state is None:
        pr.state_entered_at = None
        return
    if pr.last_notified_state != attention_state or pr.state_entered_at is None:
        # Either freshly entered, or first time we're tracking it under the new schema.
        pr.state_entered_at = datetime.now(UTC).isoformat()


def _reset_auto_fix_counter_if_stale(pr: MonitoredPR) -> None:
    today = _today_utc_str()
    if pr.auto_fix_attempt_date != today:
        pr.auto_fix_attempt_date = today
        pr.auto_fix_attempts_today = 0


def _auto_fix_attempts_today(pr: MonitoredPR) -> int:
    today = _today_utc_str()
    if pr.auto_fix_attempt_date != today:
        return 0
    return pr.auto_fix_attempts_today


def _compute_auto_fix_ok(
    pr: MonitoredPR, is_draft: bool, base_ref_name: str
) -> tuple[bool, str | None]:
    """Decide whether auto-fix is allowed for the given PR right now.

    Returns ``(ok, blocked_reason)``. ``blocked_reason`` is a short string
    suitable for skill output when ``ok`` is False; ``None`` when allowed.

    Rules, in order:
    1. Daily cap (``AUTO_FIX_DAILY_CAP``) — most-frequent block, cheapest check.
    2. Already addressed this state — ``last_auto_fix_at`` is after
       ``state_entered_at``, so we already dispatched an agent for this exact
       attention_state instance. Wait for the reviewer to respond; the state
       transition will reset ``state_entered_at`` and re-enable auto-fix.
    3. Stacked draft (isDraft=True AND base != main/master) — base-PR churn
       would force a wrong rebase target. Caller must skip.
    4. Plain draft (isDraft=True, base == main) — drafts are WIP by definition;
       skip unless the user re-registers explicitly.
    """
    if _auto_fix_attempts_today(pr) >= AUTO_FIX_DAILY_CAP:
        return False, "daily cap reached"
    if _auto_fix_already_addressed_state(pr):
        return False, "already addressed this state — waiting for reviewer"
    if is_draft and base_ref_name not in ("main", "master"):
        return False, f"draft stacked on {base_ref_name!r}"
    if is_draft:
        return False, "draft"
    return True, None


def _auto_fix_already_addressed_state(pr: MonitoredPR) -> bool:
    """Return True if we have already dispatched auto-fix for the current state
    instance.

    The attention_state has a ``state_entered_at`` timestamp that bumps on every
    transition. If we recorded an auto-fix after that bump, the current state
    instance has already been responded to — re-dispatching would just repeat
    work while we wait for the reviewer.
    """
    if not pr.last_auto_fix_at or not pr.state_entered_at:
        return False
    try:
        fixed = datetime.fromisoformat(pr.last_auto_fix_at)
        entered = datetime.fromisoformat(pr.state_entered_at)
    except ValueError:
        return False
    return fixed > entered


def _business_minutes_in_state(pr: MonitoredPR) -> int:
    if pr.state_entered_at is None:
        return 0
    try:
        entered = datetime.fromisoformat(pr.state_entered_at)
    except ValueError:
        return 0
    return _business_minutes_between(entered, datetime.now(UTC))


def _needs_channel_bump(pr: MonitoredPR, attention_state: str | None) -> bool:
    if attention_state != "ready_to_approve":
        return False
    if _business_minutes_in_state(pr) < STALE_REVIEW_THRESHOLD_MIN:
        return False
    if pr.last_channel_bump_at is None:
        return True
    try:
        last = datetime.fromisoformat(pr.last_channel_bump_at)
    except ValueError:
        return True
    return datetime.now(UTC) - last >= CHANNEL_BUMP_COOLDOWN


def _dm_escalation_reason(pr: MonitoredPR, attention_state: str | None) -> str | None:
    """Return a reason string when /review-monitor should fire a DM, else None.

    Reasons (priority order):
      - "loop"         — auto-fix cap hit today on a still-failing state
      - "week_old"     — author PR open ≥ 7 days

    Suppressed when ``last_escalated_at`` is within ``DM_ESCALATION_COOLDOWN``
    of now — prevents one DM per cycle while a long-lived condition persists.
    """
    if pr.role != "author":
        return None
    if pr.last_escalated_at:
        try:
            last = datetime.fromisoformat(pr.last_escalated_at)
            if datetime.now(UTC) - last < DM_ESCALATION_COOLDOWN:
                return None
        except ValueError:
            pass
    if (
        attention_state in ("ci_failing", "merge_blocked", "changes_requested")
        and _auto_fix_attempts_today(pr) >= AUTO_FIX_DAILY_CAP
    ):
        return "loop"
    if pr.registered_at:
        try:
            registered = datetime.fromisoformat(pr.registered_at)
            if datetime.now(UTC) - registered >= timedelta(days=7):
                return "week_old"
        except ValueError:
            pass
    return None
