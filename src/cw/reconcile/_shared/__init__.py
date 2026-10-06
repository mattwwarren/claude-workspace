"""Shared constants, dataclasses, and leaf helpers for the reconcile package.

This package holds the cross-cutting pieces used by more than one reconcile
cluster (idle, stalled, phantom, salvage, tasks, core): module-level
constants, the :class:`ReconcileReport` / :class:`ReapCandidate` dataclasses,
the :class:`ProposedAction` enum, and the transcript / worktree / queue leaf
helpers. See the ``cw.reconcile`` package ``__init__`` docstring for the full
architecture.

It was split out of a single ``reconcile/_shared.py`` module (#2214); every
``from cw.reconcile._shared import X`` site is preserved here via
re-exports. Submodules, in dependency order:

- ``_constants`` -- reason, key, template and status constants, and the
  pinned :data:`_LOGGER_NAME`. Imports from no sibling.
- ``_types`` -- ``ProposedAction``, ``ReapCandidate``, ``ReconcileReport``.
  Imports from no sibling.
- ``_worktree_evidence`` -- unsaved work, headless marker, subagent-spawn
  stamp. Imports from no sibling.
- ``_transcripts`` -- transcript location, timestamps, record iteration.
  Imports ``_constants``.
- ``_detectors`` -- transcript-tail detectors. Imports ``_constants`` and
  ``_transcripts``.
- ``_sentinels`` -- sentinel parsing, salvage, staged-emit latches. Imports
  ``_constants`` and ``_transcripts``.
- ``_roster`` -- daemon roster drift and session liveness. Imports
  ``_constants``, ``_transcripts`` and ``_types``.
- ``_reap`` -- reap policy and reap-proposed emission. Imports
  ``_transcripts`` and ``_types``.
- ``_routing`` -- sentinel-to-dev-queue routing. Imports ``_constants``.

Every submodule logs under the pinned name ``cw.reconcile._shared`` (never
``__name__``), so the emitted logger name is unchanged by the split.

A test that monkeypatches a module global the code reads (``get_client``,
``subprocess``, ``record_event``, ...) must target the submodule that owns
the reading function: a patch on this package's namespace resolves but no
longer intercepts. The public aliases at the bottom of this module are the
exception -- cluster modules read them off this package at call time.
"""

from __future__ import annotations

import logging

