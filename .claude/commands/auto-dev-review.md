---
description: "auto-dev Stage 3: Review — spawn review agents, adjudicate findings, fix loop"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "AskUserQuestion", "Skill"]
---

# auto-dev Stage 3: Review

**Orientation:** Read `.cw/context.json` for ticket context and `.cw/plan.md` for the approved plan. The feature branch must already be pushed to origin (Stage 2 complete).

This stage runs the full review pass AND the fix loop (Step 3a + Step 3b) when MUST_FIX findings exist. It does NOT create a PR — PR creation is Stage 4 (`auto-dev-finalize.md`).

In standalone headless invocation: emit `AUTO_DEV_RESULT` after this stage completes. In the interactive monolith chain: do NOT emit the sentinel here.

**Arguments:** "$ARGUMENTS"

---

## Stage 3: Review (Agents)

**Headless only — before spawning reviewers, emit `stage.entered` (`s3_review_started`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s3_review_started\",\"prev_stage\":\"s2_impl_complete\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

### Step 3a: Spawn Review Agents

**Small scope:** Spawn these reviewers:
- Code Quality Reviewer (`subagent_type: "Code Quality Reviewer"`)
- SysAdmin Reviewer (`subagent_type: "SysAdmin Reviewer"`)
- Data Safety Reviewer (`subagent_type: "Data Safety Reviewer"`) — only when the diff mutates persisted state (any DB write, external-system write, or `SENSITIVE_HITS` non-empty); skip on doc/config/style-only diffs
- Product Manager Reviewer (`subagent_type: "Product Manager Reviewer"`, Mode 2 — spec compliance)

**Large scope:** Spawn full reviewer set based on file categories (per `/review` command patterns):
- Code Quality (always)
- Architecture (any code changed)
- Test Quality (test files changed or testable code without test changes)
- Performance (Python DB/API/service layer)
- API Contract (both backend + frontend changed)
- Deployment (infra files changed)
- SysAdmin / Scope (always)
- Data Safety (always when persisted-state mutation is present)
- Product Manager (always — Mode 2 spec compliance)

Dispatch shape depends on mode (see issues #175 / #176 in claude-workspace for the orphan hazard this avoids):

- **Interactive mode:** all reviewers run with `run_in_background: true` (parallel — a human is watching and USER-origin Stop hooks do not auto-transition session state).
- **`--headless` mode:** reviewers run **serially** (no `run_in_background: true`). Block on each before dispatching the next, and do NOT end the parent turn between them. Background dispatch in headless trips the cw-side Stop-hook session-completion the same way Step 1b's Plan-agent dispatch did before `750ea77` — the parent's post-wait turn ends with `background_tasks: []` while pipeline work remains, orphaning the run with no sentinel. Losing parallelism is the price of correctness; the reviewer set typically completes in under 4 minutes serially for a Small-scope diff.

**Sandbox warning**: reviewer subagents spawned without `isolation: "worktree"` may have inconsistent file access depending on sandbox state — sometimes reads work, sometimes they're denied. The safest pattern is to **inline the full diff directly in each reviewer's prompt** (captured from the main session). This lets reviewers evaluate purely from the prompt content without needing filesystem access. Do not assume read access "just works."

**Before spawning reviewers, load project-specific extensions** (both optional, both forwarded into every reviewer prompt):
- `.claude/review-extras.md` at the project root — free-form prose rubrics the project owner wants every reviewer to apply on top of the global agent specs. Read verbatim. If absent, set `PROJECT_RUBRICS = null`.
- `.claude/sensitive-files.yml` at the project root — manifest of high-blast-radius paths. If present, diff the changed-files list against the manifest's globs. For every match, capture `(file_path, reason, category)` into `SENSITIVE_HITS`. If absent or no matches, set `SENSITIVE_HITS = []`.

**Every reviewer prompt must include:**
- **The full diff inlined as text in the prompt.** Before computing, fetch the branch so origin refs are current (the impl agent pushes from an isolation worktree; local refs may not reflect the push):
  ```bash
  git fetch origin <branch-name>
  git diff <FORK_POINT>...origin/<branch-name>
  ```
  Use `origin/<branch-name>`, not the bare local ref — the feature branch was pushed to origin from an isolation worktree and may not be visible in local refs until fetched. Use the fork point SHA from Checkpoint 2 for a deterministic diff. Do NOT use `origin/main` — it may have advanced since the worktree was created.
  For small scope, include the whole diff. For large scope, you may summarize the non-critical files but always inline the primary ones.
- Changed file list
- **`PROJECT_RUBRICS` block** (inline verbatim if non-null, omit the section entirely if null):
  ```
  ## Project-Specific Rubrics

  <verbatim contents of .claude/review-extras.md>
  ```
- **`SENSITIVE_HITS` block** (inline if non-empty, omit if empty):
  ```
  ## Sensitive Files Touched

  This diff modifies files the project flagged as high blast-radius. Apply maximum scrutiny when reviewing these paths — unintended scope changes, missing auth checks, new external write paths, error handling gaps, cross-org/tenant data leakage, destructive defaults.

  - <file_path> — <category>: <reason>
  ```
- **Business Context** (inlined verbatim — required for every reviewer, not just the Product Manager Reviewer):
  - Ticket ID and title
  - Ticket description (full text)
  - All ticket comments in chronological order (via the tracker's comment-fetch op — `list_comments` for `linear`, `gh issue view <n> --json comments` for `github-issues`) — decisions often live in comments and supersede the description
  - Step 1c ambiguity resolutions (if any were collected) — the answers the human gave to ambiguous questions
  - For free-text tickets: the user-supplied description, marked as `[free-text, no Linear ticket]`
- Review focus areas:
  1. Does the change address the actual ticket? (PM Reviewer owns this lens; other reviewers flag only if blatantly obvious from their own domain.)
  2. Did implementation stay within plan scope? Flag creep.
  3. Do tests validate meaningful behavior?
  4. Could this break anything downstream?
  5. Debug artifacts left in? (`print()`, `breakpoint()`, `pdb`, `ic()`)
- **Product Manager Reviewer only:** prepend `Mode: spec compliance` to the prompt (Mode 2 per the agent spec). Other reviewers do not need a mode declaration.
- Standard output rules: MUST_FIX / SHOULD_FIX only, no praise, NO_ISSUES if clean
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

### Checkpoint 3 (Review Approval)

Consolidate review results: deduplicate, sort by severity, group by file.

**Small scope + (NO_ISSUES or SHOULD_FIX only) → AUTO-ACCEPT.** Log:
- Review outcome: "Review clean" or "N SHOULD_FIX items noted — auto-accepted per small scope"
- List SHOULD_FIX items for transparency

**Small scope + MUST_FIX → AskUserQuestion:**
- Present MUST_FIX findings (with file, line, description, suggested fix)
- Present SHOULD_FIX findings if any
- "MUST_FIX findings block shipping. Fix and re-review, skip fixes and ship anyway, skip ticket, or abort?"

**Large scope (any result) → AskUserQuestion:**
- Present full consolidated review report
- If MUST_FIX: "Fix these issues and re-review, or abort?"
- If clean or SHOULD_FIX only: "Review complete. Proceed to PR creation?"

**Headless:** Always run reviewers. MUST_FIX → run fix loop (expected 2 cycles, hard-cap at 5; cycles 3+ or scope growth append to `friction_highlights` and set `health.fix_loop_escalated: true`). Clean/SHOULD_FIX + small → emit `stage.entered` (`s3_review_complete`) then AUTO-CONTINUE to S4:
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s3_review_complete\",\"prev_stage\":\"s3_review_started\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```
Clean/SHOULD_FIX + large → EXIT `review_pending_approval`. MUST_FIX persists after 5 cycles → EXIT `blocked` with `blocker.reason: "review_blocked"`.

### Step 3b: Fix Loop (when MUST_FIX needs fixing)

**Important**: you cannot attach a new subagent to the original implementation worktree. Subagents spawned without `isolation: "worktree"` inherit the main session's sandbox, which typically does not include other worktrees. `isolation: "worktree"` always creates a *new* worktree, not an attachment to an existing one. The correct pattern is **push-then-recheckout**.

Prerequisite: the implementation branch must already be on origin. Step 2's Implementation agent should have pushed per its instructions — if not, escalate BLOCK before starting the fix loop.

1. Remove the stale implementation worktree and the local branch ref from the main session (the branch still exists on origin):
   ```bash
   git worktree remove --force <impl-worktree-path>
   git branch -D <branch-name>  # local only; origin still has it
   ```

2. Spawn the fix agent with `isolation: "worktree"` and `run_in_background: true`. The agent's **first actions** must be:
   ```bash
   git fetch origin
   git checkout -b <branch-name> origin/<branch-name>
   git log --oneline -1  # verify at expected impl commit

   # Refresh with latest main so the fix lands on top of upstream moves.
   # Failure mode avoided: a sibling PR (e.g. another ticket in this same
   # pipeline run) may have merged to main AFTER Step 2 pushed. Without
   # this merge, subsequent pushes ship a branch that's silently missing
   # commits from main — CI passes because it runs branch-HEAD, not the
   # branch-merged-with-main state.
   git merge origin/main --no-edit
   ```
   If merge conflicts occur → BLOCK with file list. Do not force.

3. Agent fixes MUST_FIX issues, re-runs quality gates, creates a NEW commit on top (do NOT amend) **with the trailer `Auto-Dev-Fix-Cycle: <N>`** (where `<N>` is the current cycle number, 1-5; pass via `git commit --trailer "Auto-Dev-Fix-Cycle: <N>"`), and pushes to origin using the explicit-refspec form (`git push origin HEAD:refs/heads/<branch-name>`) — defensive form, robust against any local branch rename even after the `git checkout -b <branch-name> origin/<branch-name>` in step 2 above. After pushing, verify with `git rev-parse origin/<branch-name>` matching `git rev-parse HEAD`.

The `Auto-Dev-Fix-Cycle` trailer is the durable cross-session signal for fix-loop progress. The resume detector reads the max `<N>` across fix-cycle trailers on commits newer than `Auto-Dev-Stage: impl-complete` to determine current cycle. On resume into `s3_fix_loop, substage="cycle_N"`, the pipeline resumes at cycle `N+1` (next iteration), not from cycle 1 — preserving the cycle budget across session deaths.

The fix-loop agent's prompt must end with both the Friction Protocol block and the following Health Check block verbatim:
   ```
   ## Health Check
   - **Context usage**: <rough % or HIGH/MEDIUM/LOW>
   - **On-spec confidence**: HIGH | MEDIUM | LOW
   - **Shortcuts taken under pressure**: [list or NONE]
   - **Could work be incomplete?**: NO | MAYBE | YES (explain)
   - **Recommendation**: PROCEED | EXIT_FOR_HUMAN_REVIEW
   ```

   The fix-loop agent's prompt must ALSO include the same **Completion Artifacts** block as Stage 2 (Test command, test output tail, `git diff --stat`, `git log --oneline`, mypy/ruff results) — the orchestrator gates fix completion on facts the same way it gates impl completion (Subagent Reliability Mitigation 2). Incremental commits same as Stage 2 (Mitigation 3): one commit per MUST_FIX item resolved, not a single end-of-loop commit.

   The fix-loop agent's prompt must ALSO instruct: "If your fix touches any file outside the original Stage 1 approved plan's file list, OR if your changes push the diff into Large tier (>10 files OR >500 lines OR a forbidden area), report this in the friction report under a new bullet `**Scope growth**: [list affected files / explain tier change]`. The main session uses this to decide escalation."

3b. **Orchestrator fix gate** (Subagent Reliability Mitigation 1, fix-loop variant). After the fix-loop agent returns (or times out), before re-running review (or before the sparse-feedback gate in step 4):
   - Re-run the test command in the impl worktree. Non-zero exit → fix is false; treat as cycle failure (counts against the 5-cycle hard cap).
   - Re-run mypy/ruff. Non-zero on touched files → fix is false.
   - Compare pasted `git diff --stat` against live `git diff --stat $FORK_POINT`. Substantial mismatch → fix is false.
   - Verify the fix produced at least one new commit since the prior cycle (`git log $PRIOR_HEAD..HEAD --oneline` must be non-empty). Zero new commits → fix-loop agent did not actually fix anything; treat as cycle failure.

   On gate failure: log the failed check, increment the cycle counter, and re-spawn the fix agent (within the 5-cycle hard cap). Headless: append `"fix_loop_gate_failed_cycle_<N>"` to `friction_highlights`, and emit `stage.errored`:
   ```bash
   cw event record stage.errored \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s3_review_started\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"error_kind\":\"fix_cycle_failed\"}" || true
   ```
   The cycle still consumed budget — a false-completion fix counts against the cap.

   On all gates pass: proceed to step 4 (sparse gate / re-review).

4. **Sparse-feedback gate, then re-run review.** Before re-running review, check whether the cycle qualifies for the sparse-feedback skip. The fix-then-rereview cycle is NOT mandatory when initial feedback was sparse and the fix is small relative to the original change.

   **Skip re-review when ALL of the following hold (Small scope only):**
   - Scope tier was **Small** at Stage 1c, AND no scope growth was flagged in the fix-loop friction report (still Small)
   - Initial review produced ≤2 MUST_FIX items
   - Fix-loop diff is small relative to the original implementation diff — judgment call, no hard line ceiling. A 2-line touch on a 50-line PR is sparse; a rewrite of half the implementation is not. Proportionality is what matters.
   - Fix did not touch files outside the original Stage 1 plan's file list
   - No SHOULD_FIX items adjacent to the MUST_FIX areas were left unaddressed in a way that warrants a second look

   When skipping: log `Skipping re-review — Small scope, sparse fix (<N MUST_FIX> resolved, fix diff small relative to original). Proceeding to Stage 4.`, document the decision in the friction report under `**Re-review skipped**`, and jump directly to Stage 4.

   When in doubt, run re-review. The skip is for unambiguously small fixes only.

   **Headless:** apply the same criteria deterministically. If all conditions hold, skip re-review and proceed to S4 AUTO-CREATE; append `"rereview_skipped_sparse"` to `friction_highlights`. If any condition is uncertain, run re-review.

   **Otherwise, re-run review agents (same set).** Before computing the updated diff, fetch the branch to pick up the fix-loop agent's push:
   ```bash
   git fetch origin <branch-name>
   ```
   Then pass the updated full diff (`git diff <FORK_POINT>...origin/<branch-name>`) and the fix-commit diff inlined in each reviewer's prompt (see Step 3a sandbox warning). Do NOT rely on reviewers reading files from disk. Each re-spawned reviewer prompt MUST include both the Friction Protocol block and the Health Check block, identical to the initial Step 3a spawn.

5. **Cycle budget:** 2 cycles is the expected baseline. If MUST_FIX persists past cycle 2, the loop may continue with escalation visibility, hard-capped at 5 total cycles. Escalation behavior differs between modes — see below.

   **Escalation triggers** (any of these counts as "an escalation event"):
   - Cycle 3, 4, or 5 entered (i.e., MUST_FIX persisted past the expected 2)
   - Fix-loop diff touches files outside the original Stage 1 approved plan's file list
   - Fix-loop diff promotes scope tier from Small → Large (file count > 10 OR line count > 500 OR forbidden area touched)

   The fix-loop agent's friction report MUST flag scope growth explicitly so the main session can decide whether the cycle counts as an escalation event. Do not let the agent silently grow scope.

   **Interactive — on each escalation event:** log a one-line notice to the user describing the trigger (e.g., `⚠ Fix loop entered cycle 3 (expected baseline is 2)` or `⚠ Fix loop cycle 2 grew scope outside plan: <files>`). Do NOT block on AskUserQuestion for these — the user can stop the pipeline between agent dispatches after seeing the notice, and the cycle-5 hard gate provides the final decision point. This is a deliberate trade-off: the prior spec gated at cycle 2; the current spec exchanges that early hard gate for reduced prompt fatigue, accepting that interactive cycles 3-4 will run with notice-only visibility rather than gating.

   **Headless — on each escalation event:** append a string to `friction_highlights` (e.g., `"fix_loop_cycle_3_entered"`, `"fix_loop_scope_growth: <files>"`) AND set `health.fix_loop_escalated: true` in the structured output payload. Continue the loop without any AskUserQuestion. (`health.fix_loop_escalated` is distinct from `health.downgrade_applied`, which is set only by the Headless Mode health aggregation rule for confidence-driven status downgrades.)

   **Hard exit (cycle 5 failed to clear MUST_FIX) — applies in both modes:**
   - **Interactive:** AskUserQuestion: "MUST_FIX issues persist after 5 fix cycles: [details]. Continue manually from worktree, skip ticket, or abort pipeline?"
   - **Headless:** EXIT `blocked` with `blocker.reason: "review_blocked"`. The `friction_highlights` field will contain the per-cycle escalation notes from cycles 3–5; the human reviewer sees them in the structured output.

   > **Maintenance note:** the cap values (`expected 2`, `hard-cap at 5`) appear in 6 locations: this Step 3b.5 (multiple), the Checkpoint 3 Headless callout, the gate-collapse table rows for `S3 MUST_FIX`, `S3 MUST_FIX persists after 5 cycles`, and `S3 fix-loop cycle 3+`, and the `blocker.reason` table description for `review_blocked`. If you tune either value, update all locations atomically.

**Fallback — direct execution**: If the isolation fix agent also hits sandbox failures (Read/Write/Bash denied inside its own new worktree — this has been observed), the main session can apply the fix directly from its own worktree:

```bash
# From the main session's worktree
git fetch origin <branch-name>
git checkout -b <branch-name> origin/<branch-name>   # or git checkout <branch-name> if already local
git merge origin/main --no-edit                      # refresh with main (see Step 3b.2 rationale)
# apply edits via Read/Edit/Write tools
# run quality gates
git add -- <changed files>
git commit -m "..."
git push origin HEAD:refs/heads/<branch-name>        # explicit refspec — robust if local branch was renamed
test "$(git rev-parse origin/<branch-name>)" = "$(git rev-parse HEAD)"  # verify the push landed
git checkout <original-branch>   # restore main session state
```

Direct execution is slower than delegation but guaranteed to work. Use it as a last resort when two subagent attempts have failed due to sandbox issues.

---

## Stage 3 Completion (headless only)

After all Stage 3 steps complete successfully in headless mode (review clean or fix loop resolved, branch pushed with fix commits), emit the `AUTO_DEV_RESULT` sentinel:

**Only emit this sentinel when invoked as a standalone `/auto-dev-review <ticket-id> --headless` command. Do NOT emit when running as part of the interactive monolith chain (`auto-dev.md` owns the sentinel in that context).**

```bash
printf '%s' "$SENTINEL_JSON" | cw result validate -
```

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "<review_pending_approval | blocked>",
  "stage_reached": "stage3_review",
  "scope": {"tier": "<small|large>", "files": 0, "lines_estimate": 0, "lines_actual": 0, "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<sha1>", "<sha2>"],
  "pr": null,
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

See `auto-dev.md` Appendix for the full field reference and status enum. The contract for this stage's output is `cw schema stage-output review`.
