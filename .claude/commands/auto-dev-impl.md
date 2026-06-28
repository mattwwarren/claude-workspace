---
description: "auto-dev Stage 2: Implement — spawn impl agent in worktree, commit, push branch"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Agent", "Skill"]
---

# auto-dev Stage 2: Implement

**Orientation:** Read `.cw/plan.md` for the approved plan from Stage 1. If absent, Stage 1 has not completed — do not proceed without an approved plan. Read `.cw/context.json` for ticket context (or prose-delegate to `auto-dev-intake.md` first if absent).

In standalone headless invocation: emit `AUTO_DEV_RESULT` after this stage completes. In the interactive monolith chain: do NOT emit the sentinel here; the monolith owns it.

**Arguments:** "$ARGUMENTS"

---

> **Model selection:** The implementation agent spawned here is pinned to `model: "opus"` — the only stage that unambiguously requires Opus-level reasoning. Do not change to `model: inherit` — see CLAUDE.md §"Model Selection for Subagents" for the rationale and tier matrix.

## Stage 2: Implement (Agent in Worktree)

### Dispatch Detection — #766 (skip redundant EnterWorktree when already in a cw worktree)

**Before spawning the Stage 2 agent, check whether this session is already running
inside a cw dispatch worktree:**

```bash
if [ -f ".claude/cw-context.json" ]; then
  IN_DISPATCH_WORKTREE=true
else
  IN_DISPATCH_WORKTREE=false
fi
```

Alternatively: inspect `.claude/cw-context.json` for `"headless": true` — that field
is only present when cw dispatch wrote the context.

The `isolation: "worktree"` flag on the Agent() call creates a **second, nested worktree
inside the main checkout** (at `<main_repo>/.claude/worktrees/<slug>`). When cw dispatch
already provided an isolated worktree as the session cwd, this nested worktree is
redundant AND it places the impl agent in a position where the main checkout path is
trivially derivable — causing the #766 leak pattern (worker `cd`s to main checkout and
commits there).

**Spawn shape depends on mode AND dispatch context** (all variants pin `model: "opus"` — real code generation):

- **Interactive mode AND not in a dispatch worktree:** `isolation: "worktree"`, `model: "opus"`,
  `run_in_background: true` (parallel — the parent waits for the next user gate anyway,
  no orphan hazard).
- **`--headless` mode AND not in a dispatch worktree:** `isolation: "worktree"`, `model: "opus"`,
  **synchronous** (omit `run_in_background`). Same orphan-hazard rationale as the Step
  1b Plan agent fix (`750ea77`).
- **In a dispatch worktree (either mode):** **omit `isolation: "worktree"` entirely**.
  The dispatch worktree IS the impl agent's sandbox. Spawn synchronously with no
  `isolation` key, with `model: "opus"` — the agent works directly in the current cwd. The `worktree_path`
  in `.claude/cw-context.json` is the authoritative anchor for all git operations.

### Worktree Isolation Guard (headless) — #402

