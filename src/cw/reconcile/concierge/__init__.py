"""Daemon-side gate concierge: mechanical recovery reactor (RFC 0008 capstone).

Three non-destructive recovery recipes for TicketTask rows that got stuck
behind evidence-confirmed-dead sessions:

1. **Wall-clock false-park requeue** (``false_park``:
   :func:`_detect_false_park_candidates` /
   :func:`_act_on_false_park_candidates`) — a row parked
   ``stalled_retry_cap_parked`` (or with no disposition at all) whose owning
   session is confirmed dead (absent from the daemon roster, transcript
   flat) is requeued to PENDING at its current stage. Exception (GitHub
   #1674): the requeue is *refused* — the row is left parked and
   ``CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED`` is emitted — when the row's
   currently-resolved session is the exact session that already made a spawn
   attempt raise ``HookContextConflictError`` and is still non-terminal;
   respawning cannot succeed until that session is closed, so requeuing only
   burns attempts. The refusal clears itself once the session's status goes
   terminal (``cw spawn close --confirmed-dead <id>``) or a new session
   supersedes it by id — no separate manual unblock step.
2. **Park-marker-poison clear** (``park_marker_poison``:
   ``_detect_park_marker_poison_candidates`` /
   :func:`_act_on_park_marker_poison_candidates`) — a row behind a session
   repeatedly skipped by the salvage watchdog's park-marker check
   (``consecutive_salvage_skips >= 1``) whose session is confirmed dead is
   closed and requeued.
3. **Cancelled-row dead-end normalization** (``cancelled_row``:
   ``_detect_cancelled_row_candidates`` /
   :func:`_act_on_cancelled_row_candidates`) — a CANCELLED row whose worktree
   still has committed work ahead of its base branch is restored to PENDING
   so that work is not silently lost.

None of these recipes stop a daemon surface that is still alive, remove a
worktree, or delete any row — they only flip a status/reset a stage, and only
when the evidence gate says the owning session is already dead. This keeps
them non-destructive under ADR-0006: the *destructive* half of ADR-0006's
concern (stopping a live daemon, deleting a worktree) never applies here,
because every recipe's precondition is "the session is already gone" — the
recipe is undoing an over-cautious park, not authorizing a new kill. All 3
gate on ``OrchestratorConfig.concierge_enabled`` (opt-in, default False, per
Q1) and are individually toggleable via
:func:`resolve_concierge_recipe_enabled` (Q7).

Each recipe follows the repo's detect/act split (see
``reconcile/tasks.py``'s ``park_terminal_sibling_tasks`` for the closest
sibling template): a pure ``_detect_*_candidates`` classification phase, then
an ``_act_on_*_candidates`` phase that re-validates under lock and mutates.
Emit-before-act is a hard requirement per recipe:
:class:`OrchestratorEventType.CONCIERGE_RECOVERED` is recorded (durably, to
the append-only events inbox) before the corresponding task/session
mutation, so evidence of what the concierge *decided* survives even if the
subsequent write fails. See GitHub #1015.

Package layout (#2895), one submodule per concern; ``_common`` is the leaf,
the three recipe modules import it, and ``core`` imports all four:

- ``_common`` -- recipe keys, ``DEFAULT_CONCIERGE_RECOVERIES``,
  ``resolve_concierge_recipe_enabled``, ``ConciergeCandidate``, and the
  session lookup, park-marker and attempt-ceiling helpers recipes 1 and 2
  share.
- ``false_park`` -- recipe 1, with its dead-on-arrival backoff.
- ``park_marker_poison`` -- recipe 2, including the dead-session close and
  the task routing after it.
- ``cancelled_row`` -- recipe 3.
- ``core`` -- ``run_concierge_recoveries``, the combined entry point.

This module is a pure re-export. Patch a name on the submodule that reads it,
not on the package. ``_transcript_age_seconds`` is read by both
``false_park`` and ``park_marker_poison``; ``save_dev_queue`` /
``salvage_terminal_result`` / ``_close_confirmed_dead_session`` are looked up
in the recipe module that calls them; the recipe functions
``run_concierge_recoveries`` calls are bound in ``core``.
"""

from __future__ import annotations

from cw.reconcile.concierge._common import (
    DEFAULT_CONCIERGE_RECOVERIES,
    RECIPE_CANCELLED_ROW_RESTORE,
    RECIPE_FALSE_PARK_REQUEUE,
    RECIPE_PARK_MARKER_POISON_CLEAR,
    ConciergeCandidate,
    resolve_concierge_recipe_enabled,
)
from cw.reconcile.concierge.cancelled_row import _act_on_cancelled_row_candidates
from cw.reconcile.concierge.core import run_concierge_recoveries
from cw.reconcile.concierge.false_park import (
    _FALSE_PARK_ELIGIBLE_DISPOSITIONS,
    _detect_false_park_candidates,
    _resolve_false_park_recovery_backoff,
)
from cw.reconcile.concierge.park_marker_poison import (
    _act_on_park_marker_poison_candidates,
    _close_confirmed_dead_session,
    _park_marker_transcript_stale_45m,
)

__all__ = [
    "DEFAULT_CONCIERGE_RECOVERIES",
    "RECIPE_CANCELLED_ROW_RESTORE",
    "RECIPE_FALSE_PARK_REQUEUE",
    "RECIPE_PARK_MARKER_POISON_CLEAR",
    "_FALSE_PARK_ELIGIBLE_DISPOSITIONS",
    "ConciergeCandidate",
    "_act_on_cancelled_row_candidates",
    "_act_on_park_marker_poison_candidates",
    "_close_confirmed_dead_session",
    "_detect_false_park_candidates",
    "_park_marker_transcript_stale_45m",
    "_resolve_false_park_recovery_backoff",
    "resolve_concierge_recipe_enabled",
    "run_concierge_recoveries",
]
