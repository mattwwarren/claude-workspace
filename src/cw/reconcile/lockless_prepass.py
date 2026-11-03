"""Lockless pre-passes for main drift, recipe 3 and salvaged scope (#2546, #2641).

``reconcile()`` calls :func:`capture_drift_and_cancelled_probes` and
:func:`capture_salvage_scope_probes` before it takes ``sessions_lock``, so the
git those sweeps need never runs under the lock: the main-checkout drift
classification (``cw.reconcile.main_drift``), recipe 3's commits-ahead read
(``cw.reconcile.cancelled_row_probes``) and the salvaged sentinel's
branch-diff scope measurement (``cw.reconcile._shared._scope_probes``). The
in-lock consumers only look the results up, and a miss defers to the next
tick (or, for the scope check, leaves the self-report uncorrected).

This module owns the state load, the read-error containment and recipe 3's
gate; the domain modules take already-loaded data and a store. It sits above
``concierge`` (whose recipe toggle it reads), which imports
``cancelled_row_probes``, so it cannot live in either domain module. The
scope driver needs ``_sentinels``' parse, which imports the ``_scope_probes``
leaf, so it cannot live in that leaf either.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.reconcile._shared import (
    _parse_any_sentinel_from_transcript,
    capture_scope_probe,
    new_scope_probes,
    scope_probe_targets,
)
from cw.reconcile.cancelled_row_probes import (
    capture_cancelled_row_probes,
    new_cancelled_row_probes,
)
from cw.reconcile.concierge import (
    RECIPE_CANCELLED_ROW_RESTORE,
    resolve_concierge_recipe_enabled,
)
from cw.reconcile.main_drift import capture_main_drift_probes, new_main_drift_probes
from cw.reconcile.probe_store import ProbeStoreUnavailableError

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig
    from cw.reconcile._shared import ScopeProbes
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


def capture_salvage_scope_probes() -> ScopeProbes:
    """Capture the salvaged-sentinel scope measurement for each target, lock-free.

    Parses every :func:`~cw.reconcile._shared.scope_probe_targets` session's
    transcript without a store (so the parse itself runs no git) and measures
    the branch for each :class:`AutoDevResult` past the pre-impl stages. A
    state read error is logged and yields an empty store; the first budget
    refusal stops the pass with one warning, and the rest defer. Any other
    exception propagates.
    """
    try:
        state = load_state()
    except (OSError, ValueError):
        _log.warning(
            "reconcile: salvage-scope pre-pass could not read state; deferring"
            " every salvaged-scope check this tick",
            exc_info=True,
        )
        return new_scope_probes()
    probes = new_scope_probes()
    targets = scope_probe_targets(state)
    for index, session in enumerate(targets):
        parsed = _parse_any_sentinel_from_transcript(session)
        if parsed is None or not isinstance(parsed[0], AutoDevResult):
            continue
        try:
            capture_scope_probe(probes, session, parsed[0])
        except ProbeStoreUnavailableError as err:
            _log.warning(
                "reconcile: salvage-scope probe capture stopped (%s); %d captured,"
                " %d of %d session(s) deferred to the next tick",
                err.reason,
                probes.captures,
                len(targets) - index,
                len(targets),
            )
            return probes
    return probes
