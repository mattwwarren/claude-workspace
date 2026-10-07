"""The Stop hook's ``agent_spawn_stamp`` snapshot and clear (#1947, #2229).

The mutate functions ``signal_stop`` hands to ``_write_cw_context_locked``: a
live snapshot of the hook payload's ``background_tasks`` count on a deferred
turn, the clear-to-zero on a drained one, and the resolved-shape check that
lets a drained turn skip the locked write. Imports ``_constants``. Split out
of the flat ``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from cw.cli.stop_hook._constants import _LOGGER_NAME
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    extract_unresolved_spawn_count,
)

logger = logging.getLogger(_LOGGER_NAME)


def _snapshot_agent_spawn_stamp(
    context: dict[str, object], count: int
) -> dict[str, object]:
    """Overwrite ``agent_spawn_stamp`` with a live snapshot of *count*.

    #1947: replaces the removed ``PostToolUse:Agent`` decrement. Unlike
    ``agent_spawn_stamp._adjust_unresolved_count`` this is a *set*, not a
    delta -- the Stop hook payload's own ``background_tasks`` list is already
    the harness's authoritative live count for this turn, so there is nothing
    to accumulate against. ``last_stamped_at`` refreshes on every snapshot,
    even when the count is unchanged. At count > 0 it IS load-bearing: it bounds
    the #2012 distress-suppression deadline (``reconcile/liveness.py``,
    ``doctor/wedge.py``), which is why the deferral snapshot never skips a
    write. At count 0 nothing reads it.
    """
    context[AGENT_SPAWN_STAMP_KEY] = {
        AGENT_SPAWN_UNRESOLVED_COUNT_KEY: count,
        AGENT_SPAWN_LAST_STAMPED_AT_KEY: datetime.now(UTC).isoformat(),
    }
    return context


def _clear_agent_spawn_stamp(context: dict[str, object]) -> dict[str, object]:
    """Zero ``agent_spawn_stamp`` -- the counterpart of
    :func:`_snapshot_agent_spawn_stamp`.

    Runs on every Stop whose ``background_tasks`` is empty/absent -- i.e. every
    turn that is NOT deferring for pending background work -- unless the stamp
    is already the resolved shape (:func:`_agent_spawn_stamp_is_clear`, #2229).
    This is what retires a snapshot written by a prior deferred turn once the
    harness's own accounting shows nothing outstanding -- see
    :func:`signal_stop`.

    #1947 review: logs when this actually retires a nonzero count -- this
    write otherwise vanishes silently (same fail-open contract as every
    other hook write here), but the field it mutates is the sole disk
    evidence gating BLOCKED_ON_USER vs a PENDING revert on the phantom
    sweep, so a transition worth an operator's attention deserves a trail.
    """
    prior_count = extract_unresolved_spawn_count(context)
    if prior_count > 0:
        logger.info(
            "agent_spawn_stamp cleared: unresolved_count %d -> 0 "
            "(background_tasks drained)",
            prior_count,
        )
    return _snapshot_agent_spawn_stamp(context, 0)


def _agent_spawn_stamp_is_clear(context: dict[str, object]) -> bool:
    """True only when the stamp is exactly the resolved shape: a dict whose
    ``unresolved_count`` is a non-bool ``int`` equal to 0 (#2229).

    Mirrors :func:`cw.models.extract_unresolved_spawn_count` but is stricter:
    that helper collapses every malformed shape (absent, non-dict, ``"0"``,
    ``False``, negative) to 0, whereas the clear write must still normalize
    those to ``{0, <now>}``. Pure ``isinstance`` logic, so it cannot raise.
    """
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    if not isinstance(stamp, dict):
        return False
    count = stamp.get(AGENT_SPAWN_UNRESOLVED_COUNT_KEY)
    return isinstance(count, int) and not isinstance(count, bool) and count == 0
