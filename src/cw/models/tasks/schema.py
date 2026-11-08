"""``DEV_QUEUE_SCHEMA_VERSION`` and its history, plus the queue defaults.

A leaf of the ``cw.models.tasks`` package. A schema bump and its history
line land here; the field itself goes on ``TicketTask`` in ``ticket_task``.
"""

from __future__ import annotations

from typing import Literal

from cw.models.enums import Stage

# v3: added TicketTask.lane (GitHub #557).
# v4: added TicketTask.stage + stage_base_ref (GitHub #612).
# v5: added TicketTask.disposition, pr_url, completed_at (GitHub #310).
# v6: added TicketTask.regress_attempts (GitHub #770).
# v7: added TicketTask.spawn_error_count, next_eligible_at (GitHub #868).
# v8: added TicketTask.pr_state (GitHub #929).
# v9: added TicketTask.signoff (GitHub #990).
# v10: added TicketTask.escalation_parked_at/escalation_fired_at
#      (GitHub #1015, RFC 0008 capstone).
# v11: added TicketTask.false_park_recovery_count/
#      false_park_recovery_next_eligible_at (GitHub #1030).
# v12: added TicketTask.gate_recipe_failed_at (GitHub #1065, RFC 0009).
# v13: added TicketTask.gate_recipes + LaneConfig.gate_recipes (GitHub #1067,
#      RFC 0009 P4).
# v14: added TicketTask.escalate_merge_block_fired_at (GitHub #1099, RFC 0010
#      P4) — one-shot latch for the escalate_merge_block review recipe.
# v15: added DevQueueStore.watched_prs (GitHub #1154, RFC 0011 S2) — a
#      top-level list of externally-requested PRs the operator is watching,
#      distinct from the per-task queue.
# v16: added TicketTask.request_reviewer_fired_at (GitHub #1197) — one-shot
#      latch for the request_reviewer review recipe.
# v17: added TicketTask.auto_fix_ci_fired_at (GitHub #1205) — one-shot
#      latch for the auto_fix_ci review recipe.
# v18: added TicketTask.address_review_fired_at (GitHub #1206) — one-shot
#      latch for the address_review review recipe.
# v19: added TicketTask.last_blocked_result (GitHub #1266) — diagnostic-only
#      field originally populated by the _route_blocked_result_to_task
#      unrecognized-reason catch-all only; GitHub #2401 widened this to every
#      FAILED/abandoned landing in that function (the deterministic-parse and
#      validation_failed branches at their shared attempt cap, too). Lets an
#      operator distinguish "no sentinel yet" from "a rejected sentinel
#      landed this FAILED."
# v20: added TicketTask.cross_repo_override (GitHub #1198) — operator escape
#      hatch that bypasses the cross-repo dispatch guard for one row.
# v21: added TicketTask.stage_high_water (GitHub #1361) — furthest pipeline
#      stage reached across all attempts; monotonic.
# v22: added TicketTask.blocked_reason (GitHub #1511) — the blocker.reason off
#      a well-formed blocked/merge_gate_blocked AutoDevResult, stamped by
#      transition_task_status alongside disposition so it reaches `cw
#      dev-queue tasks` and the attention-event formatter.
# v23: added TicketTask.hold_finalize (GitHub #1160, RFC 0011 A3).
# v24: added TicketTask.attention_digest_buffered_at (GitHub #1162, RFC 0011
#      A6) — durable buffer-membership marker for the operator-channel
#      session.needs_attention digest coalescer (cw.cw_operator_events).
# v25: added TicketTask.salvage_no_sentinel_at (GitHub #1638) — timestamp of
#      the most recent salvage-without-sentinel park via reconcile/salvage.py's
#      LOW path (the "stopped without ever emitting a sentinel" marker).
#      Stamped by transition_task_status, not salvage.py directly (R2 seam).
#      Deliberately NOT cleared on requeue/unblock/cancel — a diagnostic-only
#      historical fact, mirroring last_blocked_result's no-clear-site
#      convention (#1266), not the escalation-latch convention: erasing it the
#      moment unblock_ticket reverts the row to PENDING would destroy the
#      exact evidence this field exists to preserve.
# v26: added TicketTask.hook_context_conflict_session_id (GitHub #1674) — the
#      session whose still-live cw-context.json made the last spawn attempt
#      raise HookContextConflictError. Stamped by the dispatch claim path,
#      read by concierge recipe 1 to refuse a requeue that cannot succeed.
# v27: added TicketTask.regressed_into_stage (GitHub #1794) — per-arrival
#      marker telling the impl-stage Pre-Stage Detector Guard that this stage
#      entry was reached via a deliberate backward move, distinct from the
#      cumulative, never-reset-on-advance regress_attempts.
# v28: added TicketTask.finalize_regress_branch_head (GitHub #1717) —
#      branch-head oracle stamped by _stage_regress only when regressing FROM
#      Stage.FINALIZE, consumed (read-and-cleared) once by dispatch/routing.py's
#      REVIEW-scoped gates to detect a finalize->impl->review round trip that
#      landed back at REVIEW with no new commit — the #1644/#1702/#1710 silent
#      repeat-park incident.
# v29: added TicketTask.pending_operator_comment (GitHub #1730) — per-arrival
#      marker telling the REVIEW stage that this entry followed a regress and
#      may carry an operator send-back to treat as a binding adjudication
#      input. Sibling of v27's regressed_into_stage, stamped at the same
#      _stage_regress seam as v28's finalize_regress_branch_head but under an
#      independent precondition, and cleared only at a REVIEW-stage spawn.
# v30: added TicketTask.stale_gate_detected_at/blocked_on_pr (GitHub #1713) —
#      detection latch + cross-reference field for
#      cw.reconcile.tasks.release_stale_gated_tasks, which re-validates a
#      BLOCKED_ON_USER row's gate condition (own PR merged, or the PR it is
#      blocked behind merged) against fresh pr.merged events/hydrated
#      pr_state, since dev-queue rows otherwise never observe that clearing.
#      stale_gate_detected_at follows the escalation_parked_at/
#      gate_recipe_failed_at unconditional-clear-on-transition convention;
#      blocked_on_pr is a bare int PR number (no owner/repo qualifier — a
#      dev-queue client is bound to exactly one repo).
# v31: added TicketTask.finding_dispositions (GitHub #1838) — the durable
#      cross-round adjudication ledger, keyed by a stringified
#      cw.review_debt.fingerprint_v1. Stamped by
#      cw.codex_background._sync_finding_dispositions_to_running_task once a
#      review pass has merged the ticket thread's REVIEW-FINDING-DISPOSITIONS
#      marker into it. Deliberately has NO clear site: it is durable memory,
#      not a per-arrival marker like v27/v29 — forgetting a settled
#      adjudication on requeue is the exact failure #1838 exists to remove.
# v32: added TicketTask.unproductive_attempts (GitHub #1750) — a second,
#      narrower attempt counter beside the raw `attempts` claim counter. Only
#      claims that exit RUNNING with no evidence of progress (no commits, no
#      review findings, no consumed operator resolution) are charged; the
#      global dispatch attempt ceiling reads this counter instead of
#      `attempts`, so a ticket making real forward progress can no longer be
#      parked at `attempt_cap_blocked` for being busy (#1727) while the
#      crashloop the ceiling exists to catch (#1653) is still bounded.
#      Incremented at exactly one seam (transition_task_status), mirroring
#      regress_attempts's v6 single-seam precedent.
# v33: added TicketTask.ever_spawned (GitHub #1631) — durable "a session was
#      genuinely spawned for this row at least once" marker. Exists because
#      the two attempt counters move asymmetrically on the UsageLimitError
#      revert path (attempts increments, spawn_error_count deliberately does
#      not — #868 fleet-wide backoff), which left a usage-limit-only history
#      structurally identical on the task record to a task that ran, shipped
#      and timed out inside its first stage, and therefore silently
#      auto-completable by reconcile's timed-out-merged backstop. Stamped
#      True at exactly one seam (dispatch/claim.py's spawn-success block),
#      seeded False at exactly one seam (dev_queue_add, the only place with
#      positive proof a row has never spawned), and — like v31's
#      finding_dispositions — deliberately has NO clear site: it is a
#      permanent historical fact, not a per-arrival marker. Model default and
#      migration fill are both True (fail-open): state we cannot reconstruct
#      must not retroactively refuse a legitimate completion.
# v34: added TicketTask.pending_fix_dispatch/fix_dispatch_session_id
#      (GitHub #2017) — the durable, non-worktree handoff surface the review
#      fix loop's asynchronous dispatch runs on. See PendingFixDispatch below.
# v35: added TicketTask.plan_approved_at (GitHub #968/#1906 follow-up) — the
#      tracker-neutral record that the PLAN-stage approval gate was released
#      via `cw dev-queue approve`. Stamped by _approve_ticket_locked; threaded
#      into the worker's cw-context.json queue_metadata at spawn so the plan
#      stage's Large-scope carve-out can accept it as approval evidence on a
#      client whose tracker (e.g. Linear) the GitHub-only `--post-marker`
#      comment can never reach. One clear site: _stage_regress into
#      Stage.PLAN (a deliberate re-plan revokes it, #2102). Otherwise durable,
#      mirroring the `<!-- auto-dev-plan-approved -->` GitHub comment it
#      stands in for, which is never revoked — #2102 tracks binding both to
#      the draft that was actually approved.
# v36: added TicketTask.plan_approved_fingerprint (GitHub #2102) — the SHA-256
#      content fingerprint of the draft the v35 approval was actually given
#      for. Without it `plan_approved_at` is a durable no-op check: it proves
#      an approval happened, never which text the operator read, so a resumed
#      draft edited after approval auto-skips Checkpoint 1's Large-scope
#      carve-out unchallenged. Stamped and cleared at exactly the seams v35
#      already uses (_stamp_plan_approval / _stage_regress into Stage.PLAN), so
#      the pair can never diverge into "approved, but for nothing".
# v37: advisory_note (#1762) — added TicketTask.advisory_note, the per-tick
#      operator advisory rendered in `cw dev-queue tasks`'s REASON column for a
#      non-terminal row (blocked_reason, stamped only on terminal transitions,
#      cannot describe an ordinary RUNNING row). Written by the reconcile
#      session-id-mismatch sweep, cleared unconditionally by
#      transition_task_status. Backfilled to None on every pre-v37 row.
# v38: added TicketTask.park_on_abandoned_exit + LaneConfig.park_on_abandoned_exit
#      (GitHub #2135) — the per-ticket / per-lane tiers of the Stop-hook
#      abandoned-exit park's 3-tier resolution, mirroring v13's gate_recipes
#      pair. A ``dict[str, bool] | None`` whose absence is indistinguishable
#      from the ``None`` default, so no migration filler is needed (same as
#      v13).
# v39: added TicketTask.codex_orphan_session_id/
#      codex_orphan_rescan_next_eligible_at (GitHub #2307) — the durable link
#      from a live-writer/inconclusive-scan codex-orphan park back to the
#      Session the boot pass left ACTIVE (the row's own session_id is cleared
#      on park), plus the fixed-interval backoff bounding how often
#      cw.reconcile.codex_reparks re-scans the process table for it. Both
#      describe the current park episode, so both follow the v30
#      unconditional-clear-on-every-transition convention. Backfilled to None
#      on every pre-v39 row: an unlinked park is exactly today's behavior.
# v40: added TicketTask.scope_drift_approved_extra_files +
#      scope_drift_approved_head (GitHub #2337) — the operator's
#      `cw dev-queue approve --scope-drift` grant for a row parked with
#      blocked_reason plan_scope_drift, bound to the branch head it was given
#      for (the #2102 plan_approved_fingerprint shape). Both ``| None`` with a
#      ``None`` default, so no migration filler is needed (same as v13/v38).
# v41: added TicketTask.usage_limit_act (GitHub #2324) — the write-ahead
#      intent of reconcile's mid-turn usage-limit act. Written once, as the
#      act's only deciding write; every later tick resumes the act from it
#      until the row's final transition clears it. Backfilled to None on every
#      pre-v41 row: no act was in flight under the older schema. Renumbered
#      from v40 to v41 during a main-sync merge: #2337 independently landed
#      on main claiming v40 for a different field pair first.
# v42: added TicketTask.must_fix_override (GitHub #2205) — the operator's
#      `cw dev-queue approve --override-must-fix` record for a codex MUST_FIX
#      park, bound to the verdict's reviewed_sha and finding fingerprints.
#      ``| None`` with a ``None`` default, so no migration filler is needed
#      (same as v13/v38/v40).
# v43: added TicketTask.finalize_regress_merge_conflict_detected (GitHub
#      #2421) — whether MERGE_HEAD was present in the ticket's worktree when a
#      FINALIZE-origin regress fired. Backfilled to False on every pre-v43
#      row: no regress under the older schema measured it.
# v44: added TicketTask.claimed_at (GitHub #2591) — the instant the row was
#      last claimed to RUNNING, which reconcile's unbound-row adoption checks
#      a session's started_at against. ``| None`` with a ``None`` default, so
#      no migration filler is needed (same as v13/v38/v40/v42); a pre-v44 row
#      is a claim of unknown age.
# v45: added TicketTask.fix_dispatch_launched_worker (GitHub #2590) — the
#      tombstone of a fix-loop worker that launched but was never recorded,
#      which holds the row until a readable daemon roster no longer lists the
#      worker's surface. ``| None`` with a ``None`` default, so no migration
#      filler is needed (same as v13/v38/v40/v42/v44); a pre-v45 row recorded
#      no unconfirmed worker.
# v46: added TicketTask.operator_resolutions (GitHub #2702) — the append-only
#      ledger `cw dev-queue resolve` writes. ``Field(default_factory=list)``,
#      so no migration filler is needed; a pre-v46 row recorded no
#      resolutions.
# v47: added TicketTask.gate_block (GitHub #2717) — the consecutive
#      gate-block backoff record. ``| None`` with a ``None`` default, so no
#      migration filler is needed; a pre-v47 row recorded no gate-block run.
# v48: added TicketTask.review_approved_at / review_approver_kind /
#      review_approved_sha (GitHub #2767) — the REVIEW-stage approval record
#      finalize Step 4.0 requires before a large ticket ships. ``| None`` with
#      a ``None`` default, so no migration filler is needed; a pre-v48 row
#      recorded no review approval.
# v49: added TicketTask.branch (GitHub #2948) — the row's explicit git
#      branch, read through cw.models.resolve_feature_branch. ``| None`` with
#      a ``None`` default, so no migration filler is needed; a pre-v49 row
#      carries no explicit branch and resolves to <prefix>/<ticket_id>.
# v50: added TicketTask.adopted_pr_url / adopted_head_sha / adopted_repin_due
#      (GitHub #2943, RFC 0016) — the adopted human PR, its pinned head SHA,
#      and the transient session-end re-pin marker. The first two are
#      ``| None`` with a ``None`` default and the third a ``False`` default,
#      so no migration filler is needed; a pre-v50 row adopted no PR.
# v51: added TicketTask.review_feedback — an optional, bounded review report
#      tied to a repo-qualified PR and exact reviewed head. ``| None`` with a
#      ``None`` default, so legacy rows need no migration filler.
# v52: added TicketTask.operator_constraints (GitHub #2964) — the operator's
#      append-only `cw review constrain` entries, read by the codex review
#      prompt and fix loop from the row. A ``Field(default_factory=list)``
#      list, so a pre-v52 row loads with ``[]`` and needs no migration filler.
# v53: added TicketTask.shipped_red / red_pr_recovery_sha (GitHub #2945,
#      RFC 0016 B2) — the per-episode latch of the red-shipped-PR sweep and
#      the marker for feedback attached by `cw dev-queue recover-red-pr`.
#      Both are ``| None`` with a ``None`` default, so no migration filler is
#      needed; a pre-v53 row recorded no red-PR episode and no recovery.
# v54: auto_merge_rearm_owed_at / auto_merge_rearm_attention_at (GitHub
#      #2993) record that red-shipped recovery disarmed auto-merge and whether
#      the one follow-up attention event was emitted; auto_merge_rearm_pr_url
#      keeps the disarmed PR identity when cancellation clears pr_url, and
#      auto_merge_rearm_pr_state throttles/readouts that PR's hydration. Nullable
#      fields are filled by migration for old rows.
DEV_QUEUE_SCHEMA_VERSION = 54
# Provenance of a v48 review approval: the human `approve` or the gate recipe.
ReviewApproverKind = Literal["operator", "recipe"]
DEFAULT_LANE: str = "default"
DEFAULT_STAGE: Stage = Stage.PLAN
