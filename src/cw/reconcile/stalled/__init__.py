"""Stalled-headless-session detection and act phases for reconcile.

A stalled headless DAEMON session is one past its wall-clock budget that
produced no further Stop-hook firings. See GitHub #185, #552, ADR-0006.

Package split (#1484). The historical flat ``cw.reconcile.stalled`` module is
now a package of four focused modules:

- ``_detect`` — the pure detect-phase classifiers (zero writes) plus the
  liveness-veto and wall-clock candidate resolvers.
- ``_mutations`` — act-phase session-state and dev-queue mutations.
- ``_events`` — act-phase lifecycle-event emission and surface teardown.
- ``core`` — reap-policy routing, the ``_act_on_stalled_candidates`` driver,
  and the standalone ``revert_stalled_headless_sessions`` entry point.

This ``__init__`` re-exports the historical import surface so every
``from cw.reconcile.stalled import X`` call site keeps working unchanged.
"""

from __future__ import annotations

# ``_deps`` is re-exported (nothing in this module calls it) so the historical
# ``cw.reconcile.stalled._deps.<fn>`` patch target keeps resolving; the call
# sites themselves live in ``_events`` and ``core``, which read the same
# ``cw.reconcile._deps`` module object.
from cw.reconcile import _deps
from cw.reconcile.stalled._detect import (
    _detect_stalled_candidates,
    _liveness_veto_candidate,
    _resolve_finalize_blocked_condition,
)
from cw.reconcile.stalled._mutations import _apply_finalize_blocked_queue_mutations
from cw.reconcile.stalled.core import (
    _act_on_stalled_candidates,
    _route_stalled_by_policy,
    revert_stalled_headless_sessions,
)

__all__ = [
    "_act_on_stalled_candidates",
    "_apply_finalize_blocked_queue_mutations",
    "_deps",
    "_detect_stalled_candidates",
    "_liveness_veto_candidate",
    "_resolve_finalize_blocked_condition",
    "_route_stalled_by_policy",
    "revert_stalled_headless_sessions",
]
