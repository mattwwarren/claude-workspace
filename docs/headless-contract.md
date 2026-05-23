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
| S3 fix-loop cycle 3+ OR scope growth at any cycle | Append to `friction_highlights`, set `health.fix_loop_escalated: true`, continue |
| Any other agent BLOCK (Plan / prep-pr / etc.) | EXIT `blocked` with `blocker.reason: "agent_block"` |
| S4a merge gate (small only — large already exited) | EXIT `merge_gate_blocked` if prior pipeline PR open |
| S4b PR creation, small | AUTO-CREATE with auto-merge |
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
  "schema_version": 2,
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
  "worktree_path": "/home/matthew/.cw/wt/abc/dev-gen-1234-fix-login",
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
| `schema_version` | int | Currently `2` (legacy `1` still accepted during the rollout window). Bump rules in §8. |
| `ticket_id` | string | Linear ID, or synthetic for free-text invocations. |
| `status` | string enum | See §4. |
| `stage_reached` | string enum | Pipeline-stage marker. Closed set: `stage1_pre_flight`, `stage1_plan`, `stage2_impl`, `stage3_review`, `stage4a_merge_gate`, `stage4b_pr_create`, `stage5_post_create`. Pre-flight exits (e.g. already-satisfied tickets) use `stage1_pre_flight`. Producer and parser must keep this list in lockstep — adding a stage is a `schema_version` bump (see §8). |
| `scope.tier` | `"small"` \| `"large"` | Per the Guard Matrix in `commands/auto-dev.md`. |
| `scope.files` | int | File count touched (or planned, if exited pre-impl). |
| `scope.lines_estimate` | int | Plan-time line estimate. |
| `scope.lines_actual` | int \| null | Actual lines touched; `null` if exited before impl. |
| `scope.forbidden_touched` | bool | Whether any `--forbidden` area was touched. |
| `plan_source` | `"linear_existing"` \| `"generated"` \| `"free_text"` \| `"none"` | How the plan was sourced. `"none"` is used for pre-flight exits where no plan was produced. |
| `branch` | string \| null | Branch name; `null` if exited before branch creation (e.g. `plan_pending_approval`). |
| `worktree_path` | string \| null | Absolute path; `null` if no worktree was created. |
| `fork_point_sha` | string \| null | Base commit at branch creation. |
| `commits` | string[] | Commit SHAs created during this run. |
| `pr` | object \| null | Non-null **only** when `status = shipped`. All other statuses — including `review_pending_approval` (whether reached via the large-scope path or the §5.1 downgrade), `merge_gate_blocked`, `plan_pending_approval`, the rejects, and `blocked` — leave `pr` as `null`. `branch` may still be non-null on these (see `branch` row). |
| `review.must_fix_initial` | int | MUST_FIX count from first review pass. |
| `review.should_fix` | int | SHOULD_FIX count carried out of the loop. |
| `review.fix_cycles_used` | int | 0 when first pass was clean. |
| `health` | object | See §5. |
| `friction_highlights` | string[] | Surfaced highlights from agent friction reports. |
| `blocker` | object \| null | See §4.2. Populated when `status = "blocked"`. |
| `next_actions` | string[] | Advisory list cw can act on without prose-parsing. See §4.3. |

---

## 4. Status Enum

The `status` field is a closed set. Consumers MUST treat unknown statuses as a parse error (skill version drift) and surface verbatim to the user.

### 4.1 Statuses

| Status | Meaning |
|---|---|
| `shipped` | PR created with auto-merge enabled; CI wait skipped. |
| `plan_pending_approval` | Large scope — plan generated and posted to Linear; no branch created; awaiting human approval. |
| `review_pending_approval` | Large scope — fix loop complete, branch pushed, no PR; awaiting human review approval. |
| `merge_gate_blocked` | Small scope — prior pipeline PR still open; cannot create next PR until gate clears. |
| `scope_exceeded` | `--scope-limit small` rejected a Large ticket before impl started. |
| `forbidden_area` | `--forbidden` constraint matched a planned file; ticket rejected before impl started. |
| `blocked` | Unrecoverable error mid-pipeline; see `blocker` field for details. |
| `no_op` | Pre-flight detected the ticket already satisfied (or otherwise not work-bearing); no plan, no branch, no PR. Introduced at `schema_version=2`. Rationale: terse, neutral wording — does not presume the cause (already-merged dupe, invalid ticket, code already covers it). The actor (skill / cw) decides what to do via `next_actions` (typically `close_issue_as_completed`). Emitted at `schema_version=3` with `stage_reached='stage1_pre_flight'` and `plan_source='none'`. (During the rollout window, parsers also accept this shape under `schema_version=2`.) |

