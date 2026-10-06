"""Re-export and logger guards for the ``cw.reconcile._shared`` package (#2214).

The flat ``reconcile/_shared.py`` -> ``reconcile/_shared/`` package split must
keep every ``from cw.reconcile._shared import X`` call site working unchanged,
keep the emitted logger name ``cw.reconcile._shared`` byte-identical, and keep
every test monkeypatch landing in the namespace the code under test actually
reads. This mirrors ``tests/test_auto_dev_result_schema_reexports.py`` (the
#2193 split): the surface is asserted against an exhaustive hardcoded set,
deliberately NOT re-derived from the package, so a dropped or renamed name is a
falsifiable failure rather than a tautology. A deliberate addition updates this
set in the same commit.
"""

from __future__ import annotations

import importlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cw.reconcile
from cw.auto_dev_result import (
    BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
    AutoDevResult,
    BlockedResult,
    Blocker,
)
from cw.config import dev_queue_file, load_state, save_state
from cw.dev_queue import save_dev_queue
from cw.models import (
    CwState,
    DevQueueStore,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import _shared
from tests._clients_yaml import staged_client, write_clients_yaml
from tests._reconcile_helpers import _mk_session, _stage_complete_payload
from tests.conftest import (
    _make_daemon_session,
    _make_ticket_task,
    _write_idle_transcript,
)

# The complete importable surface: every top-level name the flat
# ``reconcile/_shared.py`` bound before the split (134 definitions, the six
# public aliases, and ``_log``). Third-party/stdlib names the old module merely
# imported (``get_client``, ``subprocess``, ...) are deliberately NOT part of
# the surface: no consumer imports them through ``_shared``.
EXPECTED_EXPORTS = {
    # Shared reason/key/template/status constants (_constants)
    "AUTO_DEV_LABEL_PREFIX",
    "SPAWN_GRACE_SECONDS",
    "TRANSCRIPT_LIVENESS_WINDOW_SECONDS",
    "USAGE_LIMIT_BACKOFF_WINDOW_SECONDS",
    "USAGE_LIMIT_SALVAGE_WINDOW_SECONDS",
    "_CAUSE_IDLE_STALL",
    "_CAUSE_USAGE_LIMIT",
    "_DANGLING_TOOL_USE_REASON",
    "_DIRTY_WORKTREE_REASON",
    "_DISPATCH_LOOP_STALE_REASON",
    "_EXTERNAL_COUNTERPARTY_IDLE_REASON",
    "_FINALIZE_BLOCKED_REASON",
    "_FIX_DISPATCH_REF_UNRESOLVED_REASON",
    "_FIX_LOOP_AWAIT_DEADLINE_EXCEEDED_REASON",
    "_FRESHNESS_BLOCK_ESCALATED_REASON",
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
    "_RESCUE_PR_BODY_TEMPLATE",
    "_RESCUE_PR_CLOSES_TRAILER_TEMPLATE",
    "_SALVAGE_KIND_GIT_STATE",
    "_SALVAGE_PR_BODY_TEMPLATE",
    "_SALVAGE_PR_TITLE_TEMPLATE",
    "_SALVAGE_SKIP_ESCALATED_REASON",
    "_SALVAGE_SKIP_REASON",
    "_SENTINEL_ADVANCE_REFUSED_KEY",
    "_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY",
    "_SENTINEL_STAGE_MISMATCH_REFUSED_REASON",
    "_SESSION_ID_MISMATCH_ADVISORY_NOTE",
    "_SESSION_UNRESPONSIVE_REASON",
    "_SILENTLY_IDLE_REASON",
    "_STAGE_REVIEW_COMPLETE",
    "_STALLED_CAP_PARKED_REASON",
    "_STOPPED_WITHOUT_SENTINEL_REASON",
    "_TIMED_OUT_MERGED_REASON",
    "_UNCONSUMED_QUEUE_NOTIFICATION_REASON",
    "_UNRESOLVED_SUBAGENT_SPAWN_REASON",
    "_USAGE_LIMITED_MID_TURN_REASON",
    # Report / candidate / action types (_types)
    "ProposedAction",
    "ReapCandidate",
    "ReconcileReport",
    "_apply_correction_signal_fields",
    "_resolve_routed_sentinel",
    # Transcript location and record iteration (_transcripts)
    "_TranscriptRecordIterator",
    "_csid_from_transcript",
    "_effective_transcript_timestamp",
    "_iter_assistant_records",
    "_iter_notification_records",
    "_iter_transcript_records",
    "_locate_session_transcript",
    "_newest_surface_ref_transcript",
    "_project_transcripts_latest_timestamp",
    "_session_project_dir",
    "_transcript_age_seconds",
    "_transcript_recently_active",
    "_widened_transcript_timestamp",
    # Transcript-tail detectors (_detectors)
    "DanglingToolUseEvidence",
    "UsageLimitDetection",
    "_SUBAGENT_SPAWNING_TOOL_NAMES",
    "_TOOL_USE_COMMAND_SNIPPET_MAX_CHARS",
    "_apply_tool_use_block",
    "_bash_command_snippet",
    "_detect_dangling_tool_use",
    "_detect_post_review_clean",
    "_detect_provider_overload",
    "_detect_unconsumed_queue_notification",
    "_detect_usage_limit",
    "_redact_and_truncate",
    "_usage_limit_is_recent",
    # Worktree / subagent-spawn evidence (_worktree_evidence)
    "_is_headless",
    "_read_agent_spawn_stamp_context",
    "_read_unresolved_subagent_spawn",
    "_unresolved_subagent_spawn_age_seconds",
    "_worktree_dirty_reason_by_path",
    # Sentinel parsing, salvage and staged-emit latches (_sentinels)
    "_SALVAGE_TERMINAL_STATUSES",
    "_apply_salvaged_completion",
    "_foreign_result_target_queue_status",
    "_has_terminal_sentinel",
    "_parse_any_sentinel_from_transcript",
    "_parse_sentinel_from_blocks",
    "_queue_status_for_salvaged",
    "_salvage_terminal_result",
    "_sentinel_partial_route_consumed",
    "_stamp_sentinel_partial_route_consumed",
    "_validate_existing_result_for_routing",
    "_verify_salvaged_scope",
    "classify_sentinel_stage_position",
    "holds_staged_emit_result",
    "stage_refusal_latched",
    "stamp_stage_refusal",
    # Sentinel -> dev-queue routing (_routing)
    "AuditedSentinelRouteOutcome",
    "SentinelRouteOutcome",
    "_DETERMINISTIC_PARSE_FAILURES",
    "_DEV_QUEUE_LOAD_ERRORS",
    "_GENUINELY_TERMINAL_QUEUE_STATUSES",
    "_TERMINAL_NO_RETRY_STATUSES",
    "_TRANSIENT_PARSE_FAILURES",
    "_TaskLookupResult",
    "_VALIDATION_FAILED_MAX_ATTEMPTS",
    "_apply_queue_mutations",
    "_apply_sentinel_to_task",
    "_apply_sentinel_to_task_audited",
    "_blocked_result_requeue_enabled",
    "_land_blocked_result_failed",
    "_load_dev_queue_or_none",
    "_lookup_matching_task",
    "_requeue_blocked_result_under_cap",
    "_route_blocked_result_to_task",
    "_route_stopped_without_sentinel",
    "find_running_task_for_session",
    # Daemon roster, drift and session liveness (_roster)
    "SessionLivenessForTask",
    "_backfill_claude_session_ids",
    "_claude_agents_json",
    "_looks_like_daemon_outage",
    "_session_id_advisory_mismatch",
    "_stamp_session_id_mismatch_advisories",
    "compute_drift",
    "find_live_sessions_for_ticket",
    "resolve_session_for_task",
    "resolve_session_liveness_for_task",
    "session_daemon_liveness",
    "ticket_id_for_session",
    # Reap policy and reap-proposed emission (_reap)
    "_REAP_PROPOSED_ACTIONS",
    "_emit_reap_proposed",
    "feature_branch_key",
    "resolve_attempt_ceiling",
    "resolve_reap_policy",
    # Defined in the package ``__init__`` itself: the logger and the public
    # aliases cluster modules call as ``_shared.NAME``.
    "_log",
    "detect_provider_overload",
    "detect_usage_limit",
    "read_unresolved_subagent_spawn",
    "salvage_terminal_result",
    "usage_limit_is_recent",
    "worktree_dirty_reason_by_path",
}


class TestPackageExportCompleteness:
    """Guards that ``cw.reconcile._shared`` keeps its full pre-split surface."""

    def test_expected_surface_size(self) -> None:
        assert len(EXPECTED_EXPORTS) == 141

    def test_all_matches_full_surface(self) -> None:
        assert set(_shared.__all__) == EXPECTED_EXPORTS

    def test_every_expected_name_is_bound(self) -> None:
        """A dropped re-export must fail here, not at a downstream import site."""
        missing = [name for name in EXPECTED_EXPORTS if not hasattr(_shared, name)]
        assert missing == []

    def test_reconcile_package_reexports_resolve_to_same_objects(self) -> None:
        """``cw.reconcile``'s re-exports of ``_shared`` names are the same objects."""
        shared_names = set(cw.reconcile.__all__) & EXPECTED_EXPORTS
        assert shared_names
        mismatched = [
            name
            for name in sorted(shared_names)
            if getattr(cw.reconcile, name) is not getattr(_shared, name)
        ]
        assert mismatched == []


_PKG = "cw.reconcile._shared"

# (function, module global it reads, module whose namespace it reads it from).
# A test that monkeypatches the global must target that module: a patch on any
# other namespace that also binds the name resolves fine but silently stops
# intercepting -- and a no-op assertion like ``saves == []`` then passes
# vacuously. Each extraction commit repoints the rows whose function it moves.
PATCH_OWNERSHIP = [
    ("_worktree_dirty_reason_by_path", "get_client", _PKG),
    ("_worktree_dirty_reason_by_path", "unsaved_work_reason", _PKG),
    ("_worktree_dirty_reason_by_path", "_deps", _PKG),
    ("_blocked_result_requeue_enabled", "get_client", _PKG),
    ("_claude_agents_json", "subprocess", _PKG),
    (
        "_widened_transcript_timestamp",
        "_locate_session_transcript",
        f"{_PKG}._transcripts",
    ),
    (
        "_detect_unconsumed_queue_notification",
        "_locate_session_transcript",
        f"{_PKG}._detectors",
    ),
    ("_detect_post_review_clean", "read_events", f"{_PKG}._detectors"),
    ("_emit_reap_proposed", "record_event", _PKG),
    ("_emit_reap_proposed", "save_state", _PKG),
    ("_apply_sentinel_to_task", "save_dev_queue", _PKG),
    ("_apply_sentinel_to_task", "record_event", _PKG),
    ("_apply_sentinel_to_task", "_deps", _PKG),
]


class TestPatchOwnership:
    """Guards that each patched global lives where its reader looks it up."""

    @pytest.mark.parametrize(("function", "global_name", "owner"), PATCH_OWNERSHIP)
    def test_function_reads_global_from_owner(
        self, function: str, global_name: str, owner: str
    ) -> None:
        namespace = vars(importlib.import_module(owner))
        assert getattr(_shared, function).__globals__ is namespace
        assert global_name in namespace


# Every record the package emits must carry the pre-split logger name
# ``cw.reconcile._shared`` verbatim. ``caplog.at_level(..., logger=...)`` alone
# cannot catch a rename -- level inheritance and propagation make a
# ``__name__``-derived child logger (``cw.reconcile._shared._routing``) pass the
# same assertions -- so these tests pin ``record.name`` exactly, filtered to the
# record the function under test emits (propagation also captures records from
# other ``cw`` loggers). One case per pre-split ``_log`` call site.
PINNED_LOGGER_NAME = "cw.reconcile._shared"


def _names_of(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [r.name for r in caplog.records if needle in r.getMessage()]


class TestLoggerNamePinned:
    """Guards that the package split did not rename the emitted logger (#2214)."""

    def test_load_dev_queue_or_none_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.reconcile._shared import _load_dev_queue_or_none

        dev_queue_file().write_text("{ not json", encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            assert _load_dev_queue_or_none("logger_pin_probe", "T-pin") is None

        assert _names_of(caplog, "logger_pin_probe skipped") == [PINNED_LOGGER_NAME]

    def test_blocked_result_requeue_enabled_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No clients.yaml entry -> fail closed with the shadow-mode warning."""
        from cw.reconcile._shared import _blocked_result_requeue_enabled

        task = _make_ticket_task(ticket_id="T-pin-shadow", client="unconfigured-client")
        sentinel = BlockedResult(
            blocker=Blocker(
                stage="unknown",
                reason=BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
                details="test: logger pin",
            )
        )

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            assert _blocked_result_requeue_enabled(task, sentinel) is False

        assert _names_of(caplog, "sentinel.blocked_result_requeue_shadowed") == [
            PINNED_LOGGER_NAME
        ]

    def test_apply_sentinel_to_task_race_miss_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.reconcile._shared import _apply_sentinel_to_task

        write_clients_yaml(staged_client("staged-client", sentinel_mismatch_veto=True))
        ticket_id, session_id = "T-pin-race", "sess-pin-race"
        session = _make_daemon_session(id=session_id, worktree_path=None)
        task = TicketTask(
            ticket_id=ticket_id,
            client="staged-client",
            status=QueueItemStatus.FAILED,
            session_id=session_id,
            stage=Stage.IMPL,
            disposition="abandoned",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        sentinel = AutoDevResult.model_validate(_stage_complete_payload())

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            outcome = _apply_sentinel_to_task(ticket_id, session, sentinel)

        assert outcome.routed is False
        assert _names_of(caplog, "sentinel_race_miss_detected") == [PINNED_LOGGER_NAME]

    def test_csid_from_transcript_debug(
        self, tmp_config_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.reconcile._shared import _csid_from_transcript

        started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        worktree = tmp_path / "wt-logger-pin-csid"
        full_stem = "abcd1234-full-uuid-pin"
        session = _make_daemon_session(
            id="sess-pin-csid",
            worktree_path=worktree,
            surface_ref="abcd1234",
            claude_session_id=None,
            started_at=started_at,
        )
        transcript = _write_idle_transcript(
            Path.home(), worktree, filename=f"{full_stem}.jsonl"
        )
        after_ts = started_at.timestamp() + 60
        os.utime(str(transcript), (after_ts, after_ts))

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            assert _csid_from_transcript(session) == full_stem

        assert _names_of(caplog, "via transcript fallback") == [PINNED_LOGGER_NAME]

    def test_stamp_session_id_mismatch_advisories_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.reconcile._shared import _stamp_session_id_mismatch_advisories

        state = CwState(sessions=[_mk_session("pin-live-1", surface_ref="aaaaaaaa")])
        save_state(state)
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="T-pin-ghost",
                        client="client-a",
                        status=QueueItemStatus.RUNNING,
                        session_id="a2fe4bd5",
                    )
                ]
            )
        )

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            _stamp_session_id_mismatch_advisories(state, {"aaaaaaaa"})

        assert _names_of(caplog, "session_id_mismatch_advisory_set") == [
            PINNED_LOGGER_NAME
        ]

    def test_backfill_claude_session_ids_debug(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A live DAEMON session with no csid is backfilled from the roster.

        ``count > 0`` reaches both the ``_log.debug`` and the ``save_state``
        branch, so the persisted csid is asserted too.
        """
        from cw.reconcile._shared import _backfill_claude_session_ids

        session = _make_daemon_session(
            id="sess-pin-backfill",
            surface_ref="pinref01",
            claude_session_id=None,
        )
        state = CwState(sessions=[session])
        save_state(state)

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            count = _backfill_claude_session_ids(
                state, {"pinref01": "pinref01-full-csid"}
            )

        assert count == 1
        assert load_state().sessions[0].claude_session_id == "pinref01-full-csid"
        assert _names_of(caplog, "Backfilled claude_session_id") == [PINNED_LOGGER_NAME]


# Submodules that log. Each binds its own ``_log`` to the pinned name via
# ``_constants._LOGGER_NAME``, never ``__name__``.
LOGGING_SUBMODULES = ["_transcripts"]


class TestLoggerObjectsPinned:
    """Every ``_log`` in the package is the one pinned-name Logger."""

    def test_package_logger_uses_pinned_name(self) -> None:
        assert _shared._log.name == PINNED_LOGGER_NAME

    @pytest.mark.parametrize("submodule", LOGGING_SUBMODULES)
    def test_submodule_logger_is_the_package_logger(self, submodule: str) -> None:
        module_log = vars(importlib.import_module(f"{_PKG}.{submodule}"))["_log"]
        assert module_log.name == PINNED_LOGGER_NAME
        assert module_log is _shared._log
