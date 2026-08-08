# Headless `/auto-dev` Contract

Reference for the structured output and behavioral contract that the `/auto-dev` skill emits when invoked with `--headless`. `cw` is the primary consumer; this document is the cross-repo source of truth so the producer (skill) and consumer (orchestrator) stay aligned.

| Concern | Repo | Path |
|---|---|---|
| Producer (skill) | `mattwwarren/global-claude` | `commands/auto-dev.md` |
| Consumer (orchestrator) | `mattwwarren/claude-workspace` | issues #56–#59 (P1.A–D arc) |
| This spec | `mattwwarren/claude-workspace` | `docs/headless-contract.md` |

When the skill changes, this doc and the cw parser follow. When this doc changes, the skill MUST already match — don't spec aspirational fields. The `commands/auto-dev.md` source is canonical for behavior; this doc reformulates it for parser implementers.

---

## 1. Mode Activation

Pass `--headless` anywhere in the argument list:

```
/auto-dev GEN-1234 --headless
/auto-dev --cycle current --scope-limit small --headless
```

Headless mode replaces every interactive `AskUserQuestion` call with the deterministic action defined in §2. It is independent of the input forms (Linear ID, filters, free text), but **batch mode + `--headless` is undefined behavior** — the consumer MUST NOT combine batch filters with `--headless` until that gap is specified.

**Out of scope in headless mode:**
- Batch mode (multi-ticket selection)
- PR Hygiene Sweep (Steps H1, H2)
- Stage 5b feedback fix agent
- `--resume` / restart flag (tracked separately; see §7)

---

## 2. Gate-Collapse Table

All interactive gates in the pipeline collapse to one of: AUTO-SKIP, AUTO-CONTINUE, AUTO-CREATE, AUTO-APPROVE, or EXIT with a status code.