### 4.2 `blocker.reason` (when `status = "blocked"`)

Minimum shape (v1+):

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
| `agent_block` | Any other agent returned friction level BLOCK that the pipeline could not auto-resolve. |

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

Empty list for terminal-reject (`scope_exceeded`, `forbidden_area`, `blocked`). For `shipped`, `next_actions` always contains `wait_for_ci`. `next_actions` is otherwise an open vocabulary — parsers MUST pass unknown actions through unchanged (do not act on them, do not reject the payload).

### 4.4 Recognized intermediate statuses (not in closed enum)

The producer emits two additional `status` values that fall outside the §4.1 closed enum. They represent **pre-dispatch human-attention** outcomes — the run halted before producing a branch because the planning agents surfaced something a human must disposition (a premise to verify, an ambiguity to resolve). They are observed in dogfood today; they are NOT in the closed enum because the routing semantics (treat as advisory pre-dispatch context, not a terminal pipeline outcome) differ from the canonical statuses.

| Status | Meaning | Payload signal |
|---|---|---|
| `premises_pending_verification` | The Plan Soundness Reviewer flagged premises the orchestrator could not auto-verify; the run halted with the premises listed for human disposition. | `premises` array is non-empty. Each entry carries at minimum a description of the premise (key may be `premise` or `claim`) and producer-supplied verification context (any of `verify_by` / `plan_depends_on_it_for` / `evidence_in_ticket` / `how_to_verify` / `verified` / `resolution`). Consumers MUST tolerate missing keys — the shape is producer-driven and not yet stabilized. |
| `ambiguities_pending_resolution` | Ambiguity scan returned items that exceeded the auto-resolve threshold; the run halted with the ambiguities listed for human disposition. | `ambiguities` array is non-empty. Each entry typically carries `question`, `plan_assumption`, `alternatives`, `why_it_matters`, `ticket_evidence`; treat all keys as best-effort. |

**Parser behavior:** §6 (5) applies — unknown `status` values route through synthetic `BlockedResult` with `reason=status_unknown`. The producer's literal status string is preserved in `blocker.details` (`got status='<value>'; surface verbatim, do not auto-route`). Consumers that need the full payload re-extract the sentinel block via `extract_block` and read it as raw JSON.

**Consumer guidance:** Skills that want to handle these (e.g. `/cw-followup`, `/cw-validate-result`) MUST NOT key off the typed `result.status` — they should re-parse the raw sentinel block, recognize the two values, and route via the `premises` / `ambiguities` arrays directly. Treat the arrays as the first-class signal; the `status` string is the producer's label for the same condition.

**Promotion to v4 (future):** Promoting either to a canonical status is a `schema_version` bump per §8 (closed-enum addition rule). When that happens, the cross-field invariants are obvious from the table above (e.g. `status='ambiguities_pending_resolution'` requires `ambiguities` non-empty). Tracked in #191.

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
| `lowest_agent_confidence` | `HIGH` \| `MEDIUM` \| `LOW` — minimum across all agent reports. |
| `any_incomplete_risk` | `true` if any agent reported `MAYBE` or `YES` for `Could work be incomplete?`. |
| `shortcuts` | Flat list of all `Shortcuts taken under pressure` entries across agents. |
| `recommendation` | `PROCEED` if all agents recommended PROCEED; otherwise `EXIT_FOR_HUMAN_REVIEW`. |
| `downgrade_applied` | `true` only when the §5.1 rule actually downgraded `shipped` → `review_pending_approval`. |
| `fix_loop_escalated` | `true` when the fix loop tripped cycle 3+ or scope-grew at any cycle (see gate row in §2). Independent from `downgrade_applied`. |

