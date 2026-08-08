"""Foreign-result completion sweep for headless DAEMON sessions.

Evidence-only since the process-kill-timeout removal: the historical
wall-clock-budget enforcement (revert, retry-cap park, finalize-blocked park,
liveness veto) is gone -- elapsed time never dispositions a session. What
remains is COMPLETE_FOREIGN_RESULT: completing a live session whose
``last_result`` already carries a terminal sentinel recorded by another
authority. See GitHub #185, #552, #1470, ADR-0006.

- ``_detect`` — the pure detect-phase classifier (zero writes).
- ``_mutations`` — act-phase session-state and dev-queue mutations.
- ``_events`` — act-phase completion-event emission and surface teardown.
- ``core`` — the ``_act_on_stalled_candidates`` driver.
"""

from __future__ import annotations

# ``_deps`` is re-exported (nothing in this module calls it) so the historical
# ``cw.reconcile.stalled._deps.<fn>`` patch target keeps resolving; the call
# sites themselves live in ``_events``, which reads the same
# ``cw.reconcile._deps`` module object.
from cw.reconcile import _deps
from cw.reconcile.stalled._detect import _detect_stalled_candidates
from cw.reconcile.stalled.core import _act_on_stalled_candidates

__all__ = [
    "_act_on_stalled_candidates",
    "_deps",
    "_detect_stalled_candidates",
]
