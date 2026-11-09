"""The ``.claude/cw-context.json`` schema version and its history.

Extracted from ``cw.spawn`` (#3040) so the history can grow without pushing
that module past the size ceiling. ``cw.spawn`` re-exports
``CW_CONTEXT_SCHEMA_VERSION`` and stamps it into every context it writes.
Imports nothing, so it carries no import-cycle risk.
"""

from __future__ import annotations

# Schema version for cw-context.json. Increment when the shape changes so
# workers can detect whether they are reading a context written by an older cw.
# v2: added `workspace_path` (#766 — forbidden main-checkout path for git guard).
# v3: added `queue_metadata.regressed_into_stage` (#1794 — per-arrival signal to
#     the impl-stage Pre-Stage Detector Guard that this IMPL entry was reached
#     via a deliberate backward stage move, not a fresh dispatch or an ordinary
#     forward advance).
# v4: added `queue_metadata.pending_operator_comment` (#1730 — per-arrival
#     signal to the REVIEW stage that this entry followed a regress and may
#     carry an operator send-back comment to treat as a binding adjudication
#     input rather than background context).
# v5: added `agent_spawn_stamp` (#1646 — unresolved-subagent-spawn counter
#     maintained by the `cw agent-spawn-pre` / `cw agent-spawn-post` hook pair
#     and read by the phantom sweep, so a worker that died with a sub-agent
#     spawn still in flight parks under its own disposition instead of the
#     generic phantom_surface).
# v6: added `lane` (#1946 — the dispatch lane this worker's task sits in, so
#     the `cw guard-busy-wait` PreToolUse hook can resolve its per-lane config
#     override from inside the hook subprocess. The hook has no cw session or
#     queue row in hand, only this file; without the key every worker would
#     resolve the global default and the per-lane knob would be unreachable.
#     Null for USER-origin sessions, which have no lane.)
# v7: added `queue_metadata.plan_approved_at` (dev-queue schema v35 — the
#     tracker-neutral record that `cw dev-queue approve` released this row's
#     PLAN-stage approval gate, as an ISO-8601 timestamp or null. Read by
#     auto-dev-plan.md's Checkpoint 1 as operator approval evidence, so a
#     Linear-tracked ticket — whose tracker the GitHub-only `--post-marker`
#     comment never reaches — stops re-parking at plan_pending_approval on
#     every re-dispatch.)
# v8: added `queue_metadata.plan_approved_fingerprint` (dev-queue schema v36 —
#     GitHub #2102 — the SHA-256 fingerprint of the draft the v7 approval was
#     given for, as a hex string or null. Checkpoint 1 compares it against the
#     draft it is about to auto-skip; without it, v7's timestamp proves only
#     that some approval happened, so a draft edited after approval resumed
#     straight past the Large-scope carve-out.)
# v9: added `queue_metadata.scope_drift_approved_extra_files` and
#     `queue_metadata.scope_drift_approved_head` (dev-queue schema v40 —
#     GitHub #2337 — the operator's `cw dev-queue approve --scope-drift` grant,
#     as a path list and a commit SHA, or nulls. Step 2.5 gate 2 passes the
#     paths to check_plan_scope_conformance.py as an allowlist while the SHA is
#     still an ancestor of origin/<branch>.)
# v10: added `queue_metadata.must_fix_override` (dev-queue schema v42 —
#     GitHub #2205 — the operator's `cw dev-queue approve --override-must-fix`
#     record, as a JSON object or null. FINALIZE's check_must_fix_override.py
#     compares it against the worktree's live review verdict; it rides here
#     because the row's blocked_reason is cleared by the requeue to FINALIZE.)
# v11: added `merge_gate_ignore_paths` (#2431 — the client's
#     ClientConfig.merge_gate_ignore_paths, as a string list, [] when unset.
#     Read by auto-dev-finalize.md Step 4a's headless bash fence via
#     `jq -r '.merge_gate_ignore_paths // [] | .[]'` and passed to
#     check_merge_gate_overlap.py as repeated `--ignore-path` arguments — the
#     fence has no other route to a ClientConfig field from inside agent bash.)
# v12: added `queue_metadata.operator_resolutions` (dev-queue schema v46 —
#     GitHub #2702 — the row's append-only `[{text, recorded_at, actor,
#     round}]` list `cw dev-queue resolve` writes, [] when empty. Read by
#     auto-dev-plan.md Step 1b as binding resolutions. The block is built by
#     cw.context_queue_metadata.build_queue_metadata.)
# v13: added `queue_metadata.operator_resolutions_source` (GitHub #2728 — the
#     content-bound `row:<N>:<sha256>` identity of the list above, `N` the
#     highest round and the digest covering every entry text in round order;
#     null when the list is empty. cw computes it so the plan stage never
#     hashes; auto-dev-plan-appendix.md Step 1a.0b reads it as the row's
#     resolutions source identity.)
# v14: added `queue_metadata.review_approval` (dev-queue schema v48 — GitHub
#     #2767 — the row's REVIEW-stage approval record `{approved_at,
#     approver_kind, reviewed_sha}`, or null. auto-dev-finalize.md Step 4.0
#     ships a large ticket only while `reviewed_sha` is an ancestor of HEAD
#     with nothing but merges of `world_state_snapshot.origin_main_branch`
#     after it; a context below 14 is no approval.)
# v15: added `review` (GitHub #2787 — the client's ReviewConfig as
#     `{convergence, max_cycles, confirm_backend}`; `max_cycles` is the clean
#     mode ceiling, null in blocking mode. Read by `cw review converge config`.)
#     The `review` object also carries `default_merge_tolerance` (GitHub #2815,
#     additive, no version bump: an absent key means the tolerant default, so
#     no reader must tell an old context from a null value).
#     It carried `default_merge_delta_confirm` (GitHub #2918) until #2979
#     retired the delta confirm round; removing the additive key needs no
#     version bump, and no reader looks for it.
# v16: added `merge_gate_enabled` (GitHub #2851 — the client's
#     ClientConfig.merge_gate_enabled, a bool, true when unset. Read by
#     auto-dev-finalize.md Step 4a's headless bash fence via
#     `jq -r '.merge_gate_enabled == false'` (not `// true`, which jq also
#     applies to a literal false); only a literal `false` skips the whole gate.
#     A context below 16, or one without the key, means enabled.)
# v17: added `queue_metadata.branch` (dev-queue schema v49 — GitHub #2948 —
#     the row's recorded TicketTask.branch, or null for a normal row. Every
#     auto-dev stage doc's "Branch resolution" paragraph defines <branch-name>
#     as `jq -r '.queue_metadata.branch // empty'`, falling back to
#     <branch-prefix>/<ticket-id>; finalize Step 4a's fence reads it for the
#     own-ticket self-exclusion. A context below 17 reads as null.)
# v18: added `queue_metadata.review_feedback` (dev-queue schema v51) — the
#     bounded review report bound to its repo-qualified PR and reviewed head.
#     Null when absent; auto-dev-impl.md verifies the live PR, head and worktree
#     before the implementation worker considers it.
# v19: added `review.engine` (#3039, RFC 0017 I1) as `{skill, guarded_paths}`,
#     or null. Read by `cw review converge config` and the `cw review engine`
#     commands. A context below 19 reads as no engine (the built-in roster).
# v20: added `queue_metadata.review_attestation` (dev-queue schema v56 —
#     GitHub #3040, RFC 0017 II1 — the row's head-pinned review sign-off
#     attestation, or null; the key is always present). Verified by
#     `cw review attestation check`, which treats it as a claim. A context
#     below 20 reads as no attestation.
CW_CONTEXT_SCHEMA_VERSION = 20