**A headless worker MUST NEVER run a git mutation against the operator's main
checkout.** The authoritative working directory for this stage is the worktree
recorded as `worktree_path` in `.claude/cw-context.json` (injected by `cw` at
spawn — see `spawn.py:_write_hook_context`); the impl agent additionally runs
inside its own `isolation: "worktree"` sandbox. Every `git`, `git -C <path>`,
or `cd`-then-git invocation in this stage MUST target that worktree (or the
trap-cleaned temp worktree created below), **never** the client's
`workspace_path` (the operator's live checkout).

This codifies the invariant behind the #402 isolation breach, where a worker's
`git checkout` resolved "the workspace" to the operator's main checkout and
switched their branch out from under them. The interactive "continue manually
from the worktree" fallback (and any direct-git fallback that assumes the main
session's checkout is the work tree) **does not apply in headless mode**:

- If the isolation worktree or `worktree_path` is unreachable, or any step is
  tempted to fall back to direct git on another checkout, **do NOT fall back**
  — EXIT `blocked` with `blocker.reason: "impl_failed"` and let the
  orchestrator re-dispatch on a fresh worktree. Mutating a shared checkout to
  make progress is never an acceptable workaround.
- Before any commit/push, confirm the cwd resolves under `worktree_path` (or
  `$TMPWT`); if it resolves to `workspace_path`, abort the operation and exit
  `blocked` rather than proceeding.

### Pre-Stage Detector Guard

Before starting S2 work, run `detect_current_stage()` (see [Resume Detection](#resume-detection)):

- If `stage == "s2_implementing"`: branch exists but `Auto-Dev-Stage: impl-complete` trailer is absent. **Resume from current branch HEAD; do not reset.** Log the resumed-from SHA so the user can audit. Skip the worktree-create + branch-init steps below and have the new impl agent continue on top of existing commits.
- If `stage` is past S2 (`s3_*`, `s4_*`, `s5_*`, `merged`): advance to that stage's entry point; do not re-implement.
- Otherwise (`pre_flight`, any `s1_*`, or no detector signal): proceed with fresh implementation as specified below.

**Headless only — before spawning Stage 2 agent, emit `stage.entered` (`s2_impl_started`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s2_impl_started\",\"prev_stage\":\"s1_plan_reviewed\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

Stage 2 agent spawn:

**Prompt must include:**
- The approved plan (full text)
- Branch naming: `<branch-prefix>/<ticket-id-or-short-slug>` (default prefix: `dev`)
- **Branch freshness:** Before starting work, sync with latest main:
  ```bash
  git fetch origin main
  git merge origin/main
  ```
  If merge conflicts occur at this stage, report in friction as BLOCK — the worktree is starting from a conflicted state and cannot proceed.
- **Record the fork point** immediately after merging main — this is the exact commit the feature branch diverged from, used later for deterministic diffs:
  ```bash
  FORK_POINT=$(git merge-base origin/main HEAD)
  ```
  Include `FORK_POINT` in the friction report output. This value is immutable and must be used for all subsequent diff operations in the pipeline.
- Phase 1 instructions: write tests, run in isolation, confirm they FAIL
- Phase 2 instructions: implement fix, run tests again, confirm they PASS
- Post-implementation: `uv run ruff check --fix` on changed files
- **Type check gate (do NOT declare complete until clean):**
  - Run `uv run mypy <touched_files>` (pass the changed paths explicitly, not `.`).
  - Fix every mypy error in touched files — including pre-existing errors. Global CLAUDE.md treats those as active bugs, not noise to inherit.
  - Adding `# type: ignore`, `# noqa`, or weakening a type to `Any` requires explicit user approval. If you find yourself reaching for one, STOP, report it as a BLOCK in friction with the specific error and the proposed ignore, and surface for approval. Do NOT add the ignore unilaterally.
  - If touched files transitively expose untouched-file mypy errors that didn't exist before your edit, treat those as your errors too — fix them or report as BLOCK.
- Instruction to read model/schema definitions before writing code
- Instruction to use Read/Write tools for file operations, not Bash cp/mv/cat
- **Pre-mutation guard (hard, not prose) — #766:** Before any `git add`, `git commit`,
  or `git push`, run:
  ```bash
  python .claude/scripts/check_not_main_checkout.py
  ```
  This script reads `.claude/cw-context.json` (searching upward from cwd) and exits
  non-zero with a `BLOCKED (#766)` message if the current git repo root matches the
  operator's main checkout (`workspace_path` in the context). On non-zero exit: DO NOT
  proceed with the git operation — EXIT `blocked` with `blocker.reason: "impl_failed"`,
  `blocker.details: "check_not_main_checkout exited <N>: <stderr>"`. The script is a
  no-op (exit 0) when no dispatch context is found, so interactive runs are unaffected.
  The guard path is relative to the worktree root; if the script is absent (pre-#766
  checkout), skip the check and log `"check_not_main_checkout: script absent, skipped"`
  in friction, but do NOT proceed past any git op that resolves to a path other than
  `worktree_path`.
- Instruction: if anything fails or surprises you, report it in friction — do NOT silently skip or suppress
- **Incremental commits required** (Subagent Reliability Mitigation 3): Commit after every logical step — do NOT defer all commits to the end. One bad-batch commit at the end means an OOM/crash loses all the work; one commit per step means the orchestrator can resume from the last commit. For each file or coherent feature: make changes → `git add <files>` → `git commit -m "..."`. The friction report's `git log --oneline` output (see Completion Artifacts below) MUST show more than one commit for any non-trivial change. A single end-of-run commit is treated as a discipline failure.
- Instruction to stage and commit changes with a conventional commit message
- **Branch discipline (pre-commit and pre-push):** `isolation: "worktree"` provisions the worktree on an auto-generated session branch (e.g., `agent-<hash>`), NOT on `<branch-name>`. The agent MUST NOT assume the local branch matches the feature branch name. Before committing, run `git branch --show-current` and record the actual local branch in the friction report. Before pushing, do the same.
- **Instruction to push the branch to origin** after committing — use the explicit-refspec form so the local branch name (a session branch) does not need to match the remote feature branch:
  ```bash
  git push -u origin HEAD:refs/heads/<branch-name>
  ```
  Do NOT use `git push -u origin <branch-name>` (the short form): when the local branch is `agent-<hash>`, that form either fails or pushes the wrong ref. The `HEAD:refs/heads/<branch-name>` form pushes whatever the agent committed to the named feature branch on origin regardless of the local branch name. After pushing, verify with `git rev-parse origin/<branch-name>` and confirm it matches `git rev-parse HEAD`. This is critical — subsequent stages (fix loop, PR creation) depend on the branch being on origin rather than locked inside a stale isolation worktree that new subagents cannot reach.
- Instruction to include the final pushed commit SHA, the local branch name (from `git branch --show-current`), and the push confirmation (`origin/<branch-name>` SHA) in the friction report
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
- **The following Completion Artifacts block verbatim** (Subagent Reliability Mitigation 2 — orchestrator gates on these, NOT on the prose claim of "done"):
  ```
  ## Completion Artifacts (required for "done" claims)
  - **Test command used:** <exactly what was run>
  - **Test output (tail):** <paste last 50 lines verbatim>
  - **git diff --stat (vs fork point):** <paste verbatim — git diff --stat $FORK_POINT> (advisory — orchestrator re-runs live)
  - **git log --oneline (vs fork point):** <paste verbatim — git log --oneline $FORK_POINT..HEAD>
  - **git diff line count:** <total lines added + removed>
  - **Mypy result (if Python touched):** <"pass" or paste errors>
  - **Ruff result (if Python touched):** <"pass" or paste errors>
  ```
  Hallucinating or paraphrasing these is a discipline failure — the orchestrator parses them. Pasted output that contradicts the "done" claim (FAILED in test tail, non-empty mypy errors, empty diff) results in `impl_failed`.

### Step 2.5: Orchestrator Completion Gate

After the implementation agent returns (or times out — see Mitigation 4), before logging to Checkpoint 2, the main session runs deterministic gates against the impl by verifying the pushed branch on `origin`. Do NOT trust the prose "done" claim alone; do NOT read from the impl isolation worktree (the orchestrator cannot reach it).

**Pre-gate: verify branch was pushed**

```bash
git fetch origin main <branch-name>
git rev-parse --verify origin/<branch-name> || { echo "IMPL_NOT_PUSHED"; exit 1; }
```

Fetching `main` alongside `<branch-name>` ensures `origin/main` is current before the `merge-base` call below. If the impl branch ref is absent → EXIT `blocked` with `blocker.reason: "impl_not_pushed"`. The agent claimed done but nothing was pushed; the empty-diff vacuous pass would fire on every subsequent gate.

**Gate setup: create one trap-cleaned temp worktree at `origin/<branch-name>`**

```bash
FORK_POINT=$(git merge-base origin/main origin/<branch-name>)
TMPWT="/tmp/gate-wt-$$"
trap 'git worktree remove --force "$TMPWT" 2>/dev/null' EXIT
git worktree add --detach "$TMPWT" origin/<branch-name>
```

All gates below run inside `$TMPWT`. Do NOT run gates from the cw session worktree with origin-qualified refs — that cwd confusion is the bug this step fixes. FORK_POINT is recomputed from `origin` refs; do not trust the impl agent's reported value.

**Gate checks (all run in `$TMPWT`):**

1. **Diff is non-empty:**
   ```bash
   git -C "$TMPWT" diff --stat "$FORK_POINT" | grep " changed" || { echo "IMPL_FAILED: empty diff"; exit 1; }
   ```
   Empty diff → `impl_failed`. The agent claimed work; no work exists.

2. **File set is within the plan's enumeration:**
   ```bash
   git -C "$TMPWT" diff --name-only "$FORK_POINT" | sort > /tmp/touched_files-$$
   # Compare against the plan's file list (from Step 1)
   ```
   Files outside the plan's enumeration → flag as scope growth (does NOT block on its own; routes to the existing Stage 3b scope-growth handling). Missing planned files → flag as missing work.

3. **Test command exit code is 0:** Re-run the agent's claimed test command in `$TMPWT`:
   ```bash
   cd "$TMPWT" && <test_command>
   ```
   Non-zero exit → `impl_failed`. The agent's pasted tail was either fabricated or stale.

4. **Mypy/ruff clean on touched files** (if Python): Re-run in `$TMPWT`; non-zero exit → `impl_failed`.

5. **Incremental commit discipline** (Mitigation 3): `git -C "$TMPWT" log --oneline "$FORK_POINT"..HEAD | wc -l` MUST be > 1 for any non-trivial change (>50 lines OR >3 files touched). A single commit on a large change → flag as discipline failure in friction (does NOT block, but compromises OOM recovery for any follow-up fix loop; record `"impl_no_incremental_commits"` in `friction_highlights`).

**On gate failure (any of checks 1, 3, 4):**
- **Interactive:** AskUserQuestion with the failed-check output: "Implementation agent's completion claim failed gate <N>: <details>. Retry impl, abort ticket, or override?"
- **Headless:** EXIT `blocked` with `blocker.reason: "impl_failed"`, `blocker.details: "Step 2.5 gate <N> failed: <verbatim check output>"`. Do NOT spawn reviewers — the impl is not done.

**On all gates pass:** Proceed to Checkpoint 2 (existing logic).

**Headless only — on all gates pass, emit `stage.entered` (`s2_impl_complete`):**
```bash
cw event record stage.entered \
  --correlation-id "$TICKET" \
  --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s2_impl_complete\",\"prev_stage\":\"s2_impl_started\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
```

This step replaces "trust the agent's `Could work be incomplete?: NO`" with "verify the facts the agent claims." All verification runs against the pushed branch; there is no reference to the inaccessible isolation worktree.

### Checkpoint 2 (Implementation Approval)

**Small scope → AUTO-ACCEPT.** Log to user:
- Worktree path and branch name
- **Fork point SHA** (the merge-base recorded after syncing with main — required for all subsequent diffs)
- **Pushed commit SHA** (verify the agent actually pushed to origin — if not, escalate as BLOCK; the fix loop and PR creation both depend on this)
- Files changed with line counts
- Test results (pass/fail counts)
- Lint + type check results
- Friction report (highlight WARN/BLOCK — BLOCK still stops regardless of scope)

**Large scope → AskUserQuestion:**
- Present same information as above
- "Implementation complete for <ticket-id>. Approve for review, adjust, or abort?"

**Headless:** AUTO-CONTINUE always (never gate, regardless of scope). On BLOCK or 2x failure → EXIT `blocked` with `blocker.reason: "impl_failed"`.

### Implementation Failure Escalation

If any agent returns friction level **BLOCK**:
- Surface the blocker immediately via AskUserQuestion
- Do NOT proceed to next stage

If implementation agent fails tests/lint/mypy after 2 attempts:
- Surface the failure details via AskUserQuestion
- "Continue manually from worktree, skip ticket, or abort pipeline?"
- Do NOT loop indefinitely

### S2 Completion Marker

The final implementation commit on the branch MUST include the trailer:

    Auto-Dev-Stage: impl-complete

This is the durable signal the resume detector uses to advance past S2. The Stage 2 agent's commit instruction must append this trailer to its final commit — typically via `git commit --trailer "Auto-Dev-Stage: impl-complete"`. If the implementation requires multiple commits, attach the trailer only to the last one before push.

Squash-merge to main hides the trailer from main's history but it remains on the branch's commits, which is where the detector reads. On resume, a branch with this trailer + no PR → `s3_review_pending`; a branch without it → `s2_implementing` (resume in-flight).

The Stage 2 agent prompt MUST include this trailer requirement in its commit instructions. Specifically, add to the existing "Instruction to stage and commit changes with a conventional commit message" bullet:

> The final commit before push must include the trailer `Auto-Dev-Stage: impl-complete`. Use `git commit --trailer "Auto-Dev-Stage: impl-complete" -m "..."` (or append the trailer to the message body if your commit tool does not support `--trailer`). The pipeline's resume detector reads this trailer to determine S2 is complete.

---

## Stage 2 Completion (headless only)

After all Stage 2 steps complete successfully in headless mode (branch pushed, gates passed, impl-complete trailer present), emit the `AUTO_DEV_RESULT` sentinel. IMPL completes the stage and advances the pipeline to review — it does NOT create a PR (FINALIZE owns that). Use `status: "stage_complete"` (not `"shipped"`); do NOT set `pr` or include `wait_for_ci` in `next_actions`.

**Only emit this sentinel when invoked as a standalone `/auto-dev-impl <ticket-id> --headless` command. Do NOT emit when running as part of the interactive monolith chain (`auto-dev.md` owns the sentinel in that context).**

```bash
printf '%s' "$SENTINEL_JSON" | cw result validate -
```

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "<stage_complete | blocked>",
  "stage_reached": "stage2_impl",
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

See `auto-dev.md` Appendix for the full field reference and status enum. The contract for this stage's output is `cw schema stage-output impl`.
