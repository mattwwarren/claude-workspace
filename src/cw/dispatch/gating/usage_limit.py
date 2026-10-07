"""Usage-limit reconcile preamble for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503): the best-effort
``reconcile()`` call that opens every tick and reports a usage limit, and the
per-client ``dispatch.tick`` skip events emitted while its back-off window is
active.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
)
from cw.dispatch.gating.context_json import _LOGGER_NAME
from cw.events import record_event
from cw.exceptions import SessionsLockTimeoutError
from cw.models import (
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
    counts_toward_client_ceiling,
)
from cw.reconcile import (
    reconcile,
)

if TYPE_CHECKING:
    from cw.models import (
        ClientConfig,
        CwState,
        OrchestratorConfig,
    )
from cw.dispatch.claim import _lane_occupants_for_client, _lane_stats_for_client

_log = logging.getLogger(_LOGGER_NAME)


def _emit_usage_limit_skip_events(
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    state: CwState,
) -> None:
    """Emit dispatch.tick(skip_reason=USAGE_LIMITED) for every client.

    Called when the usage-limit back-off window is still active: no client is
    dispatched this tick; each gets a skip event with ``claimed=0`` and a
    per-lane breakdown.
    """
    for client in clients.values():
        running_count = sum(
            1 for s in state.sessions if counts_toward_client_ceiling(s, client.name)
        )
        cap = config.per_client_ceiling.get(client.name, config.default_ceiling)
        with dev_queue_lock():
            queue_snapshot = load_dev_queue()
        pending_count = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name and t.status == QueueItemStatus.PENDING
        )
        # Per-lane breakdown for the event payload (claimed=0 for all).
        backoff_lane_occupants = _lane_occupants_for_client(client, queue_snapshot)
        backoff_lane_stats = _lane_stats_for_client(
            client, queue_snapshot, occupants=backoff_lane_occupants
        )
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": client.name,
                "claimed": 0,
                "pending": pending_count,
                "running": running_count,
                "cap": cap,
                "skip_reason": DispatchSkipReason.USAGE_LIMITED,
                "lanes": backoff_lane_stats,
                "lane_occupants": backoff_lane_occupants,
                "occupied": sum(len(v) for v in backoff_lane_occupants.values()),
            },
        )


def _reconcile_usage_limited() -> bool:
    """Run the best-effort reconcile preamble, returning its usage-limit flag.

    Returns True if reconcile reported a usage limit; False on success without
    a limit or when reconcile raised (logged and swallowed so a transient
    failure never kills the tick — phantoms are reaped next tick).

    This is the one ``reconcile()`` caller that passes
    ``dispatch_review_jobs=True`` (#1229): the live dispatch loop is the only
    place the ``address_review`` / ``auto_fix_ci`` review recipes may act, so
    the operator commands that also call ``reconcile()`` (``cw status`` /
    ``list`` / ``start`` / ``doctor``) never spawn a worker or burn a latch.

    :class:`~cw.exceptions.SessionsLockTimeoutError` (#2491) is the one
    exception NOT swallowed: it propagates so ``dispatch_tick`` skips the tick
    (see ``dispatch/tick.py`` for the watchdog semantics of a skipped tick).
    """
    reconcile_report = None
    try:
        reconcile_report = reconcile(dispatch_review_jobs=True)
    except SessionsLockTimeoutError:
        raise
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md:316-331 (4-part justification):
        # 1. reconcile() calls ``claude agents --json`` and native-daemon roster
        #    I/O — failure modes include subprocess crash and JSON decode errors.
        # 2. Logging: _log.exception captures the full traceback with exc_info.
        # 3. Non-critical: reconcile is best-effort housekeeping. Skipping a tick
        #    just means phantoms get reaped on the next dispatch_tick.
        # 4. Paired test: tests/test_dispatch.py
        #    test_reconcile_failure_does_not_crash_dispatch_tick.
        _log.exception("reconcile failed during dispatch_tick; continuing")
    return reconcile_report is not None and reconcile_report.usage_limited
