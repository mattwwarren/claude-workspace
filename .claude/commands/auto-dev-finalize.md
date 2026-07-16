---
description: "auto-dev Stage 4+5: Finalize — PR creation via /prep-pr, merge gate, auto-merge, CI wait"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "AskUserQuestion", "Skill"]
---

# auto-dev Stage 4+5: Finalize

**Orientation:** Read `.cw/context.json` for ticket context. The feature branch must be pushed to origin with review complete (Stage 3 complete). This stage creates the PR and waits on CI.

In standalone headless invocation: emit `AUTO_DEV_RESULT` after this stage completes. In the interactive monolith chain: do NOT emit the sentinel here — `auto-dev.md` owns the single final sentinel AND the `done` stage event.

**Arguments:** "$ARGUMENTS"

---

> **Model selection:** All agent spawns in this file use explicit `model:` pins (Sonnet or Haiku). Do not change any pin to `model: inherit` — see CLAUDE.md §"Model Selection for Subagents" for the rationale and tier matrix.

## Resolve carried-through context before emitting the sentinel

Every `AUTO_DEV_RESULT` sentinel emitted from this file requires concrete, non-null `plan_source` and `scope.tier` values (see "every field above is required" below). On a normal run these were classified back in Stage 0/1 and Stage 2; on a **concierge-rescued respawn**, the fresh worker starts from a re-materialized `.claude/cw-context.json` and has no memory of that earlier classification. Resolve both explicitly before filling in any sentinel template — do not just copy the raw `"<value carried from Stage 0/1>"` / `"<small | large — same value carried through from Stage 2 scope classification>"` placeholder text verbatim.

