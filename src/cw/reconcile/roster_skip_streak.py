"""Operator signal for a streak of ticks that skipped the roster (#3335).

A stale or missing roster snapshot (``RosterUnavailableError.skips_tick``,
#2550) skips every roster-dependent sweep for the tick: the phantom sweep,
the concierge recipes, the terminal backstops. One such tick is a slow lock
wait; a run of them means the fleet is quietly unreconciled, and before this
module the only trace was one ``roster_unavailable`` WARNING per tick.

:func:`settle_roster_skip_streak` counts consecutive skipped ticks. On
reaching :data:`ROSTER_SKIP_ATTENTION_THRESHOLD` it emits one
``SESSION_NEEDS_ATTENTION`` with ``paused_status`` ``roster_skip_streak``,
stays silent while the streak persists, and re-arms after a tick that used
the roster. A failed read (``reason="failed"``) is not a skip: it keeps the
daemon-outage path, so it ends the streak too. The event is fleet-wide
(``client=""``, like ``gh_availability_outage``), so ``cw orchestrate
watch`` filters it while ``cw event tail`` and the operator channel show it.

State is an in-process edge latch, the ``dirty_checks._ANNOUNCED_OUTAGES``
precedent (#2662), with two differences. It is settled in-lock, once per
``_reconcile_locked`` under ``sessions_lock`` (which serialises it, so it
takes no lock of its own); ``record_event`` takes only the leaf inbox lock
and runs no subprocess, which ADR-0019 invariant 4 permits. And it is per
process: a short-lived ``cw start`` or ``cw doctor --reap`` holds its own
count, so in practice only the long-lived dispatch loop reaches the
threshold. Nothing is persisted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType

if TYPE_CHECKING:
    from cw.reconcile.roster_snapshot import RosterUnavailableError

_log = logging.getLogger(__name__)

# Consecutive skipped ticks before the operator is paged: the default of the
# sibling lane_circuit_breaker_threshold. One or two slow lock waits are
# noise; three in a row is a sustained problem.
ROSTER_SKIP_ATTENTION_THRESHOLD = 3

# ``paused_status`` of the attention event a skip streak emits once per episode.
ROSTER_SKIP_STREAK_STATUS = "roster_skip_streak"


class _Streak:
    """The process-level skip counter and its edge latch."""

    def __init__(self) -> None:
        self.count = 0
        self.announced = False

    def reset(self) -> None:
        """End the episode: zero the count and re-arm the event."""
        self.count = 0
        self.announced = False


_STREAK = _Streak()


def settle_roster_skip_streak(unavailable: RosterUnavailableError | None) -> None:
    """Apply this tick's roster outcome to the streak; page once at the threshold.

    *unavailable* is the tick's ``RosterUnavailableError``, or ``None`` when
    the roster was usable. Only a skipped tick (stale or missing) counts;
    anything else resets the streak. A failed emit leaves the latch unset so
    the next skipped tick retries it.
    """
    if unavailable is None or not unavailable.skips_tick:
        _STREAK.reset()
        return
    _STREAK.count += 1
    if _STREAK.announced or _STREAK.count < ROSTER_SKIP_ATTENTION_THRESHOLD:
        return
    _STREAK.announced = _announce(_STREAK.count, unavailable)


def _announce(count: int, cause: RosterUnavailableError) -> bool:
    """Emit the ``roster_skip_streak`` event; True if it was recorded."""
    payload: dict[str, object] = {
        "session_id": f"roster-skip:{count}@{datetime.now(UTC).isoformat()}",
        "session_name": "",
        "client": "",
        "ticket_id": None,
        "claude_session_id": None,
        "paused_status": ROSTER_SKIP_STREAK_STATUS,
        "breadcrumbs": (
            f"{count} consecutive ticks skipped the roster-dependent sweeps "
            f"(last cause: {cause})"
        ),
        "crashed": False,
        "lane": None,
    }
    try:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload,
            correlation_id=None,
        )
    except (CwError, OSError) as exc:
        _log.warning(
            "reconcile: %s event was not recorded: %s; retrying next tick",
            ROSTER_SKIP_STREAK_STATUS,
            exc,
        )
        return False
    return True
