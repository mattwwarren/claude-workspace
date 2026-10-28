"""In-flight inspection of live dev-queue sessions (``cw queue peek``).

A BLOCKED_ON_USER row short-circuits to the ``AWAITING_OPERATOR``
recommendation (#2212) before any of the below is computed -- it is waiting
on the orchestrator's triage, not wedged, and scoring an intentionally-idle
row would report false precision. For each RUNNING task (one client or all), look up:

- session age (primarily the session's claim time — ``Session.started_at``
  in CW_STATE — falling back to the first user message in the worker's
  transcript when claim data is unavailable)
- idle gap (last assistant message)
- last AUTO_DEV_RESULT sentinel status and stage
- PR number and state (via ``gh pr view``)
- attempt counters: raw ``attempts`` (display only) and ``unproductive_attempts``
  (the STOP-by-attempt-count branch's actual signal, mirroring the dispatch
  admission gate's #1750 counter)

Then compute a WAIT / PEEK / STOP recommendation per row so the operator can
decide whether to keep a session alive or close it via ``cw spawn close``.

Reports only — never stops sessions itself.

Package layout (#2793), one submodule per concern; dependencies run one way,
from ``rows`` to the others:

- ``locate`` -- session refs from ``CW_STATE`` and transcript location under
  ``CLAUDE_PROJECTS``.
- ``parse`` -- transcript / opencode-log and sentinel parsing.
- ``ladder`` -- the WAIT / PEEK / STOP thresholds and scoring. No I/O.
- ``rows`` -- report rows: ``build_peek_rows``, ``format_row``, PR state.
- ``render`` -- ``print_table``.

This module is a pure re-export. Patch a name on the submodule that reads it,
not on the package: ``CW_STATE`` / ``CLAUDE_PROJECTS`` below are snapshots
re-exported from ``locate`` (patch ``cw.queue_peek.locate``), and the names
``build_peek_rows`` / ``format_row`` look up are bound in ``rows`` (patch
``cw.queue_peek.rows.<name>``).
"""

from __future__ import annotations

from cw.queue_peek.ladder import (
    _STAGE_ORDER,
    IDLE_LIVE_MAX_MIN,
    IDLE_PEEK_MIN,
    IDLE_POST_PR_MIN,
    IDLE_STALL_MIN,
    PEEK_AGE_MIN,
    STOP_AGE_MIN,
    STOP_CEILING_MIN,
    STOP_UNPRODUCTIVE_ATTEMPTS_MIN,
    WAIT_AGE_MIN,
    _gate_stop_on_liveness,
    _reached_deep_stage,
    _score_session,
    _stage5_rec,
    _stall_check,
    recommend,
)

# Snapshots: patch these on ``cw.queue_peek.locate``, not on the package.
from cw.queue_peek.locate import (
    _EPOCH,
    CLAUDE_PROJECTS,
    CW_STATE,
    _compute_jsonl_idle_min,
    _find_transcript_heuristic,
    _find_transcript_in_project_dir,
    _is_known_idless_session,
    _load_session_refs,
    _matching_project_dirs,
    _parse_started_at,
    find_transcript_for_ticket,
    load_claude_session_id,
)
from cw.queue_peek.parse import (
    _newest_subagent_ts,
    _scan_assistant_content,
    _tool_result_pr_number,
    parse_opencode_transcript,
    parse_transcript,
)
from cw.queue_peek.render import print_table
from cw.queue_peek.rows import (
    _AWAITING_OPERATOR_FALLBACK_REASON,
    _BLIND_REASON_LOCAL_HANDLE,
    _BLIND_REASON_NEVER_STARTED,
    _PEEK_STATUSES,
    _SIGNAL_SOURCE_BLIND,
    _SIGNAL_SOURCE_PARKED,
    _SIGNAL_SOURCE_TRANSCRIPT,
    RECOMMEND_AWAITING_OPERATOR,
    RECOMMEND_BLIND,
    _awaiting_operator_reason,
    _awaiting_operator_row,
    build_peek_rows,
    format_row,
    gh_pr_state,
    load_attention_tasks,
    minutes_since,
)

__all__ = [
    "CLAUDE_PROJECTS",
    "CW_STATE",
    "IDLE_LIVE_MAX_MIN",
    "IDLE_PEEK_MIN",
    "IDLE_POST_PR_MIN",
    "IDLE_STALL_MIN",
    "PEEK_AGE_MIN",
    "RECOMMEND_AWAITING_OPERATOR",
    "RECOMMEND_BLIND",
    "STOP_AGE_MIN",
    "STOP_CEILING_MIN",
    "STOP_UNPRODUCTIVE_ATTEMPTS_MIN",
    "WAIT_AGE_MIN",
    "_AWAITING_OPERATOR_FALLBACK_REASON",
    "_BLIND_REASON_LOCAL_HANDLE",
    "_BLIND_REASON_NEVER_STARTED",
    "_EPOCH",
    "_PEEK_STATUSES",
    "_SIGNAL_SOURCE_BLIND",
    "_SIGNAL_SOURCE_PARKED",
    "_SIGNAL_SOURCE_TRANSCRIPT",
    "_STAGE_ORDER",
    "_awaiting_operator_reason",
    "_awaiting_operator_row",
    "_compute_jsonl_idle_min",
    "_find_transcript_heuristic",
    "_find_transcript_in_project_dir",
    "_gate_stop_on_liveness",
    "_is_known_idless_session",
    "_load_session_refs",
    "_matching_project_dirs",
    "_newest_subagent_ts",
    "_parse_started_at",
    "_reached_deep_stage",
    "_scan_assistant_content",
    "_score_session",
    "_stage5_rec",
    "_stall_check",
    "_tool_result_pr_number",
    "build_peek_rows",
    "find_transcript_for_ticket",
    "format_row",
    "gh_pr_state",
    "load_attention_tasks",
    "load_claude_session_id",
    "minutes_since",
    "parse_opencode_transcript",
    "parse_transcript",
    "print_table",
    "recommend",
]