**Resolve `plan_source`:**
1. `.claude/cw-context.json` → `queue_metadata.plan_source` — populated by dispatch's `_persist_carried_context` write-back (`_route_staged_decision`, `src/cw/dispatch.py`) from the prior stage's own sentinel, so a rescue respawn's fresh claim→spawn re-materializes it here.
2. Fallback: `.cw/context.json` — infer from the tracker (`github_issue_existing` when the ticket is a GitHub issue, the dispatch default). **If the file is absent, prose-delegate to `auto-dev-intake.md` first to materialize it** (mirroring `auto-dev-plan.md`/`auto-dev-impl.md`'s own Orientation fallback) — a concierge-rescued respawn (`task.attempts > 1`, non-local backend) has its stale `.cw/context.json` deleted before spawn (`dispatch.py:_invalidate_stale_context_json`, #1046), so this step would otherwise silently fall through to step 3 on exactly the rescue path this ticket targets.
3. Fallback: `"none"`.

Use the resolved value in every sentinel `plan_source` field below.

**Resolve `scope.tier`:** structurally mirrors `auto-dev-review.md`'s "Before emitting the sentinel, resolve `scope.tier` explicitly" section — same precedence chain, same rationale (a null tier causes `apply_staged_decision` Rule 1 to route to `BLOCKED_ON_USER`):
1. Read `.cw/plan.md` — look for an explicit `Scope tier:`, `**Scope:** Small`, `tier: small`, or similar Stage-1c marker.
2. Fallback: read `.claude/cw-context.json` → `queue_metadata.scope_hint` (operator hint).
3. Fallback: re-derive from the diff itself using the canonical Stage-1c thresholds — run `git diff --stat $FORK_POINT...origin/<branch-name>` and count changed files and lines. **Small** = ≤10 files AND ≤500 lines AND no forbidden-area touches; **Large** otherwise.
4. If no source yields `"small"` or `"large"`, do **not** emit a `shipped` or `merge_gate_blocked` sentinel — emit `blocked` instead with `blocker.reason: "scope_tier_unresolvable"` and `scope.tier: "small"` (required by the schema validator even on blocked).

`scope.tier` must always be a concrete value (`"small"` or `"large"`) in the emitted sentinel.

**R3 edge case:** `.cw/plan.md` and `.cw/deferred-findings.md` persist across a normal requeue (worktree reused, `allow_dirty_reuse=True`); the only loss path is a `StaleWorktreeError` rebuild (fresh worktree), where the plan-marker step above falls through to re-derivation and the deferred-findings section is legitimately omitted.

## Stage 4: PR Creation (Merge-Gated)

### Pre-Stage Detector Guard

Before invoking Step 4a, check for an existing PR on **this ticket's branch** (distinct from Step 4a, which scans all open pipeline PRs for the merge gate):

```bash
gh pr view <branch-name> --json number,state,url 2>/dev/null
```

- **PR exists and is OPEN** → reuse it. Skip the `/prep-pr` create-step (Step 4c); proceed directly to Step 4d (auto-merge enablement + Linear comment if not already posted) and then Stage 5.
- **PR exists and is MERGED** → detector should have returned `merged`; surface as already-done and exit successfully with `status: shipped`.
- **PR exists and is CLOSED (not merged)** → EXIT `blocked` with `blocker.reason: "pr_already_terminal"`. Do not create a duplicate; the closed PR represents a human decision and the pipeline must not work around it.
- **No PR exists** → proceed with normal Step 4a (Merge Gate Check) → Step 4b → Step 4c flow.

This guard makes Stage 4 idempotent across session restarts. Without it, a session that dies between `/prep-pr` succeeding and Step 4d completing would re-attempt PR creation on next invocation.

### Step 4a: Merge Gate Check

Before creating any PR, check for open PRs from this pipeline:

```bash
gh pr list --author @me --state open --json number,title,headRefName,mergeable,mergeStateStatus
```

Filter results for branches matching the pipeline's naming pattern (`<branch-prefix>/*`).

**If open PR found from this pipeline:**

Gather its status for the user:
```bash
gh pr checks <number> --json name,state,conclusion
gh pr view <number> --json state,mergeable,mergeStateStatus,reviewDecision
```

**AskUserQuestion:**
```
PR #<N> (<title>) is still open. The pipeline waits for merge before creating the next PR.

Status:
- CI: <passing / failing / pending>
- Reviews: <approved / changes requested / pending>
- Mergeable: <yes / no — conflicts>

Options:
1. Wait — say "continue" when the PR is merged
2. Force — create this PR anyway (parallel PRs)
3. Fix — address CI failures or merge conflicts on that PR first
4. Abort — stop pipeline processing
```

- **Wait** → Pause. When user says "continue", re-check PR status. If merged, proceed. If still open, re-ask.
- **Force** → Proceed with PR creation despite open PR. **Stacked PRs are always created as DRAFTS** so they cannot accidentally merge ahead of the bottom of the stack. `/review-monitor` promotes them to ready when the parent (oldest open pipeline PR) merges. The pipeline will continue to track the new PR for merge gating on subsequent tickets — meaning a stack of 3 still leaves later tickets gated on the bottom PR.
- **Fix** → Enter fix mode for the prior PR:
  - If CI failing: spawn agent (`model: "sonnet"`) in that branch's worktree to fix, push
  - If merge conflicts: fetch main, merge, resolve conflicts, push
  - If changes requested: enter Step 5b feedback handling for that PR
  - After fix, re-check status and re-present options
- **Abort** → Stop pipeline, summarize state

**If no open PR from this pipeline:** Proceed immediately.

**Headless:** (Small only — large already exited at S3.)

1. Collect the changed-file list for the current candidate branch:
   ```bash
   git diff --name-only <fork_point_sha>...HEAD
   ```
2. List all other open pipeline PRs:
   ```bash
   gh pr list --author @me --state open --json number,headRefName
   ```
   Filter for branches matching `<branch-prefix>/*`, excluding the current branch.
3. For each such PR, collect its changed-file list:
   ```bash
   gh pr diff <number> --name-only
   ```
   Fallback if unavailable: `git diff --name-only origin/main...origin/<headRefName>`.
4. Compute the intersection of the candidate file list with each open PR's file list.
   - **Non-empty intersection** for any open PR → EXIT `merge_gate_blocked` with populated `blocker`:
     ```json
     "blocker": {
       "stage": "stage4a_merge_gate",
       "reason": "prior_pipeline_pr_open",
       "details": "PR #<number> (<headRefName>) is open and shares files with this branch: <comma-separated overlap list>",
       "recovery_hint": "Wait for PR #<number> to merge, then re-dispatch this ticket.",
       "retry_eligible": true,
       "retry_delay_seconds": null
     }
     ```
     When multiple open PRs overlap, list all overlapping PRs in `details`.
   - **Empty intersection** for ALL open PRs (or no other open pipeline PRs) → proceed to Step 4b. Log: `"All open pipeline PRs are file-disjoint — proceeding to PR creation."`

### Step 4b: Pipeline-Level PR Approval

Present the ship summary to the user before delegating execution. This preserves the pipeline's scope-aware approval gate (scope was classified in Stage 1c and review just completed in Stage 3) — the underlying `/ship-it` is per-project and may not re-ask.

**AskUserQuestion:**
```
Ready to ship PR for <ticket-id>: <title>

Branch: <branch-prefix>/<ticket-id>
Scope: N files changed, ~M lines (<Small|Large>)
Review: <clean / N SHOULD_FIX noted / MUST_FIX fixed>
Tests: all passing
Quality gates: clean (from Stage 2)

Create PR via /prep-pr + /ship-it?
```

- **Yes** → proceed to Step 4c
- **No / Abort** → Stop, report worktree path for manual pickup

**Headless:** AUTO-CREATE PR with auto-merge enabled (skip this AskUserQuestion entirely; go directly to Step 4c).

### Step 4c: Delegate to /prep-pr

**Do NOT spawn anything in this step. The spawn happens in Step 4c.2, and ONLY after
the Step 4c.1 gate has run.** This step delegates PR creation to a `/prep-pr` agent, but
the spawn is **gated**: your **first action is the mandatory isolation check in Step 4c.1**,
whose result sets the spawn's `isolation` flag. Spawning here — before running 4c.1 — is the
exact mistake that causes the guaranteed hang documented in 4c.1 (a headless finalize skipped
straight to the spawn and hung ~40 min; #1097, #1123). The `/prep-pr` agent you spawn in 4c.2
handles: sync-with-main (+ conflict handling), quality gate detection + re-run, and ship-it
delegation (per-project PR creation conventions, branch naming, CI setup).

**Proceed to Step 4c.1 now — do not skip ahead to 4c.2.**

**Why delegate:**
- `/prep-pr` delegates to per-project `.claude/commands/ship-it.md` which knows repo-specific PR conventions (template, labels, reviewers, base branch, CI bootstrap) that the pipeline shouldn't hardcode.
- Sync-with-main and quality-gate-rerun logic live in one place instead of being duplicated between `/auto-dev` Stage 4b and `/prep-pr` Step 1/7.
- PR monitor registration is handled by `/prep-pr` Step 9.

#### Step 4c.1 — MANDATORY isolation gate (run FIRST; NEVER skip) — #766/#1047/#1097

> **STOP.** Do **not** spawn the agent with `isolation: "worktree"` by default. You **MUST**
> run the check below first and let its result choose the `isolation` flag. Skipping this
> gate and defaulting to `isolation: "worktree"` causes a **guaranteed, non-recoverable
> hang**: every cw-spawned finalize — both dev-queue dispatch **and** standalone `cw spawn
> --worktree` (Impl-direct) — already runs inside a pre-provisioned worktree with the feature
> branch checked out. A nested isolation worktree's `git checkout -B <branch>` then collides
> (`fatal: '<branch>' is already used by worktree ...`), the agent improvises an
> `EnterWorktree` into the real worktree, and **that call hangs with no result, killing both
> the child and this finalize session** until the daemon fires `needs_salvage` ~40 min later
> (observed: #1097, session `finalize`; earlier #1031/#1047).

Run this exact check before spawning:

```bash
if [ -f ".claude/cw-context.json" ]; then
  IN_DISPATCH_WORKTREE=true   # cw provisioned this worktree (dispatch OR standalone `cw spawn`)
else
  IN_DISPATCH_WORKTREE=false  # interactive hand-run from a normal checkout — the ONLY isolation case
fi
```

`cw-context.json` is written into the worktree by **both** the dispatch path and standalone
`cw spawn` (`src/cw/spawn.py`), so for every headless/cw-driven finalize the answer is
`true`. `isolation: "worktree"` is reserved solely for a human running `/auto-dev-finalize`
by hand from a normal checkout.

#### Step 4c.2 — spawn the agent, `isolation` flag SET BY the gate

Only now, with `IN_DISPATCH_WORKTREE` decided by Step 4c.1, spawn the agent.
Spawn a **general-purpose** agent (`model: "sonnet"`) scoped to run `/prep-pr`, and set its
`isolation` flag from the gate result per the two cases below:

- **`IN_DISPATCH_WORKTREE=true` (default for every headless/cw-spawned run): OMIT
  `isolation: "worktree"` entirely.** Spawn the agent scoped to the session cwd
  (`worktree_path` in `.claude/cw-context.json` is the authoritative anchor). No nested
  worktree means no branch collision and no primary-checkout path to `cd` into. **Keep the
  git-refresh sequence below unconditionally** (fetch + `checkout -B` + `merge origin/main`)
  — it still pulls in any fix-loop pushes to `origin/<branch>` not yet reflected locally; it
  just runs against the session cwd instead of a spawned worktree.
- **`IN_DISPATCH_WORKTREE=false` (interactive hand-run only): spawn WITH `isolation:
  "worktree"`** and have the agent re-checkout the feature branch from origin (same
  push-then-recheckout pattern as the Stage 3b fix loop) before invoking `/prep-pr`.

**Permission mode (known limitation, #636 — deferred):** In headless/daemon context the worker runs under `claude --bg --permission-mode auto` (cw default, `native_daemon.py` `_DEFAULT_PERMISSION_MODE`), so the `auto` permission classifier fires on `gh pr create` inside the worktree-isolated subagent; with no TTY to approve, the call blocks and `/prep-pr` aborts. The global allowlist `Bash(gh pr:*)` does NOT suppress the classifier here. The *effective* fix is to spawn the worker with a non-`auto` `permission_mode` (cw side, requires the bypassPermissions disclaimer accepted once interactively) — **deferred** to RFC 0005's FINALIZE/REVIEW stages, which own PR creation and must carry this requirement (see #622/#621). Setting `bypassPermissions` on *this subagent spawn alone* is NOT currently effective — the worker's own `auto` mode is the source. Until the stage fix lands, a classifier block surfaces as a BLOCK (below) for manual ship.

**Agent prompt must include:**
- Branch name, fork point SHA (from Checkpoint 2), and ticket ID
- Instruction to re-checkout the branch from origin AND refresh with main:
  ```bash
  git fetch origin
  # -B (not -b): idempotent reset-to-origin. cw provisions the per-ticket
  # worktree on this same feature branch (#712), so -b would fail "already
  # exists"; -B resets it to origin regardless.
  # Why: runs against the session cwd when already in a dispatch worktree
  # (#1047, see Dispatch Detection above) — the -B reset still pulls any
  # fix-loop pushes to origin/<branch> not yet reflected locally.
  git checkout -B <branch-name> origin/<branch-name>

  # Refresh with latest main — catches any upstream commits that landed
  # between the last fix push and now. Required even though /prep-pr
  # also merges main, because /prep-pr runs once; this is belt-and-
  # suspenders for pipelines that push many commits across fix rounds.
  git merge origin/main --no-edit
  ```
  If conflicts → BLOCK with file list; do NOT force.
- Instruction to invoke `/prep-pr --skip-review --base main` via the Skill tool. **If the user chose Force at Step 4a (stacking onto an open pipeline PR), append `--draft`** so `/prep-pr` passes it through to the project's `/ship-it` (`/prep-pr` Step 8 already supports `--draft` pass-through). The PR must be a draft until the parent merges.
  - `--skip-review` is required: Stage 3 already ran scope-aware review with the full reviewer set. `/prep-pr`'s own review pass is thinner and would double up.

  **Headless:** when the finalize stage itself is running headless (`--headless` in "$ARGUMENTS"), the `/prep-pr` invocation MUST include `--headless` so the flag propagates down the delegated `prep-pr` → `ship-it` chain and every interactive gate in it collapses per the gate-collapse table. Compose the invocation string as:
  - Plain case (headless, not stacking): `/prep-pr --skip-review --base main --headless`
  - Force + `--draft` case (headless, stacking onto an open pipeline PR per the Step 4a Force option): `/prep-pr --skip-review --base main --headless --draft`

  Instruct the finalize subagent explicitly: any interactive prompt anywhere in the delegated `prep-pr` → `ship-it` chain that cannot be auto-resolved (e.g. `/prep-pr`'s own Step-7 "Ship anyway" gate, or a project `ship-it.md`'s tag-confirmation prompts) MUST surface as `agent_block` per the existing gate-collapse table (`docs/headless-contract.md` §2, the "Any other agent BLOCK" row) — it must NEVER be silently skipped.
- **Required deliverable:** the JSON output of `~/.claude/scripts/prep_pr_finalize.py verify --require-automerge --json`, run from the worktree after `/prep-pr` returns. The friction report MUST include this JSON verbatim. Do NOT summarize or paraphrase it — paste it.
- Instruction: if `/prep-pr` aborts (no project `/ship-it`, merge conflicts, gate failures), escalate as BLOCK with the specific cause — do NOT fall back to inline `gh pr create`
- The friction protocol block
- The following health check block verbatim:
  ```
  ## Health Check
  - **Context usage**: <rough % or HIGH/MEDIUM/LOW>
  - **On-spec confidence**: HIGH | MEDIUM | LOW
  - **Shortcuts taken under pressure**: [list or NONE]
  - **Could work be incomplete?**: NO | MAYBE | YES (explain)
  - **Recommendation**: PROCEED | EXIT_FOR_HUMAN_REVIEW
  ```

**Unavailability classifier (#1049, generalized #1156 — RFC 0011 A2):** Immediately after the subagent returns — before the verify-script gate below — inspect its returned text (friction report / BLOCK message) for an unavailability failure. This covers the project's `ship-it.md` initial `git push -u origin "$BRANCH"` (the only push site the Step 4c subagent's returned text can reflect). Match any of these signatures verbatim — this list is a PROSE MIRROR of `src/cw/unavailability.py`'s `UNAVAILABILITY_SIGNATURES` (mirror-comment pattern: `cw.reconcile.gate_recipes._PLAN_SPEC_MARKER` mirroring `gh._PLAN_MARKER`); keep the two copies in sync, see `test_unavailability_signatures_mirrored_in_prose` for the drift guard:

- Auth-failure (#1049's original four, unchanged):
  - `Permission denied (publickey)`
  - `could not read Username`
  - `Host key verification failed`
  - `Authentication failed`
- Network-unreachable:
  - `Could not resolve host`
  - `Network is unreachable`
  - `Temporary failure in name resolution`
  - `Failed to connect to`
  - `Could not connect to server`
- GitHub 5xx / secondary-rate-limit:
  - `secondary rate limit`
  - `HTTP 502`
  - `HTTP 503`
  - `HTTP 500`

(`MCP-github-unreachable` is deliberately not mirrored here — no verified signature exists yet, see the module docstring in `src/cw/unavailability.py`.)

(Step 4c.5's own rebase-retry push is a separate site, checked directly by the main session — see the classifier added there below, which reuses this same signature list.)

If any signature is present, emit the structured `blocked` sentinel below and stop — do NOT proceed to the verify-script gate or Step 4c.5.

**Sentinel template — `push_auth_failed` blocker:**

```json
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "blocked",
  "stage_reached": "stage4b_pr_create",
  "scope": {
    "tier": "<resolved scope.tier — see 'Resolve carried-through context' above>",
    "files": <count>,
    "lines_estimate": <count>,
    "lines_actual": <count>,
    "forbidden_touched": false
  },
  "plan_source": "<resolved plan_source — see 'Resolve carried-through context' above>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path — ~/.cw/wt/<hash>/auto-dev-<ticket>>",
  "pr": null,
  "review": {"must_fix_initial": <count>, "should_fix": <count>, "fix_cycles_used": <count>, "deferred": <count>},
  "health": {
    "lowest_agent_confidence": "<HIGH | MEDIUM | LOW from health check>",
    "any_incomplete_risk": false,
    "shortcuts": [],
    "recommendation": "PROCEED",
    "downgrade_applied": false,
    "fix_loop_escalated": false
  },
  "blocker": {
    "stage": "stage4b_pr_create",
    "reason": "push_auth_failed",
    "details": "<matched signature + which push site, e.g. 'ship-it.md initial push: Permission denied (publickey)'>",
    "exception_type": null,
    "message": "git push failed authentication (SSH key locked or credentials expired)",
    "recovery_hint": "Unlock the SSH key (or refresh credentials) and requeue the ticket",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": []
}
```

**Do not add `push_auth_failed` to `FINALIZE_REGRESS_BLOCKER_REASONS`** (`auto_dev_result.py:126`, currently `{"agent_block"}`). A locked SSH key is not fixed by re-running implementation — adding this reason to the regress set would auto-regress FINALIZE→IMPL and burn `FINALIZE_REGRESS_CAP` attempts against a still-locked key. Park for the operator instead via the sentinel above.

**cw-side classification (RFC 0011 A1, #1155):** `push_auth_failed` is now retro-classified under `OPERATOR_UNAVAILABLE_BLOCKER_REASONS` (`auto_dev_result.py`), so cw tags its park with `paused_status: "awaiting_operator_availability"` instead of the generic `"blocked"`. This is a cw-side (`dispatch.py`) routing change only — no change to this producer skill's sentinel shape or logic is required.

**Producer note:** `push_auth_failed` is an open-enum addition to `blocker.reason` (per headless-contract.md §4.2 — `reason` is open by design, same precedent as `merge_conflict_post_push` below). Consumers surface it verbatim; no parser change needed.

**Main-session re-verification (do not skip):** After the subagent returns, re-run finalize from the impl worktree (using the worktree's git context — either `cd <worktree>` or `git -C <worktree>`):

```bash
~/.claude/scripts/prep_pr_finalize.py verify --require-automerge --require-monitor --json
```

Required: parse the JSON. `status` must be `"ok"` and `pr_number` must be non-null before proceeding to Step 4c.5. If either fails, treat the subagent return as a lie — report the failed checks to the user via AskUserQuestion: "Subagent claimed ship complete but finalize failed (<failed-checks>). Re-run /prep-pr in the worktree, skip ticket, or abort pipeline?"

**If the agent returns BLOCK due to "no project `/ship-it`":** The project hasn't been set up for automated PR creation. AskUserQuestion: "Project has no `.claude/commands/ship-it.md`. Create one manually and resume, skip this ticket (leave branch pushed), or abort pipeline?"

### Step 4c.5: Post-Push Mergeability Verification

**Why this exists:** `/prep-pr` performs sync-with-main *before* it creates the PR. Once the PR is open, the world keeps moving — sibling auto-dev sessions, manual merges, or any other pushes to `origin/main` can flip a freshly-opened PR from MERGEABLE to CONFLICTING in seconds. If the worker then exits to the sentinel without re-checking, the PR sits orphaned in a CONFLICTING state until something kicks `/review-monitor`. This step closes that window.

Observed incident: 2026-05-27, claude-workspace #305 — three sessions dispatched in parallel touched the same module; two merged first; the third opened PR #309 *already CONFLICTING* and the worker timed out without ever rebasing or blocking.

**Sequence (headless and interactive alike):**

After Step 4c's main-session re-verification confirms `pr_number` is non-null:

```bash
gh pr view <pr_number> --repo <owner>/<repo> --json mergeable,mergeStateStatus
```

Parse the response. Then:

| `mergeable` | `mergeStateStatus` | Action |
|---|---|---|
| `MERGEABLE` | `CLEAN` / `UNSTABLE` / `HAS_HOOKS` / `UNKNOWN` | **Proceed to Step 4d.** Mergeable wins regardless of secondary status. |
| `CONFLICTING` | `DIRTY` | **One auto-rebase attempt.** See below. |
| `MERGEABLE` | `BEHIND` | **One auto-rebase attempt.** See below. |
| anything else | `BLOCKED` | **Proceed to Step 4d.** BLOCKED means required reviews / branch protection — not a code-fixable conflict; the `/review-monitor` cycle handles it from here. |

**Single auto-rebase attempt (no loops):**

```bash
# In the impl worktree
git fetch origin main
git rebase origin/main
# If rebase fails with conflicts here → abort and emit blocker (see below)
git push --force-with-lease origin HEAD:<branch-name>
```

**Push-auth-failure check (#1049):** Before re-verifying mergeability, check this push's own output for the same signature family listed under the Step 4c classifier above (`Permission denied (publickey)`, `could not read Username`, `Host key verification failed`, `Authentication failed`). If any signature is present, emit the `push_auth_failed` sentinel (same shape as the Step 4c template above) with `stage_reached` and `blocker.stage` set to `"stage5_post_create"` (this site runs after PR creation) and `blocker.details` naming this site, e.g. `"Step 4c.5 rebase-retry push: Permission denied (publickey)"`. Stop — do not proceed to the mergeability re-check below.

After the push, re-verify mergeability once:

```bash
gh pr view <pr_number> --repo <owner>/<repo> --json mergeable,mergeStateStatus
```

- **Now MERGEABLE** → log to the cycle summary: `auto-rebase succeeded, PR <N> now mergeable`. Proceed to Step 4d.
- **Still CONFLICTING / DIRTY / BEHIND, OR rebase aborted on conflict** → emit the structured `blocked` sentinel (template below). Do **not** force, do **not** attempt a second rebase, do **not** fall through silently.

**Sentinel template — `merge_conflict_post_push` blocker:**

```json
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "blocked",
  "stage_reached": "stage5_post_create",
  "scope": {
    "tier": "<resolved scope.tier — see 'Resolve carried-through context' above>",
    "files": <count>,
    "lines_estimate": <count>,
    "lines_actual": <count>,
    "forbidden_touched": false
  },
  "plan_source": "<resolved plan_source — see 'Resolve carried-through context' above>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path — ~/.cw/wt/<hash>/auto-dev-<ticket>>",
  "pr_info": {
    "number": <pr_number>,
    "url": "<pr_url>",
    "auto_merge": <bool>,
    "base": "main"
  },
  "review": null,
  "health": {
    "lowest_agent_confidence": "<HIGH | MEDIUM | LOW from health check>",
    "any_incomplete_risk": false,
    "shortcuts": [],
    "recommendation": "PROCEED",
    "downgrade_applied": false,
    "fix_loop_escalated": false
  },
  "blocker": {
    "stage": "stage5_post_create",
    "reason": "merge_conflict_post_push",
    "details": "PR #<N> opened with conflicts after sibling merges to origin/main between /prep-pr's sync-with-main and PR open. One auto-rebase attempted and failed; conflicted files: <list>",
    "exception_type": null,
    "message": "PR is open but conflicts with main; auto-rebase failed",
    "recovery_hint": "Manual rebase in the impl worktree, OR close PR #<N> and re-dispatch the ticket",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": ["manual_intervention"]
}
```

**Critical: every field above is required.** `scope`, `plan_source`, and `health` MUST be populated with real values carried through from earlier stages. Emitting `null` or omitting them causes the consumer to fail schema validation and synthesize a separate `validation_failed` blocker — masking the real `merge_conflict_post_push` reason. This is a wider producer-side discipline (see the substrate ticket on blocker-path schema completeness).

**Producer note:** `merge_conflict_post_push` is an open-enum addition to `blocker.reason` (per headless-contract.md §4.2 — `reason` is open by design). Consumers surface it verbatim; no parser change needed.

**Defense-in-depth handoff:** the blocker is `retry_eligible: true` because `/review-monitor` (per the companion change in this PR) auto-engages on orphaned CONFLICTING PRs authored by `@me`. If `/review-monitor` succeeds in rebasing, the orchestrator can safely re-dispatch this ticket and pick up where this run left off. If `/review-monitor` also fails, human intervention takes over via `recovery_hint`.

### Step 4d: Post-Ship Pipeline Bookkeeping

After `/prep-pr` returns with a PR number:

1. **UI Evidence Gate (orchestrator-run fact gate):** project `/ship-it` skills are expected to attach screenshots / video for UI changes, but agent self-report has been observed to silently fall through to "skip with a note." Mitigation 1 philosophy (Orchestrator-Run Completion Gates) applies — verify deterministically against the PR body, do not trust the ship-it agent's prose:

   ```bash
   # Detect non-test frontend files in the shipped diff
   ui_files=$(git diff --name-only <FORK_POINT>...origin/<branch> \
     | grep -E '^frontends/.*\.(tsx|ts)$' \
     | grep -v -E '(__tests__|\.test\.|\.spec\.)' || true)

   # If any, check the PR body for embedded-media markers
   if [ -n "$ui_files" ]; then
     body=$(gh pr view <pr-number> --json body --jq .body)
     if ! echo "$body" | grep -qE '<img|!\[.*\]\(.*\)|user-attachments|\.webm|\.mp4'; then
       echo "UI_EVIDENCE_MISSING"
     fi
   fi
   ```

   Project repos can override the frontend-file regex by placing a single-line `ui_paths_regex:` entry in `.claude/project-config.yaml` — the gate falls back to the default `^frontends/.*\.(tsx|ts)$` (minus test files) when the entry is absent. Repos with no UI surface should set `ui_paths_regex: ^$` so the gate becomes a no-op.

   **If the gate fires (UI files present, no media markers in body):**

   *Interactive:* AskUserQuestion:
   ```
   PR #<N> changed UI files but the body contains no screenshots / video:
     <list of ui_files>

   Options:
   1. Capture now — spawn an agent in the worktree to run playwright-cli and embed in the body
   2. Ship anyway — proceed to auto-merge (you'll attach evidence post-merge)
   3. Hold — leave PR open, do NOT enable auto-merge, exit ticket
   ```
   - **Capture now** → apply the same Dispatch Detection test as Step 4c (`.claude/cw-context.json` present). **In a dispatch worktree:** spawn a `general-purpose` agent (`model: "haiku"`, `run_in_background: true`, no `isolation` key) scoped to the session cwd — same #766/#1047 rationale as Step 4c. **Otherwise:** spawn a `general-purpose` agent (`isolation: "worktree"`, `model: "haiku"`, `run_in_background: true`). Either way, with the playwright-cli capture + `gh pr edit --body` instructions from the project's `/ship-it` Step 6b. Re-run this gate after the agent returns. Max 2 capture attempts before falling through to the "Hold" branch.
   - **Ship anyway** → continue to step 2 (auto-merge enable). Append `"ui_evidence_missing_user_override"` to `friction_highlights`.
   - **Hold** → skip step 2 entirely (do NOT enable auto-merge). The PR exists but waits on the human to attach evidence and run `gh pr merge --auto --squash` manually. Set `pr.auto_merge: false` and `next_actions: ["attach_ui_evidence"]` in the structured output (interactive runs don't emit structured output, but a summary line at end-of-pipeline should mention it).

   *Headless:* never block on this — append `"ui_evidence_missing"` to `friction_highlights`, set `pr.auto_merge: false`, set `next_actions: ["attach_ui_evidence_and_enable_automerge"]`, skip step 2, continue to step 3. Status remains `shipped` because the PR exists; the human sees the missing-evidence signal in the structured output and decides whether to attach evidence and merge or close.

   **If the gate is clean** (no UI files in diff, OR UI files plus media markers in body): proceed to step 2.

   **Why fact-gated rather than trusting the ship-it agent:** the same way Mitigation 1 treats `git diff --stat` as filesystem truth, this gate treats the PR body grep as truth. The `/ship-it` agent may report "screenshots attached" with HIGH confidence and still have skipped the step — only the body content is binding.

2. **Append review adjudication to the PR body (record-now for DEFER + REJECT):** Stage 3 (Checkpoint 3a) owns adjudication and stashes outcomes in `.cw/deferred-findings.md`. Read that file now and write its content into the PR body. The pipeline session is gone by merge time (especially under `auto_merge: false`), so filing the deferrals must be merge-triggered — Step H3 of the next sweep harvests the `DEFERRED-REVIEW-FINDINGS` block. Read the current PR body (`gh pr view <pr-number> --json body --jq .body`), append the artifacts from `.cw/deferred-findings.md`, and re-write via `gh pr edit <pr-number> --body-file -`:

   The format written to the PR body (taken verbatim from `.cw/deferred-findings.md`):

   ```
   ## Review adjudication

   Rejected (intentional / documented tradeoff):
   - eval/runner.py — "drop retry wrapper" — deliberate: caller already retries at the queue layer

   <!-- DEFERRED-REVIEW-FINDINGS
   - severity: SHOULD_FIX
     summary: "extract shared retry helper"
     file: eval/runner.py
     rationale: "out of scope; revisit when a 3rd caller appears"
   DEFERRED-REVIEW-FINDINGS -->
   ```

   One block per PR; list every deferred finding inside the single `DEFERRED-REVIEW-FINDINGS` comment (open/close sentinels exact — Step H3 greps them verbatim). Omit the whole section when `.cw/deferred-findings.md` is absent or empty (every finding was fixed — no rejections, no deferrals). For pipeline exits that never create a PR (large-scope `review_pending_approval`, or a BLOCK), there is no body to write — rejections/deferrals stay in `friction_highlights` and surface to the human in the structured output instead.

3. **Enable auto-merge:** `gh pr merge <pr-number> --auto --squash`. GitHub allows enabling auto-merge on a draft PR — the merge won't trigger until the PR is marked ready (which `/review-monitor` does when the parent in the stack merges) AND CI passes. Enable unconditionally here, EXCEPT when the UI Evidence Gate above resolved to "Hold" (interactive) or fired in headless — in those cases this step is skipped and `pr.auto_merge` is set to `false`.
4. **Post to Linear:** Comment on the issue with PR link (skip for free-text tickets). For drafts, note in the comment: "Created as draft — stacked behind PR #<parent>; will auto-promote to ready when parent merges."
5. **Store pipeline state:** Record PR number, branch, ticket ID for the merge gate check in Step 4a of the next ticket
6. **Headless only — emit `stage.entered` (`s4_pr_created`) then proceed to Stage 5:**
   ```bash
   cw event record stage.entered \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s4_pr_created\",\"prev_stage\":\"s3_review_complete\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
   ```
7. **Proceed to Stage 5** (CI Wait)

Note: monitor registration happens inside `/prep-pr` Step 9 — do NOT re-register here.

---

## Stage 5: CI Wait

**Headless only — on entering Stage 5, emit `stage.entered` (`s5_ci_waiting`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s5_ci_waiting\",\"prev_stage\":\"s4_pr_created\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

After every push (PR creation or fix push), wait up to 10 minutes for CI to complete.

### Step 5a: Wait for CI (10 minutes max)

```bash
# Poll every 30 seconds for up to 10 minutes
gh pr checks <number> --watch --fail-fast 2>/dev/null
# If --watch not available, poll manually:
# Loop: gh pr checks <number> --json name,state,conclusion
# Exit when: all checks conclude, or 10 minutes elapsed
```

**If all checks pass within 10 minutes:** Log "CI passing" and proceed to Step 5b.

**If any check fails:**

**AskUserQuestion:**
```
CI failed on PR #<N> (<title>):

<failed check name>: <conclusion>
<failure details via: gh pr checks <number> --json name,state,conclusion,detailsUrl>

Options:
1. Fix — I'll investigate and push a fix (triggers another 10m CI wait)
2. Ignore — proceed to next ticket (auto-merge stays pending)
3. Abort — stop pipeline
```

- **Fix** → Spawn agent (`model: "sonnet"`) in the worktree to investigate CI failure, apply fix, push to branch. Loop back to Step 5a for the new push. Max 2 fix attempts, then escalate.
- **Ignore** → Proceed (user handles CI manually)
- **Abort** → Stop pipeline

**If checks still pending after 10 minutes:** Log "CI still running after 10m — proceeding. Auto-merge will complete when CI passes." Proceed to Step 5b.

**Headless:** AUTO-SKIP entire Stage 5 — return immediately after auto-merge is enabled in Step 4d. Do not poll CI; do not run Step 5b feedback handling.

### Step 5b: Initial Review Feedback Check

Check for early review comments (reviewers may be fast, or may have been tagged for auto-review):

```bash
gh api repos/{owner}/{repo}/pulls/<number>/reviews --jq '.[] | select(.state != "COMMENTED" and .state != "APPROVED") | {user: .user.login, state: .state, body: .body}'
gh api repos/{owner}/{repo}/pulls/<number>/comments --jq '.[] | {user: .user.login, path: .path, line: .line, body: .body}'
```

**If no reviews or only APPROVED/COMMENTED:** Log "No review feedback requiring action" and proceed.

**If CHANGES_REQUESTED found:**

**AskUserQuestion:**
```
PR #<N> has review feedback:

Reviewer: <username> — Changes Requested
<review body summary>

Inline comments:
- <file>:<line> — "<comment body>" (<username>)
- ...

Options:
1. Address — I'll fix the requested changes and push updates (triggers 10m CI wait)
2. Skip — proceed to next ticket (you'll address feedback manually)
3. Discuss — I'll draft reply comments for your review before posting
```

- **Address** → Spawn agent (`model: "sonnet"`) in the worktree. Agent reads all review comments, applies fixes, pushes to branch. Post a reply to each addressed comment summarizing the fix. Loop back to Step 5a for CI wait on the new push.
- **Skip** → Proceed to next ticket
- **Discuss** → Draft reply comments for each piece of feedback. Present drafts to user via AskUserQuestion before posting. Post approved replies via `gh api`.

### Step 5c: Continue to Next Ticket

1. If more tickets in queue: loop back to **PR Hygiene Sweep** (top of ticket loop) for the next ticket.
2. If no more tickets: proceed to Pipeline Summary.

---

## Stage 4+5 Completion (headless only)

After PR creation, auto-merge enablement, and CI monitoring are complete, emit the `done` event and the `AUTO_DEV_RESULT` sentinel.

**Only in standalone `/auto-dev-finalize <ticket-id> --headless` invocation. In the interactive monolith chain, `auto-dev.md` owns the `done` event and the final sentinel.**

```bash
# Emit done event (pipeline path only)
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"done\",\"prev_stage\":\"s5_ci_waiting\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true

# Validate and emit sentinel
printf '%s' "$SENTINEL_JSON" | cw result validate -
```

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "<shipped | merge_gate_blocked | blocked>",
  "stage_reached": "stage5_post_create",
  "scope": {"tier": "<resolved scope.tier — see 'Resolve carried-through context' above>", "files": 0, "lines_estimate": 0, "lines_actual": 0, "forbidden_touched": false},
  "plan_source": "<resolved plan_source (github_issue_existing | generated | free_text | none) — see 'Resolve carried-through context' above>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<sha1>", "<sha2>"],
  "pr": {
    "number": 0,
    "url": "<pr url>",
    "auto_merge": true,
    "base": "main"
  },
  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
  "health": {
    "lowest_agent_confidence": "<HIGH|MEDIUM|LOW>",
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

Note: when `status` is `merge_gate_blocked` due to file overlap with an open pipeline PR, `blocker` may be non-null:
```json
"blocker": {
  "stage": "stage4a_merge_gate",
  "reason": "prior_pipeline_pr_open",
  "details": "PR #<N> (<branch>) is open and shares files: <list>",
  "recovery_hint": "Wait for PR #<N> to merge, then re-dispatch this ticket.",
  "retry_eligible": true,
  "retry_delay_seconds": null
}
```

The sentinel must be the LAST thing emitted. No trailing prose, no further tool calls. See `auto-dev.md` Appendix for full field reference. Contract: `cw schema stage-output finalize`.
