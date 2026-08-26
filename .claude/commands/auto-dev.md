---
description: "Automated Linear→plan→implement→review→ship pipeline with scope-based approval automation"
argument-hint: "[<linear-issue-id> | --cycle <cycle> --project <project> --label <label> --team <team> --state <state> --assignee <me> --priority <N>] [--branch-prefix <prefix>] [--scope-limit small] [--forbidden <areas>] [--headless] [--resume <ticket>]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Edit", "Agent", "AskUserQuestion", "Skill"]
---

# Auto Dev Pipeline

Automated Linear→plan→implement→review→ship for development tickets.
Main session orchestrates. All work delegated to agents. Friction surfaced at checkpoints.
Scope determines which approvals are automatic vs manual.

**Arguments:** "$ARGUMENTS"

---

## Tracker Resolution

**Delegated to `auto-dev-intake.md`.** The tracker resolution logic (project-config.yaml lookup, operation mapping table, github-issues hard rules) is defined once in `.claude/commands/auto-dev-intake.md`. Read and follow those instructions. Do not copy or paraphrase the logic here.

---

## Pipeline Flow

```mermaid
flowchart TD
    Start([/auto-dev invoked]) --> S0[Stage 0: Linear Intake]
    S0 --> Mode{Single or Batch?}
    Mode -->|Single ID| Hygiene[PR Hygiene Sweep]
    Mode -->|Batch filters| Select[Select tickets] --> Hygiene

    Hygiene --> S1[Stage 1: Plan]
    S1 --> PlanSrc{Plan in Linear?}
    PlanSrc -->|Yes| Ambig[Ambiguity Scan]
    PlanSrc -->|No| AskPlan[AskUserQuestion: approve plan?]
    AskPlan --> Ambig
    Ambig --> AmbigFound{Ambiguities?}
    AmbigFound -->|Yes| AskAmbig[AskUserQuestion: resolve each] --> ScopeCheck
    AmbigFound -->|No| ScopeCheck{Scope tier?}

    ScopeCheck -->|Small: ≤10 files, ≤500 lines| S2Small[Stage 2: Implement auto]
    ScopeCheck -->|Large| AskImpl[AskUserQuestion: approve impl?]
    AskImpl --> S2Large[Stage 2: Implement]
    S2Small --> S3[Stage 3: Review]
    S2Large --> S3

    S3 --> Findings{Review findings?}
    Findings -->|Clean / SHOULD_FIX Small| S4[Stage 4: PR Creation]
    Findings -->|Clean / SHOULD_FIX Large| AskReview[AskUserQuestion] --> S4
    Findings -->|MUST_FIX| AskFix[AskUserQuestion: fix loop] --> S2Large

    S4 --> AskPR[AskUserQuestion: create PR?]
    AskPR --> S5[Stage 5: CI Wait]
    S5 --> CIResult{CI result?}
    CIResult -->|Green| Done([PR merge-ready])
    CIResult -->|Red| AskCI[AskUserQuestion: fix CI?] --> S2Large

    classDef stage fill:#1e3a5f,stroke:#4a90e2,color:#fff
    classDef checkpoint fill:#5c3317,stroke:#d4a017,color:#fff
    class S0,S1,S2Small,S2Large,S3,S4,S5,Hygiene,Ambig stage
    class AskPlan,AskImpl,AskReview,AskFix,AskPR,AskCI,AskAmbig checkpoint
```

Checkpoints (amber) are automation gates — AUTO-SKIP for Small + Linear-authored plans, AskUserQuestion otherwise. See **Guard Matrix** below for the full rule set.

---

## Headless Mode

Pass `--headless` to run the pipeline with no interactive prompts. Every `AskUserQuestion` call is replaced by the deterministic action in the gate-collapse table below.

