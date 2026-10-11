"""Recent sessions / dev-queue lock holds for cw doctor (#2637).

Reads the lock-hold sidecar ``cw._lock_hold`` writes (``lock_holds.<date>.jsonl``
in the state dir) for the last :data:`~cw._lock_hold.RECENT_WINDOW` and reports
each lock's longest completed hold, the longest wait when it reached the warn
threshold, how many holds were recorded (a lower bound once the daily cap
trips), and the aggregate share of sessions-hold time spent in
``load_state``/``save_state``. Advisory only, like ``sessions-size``: always
``ok=True``; ``warn=True`` when a hold or wait reached
:data:`~cw._lock_hold.WARN_HELD_S`. When state I/O dominates it points at
``cw session prune`` and the runbook; it never prunes or mutates anything
(ADR-0014) and never calls ``load_state()``. Leaf module -- no cross-``doctor``
dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw._lock_hold import (
    MAX_FILE_BYTES,
    RECENT_WINDOW,
    RECORD_MIN_S,
    WARN_HELD_S,
    format_longest,
    read_window,
    summarize,
)
from cw._util import format_compact_age
from cw.config import state_dir
from cw.doctor._shared import CheckResult
from cw.exceptions import SESSIONS_LOCK_NAME

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw._lock_hold import HoldWindow, LockHoldSummary

_CHECK_NAME = "lock-holds"
_BYTES_PER_MIB = 1_048_576
_COMPLETED_ONLY = "(completed holds only)"
_PRUNE_ADVISORY = (
    "state load/save dominates recorded sessions holds; consider"
    " `cw session prune` (docs/dispatch-runbook.md, 'Reading lock-hold numbers')"
)
_CAPPED_NOTE = "record cap reached; counts are lower bounds"
_CEILING_NOTE = "nothing more is recorded today"


def _window_label() -> str:
    return f"last {format_compact_age(RECENT_WINDOW)}"


def _no_records_detail() -> str:
    return (
        f"no records found in the {_window_label()} (recorded when held or"
        f" waited >= {RECORD_MIN_S:g}s; recorder failures appear only at DEBUG on"
        f" logger cw._lock_hold) {_COMPLETED_ONLY}"
    )


def _longest_wait_part(
    summaries: Sequence[LockHoldSummary], now: datetime
) -> str | None:
    waits = [s.longest_wait for s in summaries if s.longest_wait is not None]
    longest = max(waits, key=lambda r: r.waited_s, default=None)
    if longest is None or longest.waited_s < WARN_HELD_S:
        return None
    age = format_compact_age(now - longest.at)
    return f"longest wait {longest.waited_s:.1f}s ({longest.caller}, {age} ago)"


def _sessions_parts(sessions: LockHoldSummary | None) -> list[str]:
    if sessions is None:
        return ["sessions state load/save n/a"]
    parts: list[str] = []
    longest = sessions.longest
    if longest is not None and longest.state_bytes is not None:
        parts.append(f"sessions.json {longest.state_bytes}B at the longest hold")
    if sessions.state_io_dominates:
        parts.append(_PRUNE_ADVISORY)
    return parts


def _cap_parts(window: HoldWindow) -> list[str]:
    if not window.capped:
        return []
    return [_CAPPED_NOTE, _CEILING_NOTE] if window.ceiling else [_CAPPED_NOTE]


def _warns(summaries: Sequence[LockHoldSummary]) -> bool:
    return any(
        (s.longest is not None and s.longest.held_s >= WARN_HELD_S)
        or (s.longest_wait is not None and s.longest_wait.waited_s >= WARN_HELD_S)
        for s in summaries
    )


def _check_lock_holds() -> CheckResult:
    """Report the longest completed sessions / dev-queue lock holds recently."""
    now = datetime.now(UTC)
    window = read_window(state_dir(), now=now)
    summaries = summarize(window.records)
    if not summaries:
        return CheckResult(_CHECK_NAME, ok=True, detail=_no_records_detail())

    sessions = next((s for s in summaries if s.lock == SESSIONS_LOCK_NAME), None)
    cap_mib = MAX_FILE_BYTES / _BYTES_PER_MIB
    parts = [
        f"{_window_label()}: {format_longest(summaries, now)}",
        f"at least {len(window.records)} recorded (cap {cap_mib:g} MiB/day)",
    ]
    wait_part = _longest_wait_part(summaries, now)
    if wait_part is not None:
        parts.append(wait_part)
    parts.extend(_sessions_parts(sessions))
    parts.extend(_cap_parts(window))
    detail = "; ".join(parts) + f" {_COMPLETED_ONLY}"
    return CheckResult(_CHECK_NAME, ok=True, warn=_warns(summaries), detail=detail)