from cw.reconcile._shared._constants import (
    _CAUSE_IDLE_STALL,
    _CAUSE_USAGE_LIMIT,
    _DANGLING_TOOL_USE_REASON,
    _DIRTY_WORKTREE_REASON,
    _DISPATCH_LOOP_STALE_REASON,
    _EXTERNAL_COUNTERPARTY_IDLE_REASON,
    _FINALIZE_BLOCKED_REASON,
    _FIX_DISPATCH_REF_UNRESOLVED_REASON,
    _FIX_LOOP_AWAIT_DEADLINE_EXCEEDED_REASON,
    _FRESHNESS_BLOCK_ESCALATED_REASON,
    _GH_CHECK_BLOCKED_REASON,
    _LIVE_STATUSES,
    _LOGGER_NAME,
    _MAIN_CHECKOUT_DRIFT_REASON,
    _NEEDS_SALVAGE_REASON,
    _NEVER_CLAIMED_COMPLETION_REASON,
    _PAUSED_STATUS_KEY,
    _PHANTOM_REAP_MERGED_REASON,
    _QUEUE_OPERATION_ENQUEUE,
    _QUEUE_OPERATION_RECORD_TYPE,
    _REAP_ELIGIBLE_DISPOSITIONS_BASE,
    _RESCUE_PR_BODY_TEMPLATE,
    _RESCUE_PR_CLOSES_TRAILER_TEMPLATE,
    _SALVAGE_KIND_GIT_STATE,
    _SALVAGE_PR_BODY_TEMPLATE,
    _SALVAGE_PR_TITLE_TEMPLATE,
    _SALVAGE_SKIP_ESCALATED_REASON,
    _SALVAGE_SKIP_REASON,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _SESSION_ID_MISMATCH_ADVISORY_NOTE,
    _SESSION_UNRESPONSIVE_REASON,
    _SILENTLY_IDLE_REASON,
    _STAGE_REVIEW_COMPLETE,
    _STALLED_CAP_PARKED_REASON,
    _STOPPED_WITHOUT_SENTINEL_REASON,
    _TIMED_OUT_MERGED_REASON,
    _UNCONSUMED_QUEUE_NOTIFICATION_REASON,
    _UNRESOLVED_SUBAGENT_SPAWN_REASON,
    _USAGE_LIMITED_MID_TURN_REASON,
    AUTO_DEV_LABEL_PREFIX,
    SPAWN_GRACE_SECONDS,
    TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
    USAGE_LIMIT_BACKOFF_WINDOW_SECONDS,
    USAGE_LIMIT_SALVAGE_WINDOW_SECONDS,
)
from cw.reconcile._shared._detectors import (
    _SUBAGENT_SPAWNING_TOOL_NAMES,
    _TOOL_USE_COMMAND_SNIPPET_MAX_CHARS,
    DanglingToolUseEvidence,
    UsageLimitDetection,
    _apply_tool_use_block,
    _bash_command_snippet,
    _detect_dangling_tool_use,
    _detect_post_review_clean,
    _detect_provider_overload,
    _detect_unconsumed_queue_notification,
    _detect_usage_limit,
    _redact_and_truncate,
    _usage_limit_is_recent,
)
from cw.reconcile._shared._reap import (
    _REAP_PROPOSED_ACTIONS,
    _emit_reap_proposed,
    feature_branch_key,
    resolve_attempt_ceiling,
    resolve_reap_policy,
)
from cw.reconcile._shared._roster import (
    SessionLivenessForTask,
    _backfill_claude_session_ids,
    _claude_agents_json,
    _looks_like_daemon_outage,
    _session_id_advisory_mismatch,
    _stamp_session_id_mismatch_advisories,
    compute_drift,
    find_live_sessions_for_ticket,
    resolve_session_for_task,
    resolve_session_liveness_for_task,
    session_daemon_liveness,
    ticket_id_for_session,
)
from cw.reconcile._shared._routing import (
    _DETERMINISTIC_PARSE_FAILURES,
    _DEV_QUEUE_LOAD_ERRORS,
    _GENUINELY_TERMINAL_QUEUE_STATUSES,
    _TERMINAL_NO_RETRY_STATUSES,
    _TRANSIENT_PARSE_FAILURES,
    _VALIDATION_FAILED_MAX_ATTEMPTS,
    AuditedSentinelRouteOutcome,
    SentinelRouteOutcome,
    _apply_queue_mutations,
    _apply_sentinel_to_task,
    _apply_sentinel_to_task_audited,
    _blocked_result_requeue_enabled,
    _land_blocked_result_failed,
    _load_dev_queue_or_none,
    _lookup_matching_task,
    _requeue_blocked_result_under_cap,
    _route_blocked_result_to_task,
    _route_stopped_without_sentinel,
    _TaskLookupResult,
    find_running_task_for_session,
)
from cw.reconcile._shared._sentinels import (
    _SALVAGE_TERMINAL_STATUSES,
    _apply_salvaged_completion,
    _foreign_result_target_queue_status,
    _has_terminal_sentinel,
    _parse_any_sentinel_from_transcript,
    _parse_sentinel_from_blocks,
    _queue_status_for_salvaged,
    _salvage_terminal_result,
    _sentinel_partial_route_consumed,
    _stamp_sentinel_partial_route_consumed,
    _validate_existing_result_for_routing,
    _verify_salvaged_scope,
    classify_sentinel_stage_position,
    holds_staged_emit_result,
    stage_refusal_latched,
    stamp_stage_refusal,
)
from cw.reconcile._shared._transcripts import (
    _csid_from_transcript,
    _effective_transcript_timestamp,
    _iter_assistant_records,
    _iter_notification_records,
    _iter_transcript_records,
    _locate_session_transcript,
    _newest_surface_ref_transcript,
    _project_transcripts_latest_timestamp,
    _session_project_dir,
    _transcript_age_seconds,
    _transcript_recently_active,
    _TranscriptRecordIterator,
    _widened_transcript_timestamp,
)
from cw.reconcile._shared._types import (
    ProposedAction,
    ReapCandidate,
    ReconcileReport,
    _apply_correction_signal_fields,
    _resolve_routed_sentinel,
)
from cw.reconcile._shared._worktree_evidence import (
    _is_headless,
    _read_agent_spawn_stamp_context,
    _read_unresolved_subagent_spawn,
    _unresolved_subagent_spawn_age_seconds,
    _worktree_dirty_reason_by_path,
)