`downgrade_applied` and `fix_loop_escalated` are distinct signals — a run can have either, both, or neither.

---

## 6. Failure Modes for the Parser

The skill can fail to emit a complete sentinel block. cw must handle:

1. **No `<<<AUTO_DEV_RESULT` sentinel anywhere** — skill crashed before the emit step, or the run was not headless. Treat as `blocked` with synthetic blocker `{stage: "unknown", reason: "no_result_emitted", details: <last-N-lines-of-stdout>}`.
2. **Opening sentinel present, closing sentinel missing** — skill crashed mid-emit. Same handling as (1).
3. **Block present but JSON does not parse** — skill bug. Same handling as (1); include the raw block in `details`.
4. **`schema_version` higher than parser supports** — skill upgraded ahead of cw. Surface verbatim and refuse to act on `next_actions`; do not auto-merge or auto-route.
5. **Unknown `status` value** — same as (4). Two producer-emitted values (`premises_pending_verification`, `ambiguities_pending_resolution`) are recognized as pre-dispatch human-attention states; see §4.4 for consumer-side handling.
6. **Multiple complete sentinel blocks in one invocation's stdout** — skill bug (the contract is exactly one per invocation; see §3.1). Same handling as (1), with `reason: "multiple_result_blocks"` and `details` containing the count and the LAST block's raw payload.

Parser must NEVER act on a partial parse — if any of the above fire, treat the run as blocked and require human attention.

---

## 7. Resume Protocol (Reserved — Not Yet Specified)

Resume (`--resume`) is **not yet specified**. Tracked in global-claude#2 and cw#59. No contract exists until those issues ship.

Until then, cw must treat all non-terminal exits as fully manual recovery: the user re-invokes `/auto-dev` themselves or skips the ticket. Consumers MUST NOT implement speculative resume handling against any draft mechanism (file-injection, env var, etc.) — when this section is filled in, the contract will be authoritative and will not pre-honor guesses.

---

## 8. Versioning

`schema_version: 3` is the current contract. Parsers also accept `schema_version: 1` and `schema_version: 2` during the rollout window.

**Version history:**

| Version | Changes |
|---|---|
| 1 | Initial contract. |
| 2 | Added `no_op` status (§4.1) and `close_issue_as_completed` advisory action (§4.3). v1-tagged payloads with `status=no_op` are rejected as `validation_failed`. |
| 3 | Added `stage1_pre_flight` value to `stage_reached` enum (§3.3) and `none` value to `plan_source` enum (§3.3). Used together for pre-flight no_op exits. Parsers also accept this pair under v2 as a one-time rollout exception (the skill emitted them at v2 before the parser caught up — see #103). |

**Bump required when:**
- Any field is removed or renamed.
- Any existing field's type or semantics change.
- A new value is added to a closed enum (`status`, `stage_reached`).
- A new optional field is added that consumers cannot ignore without a behavior change (e.g., a new `health.*` subfield that drives routing the way `health.downgrade_applied` does today).

**No bump required when:**
- A new purely-advisory optional field is added to `health`, `pr`, `review`, or the top level (one consumers may ignore with no behavior change).
- A new `next_actions` entry is added (parsers already treat unknown actions as advisory).
- A new `blocker.reason` value is added (open enum — see §4.2).

**Cross-version status compatibility:** A status introduced at version N is invalid under any `schema_version < N`. Parsers MUST reject mismatched payloads (e.g., v1 + `no_op` → `validation_failed`). **Exception (one-time):** `stage_reached='stage1_pre_flight'` and `plan_source='none'` are accepted under both v2 and v3. This is documented under v3 in the table above; the v2 acceptance covers in-flight skill emissions that predate the parser's v3 awareness.

When bumping, update this doc, `commands/auto-dev.md`, and the cw parser in lockstep. **Order matters:** the parser must accept the new version BEFORE the skill emits it, otherwise in-flight emissions land in deployed parsers that don't recognize them. Parsers MUST defensively reject unknown `schema_version` values per §6 (4).

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
