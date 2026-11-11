"""Combined concierge entry point: ``run_concierge_recoveries``.

Runs each enabled recipe's detect phase then act phase for one reconcile
tick. The recipe functions are from-imported, so they bind here: patch
``cw.reconcile.concierge.core.<name>`` to intercept a call from this driver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.reconcile.concierge._common import (
    RECIPE_CANCELLED_ROW_RESTORE,
    RECIPE_FALSE_PARK_REQUEUE,
    RECIPE_PARK_MARKER_POISON_CLEAR,
    resolve_concierge_recipe_enabled,
)
from cw.reconcile.concierge.cancelled_row import (
    _act_on_cancelled_row_candidates,
    _detect_cancelled_row_candidates,
)
from cw.reconcile.concierge.false_park import (
    _act_on_false_park_candidates,
    _detect_false_park_candidates,
)
from cw.reconcile.concierge.park_marker_poison import (
    _act_on_park_marker_poison_candidates,
    _detect_park_marker_poison_candidates,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import OrchestratorConfig
    from cw.reconcile._shared import ScopeProbes
    from cw.reconcile.cancelled_row_probes import CancelledRowProbes


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def run_concierge_recoveries(
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    cancelled_probes: CancelledRowProbes | None = None,
    scope_probes: ScopeProbes | None = None,
    roster_captured_at: datetime | None = None,
    warned_blocks: set[str] | None = None,
) -> list[str]:
    """Run all enabled concierge recipes for one reconcile tick.

    No-op (returns ``[]`` immediately) unless ``config.concierge_enabled`` is
    True (Q1). Loads fresh state/dev-queue snapshots itself rather than
    accepting them from the caller — by the time reconcile's
    ``_reconcile_locked`` reaches this wiring point, several prior sweeps
    (stalled/idle/phantom/backstop) have already mutated and saved both
    files, so a caller-supplied snapshot would be stale. Safe to call while
    the caller already holds ``sessions_lock`` — this function never
    acquires that lock itself, only ``dev_queue_lock`` per recipe act phase
    (mirrors ``revert_timed_out_tasks``'s own fresh load/save pattern).

    Recipe 3 reads its git answers from *cancelled_probes*, captured before
    the lock (#2546); ``None`` makes recipe 3 defer (restores nothing) this tick.
    Recipe 2's salvage corrects scope from *scope_probes* (#2641).
    *roster_captured_at* is the capture time of the lockless roster
    *native_live* came from (#2550): recipes 1 and 2 do not call a session
    dead on roster absence when it was spawned or resumed too recently for
    that capture to list it. ``None`` keeps the roster-only test.

    Returns the combined list of ticket IDs recovered across all 3 recipes.
    """
    if not config.concierge_enabled:
        return []

    state = load_state()
    tasks = load_dev_queue().tasks

    recovered: list[str] = []

    if resolve_concierge_recipe_enabled(config, RECIPE_FALSE_PARK_REQUEUE):
        false_park_candidates = _detect_false_park_candidates(
            state,
            tasks,
            now=now,
            native_live=native_live,
            config=config,
            roster_captured_at=roster_captured_at,
        )
        recovered.extend(_act_on_false_park_candidates(false_park_candidates, now=now))

    if resolve_concierge_recipe_enabled(config, RECIPE_PARK_MARKER_POISON_CLEAR):
        park_marker_candidates = _detect_park_marker_poison_candidates(
            state,
            tasks,
            now=now,
            native_live=native_live,
            config=config,
            roster_captured_at=roster_captured_at,
        )
        recovered.extend(
            _act_on_park_marker_poison_candidates(
                park_marker_candidates,
                now=now,
                scope_probes=scope_probes,
                warned_blocks=warned_blocks,
            )
        )

    if resolve_concierge_recipe_enabled(config, RECIPE_CANCELLED_ROW_RESTORE):
        cancelled_candidates = _detect_cancelled_row_candidates(tasks, cancelled_probes)
        recovered.extend(_act_on_cancelled_row_candidates(cancelled_candidates))

    return recovered
