"""Session-event payload literals shared by producers and the outcome audit (#3124).

Two sides read the same wire values:

- The producers write them: the reconcile passes (``cw.reconcile``, through
  the private aliases in ``cw.reconcile._shared._constants``), ``cw spawn
  close`` (``cw.cli.spawn``) and the operator-close audit
  (``cw.reconcile.session_close``).
- ``cw.session_outcomes`` matches them when it buckets no-result sessions. It
  is audit-only and must not import ``cw.reconcile`` or ``cw.dispatch``, so it
  cannot read the producers' constants directly.

Both sides import from here, so a renamed value cannot silently drift one side
out of step with the other. ``tests/test_session_event_vocabulary.py`` pins the
values and checks with ``ast`` that no producer re-declares them.

This is a leaf module: it imports nothing from ``cw``.
"""

from __future__ import annotations

from typing import Final

# ``session.completed`` ``reason`` and ``session.needs_attention``
# ``paused_status`` when a live worker's transcript tail is a usage-limit
# message with no sentinel (#2324). One value serves both fields.
USAGE_LIMITED_MID_TURN: Final[str] = "usage_limited_mid_turn"

# ``session.timed_out`` ``cause`` when the idle watchdog finds a Claude
# session/usage-limit message in the transcript (#486).
CAUSE_USAGE_LIMIT_CUTOFF: Final[str] = "usage_limit_cutoff"

# ``session.completed`` ``reason`` when a phantom/stalled/idle session's PR was
# found MERGED before its task was reverted to PENDING (#637).
PHANTOM_REAP_MERGED_REASON: Final[str] = "phantom_reap_merged"

# History ``detail`` of the one record every explicit operator session close
# leaves (#2694).
OPERATOR_CLOSE_DETAIL: Final[str] = "operator_close"