_log = logging.getLogger(_LOGGER_NAME)


# Non-underscore aliases for the cross-cutting helpers above, so cluster modules
# can call them as public attributes (``_shared.NAME``) without tripping the
# private-member-access lint. Routing every cluster's call through the single
# ``_shared`` attribute preserves the pre-split property that one test patch at
# ``cw.reconcile._shared.NAME`` intercepts all callers. These helpers are not
# called elsewhere inside this module, so there is no dual-name hazard.
detect_usage_limit = _detect_usage_limit
detect_provider_overload = _detect_provider_overload
usage_limit_is_recent = _usage_limit_is_recent
salvage_terminal_result = _salvage_terminal_result
worktree_dirty_reason_by_path = _worktree_dirty_reason_by_path
read_unresolved_subagent_spawn = _read_unresolved_subagent_spawn

__all__ = [
    "AUTO_DEV_LABEL_PREFIX",
    "SPAWN_GRACE_SECONDS",
    "TRANSCRIPT_LIVENESS_WINDOW_SECONDS",
    "USAGE_LIMIT_BACKOFF_WINDOW_SECONDS",
    "USAGE_LIMIT_SALVAGE_WINDOW_SECONDS",
    "_CAUSE_IDLE_STALL",
    "_CAUSE_USAGE_LIMIT",
    "_DANGLING_TOOL_USE_REASON",
    "_DETERMINISTIC_PARSE_FAILURES",
    "_DEV_QUEUE_LOAD_ERRORS",
    "_DIRTY_WORKTREE_REASON",
    "_DISPATCH_LOOP_STALE_REASON",
    "_EXTERNAL_COUNTERPARTY_IDLE_REASON",
    "_FINALIZE_BLOCKED_REASON",
    "_FIX_DISPATCH_REF_UNRESOLVED_REASON",
    "_FIX_LOOP_AWAIT_DEADLINE_EXCEEDED_REASON",
    "_FRESHNESS_BLOCK_ESCALATED_REASON",
    "_GENUINELY_TERMINAL_QUEUE_STATUSES",
    "_GH_CHECK_BLOCKED_REASON",
    "_LIVE_STATUSES",
    "_MAIN_CHECKOUT_DRIFT_REASON",
    "_NEEDS_SALVAGE_REASON",
    "_NEVER_CLAIMED_COMPLETION_REASON",
    "_PAUSED_STATUS_KEY",
    "_PHANTOM_REAP_MERGED_REASON",
    "_QUEUE_OPERATION_ENQUEUE",
    "_QUEUE_OPERATION_RECORD_TYPE",
    "_REAP_ELIGIBLE_DISPOSITIONS_BASE",
    "_REAP_PROPOSED_ACTIONS",
    "_RESCUE_PR_BODY_TEMPLATE",
    "_RESCUE_PR_CLOSES_TRAILER_TEMPLATE",
    "_SALVAGE_KIND_GIT_STATE",
    "_SALVAGE_PR_BODY_TEMPLATE",
    "_SALVAGE_PR_TITLE_TEMPLATE",
    "_SALVAGE_SKIP_ESCALATED_REASON",
    "_SALVAGE_SKIP_REASON",
    "_SALVAGE_TERMINAL_STATUSES",
    "_SENTINEL_ADVANCE_REFUSED_KEY",
    "_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY",
    "_SENTINEL_STAGE_MISMATCH_REFUSED_REASON",
    "_SESSION_ID_MISMATCH_ADVISORY_NOTE",
    "_SESSION_UNRESPONSIVE_REASON",
    "_SILENTLY_IDLE_REASON",
    "_STAGE_REVIEW_COMPLETE",
    "_STALLED_CAP_PARKED_REASON",
    "_STOPPED_WITHOUT_SENTINEL_REASON",
    "_SUBAGENT_SPAWNING_TOOL_NAMES",
    "_TERMINAL_NO_RETRY_STATUSES",
    "_TIMED_OUT_MERGED_REASON",
    "_TOOL_USE_COMMAND_SNIPPET_MAX_CHARS",
    "_TRANSIENT_PARSE_FAILURES",
    "_UNCONSUMED_QUEUE_NOTIFICATION_REASON",
    "_UNRESOLVED_SUBAGENT_SPAWN_REASON",
    "_USAGE_LIMITED_MID_TURN_REASON",
    "_VALIDATION_FAILED_MAX_ATTEMPTS",
    "AuditedSentinelRouteOutcome",
    "DanglingToolUseEvidence",
    "ProposedAction",
    "ReapCandidate",
    "ReconcileReport",
    "SentinelRouteOutcome",
    "SessionLivenessForTask",
    "UsageLimitDetection",
    "_TaskLookupResult",
    "_TranscriptRecordIterator",
    "_apply_correction_signal_fields",
    "_apply_queue_mutations",
    "_apply_salvaged_completion",
    "_apply_sentinel_to_task",
    "_apply_sentinel_to_task_audited",
    "_apply_tool_use_block",
    "_backfill_claude_session_ids",
    "_bash_command_snippet",
    "_blocked_result_requeue_enabled",
    "_claude_agents_json",
    "_csid_from_transcript",
    "_detect_dangling_tool_use",
    "_detect_post_review_clean",
    "_detect_provider_overload",
    "_detect_unconsumed_queue_notification",
    "_detect_usage_limit",
    "_effective_transcript_timestamp",
    "_emit_reap_proposed",
    "_foreign_result_target_queue_status",
    "_has_terminal_sentinel",
    "_is_headless",
    "_iter_assistant_records",
    "_iter_notification_records",
    "_iter_transcript_records",
    "_land_blocked_result_failed",
    "_load_dev_queue_or_none",
    "_locate_session_transcript",
    "_log",
    "_looks_like_daemon_outage",
    "_lookup_matching_task",
    "_newest_surface_ref_transcript",
    "_parse_any_sentinel_from_transcript",
    "_parse_sentinel_from_blocks",
    "_project_transcripts_latest_timestamp",
    "_queue_status_for_salvaged",
    "_read_agent_spawn_stamp_context",
    "_read_unresolved_subagent_spawn",
    "_redact_and_truncate",
    "_requeue_blocked_result_under_cap",
    "_resolve_routed_sentinel",
    "_route_blocked_result_to_task",
    "_route_stopped_without_sentinel",
    "_salvage_terminal_result",
    "_sentinel_partial_route_consumed",
    "_session_id_advisory_mismatch",
    "_session_project_dir",
    "_stamp_sentinel_partial_route_consumed",
    "_stamp_session_id_mismatch_advisories",
    "_transcript_age_seconds",
    "_transcript_recently_active",
    "_unresolved_subagent_spawn_age_seconds",
    "_usage_limit_is_recent",
    "_validate_existing_result_for_routing",
    "_verify_salvaged_scope",
    "_widened_transcript_timestamp",
    "_worktree_dirty_reason_by_path",
    "classify_sentinel_stage_position",
    "compute_drift",
    "detect_provider_overload",
    "detect_usage_limit",
    "feature_branch_key",
    "find_live_sessions_for_ticket",
    "find_running_task_for_session",
    "holds_staged_emit_result",
    "read_unresolved_subagent_spawn",
    "resolve_attempt_ceiling",
    "resolve_reap_policy",
    "resolve_session_for_task",
    "resolve_session_liveness_for_task",
    "salvage_terminal_result",
    "session_daemon_liveness",
    "stage_refusal_latched",
    "stamp_stage_refusal",
    "ticket_id_for_session",
    "usage_limit_is_recent",
    "worktree_dirty_reason_by_path",
]
