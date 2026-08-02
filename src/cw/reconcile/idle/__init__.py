"""Silently-idle DAEMON session detection and act phases for reconcile.

A silently idle session stalled past the watchdog budget without emitting a
terminal sentinel (e.g. the child self-backgrounded a subagent and exited
before it returned). See GitHub #105, #121, #545, #552, ADR-0006.

Package split. The historical flat ``cw.reconcile.idle`` module is now a
package of four focused modules, mirroring the ``cw.reconcile.stalled``
split (#1484):

- ``_detect`` — the pure detect-phase classifiers (zero writes), including
  the advance-sentinel backstop and the confirm-before-reap counter logic.
- ``_mutations`` — act-phase session-state and dev-queue mutations.
- ``_events`` — act-phase lifecycle-event emission and surface teardown.
- ``core`` — reap-policy routing, the ``_act_on_idle_candidates`` driver,
  and the standalone ``flag_silently_idle_daemon_sessions`` entry point.

This ``__init__`` re-exports the historical import surface so every
``from cw.reconcile.idle import X`` call site keeps working unchanged.
"""

from __future__ import annotations

# ``_deps`` and ``_shared`` are re-exported (nothing in this module calls
# them) so historical ``cw.reconcile.idle._deps.<fn>`` /
# ``cw.reconcile.idle._shared.<fn>`` patch targets keep resolving; the call
# sites themselves live in the submodules, which read the same
# ``cw.reconcile._deps`` / ``cw.reconcile._shared`` module objects.
from cw.reconcile import _deps, _shared
from cw.reconcile.idle._detect import (
    _classify_idle_threshold,
    _detect_idle_candidate_for_session,
    _detect_idle_candidates,
    _detect_idle_confirmed_candidate,
    _idle_advance_sentinel_candidate,
    _revert_task_candidate,
)
from cw.reconcile.idle._events import (
    _emit_idle_completion_events,
    _emit_idle_events,
)
from cw.reconcile.idle._mutations import (
    _apply_idle_queue_mutations,
    _apply_idle_routed_mutations,
    _apply_idle_state_mutations,
)
from cw.reconcile.idle.core import (
    _act_on_idle_candidates,
    _route_idle_by_policy,
    flag_silently_idle_daemon_sessions,
)

__all__ = [
    "_act_on_idle_candidates",
    "_apply_idle_queue_mutations",
    "_apply_idle_routed_mutations",
    "_apply_idle_state_mutations",
    "_classify_idle_threshold",
    "_deps",
    "_detect_idle_candidate_for_session",
    "_detect_idle_candidates",
    "_detect_idle_confirmed_candidate",
    "_emit_idle_completion_events",
    "_emit_idle_events",
    "_idle_advance_sentinel_candidate",
    "_revert_task_candidate",
    "_route_idle_by_policy",
    "_shared",
    "flag_silently_idle_daemon_sessions",
]