| Stage / gate | Headless behavior |
|---|---|
| S1 plan, plan in Linear | AUTO-SKIP |
| S1 plan, no Linear plan, small | Generate → AUTO-APPROVE |
| S1 plan, no Linear plan, large | Generate → EXIT `plan_pending_approval` (post to Linear, no branch) |
| S1 scope-limit hit | EXIT `scope_exceeded` |
| S1 forbidden-area hit | EXIT `forbidden_area` |
| S2 impl checkpoint (any scope) | AUTO-CONTINUE — never gate |
| S2 BLOCK or 2x failure | EXIT `blocked` with `blocker.reason: "impl_failed"` |
| S3 review (any scope) | Always run reviewers |
| S3 MUST_FIX (any scope) | Run fix loop; expected 2 cycles, hard-cap at 5 |
| S3 review clean / SHOULD_FIX, small | AUTO-CONTINUE → S4 |
| S3 review clean / SHOULD_FIX, large | EXIT `review_pending_approval` (post-fix-loop diff, branch pushed, no PR) |
| S3 MUST_FIX persists after 5 cycles | EXIT `blocked` with `blocker.reason: "review_blocked"` |
| S3 non-deferrable plan-deviation finding survives fix loop or judged beyond its scope | EXIT `blocked` with `blocker.reason: "plan_deviation"` (routes to BLOCKED_ON_USER; not finalize) |
| S3 fix-loop cycle 3+ OR scope growth at any cycle | Append to `friction_highlights`, set `health.fix_loop_escalated: true`, continue |
| Any other agent BLOCK (Plan / prep-pr / etc.) | EXIT `blocked` with `blocker.reason: "agent_block"` |
| `/prep-pr` Step-7 "Ship anyway" interactive gate | EXIT `blocked` with `blocker.reason: "agent_block"` (collapses under the "Any other agent BLOCK" row above — the `--headless` flag must reach `/prep-pr` for it to fire; see `auto-dev-finalize.md` Step 4c) |
| Project `ship-it.md` interactive steps (e.g. tag-confirmation prompts) | EXIT `blocked` with `blocker.reason: "agent_block"` (collapses under the "Any other agent BLOCK" row above — never silently skipped) |
| S4a merge gate (small only — large already exited) | EXIT `merge_gate_blocked` if prior pipeline PR open |
| S4b PR creation, small | AUTO-CREATE with auto-merge |
| S4c/S4d post-arm auto-merge verification (#1140) | `gh pr merge --auto` can report success while `autoMergeRequest` reads back null; both Step 4c's main-session re-verification and Step 4d's reuse-path "Enable auto-merge" sub-step re-verify via `prep_pr_finalize.py verify --require-automerge` and EXIT `blocked` with `blocker.reason: "automerge_not_armed"` on failure |
| S5 CI wait | AUTO-SKIP — return immediately after auto-merge enabled. CI watching = orchestrator concern |
| Trailing /schedule asks | suppress |

**Philosophy:** the human only sees the diff after the machine has done all deterministic cleanup it can. Two human gates remain — both large-scope only:
- Plan approval (before work) → `plan_pending_approval`
- Review approval (after work) → `review_pending_approval`

Everything else runs to completion or exits with a structured error.

---

## 3. Structured Output

After all pipeline logic completes, the skill emits a sentinel-delimited JSON block as the **final lines of stdout**. Narrative friction reports remain above (still useful for tmux scrollback / post-mortem); this block is the parsing contract.

### 3.1 Sentinels

```
<<<AUTO_DEV_RESULT
{ ... }
AUTO_DEV_RESULT>>>
```

Parsers should locate the LAST occurrence of `<<<AUTO_DEV_RESULT\n` in stdout and read JSON until the matching `\nAUTO_DEV_RESULT>>>` line. Anything between is valid JSON. Anything outside is narrative.

The skill emits **exactly one** sentinel block per invocation. If the parser finds multiple complete blocks in one invocation's stdout, treat it as a skill bug per §6 (6). The "LAST occurrence" rule is purely defensive — for the case where narrative text above the block happens to contain the literal sentinel string.

**Interactive mode:** this block is NOT emitted. Absence of sentinels in stdout is itself a signal that the run was not headless (or the skill failed before reaching the emit step — see §6).

### 3.2 Schema

```json
{
  "schema_version": 4,
  "ticket_id": "GEN-1234",
  "status": "shipped",
  "stage_reached": "stage5_post_create",
  "scope": {
    "tier": "small",
    "files": 3,
    "lines_estimate": 42,
    "lines_actual": 47,
    "forbidden_touched": false
  },
  "plan_source": "linear_existing",
  "branch": "dev/gen-1234-fix-login",
  "worktree_path": "/home/matthew/.cw/wt/abc/auto-dev-1234",
  "fork_point_sha": "abc1234",
  "commits": ["sha1", "sha2"],
  "pr": {
    "number": 42,
    "url": "https://github.com/.../pull/42",
    "auto_merge": true,
    "base": "main"
  },
  "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
  "health": {
    "lowest_agent_confidence": "MEDIUM",
    "any_incomplete_risk": false,
    "shortcuts": [],
    "recommendation": "PROCEED",
    "downgrade_applied": false,
    "fix_loop_escalated": false
  },
  "friction_highlights": [],
  "blocker": null,
  "next_actions": ["wait_for_ci"]
}
```

### 3.3 Field Notes

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | Currently `5` (legacy `1`, `2`, `3`, `4` accepted during the rollout window). Bump rules in §8. |
| `ticket_id` | string | Linear ID, or synthetic for free-text invocations. |
| `status` | string enum | See §4. Closed set; parsers MUST treat unknown values as §6 (5) errors. |
| `stage_reached` | string enum | Pipeline-stage marker. Closed set: `stage1_pre_flight`, `stage1_plan`, `stage2_impl`, `stage3_review`, `stage4a_merge_gate`, `stage4b_pr_create`, `stage5_post_create`. Pre-flight exits (e.g. already-satisfied tickets) use `stage1_pre_flight`. Producer and parser must keep this list in lockstep — adding a stage is a `schema_version` bump (see §8). |
| `scope.tier` | `"small"` \| `"large"` \| null | Per the Guard Matrix in `commands/auto-dev.md`. `null` is accepted only on pre-impl exits (`stage_reached` of `stage1_plan` or `stage1_pre_flight`); required non-null at every later stage (#416). |
| `scope.files` | int | File count touched (or planned, if exited pre-impl). |
| `scope.lines_estimate` | int | Plan-time line estimate. |
| `scope.lines_actual` | int \| null | Actual lines touched. MUST be `null` on pre-impl exits (`stage1_plan` / `stage1_pre_flight`) and non-null at every later stage. |
| `scope.forbidden_touched` | bool | Whether any `--forbidden` area was touched. |
| `plan_source` | `"linear_existing"` \| `"github_issue_existing"` \| `"generated"` \| `"free_text"` \| `"none"` | How the plan was sourced. `"linear_existing"` and `"github_issue_existing"` are equivalent (the latter is the post-Linear, GitHub-Issues-era label — producers should emit whichever matches their tracker). `"none"` is used for pre-flight exits where no plan was produced. |
| `branch` | string \| null | The **feature branch** where the impl agent pushed (e.g. `dev/<ticket-id>`). `null` if exited before branch creation (e.g. `plan_pending_approval`). Distinct from the cw session branch (`auto-dev/<ticket>`) that hosts the orchestrating skill run. |
| `worktree_path` | string \| null | Absolute path of the **cw session worktree** (e.g. `~/.cw/wt/<hash>/auto-dev-<ticket>`). This is the worktree that cw creates on the session branch for the skill to run in — not the impl feature branch. `null` if no worktree was created. |
| `fork_point_sha` | string \| null | Base commit at branch creation. |
| `commits` | string[] | Commit SHAs created during this run. |
| `pr` | object \| null | Non-null **iff** `status` is `shipped` or `merge_pending` (#899). All other statuses — including `review_pending_approval` (whether reached via the large-scope path or the §5.1 downgrade), `merge_gate_blocked`, `plan_pending_approval`, `stage_complete`, the rejects, and `blocked` — leave `pr` as `null`. `branch` may still be non-null on these (see `branch` row). |
| `pr_created` | object \| null | **Phase D** — pre-merge PR snapshot emitted before auto-merge is triggered (§3.4). Optional; absent on payloads from older producers. When present, expected only on `status=shipped` runs. |
| `review.must_fix_initial` | int | MUST_FIX count from first review pass. |
| `review.should_fix` | int | SHOULD_FIX count carried out of the loop. |
| `review.fix_cycles_used` | int | 0 when first pass was clean. |
| `review.deferred` | int | Count of findings deferred to .cw/deferred-findings.md; 0 or absent on pre-Stage-3 exits (default). |
| `review.agents_run` | int | **v5** (#1237) — count of reviewer agents that actually produced a document. Reconciled against the executor-neutral review-verdict's `review.agents_run` (`len(documents)`) — this deliberately EXCLUDES every entry in the separate `verdict.agents_run` audit list that corresponds to a failed or skipped reviewer (timeout, error, unparseable output, budget-exhausted skip): a role that never produced usable findings must not inflate the count the clean-review gate treats as a required-non-zero signal (standing binding decision, #1236). Optional field; defaults to `0` on payloads from producers that predate v5. No longer purely advisory as of #1194: the opt-in `auto_approve_clean_review` gate recipe (RFC 0009, `cw.reconcile.gate_recipes`) requires `agents_run > 0` as part of its clean-review predicate — see Note (A9, §8). |
| `review.had_real_commit` | bool \| null | **#1723** — when non-null, true iff at least one fix cycle in the run actually committed a change (OR'd across cycles); distinguishes a fix loop that converged (no MUST_FIX survivors) because a real fix landed from one that converged purely because every cycle's fix invocation was a tolerated no-op. Purely advisory — prose-only consumption in `render_verdict_comment`'s non-blocking headline. Optional field; defaults to `null` on payloads from producers that predate this field, representing an unknown commit outcome. No `schema_version` bump required — see Note (A11, §8). |
| `health` | object | See §5. |
| `friction_highlights` | string[] | Surfaced highlights from agent friction reports. |
| `blocker` | object \| null | See §4.2. Required non-null when `status = "blocked"`. Exception (#777): `merge_gate_blocked` MAY also carry a non-null blocker to surface the gate reason (e.g. `prior_pipeline_pr_open`); `blocker` must be `null` for every other status. |
| `next_actions` | string[] | Advisory list cw can act on without prose-parsing. See §4.3. |
| `cost_usd` | number \| null | Total USD cost of the run (#124). Optional — producers that don't track cost omit it; consumers treat `null` as "cost unknown". Must be non-negative when present. |

**Note (A8, #1130):** `commits`, `friction_highlights`, and `next_actions` array items must be non-empty, non-whitespace strings; blank items are dropped at the parse boundary. Unlike A6/A7, no placeholder is injected when filtering leaves the array empty — none of these fields is gated behind a status-specific "must be non-empty" invariant (contrast `ambiguities`/`premises`, §4.4), so an empty result here is simply a valid empty list.

### 3.4 `pr_created` — Phase D pre-merge PR snapshot (issue #174)

**Gap addressed:** Small-scope tickets reach S4 (PR creation) and immediately enable auto-merge. The sentinel block previously landed after the merge completes, so the orchestrator had no window to observe the PR number or CI state at PR-creation time — only the merged state in `pr`.

**`pr_created`** captures the PR state *before* auto-merge is triggered, giving the orchestrator a hook to attach CI watchers or make merge decisions:

```json
"pr_created": {
  "number": 171,
  "url": "https://github.com/mattwwarren/claude-workspace/pull/171",
  "ci_status_at_creation": "pending",
  "auto_merge_enabled": true
}
```

| Field | Type | Notes |
|---|---|---|
| `number` | int | PR number on the hosting platform. |
| `url` | string | Full URL to the PR. |
| `ci_status_at_creation` | string | CI state at the moment the PR was opened — before any merge. Open-ish enum; observed values: `"pending"`, `"passing"`, `"failing"`. Consumers MUST treat unknown values as opaque strings and surface verbatim. |
| `auto_merge_enabled` | bool | Whether the skill successfully enabled auto-merge on the PR. |

**Advisory optional field.** Absent on payloads from producers that predate Phase D. No schema version bump required (§8: "purely advisory optional field"). When present, expected only for `status=shipped` runs.

---

## 4. Status Enum

The `status` field is a closed set. Consumers MUST treat unknown statuses as a parse error (skill version drift) and surface verbatim to the user.

### 4.1 Statuses

| Status | Meaning |
|---|---|
| `shipped` | PR created with auto-merge enabled; CI wait skipped. |
| `stage_complete` | Staged pipeline (cw dev-queue stages, RFC 0005 B2, #699) — one stage (HARDEN/PLAN/IMPL/REVIEW) finished cleanly. **Not a terminal outcome**: cw auto-advances the ticket to the next stage. PR-less by design — IMPL pushes a branch but FINALIZE creates the PR, so `pr` is `null` (`branch` may be non-null). Never a salvage-terminal status: a crashed worker whose last sentinel was `stage_complete` is routed through the stage-advance path, not dispositioned. Accepted under all supported schema versions (rollout exception, same treatment as the v4 statuses) until the producer bumps its emitted `schema_version`. |
| `plan_pending_approval` | Large scope — plan generated and posted to Linear; no branch created; awaiting human approval. |
| `review_pending_approval` | Large scope — fix loop complete, branch pushed, no PR; awaiting human review approval. |
| `merge_gate_blocked` | Small scope — prior pipeline PR still open; cannot create next PR until gate clears. |
| `merge_pending` | PR created but the CI/merge gate hasn't cleared yet (#899). **Not a failure** — cw does not re-dispatch the ticket; the operator just monitors the PR. `pr` MUST be non-null (the only status besides `shipped` that carries one); `blocker` and `next_actions` are empty. Note the parse-boundary coercion: a `blocked` sentinel arriving with a non-null `pr` is auto-coerced to `merge_pending` to preserve the PR URL (see "Parse-boundary coercions", §6). Accepted under all supported schema versions (rollout exception). |
| `scope_exceeded` | `--scope-limit small` rejected a Large ticket before impl started. |
| `forbidden_area` | `--forbidden` constraint matched a planned file; ticket rejected before impl started. |
| `blocked` | Unrecoverable error mid-pipeline; see `blocker` field for details. |
| `no_op` | Pre-flight detected the ticket already satisfied (or otherwise not work-bearing); no plan, no branch, no PR. Introduced at `schema_version=2`. Rationale: terse, neutral wording — does not presume the cause (already-merged dupe, invalid ticket, code already covers it). The actor (skill / cw) decides what to do via `next_actions` (typically `close_issue_as_completed`). Emitted at `schema_version=3` with `stage_reached='stage1_pre_flight'` and `plan_source='none'`. (During the rollout window, parsers also accept this shape under `schema_version=2`.) |
| `ambiguities_pending_resolution` | Planning halted because the ambiguity scan surfaced clarifying questions that exceed the auto-resolve threshold; no branch created. The `ambiguities` array is non-empty. Introduced at `schema_version=4`. |
| `premises_pending_verification` | Planning halted because the Plan Soundness Reviewer flagged unverified premises; no branch created. The `premises` array is non-empty. Promoted from §4.4 interim state at `schema_version=4`. (A sentinel whose every premise is fully resolved — `verified: true` + a non-empty `resolution` string — is rewritten to `stage_complete` at the parse boundary before this status is ever observed by a consumer; see the "resolved premise items downgrade" row in §6's coercion table, #1325.) |

### 4.2 `blocker.reason` (when `status = "blocked"`)

The same object shape MAY also appear on a `merge_gate_blocked` result to surface the gate reason (e.g. `prior_pipeline_pr_open`, issue #777) — `blocker = null` remains accepted for that status.

Minimum shape (v1+; `stage` and `reason` are required, `details` is optional):

```json
{"stage": "stage2_impl", "reason": "agent_block", "details": "<verbatim blocker text>"}
```

Full v3 shape with Phase B and Phase E fields (issue #174):

```json
{
  "stage": "stage4a_merge_gate",
  "reason": "ci_timeout",
  "details": "<verbatim blocker text>",
  "exception_type": "CITimeoutError",
  "message": "CI did not complete within 30 minutes",
  "recovery_hint": "Re-dispatch after CI watchers settle",
  "retry_eligible": true,
  "retry_delay_seconds": 120
}
```

| Reason | Meaning |
|---|---|
| `impl_failed` | Implementation agent returned BLOCK or failed quality gates after 2 attempts. |
| `review_blocked` | MUST_FIX findings persisted after 5 fix-loop cycles (the hard cap). |
| `plan_deviation` | A non-deferrable Stage-3 finding (impl deviates from an explicit plan requirement/prohibition) survived the fix loop or was judged beyond fix-loop scope. The pipeline does not assign plan-vs-impl blame — it exits `blocked`; the operator uses `cw dev-queue requeue --regress` to send it back to impl, or revisits the plan. |
| `agent_block` | Any other agent returned friction level BLOCK that the pipeline could not auto-resolve. |
| `automerge_not_armed` | `gh pr merge --auto` reported success but the read-back (`autoMergeRequest`) came back null — auto-merge was never actually armed. Fires from `auto-dev-finalize.md` Step 4c's re-verification or Step 4d's reuse-path arm+verify (#1140). |
| `operator_unavailable` | Operator/dependency currently unreachable (e.g. locked push key, network/GitHub outage) — not a broken implementation leg. cw classifies this via `OPERATOR_UNAVAILABLE_BLOCKER_REASONS` and tags the park distinctly (RFC 0011 A1). |

`blocker.reason` is an **open enum** — the producer may add new reasons without a `schema_version` bump. Consumers MUST treat unknown reasons as opaque strings and surface verbatim. (Unlike `status`, which is closed: see §4 and §8.)

#### Phase B fields (orchestrator routing context)

| Field | Type | Notes |
|---|---|---|
| `exception_type` | string \| null | Producer-side exception class name when the blocker originated from a raised exception. Free-form; consumers surface verbatim. |
| `message` | string \| null | Single-line summary of the blocker suitable for notification text. Distinct from `details` (which is verbatim multi-line context). |
| `recovery_hint` | string \| null | Producer's suggestion for the recovery action — typically what the human or orchestrator should try next. Free-form text. |

All three are optional. Producers SHOULD populate them when the underlying failure has a structured exception; they MAY remain null for blocker reasons that don't map to an exception (e.g. soft-blocks like `review_blocked`).

#### Phase E fields (queue-aware retry semantics)

| Field | Type | Notes |
|---|---|---|
| `retry_eligible` | bool \| null | `true` when the orchestrator MAY re-dispatch this ticket without human intervention. `false` when human review is required (MUST_FIX persistence, scope ambiguity, etc.). `null` means the producer didn't commit either way — consumer defaults to human escalation. |
| `retry_delay_seconds` | int \| null | Suggested backoff before re-dispatch when `retry_eligible=true`. Non-negative. Set to `null` to mean "no specific delay required". Invariant: a non-null `retry_delay_seconds` REQUIRES `retry_eligible=true`. |

The pair encodes three policies:
- **Retry now**: `retry_eligible=true`, `retry_delay_seconds=null` — transient or no-backoff failures.
- **Retry after delay**: `retry_eligible=true`, `retry_delay_seconds=N` — CI timeouts, classifier non-determinism (issue #183), rate-limit hits.
- **Human required**: `retry_eligible=false`, `retry_delay_seconds=null` — semantic blockers where another try with the same input won't help.

### 4.3 `next_actions` Vocabulary

Advisory only. cw acts on these without parsing prose.

| Action | When | Consumer behavior |
|---|---|---|
| `wait_for_ci` | `status = shipped` (always — `shipped` implies auto-merge enabled per §4.1) | cw polls CI; on success the orchestrator job is done. |
| `user_approve_plan` | `status = plan_pending_approval` | Notify user that a large-scope plan is in Linear awaiting approval. |
| `user_approve_review` | `status = review_pending_approval` | Notify user that a branch is pushed for review. |
| `resolve_merge_gate` | `status = merge_gate_blocked` | Notify user that the prior pipeline PR must merge first. The user then manually re-invokes `/auto-dev` for this ticket; no automatic re-dispatch exists. |
| `close_issue_as_completed` | `status = no_op` | Close the ticket as already completed (the work was a no-op because the system is already in the desired state). Consumer chooses how to close — Linear "Done", GitHub `gh issue close --reason completed`, etc. |
| `user_resolve_ambiguities` | `status = ambiguities_pending_resolution` | Notify user that the ambiguity scan surfaced questions requiring human disposition. The `ambiguities` array holds the structured questions. Re-dispatch the ticket after answers are recorded on the issue. |
| `user_verify_premises` | `status = premises_pending_verification` | Notify user that one or more premises must be verified before the plan can proceed. The `premises` array holds the structured entries. Re-dispatch after verification results are recorded on the issue. |

Empty list for terminal-reject (`scope_exceeded`, `forbidden_area`, `blocked`), with two `blocked` exceptions: a pre-flight block (`stage_reached = stage1_pre_flight`, e.g. the Origin Sync gate, #226) MUST carry a non-empty `next_actions` drawn from `{sync_local_main, manual_intervention}`, and a blocked result whose actions all start with a `user_resolve_*` / `user_decide_*` / `user_verify_*` prefix is a paused-for-human shape, not a terminal reject (#328). `stage_complete` and `merge_pending` carry no defined actions (typically empty). For `shipped`, `next_actions` always contains `wait_for_ci` — and `wait_for_ci` MUST NOT appear on any other status. For `ambiguities_pending_resolution` and `premises_pending_verification`, `next_actions` is non-empty. `next_actions` is otherwise an open vocabulary — parsers MUST pass unknown actions through unchanged (do not act on them, do not reject the payload).

### 4.4 `ambiguities` and `premises` payload arrays (v4)

Both `ambiguities_pending_resolution` and `premises_pending_verification` (§4.1) carry structured context arrays at the top level of the sentinel payload.

| Field | Status | Notes |
|---|---|---|
| `ambiguities` | `ambiguities_pending_resolution` | Non-empty list of ambiguity entries. Each entry typically carries `question`, `plan_assumption`, `alternatives`, `why_it_matters`, `ticket_evidence`; **treat all keys as best-effort** (§4.4 / A3 decision, issue #191). |
| `premises` | `premises_pending_verification` | Non-empty list of premise entries. Each entry carries at minimum a description (key may be `premise` or `claim`) and producer-supplied verification context (any of `verify_by` / `plan_depends_on_it_for` / `evidence_in_ticket` / `how_to_verify` / `verified` / `resolution`); **treat all keys as best-effort**. |

**Producer note (#1192, no schema change):** as of this ticket, a headless plan-stage run's `premises` array on a `premises_pending_verification` sentinel contains only premises the plan-stage agent could **not** self-verify with authoritative evidence in the same session (official docs, `--help` output, source, or a quoted command invocation + output). Premises the agent settled itself are excluded from this array; they are instead recorded in the persisted plan body under a `## Self-Verified Premises` section and cited in `friction_highlights`. This is a producer-side inclusion-criteria change only — the array's shape, the `claim`/`premise` key union, and the parser's shape-only validation (§4.4 A7, `_reject_empty_claim_premises`) are unchanged.

**Parser note (#1325, no schema change):** #1192 above already excludes most self-verified-this-session premises from the emitted array at the producer-skill level. The parse-boundary coercion documented in the "Parse-boundary coercions" table (§6) is defense-in-depth for the case #1192 does not cover: a premise resolved by a **pre-existing** binding resolution the producer maps onto (`verified: true` + a non-empty `resolution` string), rather than fresh in-session verification. The producer-prompt companion change (recognizing premises closed by a pre-existing binding resolution in the plan-stage partition logic) is deferred to #1411 (R1).

**Cross-field invariants (enforced by parser):**
- `status='ambiguities_pending_resolution'` requires `ambiguities` non-empty (A5).
- `status='premises_pending_verification'` requires `premises` non-empty (A5).
- Both statuses require `next_actions` non-empty (A2).
- Both statuses prohibit `branch` (pre-branch, A4) and require `scope.lines_actual=null` (pre-impl, A4).
- Both statuses require `schema_version >= 4` (A1).
- Each ambiguity item's question must be a non-empty, non-whitespace string; empty-question items are filtered at the parse boundary, and an all-empty/missing array coerces to a labeled synthetic placeholder that parks the ticket visibly as a producer glitch (**A6**, #953).
- Each premise item's claim (key `claim` or `premise`) must be a non-empty, non-whitespace string; empty-claim items are filtered at the parse boundary, and an all-empty/missing array coerces to a labeled synthetic placeholder that parks the ticket visibly as a producer glitch (**A7**, #962).

**Consumer guidance:** Key off `result.status` directly — these are now canonical statuses. Route `user_resolve_ambiguities` / `user_verify_premises` via `next_actions`. The `ambiguities` and `premises` arrays hold the structured entries for human presentation.

**Migration note:** Before v4, these values were emitted by the producer but not recognized by the parser — they routed through synthetic `BlockedResult` with `reason=status_unknown`. Skills that previously keyed off `extract_block` raw JSON (`/cw-followup`, `/cw-validate-result`) can now use the typed `result.status` directly. The `result.ambiguities` / `result.premises` fields carry the same data that was previously accessed via raw JSON. Promoted via #191.

---

## 5. Health Aggregation

Every agent prompt in headless mode (plan, impl, reviewers, fix-loop, prep-pr) MUST emit both the Friction Report and the Health Check block:

```
## Health Check
- **Context usage**: <rough % or HIGH/MEDIUM/LOW>
- **On-spec confidence**: HIGH | MEDIUM | LOW
- **Shortcuts taken under pressure**: [list or NONE]
- **Could work be incomplete?**: NO | MAYBE | YES (explain)
- **Recommendation**: PROCEED | EXIT_FOR_HUMAN_REVIEW
```

### 5.1 Aggregation Rule

Long `--print` sessions accumulate context silently. The skill main session aggregates across all agent reports and downgrades the outcome:

- **Small + clean review + all agents healthy** → `shipped`.
- **Small + clean review + any degraded agent** → downgrade to `review_pending_approval` (branch pushed, no PR). Set `health.downgrade_applied: true`.
- **Large path** is unchanged (already exits at S3); the full health summary still rides in the result payload.

A degraded agent is one that returned ANY of:
- `On-spec confidence: LOW`
- `Could work be incomplete?: MAYBE` or `YES`
- `Recommendation: EXIT_FOR_HUMAN_REVIEW`

**Scan scope:** every agent that ran to completion in the pipeline — plan, impl, each reviewer in the parallel review fan-out, every fix-loop cycle (cycles 1–5, all included), and prep-pr. A fix-loop cycle that itself reports degradation triggers `downgrade_applied`; this is independent from `fix_loop_escalated` (§5.2). If an agent did not emit a Health Check block at all, treat that agent as degraded — missing data is not healthy data.

### 5.2 `health` Subfields

| Field | Notes |
|---|---|
| `lowest_agent_confidence` | `HIGH` \| `MEDIUM` \| `LOW` — minimum across all agent reports. `null` accepted only on pre-impl exits (`stage1_plan` / `stage1_pre_flight`, #416); required non-null at every later stage. |
| `any_incomplete_risk` | `true` if any agent reported `MAYBE` or `YES` for `Could work be incomplete?`. |
| `shortcuts` | Flat list of all `Shortcuts taken under pressure` entries across agents. |
| `recommendation` | `PROCEED` if all agents recommended PROCEED; otherwise `EXIT_FOR_HUMAN_REVIEW`. |
| `downgrade_applied` | `true` only when the §5.1 rule actually downgraded `shipped` → `review_pending_approval`. |
| `fix_loop_escalated` | `true` when the fix loop tripped cycle 3+ or scope-grew at any cycle (see gate row in §2). Independent from `downgrade_applied`. |
| `agent_health_summary` | **Phase C** — per-agent breakdown. See §5.3. Optional; defaults to empty list. |

`downgrade_applied` and `fix_loop_escalated` are distinct signals — a run can have either, both, or neither.

`health.shortcuts` items are filtered the same way (blank entries dropped at the parse boundary, no placeholder — A8, #1130).

### 5.3 `health.agent_health_summary` — Phase C per-agent breakdown (issue #174)

**Gap addressed:** When the §5.1 health aggregation rule downgrades to `review_pending_approval`, the orchestrator sees `health.downgrade_applied=true` and `health.lowest_agent_confidence=LOW` but cannot tell *which* agent caused the degradation — a retry targeting the specific agent is not possible without that detail.

**`agent_health_summary`** is an array of per-agent snapshots collected across all agents that ran during the pipeline (plan, impl, reviewers, fix-loop cycles, prep-pr):

```json
"health": {
  "lowest_agent_confidence": "MEDIUM",
  "any_incomplete_risk": false,
  "shortcuts": [],
  "recommendation": "PROCEED",
  "downgrade_applied": false,
  "fix_loop_escalated": false,
  "agent_health_summary": [
    {"agent_id": "plan-reviewer-xyz", "confidence": "HIGH", "scope": "small"},
    {"agent_id": "impl-agent-abc", "confidence": "MEDIUM", "scope": "large"}
  ]
}
```

Each entry:

| Field | Type | Notes |
|---|---|---|
| `agent_id` | string | Producer-assigned identifier for the agent. Free-form; consumers surface verbatim. |
| `confidence` | `"HIGH"` \| `"MEDIUM"` \| `"LOW"` | The agent's self-reported `On-spec confidence` from its Health Check block. |
| `scope` | string \| null | Scope tier the agent was operating under. Expected values `"small"` / `"large"`; free-form string to tolerate producer-side drift. `null` for agents without a scope concept (e.g. plan-reviewer). |

**Advisory optional field.** Absent (empty list) on payloads from producers that predate Phase C. No schema version bump required (§8). Consumer use case: filter entries with `confidence=LOW` or `scope=large` to target retries.

`agent_health_summary` entries with a blank `agent_id` are dropped at the parse boundary (A8, #1130) rather than coerced to a placeholder, for the same reason as above.

---

## 6. Failure Modes for the Parser

The skill can fail to emit a complete sentinel block. cw must handle:

1. **No `<<<AUTO_DEV_RESULT` sentinel anywhere** — skill crashed before the emit step, or the run was not headless. The parser first attempts the **loose fallback** (see below); if that also fails, treat as `blocked` with synthetic blocker `{stage: "unknown", reason: "no_result_emitted", details: <last-N-lines-of-stdout>}`.
2. **Opening sentinel present, closing sentinel missing** — skill crashed mid-emit. Same handling as (1), but the loose fallback does NOT apply (the open sentinel takes precedence).
3. **Block present but JSON does not parse** — skill bug. Same handling as (1); include the raw block in `details`.
4. **`schema_version` higher than parser supports** — skill upgraded ahead of cw.
   - **One-version look-ahead (`schema_version == max_supported + 1`):** best-effort parse using the current max schema. A `WARNING` is logged naming the skew. The result is usable — proceed with normal routing. This handles the self-ship skew where a schema-bump PR ships while the running parser is still at N (issue #395).
   - **Two or more versions ahead (`schema_version >= max_supported + 2`), missing, or non-int:** surface verbatim and refuse to act on `next_actions`; do not auto-merge or auto-route. Reason code: `schema_version_unsupported`.
5. **Unknown `status` value** — same as (4). The closed enum in §4.1 covers all recognized values including `ambiguities_pending_resolution` and `premises_pending_verification` (promoted to canonical status in v4 via #191). Any value outside this set routes through `reason=status_unknown`.
6. **Multiple complete sentinel blocks in one invocation's stdout** — skill bug (the contract is exactly one per invocation; see §3.1). Same handling as (1), with `reason: "multiple_result_blocks"` and `details` containing the count and the LAST block's raw payload.

### Loose fallback (GitHub #337)

The skill occasionally emits the payload in a plain code fence without the `<<<AUTO_DEV_RESULT` / `AUTO_DEV_RESULT>>>` markers:

```
All done. Emitting the headless sentinel:

```json
{"schema_version": 2, "status": "shipped", ...}
```
```

The parser tolerates this format. When no sentinel markers are found and the opening sentinel is absent, the parser scans for the **last** `` ```json `` or `` ``` `` fenced block whose inner JSON parses as a dict containing both `schema_version` and `status`. If found, it is treated identically to a normally-framed block. A warning is logged.

**The markers are still required in the skill contract.** The loose fallback is a consumer-side safeguard, not an invitation to omit markers. Skill implementations must emit `<<<AUTO_DEV_RESULT\n{...}\nAUTO_DEV_RESULT>>>`.

Parser must NEVER act on a partial parse — if any of the above fire, treat the run as blocked and require human attention.

### Parse-boundary coercions

The parser applies several pre-validation coercions before handing the payload to Pydantic. Each coercion targets a known producer-drift pattern: a field combination that the model correctly rejects, but that arises from a well-understood producer bug rather than a genuinely ambiguous payload. These coercions are scoped tightly — they fire only for the specific `status` + `stage_reached` shapes where the producer bug has been observed. They do NOT apply to `shipped` or `blocked` (where the same field contradictions are genuinely ambiguous and should fail loudly).

| Coercion | Trigger | Action | Issue |
|---|---|---|---|
| `no_op` + stray `pr` / `branch` / `commits` | `status=no_op` and `pr`, `branch`, or `commits` is non-null/non-empty | Set `pr=null`, `branch=null`, `commits=[]` | #367 |
| `blocked` + stray `next_actions` | `status=blocked` and `next_actions` is non-empty with non-user-directed verbs (not `user_resolve_*` / `user_decide_*` / `user_verify_*` prefixes; not `stage_reached=stage1_pre_flight`) | Drop `next_actions` (set to `[]`), preserve `blocker` | #371 |
| `blocked` + non-null `pr` | `status=blocked` and `pr` is non-null (FINALIZE created a PR then couldn't merge — CI pending). Skipped when `health.downgrade_applied=true` (would trade one validation failure for another under §5.1) | **Rewrite `status` to `merge_pending`**; set `blocker=null`, `next_actions=[]`, preserving the PR URL instead of recording a failure | #899 |
| `no_op` + stray `scope.lines_actual` | `status=no_op` and `stage_reached ∈ {stage1_pre_flight, stage1_plan}` and `scope.lines_actual` is non-null | Set `scope.lines_actual=null` | #399 |
| terminal-reject strays | `status ∈ {scope_exceeded, forbidden_area}` and `branch`/`commits` non-null/non-empty, or `scope.lines_actual` non-null on a pre-impl stage | Set `branch=null`, `commits=[]`; null `scope.lines_actual` (pre-impl only) | #430 |
| pre-impl `lines_actual=0` | any status; `stage_reached ∈ {stage1_pre_flight, stage1_plan}` and `scope.lines_actual` is exactly `0` (other non-null values still hard-error) | Set `scope.lines_actual=null` | #416 |
| `shipped` missing `wait_for_ci` | `status=shipped` and `next_actions` lacks `wait_for_ci` | Append `wait_for_ci` to `next_actions` | #417 |
| near-miss `stage_reached` label | `stage_reached` is non-canonical but starts with a known `stage<1-5>` prefix (e.g. `stage4_pr_creation`) | Coerce to that stage's canonical value; genuine garbage (no stage prefix) still rejects | #748 |
| empty-question ambiguity items | `status=ambiguities_pending_resolution` and one or more ambiguity items have a null/empty/whitespace question | Drop the empty item(s); if none remain, inject labeled placeholder `{"question": "(producer emitted no usable ambiguity … see #953)"}` | #953 |
| empty-claim premise items | `status=premises_pending_verification` and one or more premise items have no usable `claim`/`premise` text | Drop the empty item(s); if none remain, inject labeled placeholder `{"claim": "(producer emitted no usable premise … see #962)"}` | #962 |
| resolved premise items downgrade | `status=premises_pending_verification` and one or more premise items carry `verified: true` (strict JSON boolean, no string/int tolerance) AND a non-empty `resolution` string mapping to an adopted/binding resolution | Drop the resolved item(s); if none remain, **rewrite `status` to `stage_complete`**, clear `premises` to `[]`, drop the stale `user_verify_premises` next_action (other actions untouched), and cite each dropped premise's claim + resolution in `friction_highlights` | #1325 |
| blank items in commits / friction_highlights / next_actions / health.shortcuts, or blank agent_id in agent_health_summary entries | any status; an array item is a blank string (or an agent_health_summary entry has a blank agent_id) | Drop the blank item(s)/entry(ies); no placeholder is injected — these fields carry no status-gated non-empty invariant | #1130 |

All coercions log a `WARNING` with the affected field names and ticket ID. The model-level invariants remain strict — coercion happens only at the parse boundary so existing test coverage for the invariants is unaffected.

---

## 7. Resume Protocol (Reserved — Not Yet Specified)

Resume (`--resume`) is **not yet specified**. Tracked in global-claude#2 and cw#59. No contract exists until those issues ship.

Until then, cw must treat all non-terminal exits as fully manual recovery: the user re-invokes `/auto-dev` themselves or skips the ticket. Consumers MUST NOT implement speculative resume handling against any draft mechanism (file-injection, env var, etc.) — when this section is filled in, the contract will be authoritative and will not pre-honor guesses.

---

## 8. Versioning

`schema_version: 5` is the current contract. Parsers also accept `schema_version: 1`, `2`, `3`, and `4` during the rollout window.

**Version history:**

| Version | Changes |
|---|---|
| 1 | Initial contract. |
| 2 | Added `no_op` status (§4.1) and `close_issue_as_completed` advisory action (§4.3). v1-tagged payloads with `status=no_op` are rejected as `validation_failed`. |
| 3 | Added `stage1_pre_flight` value to `stage_reached` enum (§3.3) and `none` value to `plan_source` enum (§3.3). Used together for pre-flight no_op exits. Parsers also accept this pair under v2 as a one-time rollout exception (the skill emitted them at v2 before the parser caught up — see #103). Also added `github_issue_existing` to `plan_source` (the post-Linear analog of `linear_existing`; treated identically). Accepted under v2 and v3 — same rollout-exception treatment, since the producer emits this value at v2 today (see #190). |
| 4 | Promoted `ambiguities_pending_resolution` and `premises_pending_verification` from §4.4 interim states (not in closed enum) to canonical `Status` values (§4.1). Added `ambiguities` and `premises` top-level fields with cross-field invariants (non-empty when corresponding status is set, §4.4). Added `user_resolve_ambiguities` and `user_verify_premises` to §4.3 vocabulary. v3-tagged payloads with either new status are rejected as `validation_failed`. Tracked in #191. |
| 5 | Added the optional `review.agents_run` int (§3.3) — count of reviewer agents that ran, reconciled against the executor-neutral review-verdict contract's `agents_run` list (#1237). Defaults to `0`; v1-v4 payloads that omit it parse unchanged — no schema_version bump was required to introduce it (purely advisory at the time). The review-verdict model group itself (`Finding`, `EscalationMetadata`, `ReviewVerdict`, ...) lives in `cw.review_findings` / the `.claude/review-verdict.json` artifact (#1108), not the sentinel block. (#1194 later graduated `agents_run` to a required-non-zero signal for one opt-in downstream consumer — see Note A9 below; that graduation itself required no further bump.) |

**Note (A6, #953):** Rejecting empty-question ambiguity items and coercing an empty/missing ambiguities array to a labeled placeholder is a parser-side strictness tightening of an existing v4 invariant — **no version bump** (consistent with the #430 `_coerce_empty_pending_array` precedent).

**Note (A7, #962):** Rejecting empty-claim/premise premise items and coercing an empty/missing premises array to a labeled placeholder is a parser-side strictness tightening of an existing v4 invariant — **no version bump** (same precedent as A6/#953 and #430).

**Note (A8, #1130):** Filtering blank items from `commits`, `friction_highlights`, `next_actions`, `health.shortcuts`, and blank `agent_id` entries from `health.agent_health_summary` is a parser-side strictness tightening — **no version bump** (same precedent as A6/#953, A7/#962, and #430). Unlike A6/A7, no placeholder is injected: none of these five fields is gated behind a status-specific "must be non-empty" invariant, so an all-blank input simply filters down to an empty list/array.

**Note (A9, #1194):** Revising an *already-existing* optional field's downstream *consumption* inside cw — a new internal automation module choosing to start reading a field that was already part of the wire contract — does **not** require a `schema_version` bump, provided the field itself is not removed/renamed, its type does not change, and no new closed-enum value is added. `gate_recipes.py`'s `auto_approve_clean_review` recipe now treats `review.agents_run` as a required-non-zero signal for its own predicate, but this is distinct from the "Bump required when" bullet below about a field "consumers cannot ignore without a behavior change" (e.g. `health.downgrade_applied`): that bullet is scoped to a field's *core-parser/wire-format* role — `health.downgrade_applied` is consumed unconditionally by every parser/orchestrator instance via the §5.1 stage-routing rule, for *all* runs, making it load-bearing for the contract itself. `agents_run`'s gate-recipe consumption is categorically different: it is one *opt-in, default-off* downstream automation module (`OrchestratorConfig.gate_recipes_enabled`) independently choosing to start treating an already-advisory field as load-bearing for its own internal predicate. No core parser behavior changes, and every other consumer of the sentinel (including cw's own stage-routing) remains free to ignore `agents_run` exactly as before.

**Note (A10, #1325):** Downgrading a `premises_pending_verification` sentinel whose every premise item is fully resolved (`verified: true` + a non-empty `resolution` string) to `stage_complete` at the parse boundary is a strictness **relaxation** — a previously-blocking/parked payload now proceeds — the opposite direction from A6/A7/A8's tightenings. **No version bump** is required: `stage_complete` is an existing enum value (not newly added), and no field is added, removed, renamed, or retyped. Same reasoning as the #899 `blocked` + non-null `pr` → `merge_pending` rewrite precedent.

**Note (A11, #1723):** Adding the optional `review.had_real_commit` bool-or-null (§3.3) — same shape of change as A9/`agents_run`: a purely advisory field, prose-only consumption (it only changes wording inside `render_verdict_comment`'s non-blocking headline), defaulting `null` so the commit outcome of every pre-#1723 payload remains explicitly unknown while rendering exactly as it always did. **No version bump** required — same precedent as the v5 `agents_run` introduction (§8, version-history row 5).

**Bump required when:**
- Any field is removed or renamed.
- Any existing field's type or semantics change.
- A new value is added to a closed enum (`status`, `stage_reached`).
- A new optional field is added that consumers cannot ignore without a behavior change (e.g., a new `health.*` subfield that drives routing the way `health.downgrade_applied` does today).

**No bump required when:**
- A new purely-advisory optional field is added to `health`, `pr`, `review`, or the top level (one consumers may ignore with no behavior change).
- A new `next_actions` entry is added (parsers already treat unknown actions as advisory).
- A new `blocker.reason` value is added (open enum — see §4.2).

**Cross-version status compatibility:** A status introduced at version N is invalid under any `schema_version < N`. Parsers MUST reject mismatched payloads (e.g., v1 + `no_op` → `validation_failed`). **Exception (one-time):** `stage_reached='stage1_pre_flight'`, `plan_source='none'`, and `plan_source='github_issue_existing'` are accepted under both v2 and v3. This is documented under v3 in the table above; the v2 acceptance covers in-flight skill emissions that predate the parser's v3 awareness. Similarly, the `ambiguities_pending_resolution` / `premises_pending_verification` statuses (officially v4) and the `stage_complete` (#699) / `merge_pending` (#899) statuses are accepted under **all** supported schema versions as a rollout exception until the producer skill bumps its emitted version.

When bumping, update this doc, `commands/auto-dev.md`, and the cw parser in lockstep. **Order matters:** the parser must accept the new version BEFORE the skill emits it, otherwise in-flight emissions land in deployed parsers that don't recognize them. Parsers MUST defensively reject unknown `schema_version` values per §6 (4).

---

## BLOCKED_ON_USER Queue Task Status

`QueueItemStatus.BLOCKED_ON_USER` marks a ticket task that paused for operator input rather than completing or failing. It differs from `PENDING` (retry-eligible) — BLOCKED_ON_USER tasks should never be auto-retried.

**Three trigger conditions:**

1. **Paused sentinel** — the dispatch sentinel router (`apply_staged_decision` in `cw.dispatch`) receives an `AutoDevResult` with `status` in `{ambiguities_pending_resolution, premises_pending_verification}`, or `status=blocked` with `next_actions` containing a `user_resolve_*/user_decide_*/user_verify_*` item. A `session.needs_attention` event fires.

2. **Silent exit** — the consume path sees headless exit code 0 but no AUTO_DEV_RESULT sentinel in stdout (the child self-backgrounded a subagent and exited early). A `session.needs_attention` event fires.

3. **Watchdog** — `reconcile()` finds a DAEMON RUNNING session with no `last_result`, surface still live in the native daemon, and `(now - started_at) > budget`. `flag_silently_idle_daemon_sessions` fires. The budget follows a four-level lookup via `resolve_idle_watchdog_budget`:
   - **Per-ticket** (`TicketTask.idle_watchdog_override`) — explicit escape hatch; beats everything.
   - **Per-tier** (`OrchestratorConfig.idle_watchdog_by_tier[task.scope_hint]`) — keyed by `TicketTask.scope_hint`. Default config ships `{"large": 3600}` so large-tier sessions, which can legitimately stall on slow tests/mypy, get a 60-minute window.
   - **Operator-tunable global** (`OrchestratorConfig.idle_watchdog_seconds`) — applies when no per-ticket or per-tier budget resolves; unset (`null`) falls through to the constant below.
   - **Hardcoded fallback** (`IDLE_WATCHDOG_SECONDS = 900`) — used when no task is found or scope_hint is unset and no global is configured. NB: on first dispatch attempt `scope_hint` is always `None` (only retries inherit it from the prior sentinel), so attempt 1 uses the global/fallback budget.

### SESSION_NEEDS_ATTENTION Event

Emitted on every BLOCKED_ON_USER transition.

```json
{
  "session_id": "<string>",
  "session_name": "<string>",
  "client": "<string>",
  "ticket_id": "<string | null>",
  "claude_session_id": "<string | null>",
  "paused_status": "<string | null>",
  "breadcrumbs": "<string>",
  "crashed": false
}
```

**Re-dispatch rule:** never auto-retry BLOCKED_ON_USER tasks. Human must review the Linear/GitHub issue for the posted ambiguities/premises, resolve them, and then re-dispatch manually.

## AWAITING_OPERATOR_SIGNOFF Queue Task Status (RFC 0007 Phase 3, GitHub #990)

`QueueItemStatus.AWAITING_OPERATOR_SIGNOFF` marks a ticket parked for an
explicit operator signoff before it ships. Distinct from `BLOCKED_ON_USER`:
the ticket isn't blocked on a producer-emitted approval-gate sentinel or a
watchdog condition — it's an operator policy decision, resolved entirely by
`cw` from configuration, with no session/last_result validation on the
clearing path.

**Resolution (3-tier precedence, highest first)** via `resolve_signoff`:

1. **Per-ticket** — `TicketTask.signoff` (`cw dev-queue add --signoff operator`).
2. **Per-lane** — `LaneConfig.signoff` on the ticket's declared lane.
3. **Global default** — `OrchestratorConfig.default_signoff` (`"none"` or `"operator"`).

**Where the gate fires:** only at the REVIEW→FINALIZE checkpoint — the point
a ticket at `Stage.REVIEW` would otherwise auto-advance (small-tier
`review_pending_approval`) or complete (`stage_complete` fired while the task
is at `Stage.REVIEW`). A small-tier `plan_pending_approval` (PLAN→IMPL) and
every other mid-pipeline `stage_complete` advance (HARDEN→PLAN, PLAN→IMPL,
IMPL→REVIEW) are never gated, even when signoff is configured — the gate is a
ship checkpoint, not a per-stage checkpoint.

**Clearing the gate:** `cw dev-queue approve` on an `AWAITING_OPERATOR_SIGNOFF`
ticket advances (or completes, at the pipeline's terminal stage) unconditionally
— no re-check. A large-tier ticket with signoff configured needs **two**
approvals at REVIEW: the first (ordinary `review_pending_approval` approval)
re-routes to `AWAITING_OPERATOR_SIGNOFF` instead of advancing to FINALIZE; the
second clears it. To reject instead of clearing forward, `cw dev-queue requeue
--stage <earlier> --regress` moves a signoff-parked ticket backward.

**Lane occupancy:** `AWAITING_OPERATOR_SIGNOFF` occupies its lane slot the same
as `BLOCKED_ON_USER` (`OCCUPIED_LANE_STATUSES`) — it is not eligible for
re-dispatch and must not be double-counted as free capacity.

**`cw dev-queue wait` exit code:** `4` (distinct from `2` for an ordinary
`BLOCKED_ON_USER` block) — see the exit-code table in `cw dev-queue wait --help`.

## The `finalize_gate_held` Hold Disposition (RFC 0011 A3, GitHub #1160)

The **proactive finalize hold** stops a ticket before an *unattended* finalize.
It is not a `blocker.reason` — no producer ever emits it, and it will never
appear in an `AutoDevResult`. It is a **`disposition`** stamped by `cw` itself
onto a `BLOCKED_ON_USER` row, sitting in the same namespace as the ordinary
status-derived dispositions (`review_pending_approval`, `blocked`, …) and the
RFC 0011 A1 `awaiting_operator` hold.

**Status landed:** `BLOCKED_ON_USER` (NOT `AWAITING_OPERATOR_SIGNOFF` — see
below for why the two gates are different things).

**Resolution (3-tier precedence, highest first)** via `resolve_hold_finalize`,
exactly mirroring `resolve_signoff`:

1. **Per-ticket** — `TicketTask.hold_finalize` (`cw dev-queue add --hold-finalize`).
2. **Per-lane** — `LaneConfig.finalize_gate` on the ticket's declared lane.
3. **Global default** — `OrchestratorConfig.default_finalize_gate` (`"auto"` or `"manual"`).

**Where the hold fires:** the same three REVIEW→FINALIZE checkpoints the
signoff gate covers, and only those — a small-tier `review_pending_approval`
advance, a `stage_complete` fired at `Stage.REVIEW`, and a multi-hop
stage-pointer walk crossing the REVIEW rung. Mid-pipeline advances
(HARDEN→PLAN, PLAN→IMPL, IMPL→REVIEW) are never held.

**Precedence:**

- flag on (any config / probe / tier) → **HELD** (the force hold wins outright,
  including Small-scope)
- flag off, lane/global `finalize_gate: manual` → **HELD** (including Small-scope)
- flag off, config `auto`/unset, availability probe unavailable → held via the
  RFC 0011 A5 probe's own mechanism, not this one
- flag off, config `auto`/unset, probe available → **unchanged** (Large blocks at
  the approval/signoff gate exactly as today; Small auto-advances as today)
- hold armed **and** signoff configured → a single park, `BLOCKED_ON_USER` /
  `finalize_gate_held`. The hold is checked first and the signoff branch is
  `elif`-chained behind it, so the row is never double-parked.

**Releasing the hold:** release via `cw dev-queue approve`. That command is the
only caller permitted to clear it — it passes `operator_initiated=True` into
`_approve_ticket_locked`, which skips the hold check entirely. Every *automatic*
caller (today: the RFC 0009 auto-approve gate recipe) omits that flag, gets the
fail-safe default, and its approve degrades to a no-op that mutates nothing and
emits a `gate.auto_approve_held` correction instead.

**Not batch-drainable:** `cw dev-queue drain` deliberately selects only the RFC
0011 A1 `awaiting_operator` subset of `HOLD_DISPOSITIONS` (RFC 0011 A4 R11), so
a force hold is never released en masse — only one ticket at a time, by hand.

### queue.session_reaped Bus Event (GitHub #380)

Emitted on the **queue-events channel** (the `cw-queue-events` MCP/SSE server, event string `queue.session_reaped`) whenever reconcile disposes of a session. The channel server polls `session.reap_reason` off the state snapshot and fires exactly once per new reason stamp. Note: this is a channel-server event, NOT written to the orchestrator inbox — it does **not** appear in `cw event tail` (the inbox carries the related `session.reap_proposed` / `session.timed_out` / `session.needs_attention` events instead).

Event string: `queue.session_reaped`

**Payload:**

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | The cw session ID. |
| `surface_ref` | string \| null | Daemon surface short-id; null when the surface is gone (backstop paths). |
| `origin` | string | `"daemon"` or `"user"`. |
| `reason` | string | See ReapReason enum below. |
| `from_status` | string | Session status before the reap (e.g. `"active"`, `"idle"`). |
| `to_status` | string | Session status after the reap (e.g. `"completed"`, `"timed_out"`). |

**`reason` values (ReapReason enum):**

Current producers (post-ADR-0014 — every one evidence-driven, never a timer):

| Value | Trigger |
|---|---|
| `phantom_surface` | Daemon surface absent from roster (`_reconcile_locked` phantom sweep). |
| `usage_limit_cutoff` | Transcript of a dead/phantom session contained a Claude usage-limit message; drives dispatch back-off. |
| `terminal_sibling` | A PENDING row was parked/cancelled because the same (client, ticket) already has a COMPLETED or CANCELLED sibling row (#876). Queue-only disposition — surfaces via `session.reap_proposed` and the task's `disposition`, not via a session reap. |
| `completed_backstop` | Backstop path (`revert_timed_out_tasks` / `revert_completed_silent_tasks`) found a TIMED_OUT (legacy) or COMPLETED DAEMON session with a still-RUNNING queue task and no prior reap_reason. |

Historical-only values (no longer produced — removed with the process-kill
timeouts, ADR-0014 — but still present in old event logs and persisted rows):

| Value | Former trigger |
|---|---|
| `idle_stall` | Idle watchdog fired; task reverted to PENDING for retry. |
| `retry_cap_parked` | Idle watchdog fired; retry cap reached; task set BLOCKED_ON_USER. |
| `stalled_retry_cap_parked` | Wall-clock sweep fired at the per-tier stalled retry cap; task set BLOCKED_ON_USER. |
| `wall_clock_budget` | Wall-clock budget exceeded; task reverted for retry. |
| `finalize_blocked` | Stalled FINALIZE-stage session with commits but no PR; parked for rescue. |
| `salvage_completed` | Git-state HIGH path: draft PR auto-created, task COMPLETED. |
| `salvage_parked` | Git-state LOW path: task set BLOCKED_ON_USER for human salvage. |

---

## 9. Cross-References

- **Producer source (canonical for behavior):** `commands/auto-dev.md` in `mattwwarren/global-claude`. Sections "Headless Mode", "Health Check Protocol", "Appendix: Structured Output". Where this doc duplicates a producer value (e.g. the §2 fix-loop hard-cap, the §5 "every agent emits Health Check" requirement), the producer wins on disputes — open an issue to reconcile.
- **Consumer arc** (titles current as of this commit; canonical text lives on GitHub):
  - cw#56 — https://github.com/mattwwarren/claude-workspace/issues/56
  - cw#57 — https://github.com/mattwwarren/claude-workspace/issues/57
  - cw#58 — https://github.com/mattwwarren/claude-workspace/issues/58
  - cw#59 — https://github.com/mattwwarren/claude-workspace/issues/59
- **Substrate** (cw 0.8.0 milestone, prerequisite for #56–#59): cw#52–#55.
- **Skill-side resume implementation:** https://github.com/mattwwarren/global-claude/issues/2.

---

## 10. Stage Event Taxonomy (Producer Contract)

### 10.1 Event Types

`stage.entered` and `stage.errored` are **orchestrator events** — recorded on cw's event bus, not part of the `<<<AUTO_DEV_RESULT` sentinel block in §3. The skill records them via `cw event record …` at each stage boundary while running in headless mode. Consumers (cw status output, TUI dashboard, automation hooks) read them back through `cw event tail` and `read_events`.

### 10.2 Stage Identifiers (closed enum)

Every `stage` and `prev_stage` payload value MUST match one of:

- `s0_intake`
- `s1_plan_generated`
- `s1_ambiguity_scan_complete`
- `s1_plan_reviewed`
- `s2_impl_started`
- `s2_impl_complete`
- `s3_review_started`
- `s3_review_complete`
- `s4_pr_created`
- `s5_ci_waiting`
- `done`

### 10.3 Payload Schema

**Required for both `stage.entered` and `stage.errored`:**

- `session_id` (str) — the cw session that owns the run.
- `ticket_id` (str) — the ticket being worked.
- `stage` (str) — the stage being entered or that errored; MUST match the closed enum in §10.2.
- `started_at` (str) — ISO8601 UTC timestamp, e.g. `2026-05-23T13:01:42Z`.

**Optional:**

- `prev_stage` (str) — the stage being departed; MUST also match the closed enum in §10.2.
- `malformed_recommendation_count` (int) — `s1_ambiguity_scan_complete` only. Count of ambiguity items whose `Recommendation:` sub-bullet was missing or malformed (defaulted to `parked` per the fail-closed rule) rather than an explicit `PARK` (#1274). Advisory optional field; absent on payloads from producers that predate this ticket. No schema_version bump required — the payload is an open dict (§10.3, no `extra=forbid`). On this event's current emission (the AUTO-CONTINUE path only, where `parked` is empty) the value is always `0`; the non-zero case surfaces instead via the `## Pending Verification Scan` comment note, not this field.

**`stage.errored`-only:**

- `error_kind` (str) — free-form classifier such as `agent_block`, `impl_failed`, `review_blocked`. Open enum — consumers MUST tolerate unknown values.

### 10.4 Producer Invocation

The skill emits stage transitions via `cw event record` from inside the headless session:

```bash
cw event record stage.entered \
  --payload '{"session_id":"$CW_SESSION_ID","ticket_id":"173","stage":"s2_impl_started","prev_stage":"s1_plan_reviewed","started_at":"2026-05-23T13:01:42Z"}'
```

### 10.5 Consumer Behavior

cw surfaces `last_stage` per running session in `cw orchestrate status` (text output). The value is derived at render time by filtering recorded events to `STAGE_ENTERED` and mapping `payload.session_id → payload.stage` (latest event wins). `STAGE_ERRORED` events are visible in `cw event tail` and `recent_events` but do NOT redefine `last_stage`. Sessions with no stage events omit the `last_stage=…` token in the text output.

### 10.6 Cross-Repo Status

The producer side ships in `mattwwarren/global-claude` `commands/auto-dev.md` as a coordinated PR; the consumer side (event taxonomy + `last_stage` plumbing) ships in cw#173.

## 11. Result-Publishing Harvest Authority (RFC 0012)

### 11.1 Harvest-Authority Model

Every backend has a designated harvest authority that pushes `Session.last_result` through one validated door — `cw.result.emit_result_on` / `emit_result_locked`. Consumers read `session.last_result` only, never transcripts; `tests/test_result_door_guard.py` enforces this as a source-level deny-list scan (#1461).

| Authority mechanism | `LastResultSource` value | Backend(s) | Call site |
|---|---|---|---|
| Manual CLI push | `emit_cli` | operator / scripted | `cw.result.result_emit` |
| Stop-hook harvest | `stop_hook_harvest` | detached Claude daemon | `cw.cli.stop_hook` |
| Executor-direct | `executor_direct` | codex, aider/local (supervised-child, synchronous) | `cw.executor` |
| Git-facts synthesis | `git_synthesis` | aider/local (no sentinel emitted) | `cw.reconcile.local` |
| Salvage-transcript | `salvage_transcript` | idle/phantom/stalled sweeps (supervising worker died mid-run) | `cw.reconcile._shared` |

### 11.2 `LastResultSource` Provenance Enum

`cw.models.enums.LastResultSource` records which mechanism wrote a session's terminal sentinel, so the door can arbitrate first-writer-wins and log a collision naming both sources:

- `EMIT_CLI = "emit_cli"`
- `STOP_HOOK_HARVEST = "stop_hook_harvest"`
- `EXECUTOR_DIRECT = "executor_direct"`
- `GIT_SYNTHESIS = "git_synthesis"`
- `SALVAGE_TRANSCRIPT = "salvage_transcript"`

The door (`emit_result_on`) stamps `session.last_result_source` alongside `session.last_result` at write time. `None` on `Session.last_result_source` means pre-migration — no writer has stamped a source yet.

### 11.3 The Salvage Label

Two distinct things carry a "salvage" connotation and must not be conflated:

- **Terminal salvage completion** (`LastResultSource.SALVAGE_TRANSCRIPT`) — reconcile's idle/phantom/stalled sweeps parse a transcript for a real, emitted terminal sentinel after the supervising worker died mid-run, then route it through the door via `_apply_salvaged_completion` → `emit_result_on`. This *is* a door write, arbitrated first-writer-wins like any other.
- **Park markers and rescue bookkeeping** — the 13 sites allowlisted by `tests/test_result_door_guard.py` (`idle.py`, `phantom.py`, `salvage.py`, `stalled/_mutations.py`, `dev_queue/requeue.py`). These write a `paused_status`-only dict (no `"status"` key, so `has_terminal_result()` stays `False`) or add a bookkeeping flag (e.g. `rescue_attempted`) onto an existing dict, or erase `last_result` back to `None` ahead of a requeue. None of these publish a terminal sentinel, so door arbitration is not implicated — they are direct, individually-rationaled writes to `Session.last_result` outside the door, distinct from the salvage-transcript mechanism above.

### 11.4 The Opencode Slot-In Rule

From RFC 0012's Summary (`docs/rfcs/0012-unified-result-publishing.md`):

> A future backend (e.g. opencode) slots in by answering one question: is cw its
> supervising parent? If yes, its executor is the harvest authority (prefer
> structured output enforced at the tool level, as codex does with
> `--output-schema`). If no, it needs a stop-signal harvest path like Claude's.

### 11.5 Blessed Read-Only Transcript Surfaces

Three surfaces re-parse or re-display transcript/sentinel data for the operator without ever writing `Session.last_result`:

- **`cw dev-queue wait`** (`src/cw/cli/dev_queue/wait.py`) — blessed by a zero-hit scan: `tests/test_result_door_guard.py::TestReadOnlySurfacesNeverWrite` asserts no `last_result` assignment exists in this file. It re-parses transcripts for display only.
- **`queue_peek`** (`src/cw/queue_peek.py`) — same zero-hit scan coverage; re-parses transcripts for display only, never writes `Session.last_result`.
- **`.claude/skills/cw-followup/scripts/parse_sentinel.py`** — outside `src/cw/`, so the enforcement test structurally cannot cover it. Its blessing is doc-prose only, stated here: it parses a completed session's sentinel for the `/cw-followup` skill's disposition logic and never writes back to `Session.last_result`.
