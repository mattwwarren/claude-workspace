"""Emitted-sentinel router for live DAEMON sessions.

Evidence-only since the process-kill-timeout removal: the historical
idle-watchdog machinery (budget, confirm-before-reap counter, git-salvage,
revert, park) is gone -- transcript quietness never dispositions a session.
What remains is the unrouted-sentinel check (#578): a session whose
transcript already carries a sentinel that ``signal_stop`` never routed is
routed forward and completed on that positive evidence.
See GitHub #105, #121, #552, #578, ADR-0006.

- ``_detect`` — the pure detect-phase classifier (zero writes).
- ``_mutations`` — act-phase session-state and task-routing mutations.
- ``_events`` — act-phase completion-event emission and surface teardown.
- ``core`` — the ``_act_on_idle_candidates`` driver.
"""

from __future__ import annotations

# ``_deps`` and ``_shared`` are re-exported (nothing in this module calls
# them) so historical ``cw.reconcile.idle._deps.<fn>`` /
# ``cw.reconcile.idle._shared.<fn>`` patch targets keep resolving; the call
# sites themselves live in the submodules, which read the same
# ``cw.reconcile._deps`` / ``cw.reconcile._shared`` module objects.
from cw.reconcile import _deps, _shared
from cw.reconcile.idle._detect import (
    _detect_idle_candidate_for_session,
    _detect_idle_candidates,
)
from cw.reconcile.idle._events import _emit_idle_completion_events
from cw.reconcile.idle._mutations import _apply_idle_routed_mutations
from cw.reconcile.idle.core import _act_on_idle_candidates

__all__ = [
    "_act_on_idle_candidates",
    "_apply_idle_routed_mutations",
    "_deps",
    "_detect_idle_candidate_for_session",
    "_detect_idle_candidates",
    "_emit_idle_completion_events",
    "_shared",
]
