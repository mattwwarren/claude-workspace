"""Dev-queue schema migration: raw-payload default-fillers + migrate_dev_queue.

Extracted from the flat ``cw.dev_queue`` module (#1317, part 1). Holds the pure
dict-in / dict-out schema-normalisation layer: the per-task
``_fill_*_default`` helpers, the store-level ``_fill_watched_prs_default``, and
the ``migrate_dev_queue`` entry point that stamps a raw ``dev_queue.json``
payload up to the current schema version. No I/O, no locking — imported by
``cw.dev_queue.storage.load_dev_queue``.
"""

from __future__ import annotations

from typing import Any

from cw.models import DEFAULT_LANE, DEFAULT_STAGE, DEV_QUEUE_SCHEMA_VERSION


def _fill_task_cost_default(task_raw: dict[str, Any]) -> None:
    """Fill total_cost_usd introduced in dev-queue schema v2."""
    if "total_cost_usd" not in task_raw:
        task_raw["total_cost_usd"] = None


def _fill_lane_default(task_raw: dict[str, Any]) -> None:
    """Fill lane introduced in dev-queue schema v3."""
    if "lane" not in task_raw:
        task_raw["lane"] = DEFAULT_LANE


def _fill_task_stage_default(task_raw: dict[str, Any]) -> None:
    """Fill stage introduced in dev-queue schema v4 (GitHub #612). Idempotent."""
    if "stage" not in task_raw:
        task_raw["stage"] = DEFAULT_STAGE.value


def _fill_task_stage_base_ref_default(task_raw: dict[str, Any]) -> None:
    """Fill stage_base_ref from dev-queue schema v4 (GitHub #612). Idempotent."""
    if "stage_base_ref" not in task_raw:
        task_raw["stage_base_ref"] = None


def _fill_disposition_default(task_raw: dict[str, Any]) -> None:
    """Fill disposition introduced in dev-queue schema v5 (GitHub #310). Idempotent."""
    if "disposition" not in task_raw:
        task_raw["disposition"] = None


def _fill_pr_url_default(task_raw: dict[str, Any]) -> None:
    """Fill pr_url introduced in dev-queue schema v5 (GitHub #310). Idempotent."""
    if "pr_url" not in task_raw:
        task_raw["pr_url"] = None


def _fill_task_completed_at_default(task_raw: dict[str, Any]) -> None:
    """Fill completed_at introduced in dev-queue schema v5 (GitHub #310). Idempotent."""
    if "completed_at" not in task_raw:
        task_raw["completed_at"] = None


def _fill_regress_attempts_default(task_raw: dict[str, Any]) -> None:
    """Fill regress_attempts introduced in schema v6 (GitHub #770). Idempotent."""
    if "regress_attempts" not in task_raw:
        task_raw["regress_attempts"] = 0


def _fill_spawn_error_backoff_default(task_raw: dict[str, Any]) -> None:
    """Fill spawn_error_count/next_eligible_at introduced in schema v7 (GitHub #868).

    Idempotent."""
    if "spawn_error_count" not in task_raw:
        task_raw["spawn_error_count"] = 0
    if "next_eligible_at" not in task_raw:
        task_raw["next_eligible_at"] = None


def _fill_pr_state_default(task_raw: dict[str, Any]) -> None:
    """Fill pr_state introduced in dev-queue schema v8 (GitHub #929). Idempotent."""
    if "pr_state" not in task_raw:
        task_raw["pr_state"] = None


def _fill_signoff_default(task_raw: dict[str, Any]) -> None:
    """Fill signoff introduced in dev-queue schema v9 (GitHub #990). Idempotent."""
    if "signoff" not in task_raw:
        task_raw["signoff"] = None


def _fill_escalation_defaults(task_raw: dict[str, Any]) -> None:
    """Fill escalation_parked_at/escalation_fired_at introduced in dev-queue
    schema v10 (GitHub #1015, RFC 0008 capstone). Idempotent."""
    if "escalation_parked_at" not in task_raw:
        task_raw["escalation_parked_at"] = None
    if "escalation_fired_at" not in task_raw:
        task_raw["escalation_fired_at"] = None


def _fill_false_park_recovery_backoff_default(task_raw: dict[str, Any]) -> None:
    """Fill false_park_recovery_count/false_park_recovery_next_eligible_at
    introduced in dev-queue schema v11 (GitHub #1030). Idempotent."""
    if "false_park_recovery_count" not in task_raw:
        task_raw["false_park_recovery_count"] = 0
    if "false_park_recovery_next_eligible_at" not in task_raw:
        task_raw["false_park_recovery_next_eligible_at"] = None


def _fill_gate_recipe_failed_default(task_raw: dict[str, Any]) -> None:
    """Fill gate_recipe_failed_at introduced in dev-queue schema v12
    (GitHub #1065, RFC 0009). Idempotent."""
    if "gate_recipe_failed_at" not in task_raw:
        task_raw["gate_recipe_failed_at"] = None


def _fill_escalate_merge_block_default(task_raw: dict[str, Any]) -> None:
    """Fill escalate_merge_block_fired_at introduced in dev-queue schema v14
    (GitHub #1099, RFC 0010 P4). Idempotent."""
    if "escalate_merge_block_fired_at" not in task_raw:
        task_raw["escalate_merge_block_fired_at"] = None


