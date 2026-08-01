"""Phantom-session detection and act phases for reconcile.

A phantom session is ACTIVE/IDLE in cw state but absent from the daemon
roster (its surface is dead). See GitHub #552, ADR-0006.

Package split. The historical flat ``cw.reconcile.phantom`` module is now a
package of four focused modules, mirroring the ``cw.reconcile.stalled``
split (#1484):

- ``_detect`` — the pure detect-phase classifiers (zero writes), including
  the advance-sentinel routing and the sentinel-mismatch veto.
- ``_mutations`` — act-phase session-state and dev-queue mutations.
- ``_events`` — act-phase lifecycle-event emission and surface teardown.
- ``core`` — reap-policy routing and the ``_act_on_phantom_candidates``
  driver.

This ``__init__`` re-exports the historical import surface so every
``from cw.reconcile.phantom import X`` call site keeps working unchanged.
"""

from __future__ import annotations

# ``_deps`` and ``_shared`` are re-exported (nothing in this module calls
# them) so historical ``cw.reconcile.phantom._deps.<fn>`` /
# ``cw.reconcile.phantom._shared.<fn>`` patch targets keep resolving; the
# call sites themselves live in the submodules, which read the same
# ``cw.reconcile._deps`` / ``cw.reconcile._shared`` module objects.
from cw.reconcile import _deps, _shared
from cw.reconcile.phantom._detect import (
    _detect_phantom_candidates,
    _phantom_advance_sentinel_candidate,
    _sentinel_mismatch_veto_candidate,
    _split_crash_candidates,
)
from cw.reconcile.phantom._events import (
    _SENTINEL_MISMATCH_VETO_CAP_EXHAUSTED_REASON,
    _emit_phantom_routed_events,
    _emit_phantom_terminal_events,
    _emit_sentinel_mismatch_veto_escalation_events,
)
from cw.reconcile.phantom._mutations import (
    _apply_phantom_queue_mutations,
    _apply_phantom_routed_mutations,
    _apply_phantom_salvage_mutations,
)
from cw.reconcile.phantom.core import (
    _act_on_phantom_candidates,
    _route_phantom_by_policy,
)

__all__ = [
    "_SENTINEL_MISMATCH_VETO_CAP_EXHAUSTED_REASON",
    "_act_on_phantom_candidates",
    "_apply_phantom_queue_mutations",
    "_apply_phantom_routed_mutations",
    "_apply_phantom_salvage_mutations",
    "_deps",
    "_detect_phantom_candidates",
    "_emit_phantom_routed_events",
    "_emit_phantom_terminal_events",
    "_emit_sentinel_mismatch_veto_escalation_events",
    "_phantom_advance_sentinel_candidate",
    "_route_phantom_by_policy",
    "_sentinel_mismatch_veto_candidate",
    "_shared",
    "_split_crash_candidates",
]
