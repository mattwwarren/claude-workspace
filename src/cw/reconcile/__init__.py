"""Reconcile cw session state with the native Claude daemon.

A cw session is "live" if its ``surface_ref`` appears in the roster
returned by ``claude agents --json``.  ``reconcile()`` is split into two
phases that run under ``sessions_lock`` (see ADR-0005):

**Detect phase** — pure classification, no state writes.
Three sweeps (stalled, idle/phantom, post-salvage) each call a
``_detect_*`` helper that returns :class:`ReapCandidate` objects.  After
each sweep ``_emit_reap_proposed`` fires
:attr:`OrchestratorEventType.SESSION_REAP_PROPOSED`
for every candidate whose :attr:`Session.reap_proposed_at` is ``None``,
stamping that field to deduplicate across ticks.

Emitting before the act phase keeps the event visible even when the act
phase is suppressed by ``signal_only`` — consumers (the lane's ORCHESTRATE
session or the operator) see the proposal and decide.

**Act phase** — gated by ``reap_policy`` (ADR-0006).
Under the default ``signal_only`` policy the act phase routes the owning
:class:`TicketTask` to ``BLOCKED_ON_USER`` but performs no destructive
mutation (no daemon stop, no worktree removal, no RUNNING→PENDING revert).
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

``_reconcile_locked()`` runs the above sequence while ``sessions_lock``
is held; helper functions called from within it (``revert_stalled_*``,
``flag_silently_idle_*``) ``save_state`` directly without re-acquiring.

See ADR-0005 (single state lock) and ADR-0006 (reaping is gated by an
authority) for the invariants this module enforces.

This package was split out of a single ``reconcile.py`` module; the public
import surface (``from cw.reconcile import X``) is preserved here via
re-exports. Submodules:

- ``_shared`` — constants, dataclasses/enums, and cross-cutting leaf helpers.
- ``stalled`` — wall-clock-budget stalled-headless sweep.
- ``idle`` — idle-watchdog (silently idle) sweep.
- ``phantom`` — phantom (dead-surface) sweep.
- ``salvage`` — git-state salvage post-pass (draft PR / flag).
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
    _MAIN_CHECKOUT_DRIFT_REASON,
    _NEEDS_SALVAGE_REASON,
    _SALVAGE_KIND_GIT_STATE,
    _SALVAGE_SKIP_ESCALATED_REASON,
    _SALVAGE_SKIP_REASON,
    _SALVAGE_TERMINAL_STATUSES,
    _SILENTLY_IDLE_REASON,
    _STAGE_REVIEW_COMPLETE,
    _STALLED_CAP_PARKED_REASON,
    _VALIDATION_FAILED_MAX_ATTEMPTS,
    AUTO_DEV_LABEL_PREFIX,
    DEFAULT_IDLE_RETRY_CAP,
    DEFAULT_STALLED_RETRY_CAP,
    HEADLESS_TIMEOUT_SECONDS,
    IDLE_WATCHDOG_SECONDS,
    SPAWN_GRACE_SECONDS,
    SUBAGENT_LIVENESS_WINDOW_SECONDS,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    ProposedAction,
    ReapCandidate,
    ReconcileReport,
    _apply_salvaged_completion,
    _apply_sentinel_to_task,
    _assistant_text_from_transcript,
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
    _salvage_terminal_result,
    _session_project_dir,
    _transcript_age_seconds,
    _transcript_recently_active,
    _worktree_dirty_by_path,
    compute_drift,
    feature_branch_key,
    resolve_headless_budget,
    resolve_idle_retry_cap,
    resolve_idle_watchdog_budget,
    resolve_reap_policy,
    resolve_stalled_retry_cap,
    ticket_id_for_session,
)
from cw.reconcile.core import (
    _reconcile_locked,
    _verify_supervisor_session_id,
    reconcile,
)
from cw.reconcile.idle import (
    _act_on_idle_candidates,
    _detect_idle_candidates,
    flag_silently_idle_daemon_sessions,
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
from cw.reconcile.salvage import (
    rescue_finalize_blocked_sessions,
    salvage_committed_no_pr_sessions,
)
from cw.reconcile.stalled import (
    _act_on_stalled_candidates,
    _detect_stalled_candidates,
    revert_stalled_headless_sessions,
)
from cw.reconcile.tasks import (
    complete_timed_out_merged_tasks,
    park_terminal_sibling_tasks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)

__all__ = [
    "AUTO_DEV_LABEL_PREFIX",
    "DEFAULT_IDLE_RETRY_CAP",
    "DEFAULT_STALLED_RETRY_CAP",
    "HEADLESS_TIMEOUT_SECONDS",
    "IDLE_WATCHDOG_SECONDS",
    "SPAWN_GRACE_SECONDS",
    "SUBAGENT_LIVENESS_WINDOW_SECONDS",
    "TRANSCRIPT_LIVENESS_WINDOW_SECONDS",
    "_CAUSE_IDLE_STALL",
    "_CAUSE_USAGE_LIMIT",
    "_DIRTY_WORKTREE_REASON",
    "_FINALIZE_BLOCKED_REASON",
    "_FRESHNESS_BLOCK_ESCALATED_REASON",
    "_MAIN_CHECKOUT_DRIFT_REASON",
    "_NEEDS_SALVAGE_REASON",
    "_SALVAGE_KIND_GIT_STATE",
    "_SALVAGE_SKIP_ESCALATED_REASON",
    "_SALVAGE_SKIP_REASON",
    "_SALVAGE_TERMINAL_STATUSES",
    "_SILENTLY_IDLE_REASON",
    "_STAGE_REVIEW_COMPLETE",
    "_STALLED_CAP_PARKED_REASON",
    "_VALIDATION_FAILED_MAX_ATTEMPTS",
    "ProposedAction",
    "ReapCandidate",
    "ReconcileReport",
    "_act_on_idle_candidates",
    "_act_on_local_harvest_candidates",
    "_act_on_main_drift_candidates",
    "_act_on_phantom_candidates",
    "_act_on_stalled_candidates",
    "_apply_salvaged_completion",
    "_apply_sentinel_to_task",
    "_assistant_text_from_transcript",
    "_awaiting_subagent",
    "_backfill_claude_session_ids",
    "_claude_agents_json",
    "_compute_worktree_dirty",
    "_csid_from_transcript",
    "_detect_idle_candidates",
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
    "flag_silently_idle_daemon_sessions",
    "park_terminal_sibling_tasks",
    "reconcile",
    "rescue_finalize_blocked_sessions",
    "resolve_headless_budget",
    "resolve_idle_retry_cap",
    "resolve_idle_watchdog_budget",
    "resolve_reap_policy",
    "resolve_stalled_retry_cap",
    "revert_completed_silent_tasks",
    "revert_stalled_headless_sessions",
    "revert_timed_out_tasks",
    "salvage_committed_no_pr_sessions",
    "ticket_id_for_session",
]
