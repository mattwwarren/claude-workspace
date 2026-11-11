"""Dev-queue task-revert backstops and task-release paths for reconcile.

Pure re-export: the names live in the concern modules below, listed in
dependency order (#2631 package split). Import sites keep using
``cw.reconcile.tasks``.

- ``_client_config``: per-client resolvers (leaf, no sibling imports).
- ``backstops``: revert backstops for RUNNING tasks (#421, #488, #637).
- ``terminal_siblings``: park tasks behind a terminal sibling row.
- ``merged_completion``: complete timed-out-merged tasks (#488, #1269).
- ``stale_gate``: release stale gated tasks (#1713, #1902, #1927).
"""

from cw.reconcile.tasks._client_config import (
    _client_cwd,
    _is_dangling_client,
    _resolve_task_policy,
)
from cw.reconcile.tasks.backstops import (
    _COMPLETION_ROUTING_GRACE_SECONDS,
    _revert_running_tasks_for_sessions,
    _revert_terminal_backstop,
    _sessions_past_completion_grace,
    backstop_targets,
    capture_backstop_dirty_checks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from cw.reconcile.tasks.merged_completion import (
    _collect_timed_out_merged_candidates,
    _emit_never_claimed_refusals,
    _filter_merged_candidates,
    _is_never_claimed,
    complete_timed_out_merged_tasks,
)
from cw.reconcile.tasks.stale_gate import (
    _PROPOSED_ACTION_VARIANT_A,
    _PROPOSED_ACTION_VARIANT_B,
    _STALE_GATE_CONSUMER,
    _VARIANT_A_BLOCKED_DISPOSITION,
    _VARIANT_A_MERGE_PENDING_DISPOSITION,
    _VARIANT_B_DISPOSITION,
    _closed_pr_numbers_by_client,
    _index_variant_a_candidates,
    _is_variant_a_gate_task,
    _is_variant_b_gate_task,
    _merged_pr_numbers_by_client,
    _pr_numbers_by_client_in_state,
    _release_variant_a,
    _release_variant_b,
    release_stale_gated_tasks,
)
from cw.reconcile.tasks.terminal_siblings import (
    _TERMINAL_SIBLING_STATUSES,
    _latest_terminal_at_index,
    park_terminal_sibling_tasks,
)

__all__ = [
    "_COMPLETION_ROUTING_GRACE_SECONDS",
    "_PROPOSED_ACTION_VARIANT_A",
    "_PROPOSED_ACTION_VARIANT_B",
    "_STALE_GATE_CONSUMER",
    "_TERMINAL_SIBLING_STATUSES",
    "_VARIANT_A_BLOCKED_DISPOSITION",
    "_VARIANT_A_MERGE_PENDING_DISPOSITION",
    "_VARIANT_B_DISPOSITION",
    "_client_cwd",
    "_closed_pr_numbers_by_client",
    "_collect_timed_out_merged_candidates",
    "_emit_never_claimed_refusals",
    "_filter_merged_candidates",
    "_index_variant_a_candidates",
    "_is_dangling_client",
    "_is_never_claimed",
    "_is_variant_a_gate_task",
    "_is_variant_b_gate_task",
    "_latest_terminal_at_index",
    "_merged_pr_numbers_by_client",
    "_pr_numbers_by_client_in_state",
    "_release_variant_a",
    "_release_variant_b",
    "_resolve_task_policy",
    "_revert_running_tasks_for_sessions",
    "_revert_terminal_backstop",
    "_sessions_past_completion_grace",
    "backstop_targets",
    "capture_backstop_dirty_checks",
    "complete_timed_out_merged_tasks",
    "park_terminal_sibling_tasks",
    "release_stale_gated_tasks",
    "revert_completed_silent_tasks",
    "revert_timed_out_tasks",
]