**No interactive escalation, ever (#1890).** In headless mode there is no listener. Never escalate a detected blocker by asking a question — via `AskUserQuestion` or an unprompted question in prose — and ending your turn; that leaves the session silently parked with nothing for the orchestrator to route (the #1750 failure shape: a correct blocker detection wasted by the wrong channel). Escalate every blocker exclusively via the sentinel's `blocker` field with `status: "blocked"`, using the gate-collapse table's deterministic action for whichever gate is firing.

**Purpose:** autonomous orchestrator dispatch from `cw` (`mattwwarren/claude-workspace`). Issues cw#56–#59 are built against this contract and consume the structured output defined in the Appendix.

**Cross-repo spec:** [`claude-workspace/docs/headless-contract.md`](https://github.com/mattwwarren/claude-workspace/blob/main/docs/headless-contract.md) reformulates this section + the Appendix as a parser-implementer reference. This file (`commands/auto-dev.md`) is the producer source of truth; the spec doc is the link target for cw and any other consumer.

**Philosophy:** the human only sees the diff after the machine has done all deterministic cleanup it can. Two gates remain:
- Plan approval (large scope only) is the only "before work" gate.
- Review approval (large scope only) is the only "after work" gate.
Everything else runs to completion or exits with a structured error.

**Health aggregation rule:** If any spawned agent returns `On-spec confidence: LOW`, `Could work be incomplete?: MAYBE` or `YES`, or `Recommendation: EXIT_FOR_HUMAN_REVIEW`, downgrade the outcome:
- Small + clean review + all agents healthy → `shipped`
- Small + clean review + any degraded agent → downgrade to `review_pending_approval` (branch pushed, no PR)
- Large path is unchanged (already exits at S3), but include the full health summary in the result payload.
- When this rule downgrades the status, set `health.downgrade_applied: true` in the structured output. (Distinct from `health.fix_loop_escalated` which signals fix-loop escalation events — see Step 3b.5.)

**Agent spawn rule:** every agent prompt in headless mode MUST include BOTH the Friction Protocol block AND the Health Check block. Implementation and fix-loop agents MUST additionally include the Completion Artifacts block (see Subagent Reliability Mitigations section). Every spawn MUST set a wall-clock timeout per Mitigation 4 (recommended: 30m for impl, 15m for fix/review, 10m for lightweight); a non-returning task is treated as `agent_block`, not a hang.

**Out of scope in headless mode:**
- Batch mode (multi-ticket selection) — stays interactive-only; `--headless` with batch filters is undefined behavior
- PR Hygiene Sweep (Steps H1, H2, H3) — stays interactive-only
- Stage 5b feedback fix agent — stays interactive-only

### Gate-Collapse Table

The rows below define the deterministic headless action for every interactive gate in the pipeline:

> **Maintenance:** the `expected 2` and `hard-cap at 5` cycle values appear in 6 locations across this file. See the maintenance note in Step 3b.5 for the full sync list before editing the cycle-related rows here.

| Stage / gate | Headless behavior |
|---|---|
| Pre-flight, local main not in sync with origin/main | EXIT `blocked` with `blocker.reason: "local_main_diverged_from_origin"`, `retry_eligible: true`, `next_actions: ["sync_local_main"]` (see Pre-flight section) |
| S1 plan, plan in Linear | AUTO-SKIP plan-approval question (ambiguity scan still runs) |
| S1 plan, no Linear plan, small | Generate → AUTO-APPROVE (ambiguity scan still runs) |
| S1 plan, no Linear plan, large | Generate → EXIT `plan_pending_approval` through the consolidated park (#1650): one `## Pending Verification Scan` comment carrying advisory Step-1f findings, `### Approval requested`, and the full draft; draft persisted to `.cw/plan-draft.md`; no branch |
| S1 ambiguity scan, no ambiguities | AUTO-CONTINUE |
| S1 ambiguity scan, ambiguities found (parked) | EXIT `ambiguities_pending_resolution` through the consolidated park (#1650): parked questions + advisory Step-1f findings (+ `### Approval requested` when Large) + draft, ONE comment; draft persisted; no branch |
| S1 ambiguity scan, non-empty `PREMISES TO VERIFY` (unverified) | EXIT `premises_pending_verification` through the consolidated park (#1650): unverified premises (+ parked ambiguities) + advisory Step-1f findings + draft, ONE comment; draft persisted; no branch — verification is human/investigation work, not a plan revision |
| S1 pre-flight finds ticket already satisfied | EXIT `no_op` (no branch, `next_actions: ["close_issue_as_completed"]`) |
| S1 plan review, both markers present + current | AUTO-SKIP (no reviewer spawn, no marker re-append) |
| S1 spec review, NO_ISSUES / SHOULD_FIX / PRINCIPLE only | Append `plan-spec-reviewed` marker, continue |
| S1 spec review, MUST_FIX, 1st cycle | Spawn plan-revision agent (Step 1f.4), re-review once |
| S1 spec review, MUST_FIX persists after 1 revision | EXIT `blocked` with `blocker.reason: "plan_unreviewable"` (also posts blocking findings as a tracker comment, #1815) |
| S1 soundness review, NO_ISSUES | Append `plan-soundness-reviewed` marker, continue |
| S1 soundness review, RISK (interactive) | AskUserQuestion per finding: acknowledge / treat as MUST_FIX / codify as §7. Then append marker |
| S1 soundness review, RISK (headless) | Write `codify:` lessons to the wiki inbox + `friction_highlights`, append marker, continue (advisory — never blocks) |
| S1 soundness review, MUST_FIX, interactive | AskUserQuestion: revise approach / override / skip ticket. On "revise" → Step 1f.4 |
| S1 soundness review, MUST_FIX, headless OR persists after 1 revision | EXIT `blocked` with `blocker.reason: "plan_unsound"` (also posts blocking findings as a tracker comment, #1815) |
| S1 plan reviewer agent BLOCK (either station) | EXIT `blocked` with `blocker.reason: "agent_block"` |
| S1 tier re-verification (Step 1g.0), stamped tier stale vs. current classification | EXIT `blocked` with `blocker.reason: "scope_tier_stale"` (also posts blocking findings as a tracker comment, #1897) |
| S1 scope-limit hit | EXIT `scope_exceeded` |
| S1 forbidden-area hit | EXIT `forbidden_area` |
| S2 impl checkpoint (any scope) | AUTO-CONTINUE — never gate |
| S2 BLOCK or 2x failure | EXIT `blocked` with `blocker.reason: "impl_failed"` |
| S2.5 branch not pushed (`origin/<branch>` absent after fetch) | EXIT `blocked` with `blocker.reason: "impl_not_pushed"`. Do NOT spawn reviewers |
| S2.5 completion gate fails (diff empty, test fail) | EXIT `blocked` with `blocker.reason: "impl_failed"`, `blocker.details: "Step 2.5 gate <N>: <output>"`. Do NOT spawn reviewers |
| S2.5 missing planned files | Treat as missing work; route to impl retry (counts against 2x failure budget) |
| S2.5 files outside plan, within threshold | Append `"impl_scope_growth: <files>"` to `friction_highlights`; continue (routes through existing scope-growth handling) |
| S2.5 files outside plan, threshold exceeded (`check_plan_scope_conformance.py` exit 1 **with a valid JSON verdict**) | EXIT `blocked` with `blocker.reason: "plan_scope_drift"`, `blocker.details` enumerating the unplanned paths (routes to BLOCKED_ON_USER; not finalize). Do NOT spawn reviewers |
| S2.5 scope-conformance script exit 1 **without** a valid JSON verdict (tooling failure, not drift) | EXIT `blocked` with `blocker.reason: "impl_failed"`, `blocker.details: "Step 2.5 gate 2: scope-conformance script exited 1 without producing a valid verdict — treating as tooling failure, not drift: <stderr/stdout excerpt>"`. Do NOT spawn reviewers |
| S2.5 scope-conformance script exit 2 (plan has no parseable `## Files Modified`) | Append `"impl_scope_conformance_unparsed: <stderr>"` to `friction_highlights`; continue (fail-open — a plan the gate cannot read is not an implementation failure) |
| S2 / S3b agent timeout (Mitigation 4) | EXIT `blocked` with `blocker.reason: "agent_block"`, `blocker.details: "agent timed out after <N>m"` |
| S2 / S3b single-commit on non-trivial change | Append `"impl_no_incremental_commits"` to `friction_highlights`; continue (advisory only) |
| S3 review (any scope) | Always run reviewers, then adjudicate every finding into FIX NOW / REJECT / DEFER (Checkpoint 3a) |
| S3 action list non-empty (post-adjudication) | Run fix loop on the action list (accepted MUST_FIX + SHOULD_FIX); expected 2 cycles, hard-cap at 5 |
| S3b fix gate fails (test/mypy/diff mismatch / no new commits) | Count as cycle failure; re-spawn within 5-cycle cap; append `"fix_loop_gate_failed_cycle_<N>"` |
| S3 fix-loop, Small + sparse fix (Step 3b.5 criteria all hold) | Skip re-review → S4. Append `"rereview_skipped_sparse"` to `friction_highlights` |
| S3 action list empty (every finding fixed / rejected / deferred), small | AUTO-CONTINUE → S4. Rejections recorded in PR body + `friction_highlights`; deferrals queued for merge-time ticketing (Step H3) |
| S3 large scope, action list resolved (clean, or fix loop complete) | EXIT `review_pending_approval` (post-fix-loop diff, branch pushed, no PR); adjudication applies within the human review |
| S3 action list non-empty after 5 fix cycles | EXIT `blocked` with `blocker.reason: "review_blocked"` (also posts blocking findings as a tracker comment, #1815) |
| S3 non-deferrable plan-deviation finding survives fix loop or judged beyond its scope | EXIT `blocked` with `blocker.reason: "plan_deviation"` (routes to BLOCKED_ON_USER; not finalize; also posts the blocking findings that caused the exit as a tracker comment, #1817) |
| S3 accepted MUST_FIX finding whose remedy is outside the diff (`no_diff_anchor`) | EXIT `blocked` with `blocker.reason: "review_operator_actionable"` (routes to BLOCKED_ON_USER; not finalize; posts the operator checklist as a tracker comment, #1817) |
| S3 fix-loop cycle 3+ OR scope growth at any cycle | Append to `friction_highlights`, set `health.fix_loop_escalated: true`, continue |
| Any other agent BLOCK (Plan / prep-pr / etc.) | EXIT `blocked` with `blocker.reason: "agent_block"` |
| Tool call denied by auto-mode classifier (any stage) | EXIT `blocked` with `blocker.reason: "tool_denied"`, `retry_eligible: true`, `next_actions: ["redispatch_ticket"]` (see Tool-Use Denial Exit section) |
| S4a merge gate (small only — large already exited) | EXIT `merge_gate_blocked` if prior pipeline PR open |
| S4b PR creation, small | AUTO-CREATE with auto-merge |
| S4d UI Evidence Gate, no UI files OR media present in body | AUTO-CONTINUE — enable auto-merge |
| S4d UI Evidence Gate, UI files present + body missing media | Skip auto-merge enable; append `"ui_evidence_missing"` to `friction_highlights`; set `pr.auto_merge: false`; set `next_actions: ["attach_ui_evidence_and_enable_automerge"]`; continue to Post-to-Linear |
| S5 CI wait | AUTO-SKIP — return immediately after auto-merge enabled. CI watching = orchestrator concern |
| Invoked with `--resume <ticket>` | Run `detect_current_stage()` and jump to the detected stage's entry point; emit `resumed_from_stage: "<stage>"` in the structured output. Skip Pre-flight and S0 (ticket already known). Not a blocker — informational only. If detector returns `pre_flight`, behave as a normal `--headless` run on `<ticket>` |
| Headless invocation without `--resume` (every queue re-dispatch) | Implicit resume (#1652): run `detect_current_stage()` first; unambiguous durable signals → enter at the detected stage exactly as `--resume` would (emit `resumed_from_stage`); conflicting/ambiguous signals → enter Stage 1 as before. See Resume Detection §Headless implicit resume |
| S4 PR-exists check, branch already has open PR | Skip Step 4a/4b/4c; proceed to Step 4d (auto-merge enable + Linear comment if missing) and S5. Append `"pr_reused_existing"` to `friction_highlights` |
| S4 PR-exists check, branch has closed (unmerged) PR | EXIT `blocked` with `blocker.reason: "pr_already_terminal"`, `retry_eligible: false` — closed PR represents human decision; pipeline must not create a duplicate |
| Trailing /schedule asks | suppress |

---

## Resume Detection

Auto-dev runs `detect_current_stage(ticket_id)` at the top of every invocation. The detector is **pure derivation** from external state (Linear comments, git branch + commit trailers, GitHub PR fields). No `.auto-dev-state.json` or equivalent file is written — the branch, the Linear issue, and the PR are the state store.

### `--resume <ticket>` flag

Pass `--resume <ticket>` to resume a previously-started pipeline from the latest detected stage. Skips earlier stages entirely — no re-planning, re-implementing, or re-reviewing of work already marked complete via durable signals (Linear plan markers, branch commit trailers, PR state).

Combines with `--headless` for non-interactive resume (e.g. cron-driven retry of a ticket that previously exited `plan_pending_approval` and has since received approval). In **interactive mode** without `--resume`, the detector still runs but is informational — used inside each stage to short-circuit redundant work (e.g. S1's existing marker-skip at the plan-reviewer step), not to jump stages.

### Headless implicit resume (#1652)

**Headless invocations always enter via the detector.** Every `--headless` run — including every queue re-dispatch, with or without an explicit `--resume` flag — runs `detect_current_stage()` first and, when the durable signals are unambiguous, enters at the latest detected stage, identical to explicit `--resume` (skip earlier stages, emit `resumed_from_stage: "<stage>"`). A queue re-dispatch after a park is a continuation of the same ticket, not a fresh start: without this, a re-dispatch re-enters Stage 1 against a branch whose implementation already shipped, re-verifies merged code, and re-posts questions already open on the ticket — a pure waste round plus tracker noise (observed: a full 10-minute Stage-1 re-run whose own friction report stated "this Stage-1 pass ran after Stage 2 already shipped").

- **Unambiguous signal set → authoritative.** The detected stage's signals must be internally consistent with every earlier stage having completed: a branch claiming S2+ has current plan markers behind it; commits claiming S3 carry the `Auto-Dev-Stage: impl-complete` trailer; a PR claiming S4+ sits on the detected branch. A clean signal set jumps.
- **Signal conflict or ambiguity → fall back to Stage 1 as today.** Examples: a branch exists but the plan markers are absent or stale; branch commits carry no pipeline trailers; PR and branch disagree. Ambiguity favors the conservative start — re-running Stage 1 wastes minutes, while a wrong jump ships unreviewed work.
- **Stage-entry gates are unchanged.** Entering a stage via implicit resume honors that stage's own guards exactly as explicit `--resume` does (idempotency requirement below, per-stage Pre-Stage Detector Guards, approval gates). Implicit resume changes where the pipeline starts, never what it may skip within a stage.
- **Interactive mode without `--resume` keeps current behavior** (detector informational only) — a human watching a fresh invocation expects a fresh start unless they asked to resume.

### Durable signals

| Signal | Stored in | Meaning |
|---|---|---|
| `<!-- plan-quality-reviewed -->` + `<!-- plan-soundness-reviewed -->` markers | Linear plan comment | S1 reviewers ran; markers current |
| Approval comment on plan thread (human reply, or auto-approved-small flag) | Linear | S1 → S2 gate cleared |
| Branch exists on `origin/<branch>` | git | S2 started |
| Commit trailer `Auto-Dev-Stage: impl-complete` on a branch commit | git | S2 done; S3 may start |
| Commit trailer `Auto-Dev-Fix-Cycle: <N>` on a branch commit | git | S3 fix-loop cycle N reached |
| PR open for branch | GitHub | S4 done |
| PR checks state | GitHub | S5 substage |
| PR merged | GitHub | terminal |

### Stage states returned by the detector

`pre_flight` · `s1_drafting` · `s1_pending_ambiguity_resolution` · `s1_pending_human_approval` · `s1_plan_approved` · `s2_implementing` · `s3_review_pending` · `s3_fix_loop` (with substage `cycle_N`) · `s4_pr_open` · `s5_ci_pending` · `s5_ci_passed` · `s5_ci_failed` · `merged`

### Detection algorithm (highest stage wins)

1. **`gh pr view <branch>`** — if PR is MERGED → `merged`. If PR is OPEN, inspect checks: any failing → `s5_ci_failed`; all passing → `s5_ci_passed`; running → `s5_ci_pending`; none yet → `s4_pr_open`.
2. **Branch exists, no PR** — parse commit trailers on branch (commits unique vs `origin/main`). If `Auto-Dev-Stage: impl-complete` trailer present:
   - Any `Auto-Dev-Fix-Cycle: <N>` trailers on commits *newer than* impl-complete → `s3_fix_loop`, substage `cycle_max(N)`.
   - Otherwise → `s3_review_pending`.
   - No impl-complete trailer → `s2_implementing` (resume from current branch HEAD; do not reset).
3. **No branch, Linear plan comment present** — both quality+soundness markers present:
   - Scope `small` or approval comment found → `s1_plan_approved`.
   - Otherwise → `s1_pending_human_approval`.
   - Markers missing → `s1_drafting`.
4. **No branch, Linear ambiguity comment present** — user responses to ambiguities present → `s1_drafting`; otherwise → `s1_pending_ambiguity_resolution`.
5. **Nothing started** → `pre_flight`.

### Stage idempotency requirement

Every stage entry point MUST be idempotent. Calling S4 when a PR already exists must reuse the PR, never duplicate-create. Calling S2 when the branch already has commits must resume from HEAD, never reset. Per-stage Pre-Stage Detector Guard subsections enforce this — see S2 §Pre-Stage Detector Guard, S4 §Pre-Stage Detector Guard.

---

## Friction Protocol

Every agent prompt MUST end with this block verbatim:

```
FRICTION PROTOCOL — include this section at the END of your response:

## Friction Report
- **Level**: NONE | INFO | WARN | BLOCK
- **Scope**: N files changed, ~M lines
- **Assumptions**: [decisions you made without explicit guidance — if none, say NONE]
- **Deviations**: [anything done differently from plan/instructions — if none, say NONE]
- **Discoveries**: [unexpected findings: related bugs, dead code, schema surprises, scope creep risk — if none, say NONE]
- **Risks**: [shared code touched, interface changes, potential downstream breakage — if none, say NONE]

BLOCK level means you cannot proceed without user input. Explain what you need.
```

---

## Health Check Protocol

Every agent prompt in headless mode MUST ALSO end with this block, appended after the Friction Report:

```
## Health Check
- **Context usage**: <rough % or HIGH/MEDIUM/LOW>
- **On-spec confidence**: HIGH | MEDIUM | LOW
- **Shortcuts taken under pressure**: [list or NONE]
- **Could work be incomplete?**: NO | MAYBE | YES (explain)
- **Recommendation**: PROCEED | EXIT_FOR_HUMAN_REVIEW
```

The main session aggregates these reports per the rule documented in the Headless Mode section above. In interactive mode the block is optional but recommended.

---

## Worker Execution Discipline

Every agent prompt in headless mode MUST ALSO include:

1. Wrap gh/git push-fetch-pull/curl Bash calls in a wall-clock guard:
   `timeout 120 <cmd>`. An unbounded network call in a headless worker
   has no human to interrupt it.
2. WebFetch of external documentation sites is prohibited during
   headless runs (codifying the #930 precedent). Workers must rely on
   local repo context, tracker content already fetched, or ask for
   escalation rather than fetching arbitrary external URLs mid-run.

---

## Tool-Use Denial Exit

The Claude Code auto-mode classifier can deny a tool call mid-pipeline (typical case: external-system writes like `gh issue comment` under the agent's identity). In interactive mode the human re-authorizes; in headless mode there is no human and no retry path, so an undirected denial produces a silent stall until the Layer 1 backstop times the session out (~30 min, see claude-workspace#176).

**Detection.** On every `tool_result` block with `is_error: true`, check whether the content begins with the literal phrase:

> `Permission for this action was denied by the Claude Code auto mode classifier`

Match is a case-sensitive substring check against the tool_result content. The classifier's deny message is structured and stable; do NOT loosen the match to "denied" alone (that would catch normal exit-code-1 errors).

**Action (headless).** On match, emit the `blocked` sentinel in the **same parent turn** as the denied tool result, before any other prose, then exit. Do NOT attempt to work around the denial with a different tool — the orchestrator's job is to re-dispatch under fresh classifier conditions (claude-workspace#183 — classifier non-determinism), not to evade.

**Sentinel shape:**

```json
{
  "schema_version": 4,
  "status": "blocked",
  "stage_reached": "<whichever stage was running>",
  "blocker": {
    "stage": "<same as stage_reached>",
    "reason": "tool_denied",
    "tool_name": "<Bash | Edit | Write | ...>",
    "denial_reason": "<verbatim classifier reason text after `Reason:`>",
    "details": "<verbatim full denial message>",
    "recovery_hint": "Re-dispatch the ticket; the auto-mode classifier is non-deterministic and may permit the same operation on a fresh session. If denial is reproducible, the operation belongs off the agent identity.",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": ["redispatch_ticket"]
}
```

`retry_eligible: true` by default — the classifier is the source of non-determinism, not the plan or the impl. If a future denial form indicates a hard policy refusal (not yet observed), set `retry_eligible: false` and leave the orchestrator to surface to the human.

**Action (interactive).** The denial appears verbatim to the user in the normal tool-result stream; no skill action is required. The user re-authorizes or alters the operation as they would for any classifier prompt.

**Precedent.** This is the analogue of the Stage 4 `merge_gate_blocked` exit (a classifier-style gate that exits cleanly rather than stalling). Tool denial is the cross-stage version of the same principle.

---

## Guard Matrix

Two independent axes control approval automation.

**Axis 1 — Plan source (determines plan APPROVAL checkpoint, Checkpoint 1):**

| Condition | Plan Approval Checkpoint |
|-----------|----------------|
| Plan found in Linear issue (or issue description is sufficient) | AUTO-SKIP — pre-approved by authoring it |
| No plan / partial plan only | AskUserQuestion |

**Axis 1b — Plan signoff markers (determines plan REVIEW gate, Step 1f):**

Two independent markers, one per station — `<!-- plan-spec-reviewed: YYYY-MM-DD vN -->` and `<!-- plan-soundness-reviewed: YYYY-MM-DD vN -->`. For each:

- **Marker present + version current** → AUTO-SKIP that station. Cheap grep, no agent spawn.
- **Marker absent OR version stale** → spawn that station; gate per Step 1f.3.

A marker is appended only when its station clears (NO_ISSUES, SHOULD_FIX/PRINCIPLE-only, an acknowledged RISK, or an explicit human override). A blocking MUST_FIX never appends a marker — the plan must be revised first. Versions are bumped per station independently to invalidate that station's markers without forcing a re-review of the other.

**Axis 2 — Scope tier (determines impl + review checkpoints):**

| Tier | Criteria |
|------|----------|
| **Small** | ≤10 files AND ≤500 lines AND no forbidden-area touches |
| **Large** | >10 files OR >500 lines OR touches forbidden areas |

| Checkpoint | Small Scope | Large Scope |
|------------|-------------|-------------|
| Implementation | AUTO-ACCEPT (log summary) | AskUserQuestion |
| Review (clean / SHOULD_FIX only) | AUTO-ACCEPT | AskUserQuestion |
| Review (MUST_FIX) | AskUserQuestion (always) | AskUserQuestion (always) |
| PR creation | AskUserQuestion (always) | AskUserQuestion (always) |

---

## Pre-flight: Origin Sync Check

**Delegated to `auto-dev-intake.md`.** The origin sync check (Steps P1, P2, P3) is defined once in `.claude/commands/auto-dev-intake.md`. Read and follow those instructions.

---

## Stage 0: Ticket Intake

**Delegated to `auto-dev-intake.md`.** Read and follow the instructions in `.claude/commands/auto-dev-intake.md`. This stage fetches the ticket, checks origin sync, resolves the tracker, and materializes `.cw/context.json`.

After intake completes, proceed to PR Hygiene Sweep, then Stage 1.

---

## PR Hygiene Sweep (runs before each ticket)

Before starting work on a new ticket, check all open pipeline PRs for issues that need attention. This prevents PR backlog from accumulating while the pipeline is heads-down on new work.

**When:** At the start of each ticket in the queue (before Stage 1). Skip for the very first ticket (no prior PRs exist yet).

### Step H1: Scan Open Pipeline PRs

```bash
gh pr list --author @me --state open --json number,title,headRefName,reviewDecision,mergeable,mergeStateStatus
```

Filter for branches matching the pipeline's naming pattern (`<branch-prefix>/*`).

For each open pipeline PR, gather status:
```bash
gh pr checks <number> --json name,state,conclusion
gh api repos/{owner}/{repo}/pulls/<number>/reviews --jq '.[] | select(.state == "CHANGES_REQUESTED") | {user: .user.login, state: .state, body: .body}'
gh api repos/{owner}/{repo}/pulls/<number>/comments --jq '.[] | {user: .user.login, path: .path, line: .line, body: .body, created_at: .created_at}'
```

### Step H2: Report and Triage

**If all open PRs are healthy** (CI passing or pending, no changes requested, mergeable): Log "All N open PRs healthy" and proceed to Stage 1.

**If any PR needs attention:**

**AskUserQuestion:**
```
PR Hygiene Check — N open pipeline PRs:

PR #<A> (<title>): ✅ CI passing, awaiting merge
PR #<B> (<title>): ❌ CI failing — <failed check>
PR #<C> (<title>): 🔄 Changes requested by <reviewer>
  - <file>:<line> — "<comment>"
  - ...
PR #<D> (<title>): ⚠️ Merge conflicts with main

Options:
1. Fix all — address CI failures, review feedback, and conflicts before starting next ticket
2. Fix critical — fix CI failures and conflicts only (skip review feedback for now)
3. Skip — proceed to next ticket (handle PR issues later)
4. Cherry-pick — tell me which PRs to fix (e.g., "fix #B and #C")
```

- **Fix all / Fix critical / Cherry-pick** → For each PR being fixed:
  - **CI failure:** Spawn agent in that PR's branch to investigate and fix. Push. Wait 10m for CI (per Stage 5 protocol).
  - **Changes requested:** Surface full feedback, apply fixes, push, reply to comments. Wait 10m for CI.
  - **Merge conflicts:** Fetch main, merge, resolve conflicts, re-run quality gates, push. Wait 10m for CI.
  - After all fixes: re-scan to confirm health, then proceed to Stage 1.
- **Skip** → Proceed to Stage 1 for the next ticket.

**Note:** This sweep is intentionally lightweight — just API calls. The heavier fix work happens only when issues are found. The goal is to catch and clear PR debt early so it doesn't pile up.

### Step H3: Harvest deferred review findings from merged PRs

The pipeline records deferred review findings in the PR body (Stage 4 Step 4d) but cannot file them at merge time — the session is gone, especially under `auto_merge: false`. This step is the merge-triggered filing: it runs as part of the per-ticket sweep and scans **recently-merged** pipeline PRs for an un-harvested `DEFERRED-REVIEW-FINDINGS` block. Label-gated and idempotent — re-running is safe.

1. **Find candidates** — merged pipeline PRs lacking the harvested label:
   ```bash
   gh pr list --author @me --state merged --search '-label:review-debt-harvested' \
     --json number,headRefName,body
   ```
   Filter to the pipeline's branch-prefix pattern (same as Step H1). No time window — the label gates idempotency, so an unbounded scan is correct (correctness over speed). If the merged-PR list grows large, a `merged:>=<date>` 7-day bound is acceptable.

2. **Extract** the `DEFERRED-REVIEW-FINDINGS` block from each candidate's body (the content between the `<!-- DEFERRED-REVIEW-FINDINGS` and `DEFERRED-REVIEW-FINDINGS -->` sentinels). No block → nothing to file; apply the harvested label (step 5) anyway so the PR is skipped next sweep.

3. **Resolve the tracker** for the merged PR's repo from its `.claude/project-config.yaml` (per **Tracker Resolution**). Deferred tickets land in the **code's own repo** — `linear` or `github-issues` as that repo configures, not a central backlog.

4. **For each finding — dedup, then file:**
   - **Dedup:** search the tracker's open issues for the same summary/file before filing (prior-art culture — do not create duplicates). `gh issue list --search "<summary> in:title"` for `github-issues`; the `search_issues` op for `linear`. A plausible match → skip filing, log `deferred finding already tracked: <summary>`.
   - **File** the ticket against that repo with a `review-debt` label: title = the finding summary, body = file + rationale + a backlink to the source PR.

5. **Mark harvested:** apply the `review-debt-harvested` label so the next sweep skips the PR:
   ```bash
   gh pr edit <number> --add-label review-debt-harvested
   ```
   This is the idempotency gate — filing and labeling both happen, but a re-run that finds the label already present is a no-op.

**Headless:** Step H3 is interactive-only (stays in the PR Hygiene Sweep, which runs in interactive mode only per the Headless Mode out-of-scope notes). Deferred findings are written to the PR body by Step 4d and harvested on the next interactive sweep.

### Quick Feedback Checks (stage boundaries)

In addition to the full hygiene sweep before each ticket, run a **quick feedback check** at natural pause points within a ticket's lifecycle — specifically between non-code-writing phases where we're waiting on agent results anyway:

- **After plan agent returns** (before Checkpoint 1)
- **After the plan-review stations return** (both Plan Reviewer and Plan Soundness Reviewer, before Step 1g — Post Plan to Linear)
- **After implementation agent returns** (before Checkpoint 2)
- **After review agents return** (before Checkpoint 3)

Quick check is lightweight — scan only, no fix:

```bash
gh api repos/{owner}/{repo}/pulls/<prior-pr-number>/reviews --jq '.[] | select(.state == "CHANGES_REQUESTED") | .user.login' 2>/dev/null
gh pr checks <prior-pr-number> --json state,conclusion --jq '.[] | select(.conclusion == "FAILURE")' 2>/dev/null
```

**If issues found:** Log a one-line notice: "⚠ PR #N has new feedback / CI failure — will address at next hygiene sweep or merge gate."

**Do NOT interrupt the current ticket's flow** for a quick check — just surface awareness. The user can choose to pause and address it, or let the pipeline continue to the next natural triage point (hygiene sweep or merge gate).

This keeps max feedback latency to roughly one pipeline stage (~minutes) rather than one full ticket (~could be much longer for large scope).

---

## Stage 1: Plan

**Delegated to `auto-dev-plan.md`.** Read and follow the instructions in `.claude/commands/auto-dev-plan.md`. This stage generates or validates the plan, runs the ambiguity scan, runs plan quality review (spec + soundness), and posts the approved plan to the active tracker.

**Chaining note:** when following `auto-dev-plan.md`'s instructions as part of this interactive session, do NOT emit the `AUTO_DEV_RESULT` sentinel at the end of Stage 1. Continue to Stage 2 immediately. The sentinel is suppressed in the chained path; `auto-dev.md` owns the single final sentinel.

After Stage 1 completes, proceed to Stage 2.

---

## Stage 2: Implement

**Delegated to `auto-dev-impl.md`.** Read and follow the instructions in `.claude/commands/auto-dev-impl.md`. This stage spawns an implementation agent in a worktree, runs the Stage 2 orchestrator completion gate, and confirms the branch is pushed to origin.

**Chaining note:** when following `auto-dev-impl.md`'s instructions in this session, do NOT emit the `AUTO_DEV_RESULT` sentinel at the end of Stage 2. Continue to Stage 3. The sentinel is suppressed in the chained path.

After Stage 2 completes, proceed to Stage 3.

---

## Stage 3: Review

**Delegated to `auto-dev-review.md`.** Read and follow the instructions in `.claude/commands/auto-dev-review.md`. This stage spawns review agents, adjudicates findings per Checkpoint 3a (in `auto-dev-review.md`), and runs the fix loop (Step 3b) for action-list items. Adjudication outcomes (rejections + deferrals) are stashed to `.cw/deferred-findings.md` for Stage 4 to consume in Step 4d.

**Chaining note:** when following `auto-dev-review.md`'s instructions in this session, do NOT emit the `AUTO_DEV_RESULT` sentinel at the end of Stage 3. Continue to Stage 4. The sentinel is suppressed in the chained path.

After Stage 3 completes, proceed to Stage 4.

---

## Stage 4: PR Creation (Merge-Gated)

**Delegated to `auto-dev-finalize.md`.** Read and follow the instructions in `.claude/commands/auto-dev-finalize.md`. This stage handles the merge gate check, PR creation via `/prep-pr`, auto-merge enablement, and CI monitoring.

**Chaining note:** when following `auto-dev-finalize.md`'s instructions in this session, do NOT emit the `AUTO_DEV_RESULT` sentinel or the `done` stage event from within the per-stage file instructions. `auto-dev.md` owns both the `done` event and the final sentinel in the chained path.

## Stage 5: CI Wait

**Delegated to `auto-dev-finalize.md`** (included in Stage 4 above). Stage 4 and Stage 5 are handled together in `auto-dev-finalize.md`.

---

## Error Recovery

At any failure point, present options via **AskUserQuestion:**

1. **Skip ticket** — move to next ticket, leave partial work for manual pickup
2. **Retry** — re-enter pipeline at the failed stage with existing state preserved
3. **Abort pipeline** — stop all processing

On skip or abort, report what was completed and the worktree path/branch if partial work exists. Don't clean up worktrees.

---

## Escalation

If any agent returns friction level **BLOCK**:
- Surface the blocker immediately via AskUserQuestion
- Do NOT proceed to next stage
- "Resolve this manually and resume, skip ticket, or abort pipeline?"

**Headless:** EXIT `blocked` with `blocker.reason: "agent_block"`.

---

## Chained Pipeline Flow (Interactive Mode)

When invoked as `/auto-dev <ticket-id>` (interactive, not headless), follow these stages in sequence within this single session:

1. **Intake** → read and follow `.claude/commands/auto-dev-intake.md`
2. **PR Hygiene Sweep** → run per the PR Hygiene Sweep section above
3. **Plan** → read and follow `.claude/commands/auto-dev-plan.md` (suppress its sentinel)
4. **Implement** → read and follow `.claude/commands/auto-dev-impl.md` (suppress its sentinel)
5. **Review** → read and follow `.claude/commands/auto-dev-review.md` (suppress its sentinel)
6. **Finalize** → read and follow `.claude/commands/auto-dev-finalize.md` (suppress its sentinel AND its `done` event)
7. **Headless only (monolith invoked with `--headless`):** emit the `done` event then the single final `AUTO_DEV_RESULT` sentinel per the Appendix

The "suppress sentinel" instruction in each stage is critical — in the chained path, emitting intermediate sentinels would leave the session wedged (`working`) and the queue task unrouted (#578).

---

## Pipeline Summary (on completion or abort)

Print:
- Tickets completed (with PR numbers/URLs)
- Tickets failed (with stage and error summary)
- Tickets skipped
- Total PRs created
- Total files and lines changed across all tickets

### Fresh-Session Boundary (interactive only)

After printing the summary, append this reminder verbatim:

```
─────────────────────────────────────────────────────────
This auto-dev session is complete. If your next ask is **net-new work**
(new ticket, new investigation, new feature, debug fork on something
unrelated to the PRs above), strongly prefer a fresh session:

  1. Run `/handoff` to capture state
  2. Exit this session
  3. Start a new one with the handoff for context

Why: extended auto-dev sessions accumulate context that biases the model
toward the prior work's framing. Net-new work — especially debugging —
goes deeper and faster in a clean session.

Continue here only for: follow-ups on the PRs just shipped, hygiene
work on those branches, or thin clarification questions.
─────────────────────────────────────────────────────────
```

If the user's next prompt looks like net-new work despite this reminder (new ticket ID, unrelated bug, new feature description), surface the boundary again before acting: "That looks like net-new work — recommend `/handoff` and a fresh session. Proceed here anyway?" Then wait for confirmation. Do not silently continue.

**Headless:** Suppress this reminder block AND any trailing `/schedule` offers in headless output.

---

## Scope Tier Evolution

These thresholds determine guard levels, not rejection. Tune as trust grows:

| Version | Small Tier Ceiling | Forbidden Areas (auto-escalate to Large) |
|---------|-------------------|------------------------------------------|
| v1 (current) | 10 files, 500 lines | migrations, auth/security, CI/CD [^1], shared bases (3+ consumers) |
| v2 (future) | 15 files, 800 lines | migrations, auth/security |
| v3 (future) | 25 files, 1500 lines | migrations |

[^1]: CI/CD auto-escalate requires diff content touching a pipeline-behavior YAML key, not merely a path match under `.github/workflows/**` — see `auto-dev-plan.md` Step 1d.2b.

---

## Post-Impl Scope Conformance Threshold

Distinct from the tiers above: those bound what a plan may *propose*, this bounds
how far an implementation may *drift from the plan it was given*. Enforced at
Step 2.5 gate 2 by `.claude/scripts/check_plan_scope_conformance.py` against the
plan's `## Files Modified` enumeration (#1779).

```
allowed_extra = max(SCOPE_DRIFT_ABS_FLOOR, round(plan_file_count * (SCOPE_DRIFT_RATIO - 1)))
triggered     = len(delivered_files - planned_files) > allowed_extra
```

| Version | `SCOPE_DRIFT_RATIO` | `SCOPE_DRIFT_ABS_FLOOR` | Effect |
|---------|--------------------|------------------------|--------|
| v1 (current) | 1.5 | 5 | Up to 50% more files than planned, never fewer than 5 extra |

The floor exists so tiny plans are not tripped by one missed call site; the ratio
exists so a large plan's allowance scales with it. Only *delivered-but-unplanned*
files count — a planned file missing from the diff is the separate "missing work"
signal, and folding it in would let an under-delivering run look like drift.

**Per-repo override** — add to the repo's `pyproject.toml` rather than editing the
shared script:

```toml
[tool.cw.scope_conformance]
ratio = 1.25      # optional; defaults to SCOPE_DRIFT_RATIO
abs_floor = 3     # optional; defaults to SCOPE_DRIFT_ABS_FLOOR
```

The read fails **open**: a missing file, missing table, missing key, wrong-typed
value, or malformed TOML each fall back to the shipped default for that value. A
repo whose `pyproject.toml` cannot be parsed must not silently change gate
behavior.

---

## Subagent Reliability Mitigations

The auto-dev pipeline delegates implementation, review, and prep work to spawned agents. Agent self-report (Friction/Health checks) was historically the only ground truth for stage advancement. Session-in-practice revealed three failure modes that self-report does not catch:

1. **OOM/crash mid-task** — agent loses uncommitted work, returns BLOCK or dies silently
2. **False-completion claim** — agent reports "done" with HIGH confidence while code is incomplete or broken
3. **Non-returning task** — a spawned agent hangs and never returns, leaving the orchestrator suspended

Root cause: The pipeline trusts agent self-assessment. A falsely-completing agent fills in the same Friction/Health fields as an honest one — indistinguishable until the diff is inspected by a human.

**Meta-principle:** Gate on *observable facts* the orchestrator witnesses (git state, exit codes, test output, file existence), not agent prose.

### Mitigation 1: Orchestrator-Run Completion Gates (highest priority)

After each stage's spawned agent returns a "done" claim, before advancing to the next stage, the orchestrator runs deterministic, non-LLM verification:

#### Stage 2 (Implementation) — after agent claims impl complete

Run these checks from the main session against the pushed branch — not from the impl isolation worktree (inaccessible) and not from bare cwd (no impl changes there):

```bash
# 0. Verify the branch was pushed; abort immediately if not
git fetch origin main <branch-name>
git rev-parse --verify origin/<branch-name> || { echo "IMPL_NOT_PUSHED"; exit 1; }

# Set up one trap-cleaned temp worktree at origin/<branch-name>
FORK_POINT=$(git merge-base origin/main origin/<branch-name>)
: "${CW_SESSION:?CW_SESSION must be set}"
TMPWT="/tmp/gate-wt-$CW_SESSION"
# Deterministic path (keyed on $CW_SESSION, not $$) — reconstructable by an
# external reconciler even if this invocation is SIGKILLed before any trap runs.
git worktree remove --force "$TMPWT" 2>/dev/null
rm -rf "$TMPWT" 2>/dev/null
git worktree prune
gate_wt_cleanup() { git worktree remove --force "$TMPWT" 2>/dev/null; rm -rf "$TMPWT" 2>/dev/null; }
trap gate_wt_cleanup EXIT
# INT/TERM must also actually stop the script — a trap alone only runs
# cleanup and then resumes execution; without the explicit exit here the
# gate would keep running gate checks after the harness believed it had
# killed this invocation.
trap 'gate_wt_cleanup; exit 143' INT TERM
git worktree add --detach "$TMPWT" origin/<branch-name> || { echo "IMPL_FAILED: git worktree add exited $?"; exit 1; }

# 1. Diff is non-empty
git -C "$TMPWT" diff --stat "$FORK_POINT" | grep " changed" || { echo "IMPL_FAILED: empty diff"; exit 1; }

# 2. File set matches the implementation plan's file list
git -C "$TMPWT" diff --name-only "$FORK_POINT" | sort > /tmp/touched_files-$$
echo "<files from plan>" | sort > /tmp/planned_files-$$
comm -23 /tmp/touched_files-$$ /tmp/planned_files-$$ | wc -l
# (output must be 0 — no unexpected file touches)

# 3. If the plan lists a test command, run it
cd "$TMPWT" && <test_command> --tb=short > /tmp/test.log-$$ 2>&1
exit_code=$?
if [ $exit_code -ne 0 ]; then
  echo "IMPL_FAILED: test exited $exit_code"
  exit 1
fi

# 4. If Python files touched, re-run mypy/ruff
# (run from $TMPWT; non-zero exit → impl_failed)
# uv run mypy <touched_files>
# uv run ruff check <touched_files>

# 5. Incremental commit discipline
commit_count=$(git -C "$TMPWT" log --oneline "$FORK_POINT"..HEAD | wc -l)
# Must be > 1 for non-trivial changes (>50 lines OR >3 files)
# Violation: append "impl_no_incremental_commits" to friction_highlights (advisory, not blocking)
```

**Headless behavior:** If any check fails (including `IMPL_NOT_PUSHED`, or gate setup itself — e.g. `git worktree add` erroring), set `status: "blocked"`, use the matching `blocker.reason` (`"impl_not_pushed"` or `"impl_failed"`), and do NOT spawn reviewers. Interactive: AskUserQuestion with the failed check output.

**Why this works:**
- Gates run in a checkout of the pushed branch — not the inaccessible impl worktree
- `git diff --stat` against the pushed ref cannot lie — it is filesystem truth
- Exit codes are not prose — they are measurable facts
- File set is enumerable — the plan specifies the boundary upfront
- No room for hallucination or self-assessment bias

#### Stage 3 (Review fix loop) — after agent claims fix complete

Same deterministic gates: re-run the test command, re-run mypy/ruff if touched, compare new diff to prior diff. If the fix claim is false, the test still fails or the linter still complains — the agent's prose adds zero value. The facts speak.

**Note:** This is not a re-review. Reviewers already ran once post-impl and produced MUST_FIX findings. We are verifying the agent's subsequent claim that fixes were applied. The fix is real or it isn't.

### Mitigation 2: Completion Claims Carry Verifiable Artifacts

Extend the Friction Protocol's evidence discipline to implementation completion. When an agent claims "impl done," require them to paste actual, verifiable output — not a summary.

#### For Stage 2 impl agents:

Add to the required friction report (after `## Friction Report` block):

```
## Completion Artifacts (required for "done" claims)

- **Test command used:** <exactly what was run>
- **Test output (tail):** <paste last 50 lines of pytest/test output>
- **git diff --stat:** <paste verbatim>
- **git log --oneline (vs fork point):** <paste verbatim — git log --oneline $FORK_POINT..HEAD>
- **git diff line count:** <total lines added/removed>
- **Quality gate results — one row per gate command** (report every command named in this session's quality-gate sentence as its own row — e.g. `ruff check` and `ruff format --check` are separate gates and MUST be separately reportable): for each configured gate, report `<command>` → `pass` | `<exit code + error output>` | `not_run`. A gate not run MUST be reported `not_run` — never omitted, never folded into a sibling gate's result.
```

**Orchestrator verification:**
- Parse the pasted test tail; if it contains FAILED, ERROR, or non-zero exit, the claim is false
- Parse git diff --stat; if it's empty or doesn't match the plan's file list, the claim is false
- Every gate named in the session's quality-gate sentence must have its own row showing `pass`; a `not_run` row, a non-zero result, or a missing row for any configured gate means the claim is false — same `impl_failed` disposition as a contradicted artifact

**Interactive mode:** If any artifact contradicts the "done" claim, call AskUserQuestion: "Artifacts show tests failed / linter errors / no changes. Fix and retry, or abort?"

**Headless mode:** Same — treat contradictory artifacts as `impl_failed`, do NOT spawn reviewers.

**Why this works:**
- Artifacts are generated by deterministic tools, not hallucinated
- A pasted test tail is unforgeable — it is what actually happened
- The diff output is git's, not the agent's opinion
- Inconsistency is detectable instantly

### Mitigation 3: Mandatory Incremental Commits

Impl and fix-loop agents MUST commit their work incrementally — not all-at-once at the end. Each logical step gets its own commit.

#### Rationale:

- **OOM recovery:** If an agent OOMs mid-task, the orchestrator reads `git log` and resumes from the last commit, not from zero. Work is not lost; only the uncommitted tail is lost.
- **Progress visibility:** The orchestrator can inspect `git log --oneline` to audit what the agent actually did, not what it claims.
- **Idempotency:** The resume agent starts from the last commit, naturally idempotent.

#### Enforcement:

- Agent prompt MUST include: "Commit after every logical step. Do NOT defer commits to the end. Orchestrator will inspect `git log` to verify progress."
- If agent returns but `git log` shows only one commit or zero commits, treat as `impl_failed`.

#### Implementation agent checklist:

```
For each file or feature modified:
1. Make changes
2. `git add <files>`
3. `git commit -m "implement: <specific change>"`
4. Do NOT accumulate uncommitted work

Bad: "I changed 5 files, let me commit once at the end."
Good: "I changed file A (commit), then file B (commit), then file C (commit)."
```

#### Fix-loop agent checklist:

Same discipline. Each MUST_FIX finding from reviewers gets a dedicated commit (or grouped by related findings).

### Mitigation 4: Dead-Agent Detection

If a spawned agent's task never returns — it hangs indefinitely, crashes without exiting, or enters an infinite loop — the orchestrator has no way to detect this today. The pipeline suspends forever.

#### Timeout mechanism:

- Every spawned agent gets a wall-clock timeout. Recommended: 30 minutes for implementation, 15 minutes for reviews/fixes, 10 minutes for lightweight tasks.
- If the agent's task exceeds the timeout, forcibly terminate it and treat as BLOCK with `blocker.reason: "agent_block"` and `blocker.details: "impl agent timed out after 30 minutes; work lost or agent hung"`.

**Headless:** EXIT `blocked` with the timeout reason.

**Interactive:** AskUserQuestion: "Agent timed out mid-task. Retry, skip ticket, or abort pipeline?"

#### Implementation:

Use the Agent tool's timeout parameter (if available) or wrap the agent spawn in a background task monitor:

```bash
# Pseudo-code (actual implementation depends on harness)
timeout 1800 run_agent(prompt) &
pid=$!
wait $pid
exit_code=$?
if [ $exit_code -eq 124 ]; then
  # timeout(1) returns 124 on timeout
  echo "Agent PID $pid timed out"
  exit 1
fi
```

**Note:** OOM is distinct from timeout. An OOM crash should still return a friction report (BLOCK level). A hung process that never returns is the case this mitigation catches.

### Integration into the Pipeline

#### Stage 2 (Impl):

1. Spawn impl agent (existing)
2. Agent returns (or times out → BLOCK)
3. **NEW:** Run orchestrator completion gates (Mitigation 1) + parse artifacts (Mitigation 2) + audit git log (Mitigation 3)
4. If any gate fails → BLOCK, do NOT spawn reviewers
5. If all gates pass → proceed to Stage 3

#### Stage 3 (Review fix loop):

1. Reviewers spawn and return findings (existing)
2. On MUST_FIX → spawn fix agent (existing)
3. Fix agent returns (or times out → BLOCK)
4. **NEW:** Run orchestrator gates + parse artifacts + audit git log
5. If any gate fails → BLOCK or escalate per existing fix-loop limits
6. If gates pass → reviewers re-spawn for Stage 3b re-review

#### All spawns:

- **NEW:** Timeout enforcement (Mitigation 4) wraps every agent spawn
- **NEW:** Agent prompt includes incremental-commit checklist and completion-artifacts template

### Health Aggregation Rule (revised)

The existing health-aggregation rule (Headless Mode section) is superseded by deterministic gates. An agent that returns HIGH confidence but fails a gate has lied, and the pipeline stops — we do not "downgrade the status" and continue.

Old rule: "If any agent returns LOW confidence or EXIT_FOR_HUMAN_REVIEW, downgrade the outcome."

New rule: "If any agent fails an orchestrator-run gate (diff missing, test fails, timeout), treat as `impl_failed` and BLOCK — do not proceed. Agent self-assessment is advisory only; facts are binding."

The Friction/Health checks remain useful for diagnostics and post-mortems, but they no longer gate advancement. **Fact gates advancement.**

---

## Appendix: Structured Output

In headless mode, after all pipeline logic completes, emit `stage.entered` (`done`) then emit the sentinel-delimited JSON block as the final lines of stdout. The narrative friction reports remain above (still useful for tmux scrollback / post-mortem); this block is the parsing contract for `cw`.

**The sentinel is the LAST thing you do — end the turn immediately after it.**
The closing `AUTO_DEV_RESULT>>>` frame must be the final characters of your
final message: no trailing prose, no "next steps" summary, no further tool
calls, no background tasks left running (kill or await them BEFORE the
sentinel). The Stop hook fires only when the turn completes; anything that
keeps the turn open after the sentinel leaves the session wedged `working`
and the queue task unrouted (#578 — observed four times in the 1.1 waves).

**Headless only — before emitting the sentinel, emit `stage.entered` (`done`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"done\",\"prev_stage\":\"s5_ci_waiting\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

**Pre-emit validation gate.** Before framing the sentinel block, validate the inner JSON payload with `cw result validate`. A validation failure means the payload is malformed — fix the field errors, do not emit invalid JSON.

```bash
# Write the payload to a temp file and validate before emission
printf '%s' "$SENTINEL_JSON" | cw result validate -
# On success: cw result validate exits 0 and prints normalized JSON to stdout
# On failure: exits 1 and prints field.path: message lines to stderr
# Fix all reported errors before proceeding to emit the framed block
```

**Validating is not emitting (#1890).** `cw result validate -` confirms the JSON is well-formed — it does not emit the sentinel. Never narrate emission as a separate act from performing it (e.g. writing "Sentinel validated. Emitting the final result." and stopping there): the literal `<<<AUTO_DEV_RESULT` / `AUTO_DEV_RESULT>>>` frame, wrapping the validated JSON, MUST be the final characters of this same message — not a description of what you are about to do next.

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "PROJ-1234",
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
  "branch": "dev/proj-1234-fix-login",
  "worktree_path": "~/.cw/wt/abc/auto-dev-proj-1234",
  "fork_point_sha": "abc1234",
  "commits": ["sha1", "sha2"],
  "pr": {
    "number": 42,
    "url": "https://github.com/.../pull/42",
    "auto_merge": true,
    "base": "main"
  },
  "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0, "deferred": 0},
  "health": {
    "lowest_agent_confidence": "MEDIUM",
    "any_incomplete_risk": false,
    "shortcuts": [],
    "recommendation": "PROCEED",
    "downgrade_applied": false,
    "fix_loop_escalated": false
  },
  "friction_highlights": [],
  "ambiguities": [],
  "blocker": null,
  "prior_pr_warnings": [],
  "next_actions": []
}
AUTO_DEV_RESULT>>>
```

### `plan_source` Values (closed)

The `plan_source` field is a closed literal — consumers will reject any value not in this list. Common emission bug: typo'ing `github_issue_existing` as `github_existing`.

| Value | Meaning |
|---|---|
| `linear_existing` | Plan came from a pre-existing Linear ticket description or comment |
| `github_issue_existing` | Plan came from a pre-existing GitHub Issue body or comment |
| `generated` | Plan agent generated the plan in this session (typical for ambiguous tickets) |
| `free_text` | Plan came from inline `--prompt` / `--plan` text passed at dispatch |
| `none` | No plan; `no_op` and similar early-exit paths where no plan was needed |

### `review` Field Shape

The `review` field is **always a Review dict**, never null — even for pre-impl statuses where no review has occurred. Emit a zero-valued placeholder for any status that hasn't yet completed Stage 3 review:

```json
"review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0, "deferred": 0}
```

Applies to: `no_op`, `plan_pending_approval`, `ambiguities_pending_resolution`, `premises_pending_verification`, `scope_exceeded`, `forbidden_area`, and `blocked` exits before Stage 3. Use real values once Stage 3 reviewers have actually run. `deferred` counts the findings stashed to `.cw/deferred-findings.md` (bucket 3); it defaults to `0` and is optional (omitted → 0) for backward compatibility.

### Status Enum (closed)

| Status | Meaning |
|---|---|
| `shipped` | PR created with auto-merge enabled; CI wait skipped |
| `no_op` | Stage 1 pre-flight verification found all targeted changes already in the desired state; no branch created; `next_actions: ["close_issue_as_completed"]`. Distinct from `blocked` — this is a healthy outcome, not a failure |
| `plan_pending_approval` | Large scope — plan generated and posted to Linear; no branch created; awaiting human approval |
| `ambiguities_pending_resolution` | Plan was clear enough to proceed by tier rules, but the Step 1c ambiguity scan surfaced clarifying questions; posted to Linear; no branch created; awaiting human answers |
| `premises_pending_verification` | The Step 1c ambiguity scan surfaced one or more unverified premises — factual claims about an external system the plan's correctness depends on; posted to Linear; no branch created; awaiting human verification (not a plan revision) |
| `review_pending_approval` | Large scope — fix loop complete, branch pushed, no PR; awaiting human review approval |
| `merge_gate_blocked` | Small scope — prior pipeline PR still open; cannot create next PR until gate clears |
| `scope_exceeded` | `--scope-limit small` rejected a Large ticket before impl started |
| `forbidden_area` | `--forbidden` constraint matched a planned file; ticket rejected before impl started |
| `blocked` | Unrecoverable error mid-pipeline; see `blocker` field for details |
| `empty_diff_blocked` | Branch pushed but measures zero commits ahead of `origin/<default_branch>` — nothing to review or ship; dispatch's #1870 gate or the review-stage synthesis itself detected this and parked for human triage rather than presenting a normal scope-approval decision. `branch` is non-null; `next_actions` is empty; `blocker.reason` is typically `empty_diff_no_commits` |
| `stale_dispatch` | This ticket already has an open, **unmerged** PR from an earlier dispatch, so the run refuses rather than re-implementing work already in review (#1862). Detected by the Stage 0 intake self-check (see `auto-dev-intake.md` Step 3), or by `cw`'s own pre-dispatch gate before a session is even spawned. `pr` stays **null** — this run did not create that PR; its number/URL/review state go in `blocker.details`. `next_actions` is empty; `blocker.reason` is `pr_already_open`. Distinct from `no_op` (nothing is complete — the PR is unmerged) and from `merge_gate_blocked` (that is a *different* ticket's PR blocking this one) |

### `blocker.reason` Values

When `status: "blocked"`, the `blocker.reason` field carries one of:

> **Maintenance:** the `review_blocked` row references the `5`-cycle hard cap. If you tune cap values, see the maintenance note in Step 3b.5 for the full sync list.

| Reason | Meaning |
|---|---|
| `impl_not_pushed` | Step 2.5 pre-gate: `origin/<branch-name>` was absent after `git fetch` — impl agent claimed done but never pushed. `retry_eligible: true`; orchestrator may re-dispatch or resume from S2 |
| `impl_failed` | Implementation agent returned BLOCK or failed quality gates after 2 attempts |
| `review_blocked` | MUST_FIX findings persisted after 5 fix-loop cycles (the hard cap). Also posts the still-unresolved blocking findings as a tracker comment (#1815) |
| `plan_deviation` | A non-deferrable Stage-3 finding (impl deviates from an explicit plan requirement/prohibition) survived the fix loop or was judged beyond fix-loop scope. The pipeline does not assign plan-vs-impl blame — it always exits `blocked`; the operator uses `cw dev-queue requeue --regress` to send it back to impl, or revisits the plan. Also posts the blocking findings that caused the exit — regardless of severity — as a tracker comment (#1817) |
| `review_operator_actionable` | An accepted MUST_FIX finding carrying `no_diff_anchor: true` — the session judged it valid and in-scope, but its remedy lies outside this diff entirely (a follow-up ticket that was never filed, an artifact that exists nowhere), so the fix loop structurally cannot act on it. Adjudicated `outcome: "operator_action"` at Checkpoint 3a and posted to the ticket as a `## Operator-Actionable Review Findings` checklist; `blocker.stage` is `"stage3_review"` and it routes to BLOCKED_ON_USER, not finalize. Distinct from `plan_deviation`, which is a NON_DEFERRABLE plan-conformance judgment: a finding that is both `no_diff_anchor` and NON_DEFERRABLE never reaches this reason — it exits `plan_deviation` instead (#1817) |
| `plan_scope_drift` | Step 2.5 gate 2: the delivered diff touched more unplanned files than `check_plan_scope_conformance.py`'s allowance (#1779). **Mechanical and pre-review** — it is a file-set measurement taken *after impl, before review*, where `plan_deviation` above is a reviewer's *judgment* about content, raised during Stage 3. `blocker.stage` is `"stage2_impl"` (vs `plan_deviation`'s `"stage3_review"`), and `blocker.details` enumerates the specific unplanned paths — that list is the operator's entire authorization surface: requeue the parked task if the growth was legitimate, or `cw dev-queue requeue --regress` to send it back for a tighter diff. Distinct from `scope_exceeded`, which is a Status (not a blocker reason) fired *before impl started*, from the Stage-1 plan's own estimate |
| `plan_unreviewable` | Plan Reviewer (spec station) returned MUST_FIX both before and after a single Step 1f.4 revision cycle — the plan needs human triage, not another auto-revision. No branch created. Also posts the persisting blocking findings as a tracker comment (#1815) |
| `plan_unsound` | Plan Soundness Reviewer returned a MUST_FIX (direction contradicts a codified `ARCHITECTURE.md` §7/§8 rule) in a headless run, or it persisted after a Step 1f.4 revision cycle — the chosen direction needs human judgment. No branch created. Also posts the persisting blocking findings as a tracker comment (#1815) |
| `ambiguity_scan_unconverged` | Step 1c's ambiguity/premise scan parked for 2 consecutive rounds without converging — the round cap (`plan-stage-scan-round`, cap 2, tracked on `.cw/plan-draft.md`'s first line) was reached, so another park round would just re-ask what the operator has already been asked twice. No branch created. `blocker.stage` is `"stage1_plan"` and `retry_eligible: true`; the still-open item(s) and the round count at exhaustion (always 2) are named verbatim in `blocker.details`, folded into the same consolidated `## Pending Verification Scan` comment as any ambiguities, premises, and advisory findings raised that round (#1683) |
| `deferred_stub_unresolved` | Step 1c's pre-branch stub check found a `## Deferred Premises` entry still marked `PENDING — agent must supply on next scan` after the scan that was required to classify it — the halt-check the plan depends on is un-enforced, so the round blocks instead of proceeding to any Step 4c outcome, AUTO-CONTINUE included. No branch created. `blocker.stage` is `"stage1_plan"` and `retry_eligible: true`; the unresolved stub(s) are named verbatim in `blocker.details` and folded into the same consolidated park comment (#1683) |
| `scope_tier_stale` | Step 1g.0's tier re-verification, immediately before the `**Scope tier:**` stamp is written, found the tier last computed this invocation differs from the tier freshly recomputed from the plan's current state — a Step 1f.4 revision (or an accumulated resumed round) could otherwise carry an earlier round's stale tier through to the persisted stamp, silently skipping the Large-tier operator-approval gate at Checkpoint 1. No branch created. `blocker.stage` is `"stage1_plan"` and `retry_eligible: true`; `blocker.details` names both tiers, each with its full `(files, lines, forbidden_touched)` tuple. Also posts the mismatch as a tracker comment under `## Blocking Review Findings` (#1815, #1897) |
| `fix_loop_dispatch_failed` | Step 3b's `dispatch_fix_agent` call — the `uv run python -c` boundary that provisions the fix agent's worktree, verifies HEAD, merges `origin/<default-branch>`, and spawns the cw session — exited non-zero (#2017). Causes: a stale worktree `create_worktree` refused to reuse, a merge conflict against main (aborted, worktree left clean, conflicting files named), or a `SpawnUnregisteredError` from `spawn_create_impl`'s roster check. `blocker.stage` is `"stage3_review"`. No `FIX_SESSION_ID` was produced, so the run composes into Step 3b.5's fix gate as a cycle failure (no new commit since the prior cycle) and consumes budget against the 5-cycle cap — this row documents what that gate already handles, it is not a separate sentinel-emitting exit |
| `fix_loop_wait_timeout` | Step 3b's `cw session wait "$FIX_SESSION_ID" --timeout 900` returned non-zero (#2017): the fix session reached a non-`completed` terminal status (exit 1) or hit the hard 900s ceiling (exit 124). `blocker.stage` is `"stage3_review"`; `retry_eligible: true` — a wall-clock overrun on a contended host clears on re-dispatch. Same composition as `fix_loop_dispatch_failed` above: it falls through to Step 3b.5's fix gate as a cycle failure rather than emitting its own exit |
| `agent_block` | Any other agent returned friction level BLOCK that the pipeline could not auto-resolve |
| `tool_denied` | The Claude Code auto-mode classifier denied a tool call mid-pipeline. Often classifier-flaky (see claude-workspace#183) so `retry_eligible: true` by default; the orchestrator may re-dispatch the ticket on a fresh session. Populate `blocker.tool_name` and `blocker.denial_reason` (verbatim classifier `Reason:` text). See Tool-Use Denial Exit section |

Other `blocker.reason` values are reserved for future use; consumers should treat unknown reasons as opaque strings and surface them to the user verbatim.

### Field Notes

**`blocker`** — populated when `status=blocked`. Base shape:
```json
{"stage": "stage2_impl", "reason": "agent_block", "details": "<verbatim blocker text>"}
```
Optional keys (some `reason` values require them):
- `tool_name`, `denial_reason`, `recovery_hint`, `retry_eligible`, `retry_delay_seconds` — required when `reason: "tool_denied"`; see Tool-Use Denial Exit section
- `recovery_hint`, `retry_eligible`, `retry_delay_seconds` — used when `reason: "local_main_diverged_from_origin"`; see Step P3
- `message` — optional human-readable summary alongside `details`

**`stage_reached`** — canonical values per terminal status:
- `shipped` → `"stage5_post_create"`
- `no_op` → `"stage1_pre_flight"`
- `plan_pending_approval` → `"stage1_plan"`
- `review_pending_approval` → `"stage3_review"`
- `merge_gate_blocked` → `"stage4a_merge_gate"`
- `scope_exceeded` / `forbidden_area` → whichever stage detected the violation (`"stage1_plan"` at planning, or the impl stage if later); mirror in `blocker.stage`
- `empty_diff_blocked` → `"stage2_impl"` or `"stage3_review"` — whichever stage measured the empty branch; both are legal. Mirror it in `blocker.stage`
- `stale_dispatch` → `"stage1_pre_flight"` for the Stage 0 intake self-check (the common case), or whichever later stage a resume path discovered the open PR at. Mirror it in `blocker.stage`
- `blocked` with `blocker.reason: "plan_unreviewable"`, `"plan_unsound"`, `"ambiguity_scan_unconverged"`, `"deferred_stub_unresolved"`, or `"scope_tier_stale"` → `"stage1_plan"`
- `blocked` (other reasons) → whichever stage produced the BLOCK; mirror this in `blocker.stage`.

**`worktree_path`** and **`branch`** — these are two distinct namespaces; do NOT conflate them.

- `worktree_path` — the cw *session* worktree created by dispatch (format: `~/.cw/wt/<hash>/auto-dev-<ticket>`). This is the directory the orchestrator Claude session runs in. `cw dispatch` creates it under the `auto-dev/<ticket>` label.
- `branch` — the *feature* branch the Stage 2 impl agent pushed to `origin` (format: `dev/<ticket-id>`). The impl agent works on an internal `agent-<hash>` local branch and pushes via `git push origin HEAD:refs/heads/dev/<ticket-id>`.

A `worktree_path` ending in `dev-proj-1234-fix-login` is wrong — that pattern names a feature branch, not a session worktree.

**`next_actions`** — advisory list `cw` can act on without prose-parsing. Empty for terminal success. Examples:
- `"wait_for_ci"` — auto-merge is pending CI; cw can poll
- `"user_approve_plan"` — large scope plan posted to Linear; cw should notify user
- `"resolve_merge_gate"` — prior PR must merge before this ticket can ship
- `"user_approve_review"` — large scope branch pushed; cw should notify user for review
- `"user_resolve_ambiguities"` — Step 1c surfaced ambiguities; cw should notify user; answers belong on the Linear ticket before re-invoking
- `"close_issue_as_completed"` — `no_op` outcome: the targeted change was already in place; cw should close the ticket as completed (the skill does not auto-close)
- `"attach_ui_evidence"` — Stage 4d UI Evidence Gate "Hold" branch (interactive only): PR was created with frontend file changes but the body has no screenshots/video, the human chose to hold rather than ship-anyway or capture-now; auto-merge was NOT enabled
- `"attach_ui_evidence_and_enable_automerge"` — Stage 4d UI Evidence Gate fired in headless: PR was created with frontend file changes but the body has no screenshots/video; auto-merge was NOT enabled; the human should embed media in the PR body then run `gh pr merge --auto --squash`

**`ambiguities`** — populated when `status="ambiguities_pending_resolution"`. List of structured items, one per question. Each item:
```json
{"question": "<verbatim question>", "plan_assumption": "<plan's current interpretation>", "alternatives": ["<alt 1>", "<alt 2>"], "why_it_matters": "<one sentence>", "ticket_evidence": "<verbatim quote>"}
```
Empty (`[]`) when the status is anything else. The cw orchestrator can render these as a Linear comment template or pass them to the user verbatim.

**`premises`** — populated when `status="premises_pending_verification"`. List of structured items, one per premise (a factual claim the plan's correctness depends on — not a preference). Each item:
```json
{"premise": "<the claim>", "details": "<what the plan depends on it for + how to verify>", "verified_by_investigation": "<evidence gathered, or empty if unverified>"}
```
Empty (`[]`) when the status is anything else. Per the headless contract §4.4, consumers treat the keys as best-effort.

**`schema_version: 4`** — increment when fields are added or semantics change so `cw` can version-gate its parser. Bump history:
- **v2** — adds the `no_op` status (Stage 1 pre-flight already-satisfied path).
- **v3** — `no_op` emitted with `stage_reached="stage1_pre_flight"` and `plan_source="none"`.
- **v4** — promotes `ambiguities_pending_resolution` and `premises_pending_verification` to canonical statuses (previously interim values the parser routed through the synthetic-block fallback), and adds the top-level `ambiguities` / `premises` arrays (non-empty when their corresponding status is set).
- **v5** — adds the optional `review.agents_run` int (count of reviewer agents that ran). Defaults to `0`; older payloads that omit it parse unchanged.
- **v6** — adds the `empty_diff_blocked` status and its canonical `empty_diff_no_commits` `blocker.reason` (#1870).
- **v7** — adds the `stale_dispatch` status and its canonical `pr_already_open` `blocker.reason`, and widens `stage_reached="stage1_pre_flight"`'s allowed statuses (previously `no_op`/`blocked`) to admit it (#1862).

v5-v7 are accepted by the parser under **all** supported schema versions as a rollout exception, so this skill may keep emitting its current `schema_version` while using them. See `docs/headless-contract.md §8` for the authoritative table.

A parser older than the emitted version routes unknown statuses through the synthetic-block fallback, so the consumer side must merge before producers emit a new version. The `cw` parser accepts legacy v1–v3 during the rollout window; the v4 parser side shipped in `claude-workspace#191` and must be deployed before this skill emits `schema_version: 4`.

**Interactive mode:** this block is NOT emitted. Structured output is headless-only.
