"""Task claim + spawn primitives for the dispatch loop.

Part of the ``cw.dispatch`` package split (#1310): the atomic claim step, the
per-lane occupant/stat snapshots, and the worktree-provision + spawn path.

This package was split out of a single ``claim.py`` module (#2378); the import
surface (``from cw.dispatch.claim import X``) is preserved here via
re-exports. Internal cross-references use the direct submodule path, never
this package. A test that patches a name one of these submodules imported
from elsewhere must target the submodule that looks it up at call time
(``cw.dispatch.claim.spawn.create_worktree``), not this package. Submodules:

- ``events`` — per-task ``dispatch.tick`` / ``SESSION_NEEDS_ATTENTION``
  emitters shared by the claim screen and the spawn path.
- ``outcome`` — the ``_SpawnOutcome`` record (a leaf, so ``spawn`` can import
  the codex gate at module top without a cycle).
- ``claimed_row`` — locked load/re-find/mutate/save primitives for one
  claimed RUNNING row: revert, park, spawn-success stamp.
- ``codex_capability`` — TTL-cached codex CLI probe and pre-spawn gate
  (#1238).
- ``lane_stats`` — per-lane occupant lists and occupancy counts (ADR-0006).
- ``screening`` — candidate screening and the atomic claim transaction.
- ``spawn`` — worktree provisioning, the stale/occupied-worktree guards, the
  approved-plan bypass, and the executor spawn for one claimed task.
"""

from __future__ import annotations

from cw.dispatch.claim.claimed_row import (
    _SPAWN_ERROR_BACKOFF_CAP_SECONDS,
    _SPAWN_ERROR_BACKOFF_INITIAL_SECONDS,
    _find_running_row,
    _park_running_task_blocked_on_user,
    _revert_claimed_task_to_pending,
    _stamp_spawn_success,
)
from cw.dispatch.claim.codex_capability import (
    _CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS,
    _CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD,
    _CODEX_CAPABILITY_PROBE_TTL_SECONDS,
    _cached_codex_capability_diagnosis,
    _codex_capability_cache,
    _codex_capability_gate,
    _codex_capability_park_count,
    _reset_codex_capability_cache,
)
from cw.dispatch.claim.events import (
    _emit_attempt_cap_attention_event,
    _emit_attempt_cap_blocked_event,
    _emit_stale_dispatch_attention_event,
    _emit_stale_dispatch_blocked_event,
    _emit_worktree_occupied_skip_event,
)
from cw.dispatch.claim.lane_stats import (
    _lane_occupants_for_client,
    _lane_stats_for_client,
)
from cw.dispatch.claim.outcome import _SpawnOutcome
from cw.dispatch.claim.screening import (
    _CLAIM_BACKOFF,
    _CLAIM_CLAIMED,
    _CLAIM_SKIPPED,
    _claim_next_pending,
    _is_backstop_exempt,
    _is_fix_dispatch_held,
    _is_stale_pr_gated,
    _park_stale_pr_task,
    _screen_and_claim,
    resolve_occupied_ticket_ids,
)
from cw.dispatch.claim.spawn import (
    _OCCUPIED_DEFER_SECONDS,
    _apply_plan_bypass_if_available,
    _defer_genuinely_live_hook_conflict,
    _defer_occupied_claim,
    _handle_hook_context_conflict,
    _raise_if_stale_tree_occupied,
    _spawn_claimed_task,
)

__all__ = [
    "_CLAIM_BACKOFF",
    "_CLAIM_CLAIMED",
    "_CLAIM_SKIPPED",
    "_CODEX_CAPABILITY_GATE_TIMEOUT_SECONDS",
    "_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD",
    "_CODEX_CAPABILITY_PROBE_TTL_SECONDS",
    "_OCCUPIED_DEFER_SECONDS",
    "_SPAWN_ERROR_BACKOFF_CAP_SECONDS",
    "_SPAWN_ERROR_BACKOFF_INITIAL_SECONDS",
    "_SpawnOutcome",
    "_apply_plan_bypass_if_available",
    "_cached_codex_capability_diagnosis",
    "_claim_next_pending",
    "_codex_capability_cache",
    "_codex_capability_gate",
    "_codex_capability_park_count",
    "_defer_genuinely_live_hook_conflict",
    "_defer_occupied_claim",
    "_emit_attempt_cap_attention_event",
    "_emit_attempt_cap_blocked_event",
    "_emit_stale_dispatch_attention_event",
    "_emit_stale_dispatch_blocked_event",
    "_emit_worktree_occupied_skip_event",
    "_find_running_row",
    "_handle_hook_context_conflict",
    "_is_backstop_exempt",
    "_is_fix_dispatch_held",
    "_is_stale_pr_gated",
    "_lane_occupants_for_client",
    "_lane_stats_for_client",
    "_park_running_task_blocked_on_user",
    "_park_stale_pr_task",
    "_raise_if_stale_tree_occupied",
    "_reset_codex_capability_cache",
    "_revert_claimed_task_to_pending",
    "_screen_and_claim",
    "_spawn_claimed_task",
    "_stamp_spawn_success",
    "resolve_occupied_ticket_ids",
]
