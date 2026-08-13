"""Reconcile cw session state with the native Claude daemon.

A cw session is "live" if its ``surface_ref`` appears in the roster
returned by ``claude agents --json``.  ``reconcile()`` is split into two
phases that run under ``sessions_lock`` (see ADR-0005):

**Detect phase** — pure classification, no state writes.
Each sweep calls a ``_detect_*`` helper that returns candidate objects.
``_emit_reap_proposed`` fires
:attr:`OrchestratorEventType.SESSION_REAP_PROPOSED`
for every proposal-worthy candidate whose :attr:`Session.reap_proposed_at`
is ``None``, stamping that field to deduplicate across ticks.

**Act phase** — gated by ``reap_policy`` (ADR-0006) where destructive.
Since the process-kill-timeout removal, no sweep dispositions a session off
elapsed time or transcript quietness:

- the **foreign-result** sweep (``stalled``) and **emitted-sentinel router**
  (``idle``) act only on positive completion evidence (a recorded terminal
  result / an emitted sentinel) — constructive, never a reap;
- the **phantom** sweep acts only on roster absence (the process is
  genuinely gone) and remains gated by ``reap_policy``
  (default ``signal_only``);
- the **liveness** sweep is signal-only: it latches transcript-staleness
  buckets, emits ``session.liveness_changed``, and pages the operator
  (``SESSION_NEEDS_ATTENTION``) when a live worker crosses the top bucket
  with no sentinel and no pending subagent — it never stops a daemon,
  reverts a task, or removes a worktree.

Destructive acts require either ``reap_policy: auto`` on the session's lane
*or* an explicit operator command (``cw doctor --reap``).

Lock note: act-phase helpers call ``save_state()`` directly — not
``mutate_state()`` (ADR-0005) — because ``_reconcile_locked()`` already
holds ``sessions_lock`` and re-acquiring the same per-open-fd flock would
self-deadlock (#387).  Serialisation against concurrent Stop-hook writes
is enforced by the held lock.

Transient-outage guard: ``reconcile`` skips the act phase entirely when
``claude agents --json`` fails or returns an empty roster while
ACTIVE/IDLE sessions with surface refs exist — a hiccup must not mass-reap.

See ADR-0005 (single state lock) and ADR-0006 (reaping is gated by an
authority) for the invariants this module enforces.

This package was split out of a single ``reconcile.py`` module; the public
import surface (``from cw.reconcile import X``) is preserved here via
re-exports. Submodules:

- ``_shared`` — constants, dataclasses/enums, and cross-cutting leaf helpers.
- ``stalled`` — foreign-result completion sweep for headless sessions.
- ``idle`` — emitted-sentinel router (#578).
- ``liveness`` — transcript-staleness bucket sweep + operator distress
  signal (RFC 0008 W2; signal-only, no disposition).
- ``phantom`` — phantom (dead-surface) sweep.
- ``tasks`` — dev-queue revert backstops and timed-out-merged completion.
- ``core`` — ``reconcile`` / ``_reconcile_locked`` orchestration.
"""

from __future__ import annotations

from cw.reconcile._shared import (
    _CAUSE_IDLE_STALL,
    _CAUSE_USAGE_LIMIT,
    _DIRTY_WORKTREE_REASON,
    _FINALIZE_BLOCKED_REASON,
    _FRESHNESS_BLOCK_ESCALATED_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _MAIN_CHECKOUT_DRIFT_REASON,
    _NEEDS_SALVAGE_REASON,
    _NEVER_CLAIMED_COMPLETION_REASON,
    _SALVAGE_KIND_GIT_STATE,
    _SALVAGE_SKIP_ESCALATED_REASON,
    _SALVAGE_SKIP_REASON,
    _SALVAGE_TERMINAL_STATUSES,
    _SESSION_UNRESPONSIVE_REASON,
    _SILENTLY_IDLE_REASON,
    _STAGE_REVIEW_COMPLETE,
    _STALLED_CAP_PARKED_REASON,
    _UNRESOLVED_SUBAGENT_SPAWN_REASON,
    _VALIDATION_FAILED_MAX_ATTEMPTS,
    AUTO_DEV_LABEL_PREFIX,
    SPAWN_GRACE_SECONDS,
    SUBAGENT_LIVENESS_WINDOW_SECONDS,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    ProposedAction,
    ReapCandidate,
    ReconcileReport,
    SentinelRouteOutcome,
    UsageLimitDetection,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _awaiting_subagent,
    _backfill_claude_session_ids,
    _claude_agents_json,
    _compute_worktree_dirty,
    _csid_from_transcript,
    _detect_post_review_clean,
    _detect_usage_limit,
    _emit_reap_proposed,
    _has_terminal_sentinel,
    _is_headless,
    _locate_session_transcript,
    _looks_like_daemon_outage,
    _parse_any_sentinel_from_transcript,
    _read_unresolved_subagent_spawn,
    _salvage_terminal_result,
    _session_project_dir,
    _transcript_age_seconds,
    _transcript_recently_active,
    _worktree_dirty_by_path,
    compute_drift,
    feature_branch_key,
    resolve_reap_policy,
    ticket_id_for_session,
)
from cw.reconcile.concierge import (
    DEFAULT_CONCIERGE_RECOVERIES,
    RECIPE_CANCELLED_ROW_RESTORE,
    RECIPE_FALSE_PARK_REQUEUE,
    RECIPE_PARK_MARKER_POISON_CLEAR,
    ConciergeCandidate,
    resolve_concierge_recipe_enabled,
    run_concierge_recoveries,
)
from cw.reconcile.core import (
    _reconcile_locked,
    _verify_supervisor_session_id,
    reconcile,
)
from cw.reconcile.escalation import (
    ESCALATION_PARK_MINUTES,
    run_escalation_sweep,
)
from cw.reconcile.idle import (
    _act_on_idle_candidates,
    _detect_idle_candidates,
)
from cw.reconcile.liveness import (
    LivenessCandidate,
    _act_on_liveness_candidates,
    _classify_liveness_bucket,
    _detect_liveness_candidates,
    record_session_liveness_changes,
)
from cw.reconcile.local import (
    _act_on_local_harvest_candidates,
    _detect_local_harvest_candidates,
)
from cw.reconcile.main_drift import (
    _act_on_main_drift_candidates,
    _detect_main_drift_candidates,
)
from cw.reconcile.phantom import (
    _act_on_phantom_candidates,
    _detect_phantom_candidates,
)
from cw.reconcile.stalled import (
    _act_on_stalled_candidates,
    _detect_stalled_candidates,
)
from cw.reconcile.tasks import (
    complete_timed_out_merged_tasks,
    park_terminal_sibling_tasks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)

