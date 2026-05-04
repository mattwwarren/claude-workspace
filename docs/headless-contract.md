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
  "schema_version": 1,
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
| `schema_version` | int | Currently `1`. Bump rules in §8. |
| `ticket_id` | string | Linear ID, or synthetic for free-text invocations. |
| `status` | string enum | See §4. |
| `stage_reached` | string enum | Pipeline-stage marker. Closed set: `stage1_plan`, `stage2_impl`, `stage3_review`, `stage4a_merge_gate`, `stage4b_pr_create`, `stage5_post_create`. Producer and parser must keep this list in lockstep — adding a stage is a `schema_version` bump (see §8). |
| `scope.tier` | `"small"` \| `"large"` | Per the Guard Matrix in `commands/auto-dev.md`. |
| `scope.files` | int | File count touched (or planned, if exited pre-impl). |
| `scope.lines_estimate` | int | Plan-time line estimate. |
| `scope.lines_actual` | int \| null | Actual lines touched; `null` if exited before impl. |
| `scope.forbidden_touched` | bool | Whether any `--forbidden` area was touched. |
| `plan_source` | `"linear_existing"` \| `"generated"` \| `"free_text"` | How the plan was sourced. |
| `branch` | string \| null | Branch name; `null` if exited before branch creation (e.g. `plan_pending_approval`). |
| `worktree_path` | string \| null | Absolute path; `null` if no worktree was created. |
| `fork_point_sha` | string \| null | Base commit at branch creation. |
| `commits` | string[] | Commit SHAs created during this run. |
| `pr` | object \| null | `null` for all statuses except `shipped`. On the §5.1 downgrade (small + degraded → `review_pending_approval`), `branch` is non-null but `pr` is `null`. |
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

### 4.2 `blocker.reason` (when `status = "blocked"`)

```json
{"stage": "stage2_impl", "reason": "agent_block", "details": "<verbatim blocker text>"}
```

| Reason | Meaning |
|---|---|
| `impl_failed` | Implementation agent returned BLOCK or failed quality gates after 2 attempts. |
| `review_blocked` | MUST_FIX findings persisted after 5 fix-loop cycles (the hard cap). |
| `agent_block` | Any other agent returned friction level BLOCK that the pipeline could not auto-resolve. |

`blocker.reason` is an **open enum** — the producer may add new reasons without a `schema_version` bump. Consumers MUST treat unknown reasons as opaque strings and surface verbatim. (Unlike `status`, which is closed: see §4 and §8.)

### 4.3 `next_actions` Vocabulary

Advisory only. cw acts on these without parsing prose.

| Action | When | Consumer behavior |
|---|---|---|
| `wait_for_ci` | `status = shipped` (always — `shipped` implies auto-merge enabled per §4.1) | cw polls CI; on success the orchestrator job is done. |
| `user_approve_plan` | `status = plan_pending_approval` | Notify user that a large-scope plan is in Linear awaiting approval. |
| `user_approve_review` | `status = review_pending_approval` | Notify user that a branch is pushed for review. |
| `resolve_merge_gate` | `status = merge_gate_blocked` | Notify user that the prior pipeline PR must merge before re-dispatch. **No automatic resume** until §7 is specified. |

Empty list for terminal-reject (`scope_exceeded`, `forbidden_area`, `blocked`). For `shipped`, `next_actions` always contains `wait_for_ci`.

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

**Scan scope:** every agent that ran to completion in the pipeline — plan, impl, each reviewer in the parallel review fan-out, every fix-loop cycle (cycles 2–5 included), and prep-pr. A fix-loop cycle that itself reports degradation triggers `downgrade_applied`; this is independent from `fix_loop_escalated` (§5.2). If an agent did not emit a Health Check block at all, treat that agent as degraded — missing data is not healthy data.

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
5. **Unknown `status` value** — same as (4).
6. **Multiple complete sentinel blocks in one invocation's stdout** — skill bug (the contract is exactly one per invocation; see §3.1). Same handling as (1), with `reason: "multiple_result_blocks"` and `details` containing the count and the LAST block's raw payload.

Parser must NEVER act on a partial parse — if any of the above fire, treat the run as blocked and require human attention.

---

## 7. Resume Protocol (Reserved — Not Yet Specified)

Resume (`--resume`) is **not yet specified**. Tracked in global-claude#2 and cw#59. No contract exists until those issues ship.

Until then, cw must treat all non-terminal exits as fully manual recovery: the user re-invokes `/auto-dev` themselves or skips the ticket. Consumers MUST NOT implement speculative resume handling against any draft mechanism (file-injection, env var, etc.) — when this section is filled in, the contract will be authoritative and will not pre-honor guesses.

---

## 8. Versioning

`schema_version: 1` is the current contract.

**Bump to 2 required when:**
- Any field is removed or renamed.
- Any existing field's type or semantics change.
- A new value is added to a closed enum (`status`, `stage_reached`).
- A new optional field is added that consumers cannot ignore without a behavior change (e.g., a new `health.*` subfield that drives routing the way `health.downgrade_applied` does today).

**No bump required when:**
- A new purely-advisory optional field is added to `health`, `pr`, `review`, or the top level (one consumers may ignore with no behavior change).
- A new `next_actions` entry is added (parsers already treat unknown actions as advisory).
- A new `blocker.reason` value is added (open enum — see §4.2).

When bumping, update this doc, `commands/auto-dev.md`, and the cw parser in lockstep. The skill SHOULD coordinate rollout so it does not emit a higher `schema_version` than deployed parsers support; this is a deployment-process concern (no in-band negotiation mechanism exists today). Parsers MUST defensively reject unknown `schema_version` values per §6 (4).

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
