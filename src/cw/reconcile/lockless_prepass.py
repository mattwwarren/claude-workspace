"""Lockless pre-pass for the main-drift checks and concierge recipe 3 (#2546).

``reconcile()`` calls :func:`capture_drift_and_cancelled_probes` before it
takes ``sessions_lock``, so the git both sweeps need never runs under the
lock: the main-checkout drift classification (``cw.reconcile.main_drift``) and
recipe 3's commits-ahead read (``cw.reconcile.cancelled_row_probes``). The
in-lock detects only look the results up, and a miss defers to the next tick.

This module owns the state load, the read-error containment and recipe 3's
gate; the domain modules take already-loaded data and a store. It sits above
``concierge`` (whose recipe toggle it reads), which imports
``cancelled_row_probes``, so it cannot live in either domain module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.reconcile.cancelled_row_probes import (
    capture_cancelled_row_probes,
    new_cancelled_row_probes,
)
from cw.reconcile.concierge import (
    RECIPE_CANCELLED_ROW_RESTORE,
    resolve_concierge_recipe_enabled,
)
from cw.reconcile.main_drift import capture_main_drift_probes, new_main_drift_probes

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig
    from cw.reconcile.cancelled_row_probes import CancelledRowProbes
    from cw.reconcile.main_drift import MainDriftProbes

_log = logging.getLogger(__name__)


def capture_drift_and_cancelled_probes(
    *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
) -> tuple[MainDriftProbes, CancelledRowProbes]:
    """Capture the main-drift and cancelled-row probes, lock-free.

    The dev queue is read, and recipe 3's git run, only when the concierge
    and its ``cancelled_row_restore`` recipe are both enabled. A state or
    dev-queue read error is logged and yields two empty stores, so every
    client and row defers in-lock; nothing broader is caught. Each store's
    budget starts at its own capture, so a slow drift pass cannot spend
    recipe 3's budget.
    """
    enabled = config.concierge_enabled and resolve_concierge_recipe_enabled(
        config, RECIPE_CANCELLED_ROW_RESTORE
    )
    try:
        state = load_state()
        tasks = load_dev_queue().tasks if enabled else []
    except (OSError, ValueError):
        _log.warning(
            "reconcile: main-drift / cancelled-row pre-pass could not read state;"
            " deferring both this tick",
            exc_info=True,
        )
        return new_main_drift_probes(), new_cancelled_row_probes()
    drift = new_main_drift_probes()
    capture_main_drift_probes(state, clients, probes=drift)
    cancelled = new_cancelled_row_probes()
    capture_cancelled_row_probes(tasks, probes=cancelled)
    return drift, cancelled