__all__ = [
    "AUTO_DEV_LABEL_PREFIX",
    "DEFAULT_CONCIERGE_RECOVERIES",
    "ESCALATION_PARK_MINUTES",
    "RECIPE_CANCELLED_ROW_RESTORE",
    "RECIPE_FALSE_PARK_REQUEUE",
    "RECIPE_PARK_MARKER_POISON_CLEAR",
    "SPAWN_GRACE_SECONDS",
    "SUBAGENT_LIVENESS_WINDOW_SECONDS",
    "TRANSCRIPT_LIVENESS_WINDOW_SECONDS",
    "_CAUSE_IDLE_STALL",
    "_CAUSE_USAGE_LIMIT",
    "_DIRTY_WORKTREE_REASON",
    "_FINALIZE_BLOCKED_REASON",
    "_FRESHNESS_BLOCK_ESCALATED_REASON",
    "_GH_CHECK_BLOCKED_REASON",
    "_MAIN_CHECKOUT_DRIFT_REASON",
    "_NEEDS_SALVAGE_REASON",
    "_NEVER_CLAIMED_COMPLETION_REASON",
    "_SALVAGE_KIND_GIT_STATE",
    "_SALVAGE_SKIP_ESCALATED_REASON",
    "_SALVAGE_SKIP_REASON",
    "_SALVAGE_TERMINAL_STATUSES",
    "_SESSION_UNRESPONSIVE_REASON",
    "_SILENTLY_IDLE_REASON",
    "_STAGE_REVIEW_COMPLETE",
    "_STALLED_CAP_PARKED_REASON",
    "_UNRESOLVED_SUBAGENT_SPAWN_REASON",
    "_VALIDATION_FAILED_MAX_ATTEMPTS",
    "ConciergeCandidate",
    "LivenessCandidate",
    "ProposedAction",
    "ReapCandidate",
    "ReconcileReport",
    "SentinelRouteOutcome",
    "UsageLimitDetection",
    "_act_on_idle_candidates",
    "_act_on_liveness_candidates",
    "_act_on_local_harvest_candidates",
    "_act_on_main_drift_candidates",
    "_act_on_phantom_candidates",
    "_act_on_stalled_candidates",
    "_apply_salvaged_completion",
    "_apply_sentinel_to_task",
    "_awaiting_subagent",
    "_backfill_claude_session_ids",
    "_classify_liveness_bucket",
    "_claude_agents_json",
    "_compute_worktree_dirty",
    "_csid_from_transcript",
    "_detect_idle_candidates",
    "_detect_liveness_candidates",
    "_detect_local_harvest_candidates",
    "_detect_main_drift_candidates",
    "_detect_phantom_candidates",
    "_detect_post_review_clean",
    "_detect_stalled_candidates",
    "_detect_usage_limit",
    "_emit_reap_proposed",
    "_has_terminal_sentinel",
    "_is_headless",
    "_locate_session_transcript",
    "_looks_like_daemon_outage",
    "_parse_any_sentinel_from_transcript",
    "_read_unresolved_subagent_spawn",
    "_reconcile_locked",
    "_salvage_terminal_result",
    "_session_project_dir",
    "_transcript_age_seconds",
    "_transcript_recently_active",
    "_verify_supervisor_session_id",
    "_worktree_dirty_by_path",
    "complete_timed_out_merged_tasks",
    "compute_drift",
    "feature_branch_key",
    "park_terminal_sibling_tasks",
    "reconcile",
    "record_session_liveness_changes",
    "resolve_concierge_recipe_enabled",
    "resolve_reap_policy",
    "revert_completed_silent_tasks",
    "revert_timed_out_tasks",
    "run_concierge_recoveries",
    "run_escalation_sweep",
    "ticket_id_for_session",
]