def _fill_request_reviewer_fired_default(task_raw: dict[str, Any]) -> None:
    """Fill request_reviewer_fired_at introduced in dev-queue schema v16
    (GitHub #1197). Idempotent."""
    if "request_reviewer_fired_at" not in task_raw:
        task_raw["request_reviewer_fired_at"] = None


def _fill_auto_fix_ci_fired_default(task_raw: dict[str, Any]) -> None:
    """Fill auto_fix_ci_fired_at introduced in dev-queue schema v17
    (GitHub #1205). Idempotent."""
    if "auto_fix_ci_fired_at" not in task_raw:
        task_raw["auto_fix_ci_fired_at"] = None


def _fill_address_review_fired_default(task_raw: dict[str, Any]) -> None:
    """Fill address_review_fired_at introduced in dev-queue schema v18
    (GitHub #1206). Idempotent."""
    if "address_review_fired_at" not in task_raw:
        task_raw["address_review_fired_at"] = None


def _fill_last_blocked_result_default(task_raw: dict[str, Any]) -> None:
    """Fill last_blocked_result introduced in dev-queue schema v19
    (GitHub #1266). Idempotent."""
    if "last_blocked_result" not in task_raw:
        task_raw["last_blocked_result"] = None


def _fill_cross_repo_override_default(task_raw: dict[str, Any]) -> None:
    """Fill cross_repo_override introduced in dev-queue schema v20
    (GitHub #1198). Idempotent."""
    if "cross_repo_override" not in task_raw:
        task_raw["cross_repo_override"] = False


def _fill_stage_high_water_default(task_raw: dict[str, Any]) -> None:
    """Fill stage_high_water introduced in dev-queue schema v21
    (GitHub #1361), seeded from the task's current stage. Idempotent."""
    if "stage_high_water" not in task_raw:
        task_raw["stage_high_water"] = task_raw.get("stage", DEFAULT_STAGE.value)


def _fill_blocked_reason_default(task_raw: dict[str, Any]) -> None:
    """Fill blocked_reason introduced in dev-queue schema v22
    (GitHub #1511). Idempotent."""
    if "blocked_reason" not in task_raw:
        task_raw["blocked_reason"] = None


def _fill_hold_finalize_default(task_raw: dict[str, Any]) -> None:
    """Fill hold_finalize introduced in dev-queue schema v23
    (GitHub #1160, RFC 0011 A3). Idempotent."""
    if "hold_finalize" not in task_raw:
        task_raw["hold_finalize"] = None


def _fill_attention_digest_buffered_default(task_raw: dict[str, Any]) -> None:
    """Fill attention_digest_buffered_at introduced in dev-queue schema v24
    (GitHub #1162, RFC 0011 A6). Idempotent."""
    if "attention_digest_buffered_at" not in task_raw:
        task_raw["attention_digest_buffered_at"] = None


def _fill_salvage_no_sentinel_at_default(task_raw: dict[str, Any]) -> None:
    """Fill salvage_no_sentinel_at introduced in dev-queue schema v25
    (GitHub #1638). Idempotent."""
    if "salvage_no_sentinel_at" not in task_raw:
        task_raw["salvage_no_sentinel_at"] = None


def _fill_watched_prs_default(raw: dict[str, Any]) -> None:
    """Fill the top-level watched_prs list introduced in schema v15 (#1154).

    Store-level (not per-task), so this takes the raw store dict rather than a
    task dict and is called once outside migrate_dev_queue's per-task loop.
    Idempotent."""
    if "watched_prs" not in raw:
        raw["watched_prs"] = []


def migrate_dev_queue(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw dev_queue.json payload into a currently-valid shape."""
    tasks = raw.get("tasks")
    if isinstance(tasks, list):
        for task_raw in tasks:
            if isinstance(task_raw, dict):
                _fill_task_cost_default(task_raw)
                _fill_lane_default(task_raw)
                _fill_task_stage_default(task_raw)
                _fill_task_stage_base_ref_default(task_raw)
                _fill_disposition_default(task_raw)
                _fill_pr_url_default(task_raw)
                _fill_task_completed_at_default(task_raw)
                _fill_regress_attempts_default(task_raw)
                _fill_spawn_error_backoff_default(task_raw)
                _fill_pr_state_default(task_raw)
                _fill_signoff_default(task_raw)
                _fill_escalation_defaults(task_raw)
                _fill_false_park_recovery_backoff_default(task_raw)
                _fill_gate_recipe_failed_default(task_raw)
                _fill_escalate_merge_block_default(task_raw)
                _fill_request_reviewer_fired_default(task_raw)
                _fill_auto_fix_ci_fired_default(task_raw)
                _fill_address_review_fired_default(task_raw)
                _fill_last_blocked_result_default(task_raw)
                _fill_cross_repo_override_default(task_raw)
                _fill_stage_high_water_default(task_raw)
                _fill_blocked_reason_default(task_raw)
                _fill_hold_finalize_default(task_raw)
                _fill_attention_digest_buffered_default(task_raw)
                _fill_salvage_no_sentinel_at_default(task_raw)
    _fill_watched_prs_default(raw)
    raw["schema_version"] = DEV_QUEUE_SCHEMA_VERSION
    return raw
