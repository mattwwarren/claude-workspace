---
description: "Full PR prep: commit, self-review, fix loop, scope check, ship"
argument-hint: "[--max-cycles N] [--skip-review] [--base <branch>]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task", "Skill", "AskUserQuestion"]
---

# Prep PR

Full PR preparation orchestrator: commit → self-review → fix → quality gates → ship. Monitors scope creep and surfaces conflicts to the user at each step.

**Arguments:** "$ARGUMENTS"

---

## Step 0: Validate Preconditions

Check that we're ready to prepare a PR:

1. **Not on main/master:**
   ```bash
   git branch --show-current
   ```
   If on `main` or `master`, abort with: "Cannot prep a PR from the main branch. Create a feature branch first."

2. **Parse arguments:**
   - `--base <branch>` → base branch for diff comparison (default: `main`)
   - `--max-cycles N` → maximum review-fix cycles (default: 3)
   - `--skip-review` → skip self-review, jump straight to quality gates

3. **Has work to ship:**
   ```bash
   git log --oneline <base>..HEAD
   git status --short
   ```
   If no commits and no uncommitted changes, abort: "Nothing to ship. Make some changes first."

4. **Uncommitted changes:** If any exist, ask the user:
   > "There are uncommitted changes. Commit them now before starting PR prep?"
   - **Yes** → Stage and commit (selective staging, descriptive message, never amend)
   - **No** → Abort: "Commit or stash changes before running /prep-pr"

## Step 1: Sync with Base Branch

Pull the latest base branch and merge it into the current branch to ensure we're working against up-to-date code:

```bash
git fetch origin <base>
git merge origin/<base>
```

- **If merge succeeds cleanly** → proceed to Step 2
- **If merge conflicts** → surface the conflicting files to the user:
  > "Merge conflicts with `<base>`. Conflicting files: [list]. Resolve before continuing?"
  - **Yes** → help resolve conflicts, commit the merge
  - **Abort** → `git merge --abort`, stop /prep-pr

## Step 2: Detect Quality Gates

Run the backing script to auto-detect quality gates. If you're operating from the checked-out `global-claude` repo or a Codex wrapper, prefer the repo's `scripts/prep_pr_state.py`; otherwise the installed `~/.claude/scripts/...` path is fine:

```bash
~/.claude/scripts/prep_pr_state.py detect-gates
```

This scans for `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod` and checks the project's `CLAUDE.md` for `## Quality Gates` overrides. `CLAUDE.md` remains the canonical contract filename even when this workflow is reused from Codex.

Store the result — you'll run these gates in Step 7.

## Step 3: Capture Initial Scope Snapshot

```bash
~/.claude/scripts/prep_pr_state.py snapshot --base <base> --max-cycles <N>
```

This records the initial diff metrics (files, additions, deletions, directories) for scope creep detection later.

If `--skip-review` is in arguments → jump to **Step 7**.

## Step 4: Run Self-Review

Invoke the `/review` skill targeting the diff against the base branch:

```
/review <base>
```

Parse the output:
- **"No actionable issues found"** (or equivalent clean result) → proceed to **Step 7** (quality gates)
- **Findings exist** → proceed to **Step 5**

## Step 5: Present Findings, Get User Decision

Summarize the review findings:
- Count of **MUST_FIX** findings
- Count of **SHOULD_FIX** findings
- List each finding: file, line, severity, description

Ask the user:

> "Review found N MUST_FIX and M SHOULD_FIX issues. How to proceed?"

Options:
1. **Fix all issues** — fix both MUST_FIX and SHOULD_FIX
2. **Fix MUST_FIX only** — skip SHOULD_FIX items
3. **Skip fixes** → jump to quality gates
4. **Abort** — stop /prep-pr entirely

## Step 6: Fix Issues

### Small number of findings (1–3):

Fix directly in the current session. For each finding:
1. Read the file
2. Apply the fix
3. Commit the change

### Large number of findings (4+):

Spawn parallel subagents via the Task tool, grouped by file for exclusive ownership. Each agent receives:
- The findings for its assigned files
- The project's CLAUDE.md (if present)
- Instruction to fix and commit each change

**After all fixes are applied:**

1. **Capture new scope snapshot:**
   ```bash
   ~/.claude/scripts/prep_pr_state.py snapshot --base <base>
   ```

2. **Check for scope creep:**
   ```bash
   ~/.claude/scripts/prep_pr_state.py check-scope
   ```

3. **If scope creep is detected** (file count +30%, line count +50%, new non-test files, new directories):

   Surface to the user with specific metrics:
   > "Scope creep detected: file count grew by X%, line count by Y%. New files: [list]. Continue, revert fixes, or abort?"

   Options:
   - **Continue** — proceed with expanded scope
   - **Revert** — `git revert` the fix commits
   - **Abort** — stop entirely

4. **Conflict detection:** If a fix introduced new findings that didn't exist before (regressions), flag this to the user:
   > "Fix cycle may have introduced new issues. Review recommended before continuing."

## Step 7: Run Quality Gates

Run each gate detected in Step 2, in order:

For each gate:
1. Run the command
2. If it **fails** and has an autofix command (e.g., ruff, eslint):
   - Run the autofix command
   - Re-run the original check to verify it passes
   - Commit autofix changes
3. If it **fails** without autofix (e.g., mypy, tsc, pytest):
   - Report the failure output
   - Ask: "Gate [name] failed. Attempt to fix, or ship anyway?"

### Loop Control

After all gates pass:
- If review was clean (or skipped) → proceed to **Step 8**
- If fixes were applied in this cycle → loop back to **Step 4** for re-review
- Track cycle count. At `--max-cycles` (default 3):

  > "Reached maximum review cycles (N). Remaining issues: [summary]. Ship anyway, fix manually, or abort?"

  Options:
  - **Ship anyway** — proceed to Step 8 with known issues
  - **Fix manually** — exit /prep-pr so user can fix by hand
  - **Abort** — stop entirely

## Step 8: Ship

1. Commit any remaining uncommitted changes (staged autofix results, etc.)
2. Check for a project-level ship-it command:
   ```bash
   test -f .claude/commands/ship-it.md && echo "project"
   ```
   - **If project-level exists**: Read `.claude/commands/ship-it.md` and follow those instructions step-by-step. Execute every step that produces side effects (push, gh pr create, slack post, monitor register). Reading the file is not the same as running it.
   - **If no project-level exists**: **STOP.** Tell the user:
     > "This project has no `/ship-it` command (`.claude/commands/ship-it.md`). Create a project-level ship-it that knows your repo's PR conventions, branch naming, and CI setup. The generic global one was removed because it caused more problems than it solved."
     >
     > Do NOT fall back to any global ship-it. Do NOT try to create a PR yourself. The user must set up a project-specific ship-it first.

Pass through relevant arguments when invoking the project-level ship-it:
- `--draft` if the user's original arguments included it
- `--title` if provided

## Step 9: Finalize — Verify Ship & Emit Summary

**This step is the contract that proves /ship-it actually ran.** Do not skip it. Do not paraphrase it. Run the script. Prefer the checked-out repo script when available; otherwise `~/.claude/scripts/...` is acceptable.

```bash
~/.claude/scripts/prep_pr_finalize.py verify --require-automerge
```

The script verifies:
- Branch is pushed to origin and origin SHA matches local HEAD
- A PR exists for the current branch (`gh pr view` succeeds)
- PR head SHA matches local HEAD (push and PR are in sync)
- Auto-merge is enabled (required — auto-dev relies on this)
- Monitor registered (reported as optional unless `--require-monitor` is passed)

Output is the canonical Ship Summary (markdown). For programmatic callers (e.g. /auto-dev's subagent), pass `--json`.

**If the script exits non-zero:**
- Report the failed checks verbatim to the user
- Do NOT claim success
- Diagnose: most failures mean a /ship-it sub-step was skipped (no push, no PR, no auto-merge). Re-run the missing step rather than papering over it.

**If monitor registration is missing**, run it now:
```bash
PR_NUMBER=$(gh pr view --json number --jq .number)
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
REPO_PATH=$(git rev-parse --show-toplevel)
HEAD_SHA=$(gh pr view --json headRefOid --jq .headRefOid)

~/.claude/scripts/review_monitor.py register "$PR_NUMBER" \
  --role author \
  --repo "$REPO" \
  --repo-path "$REPO_PATH" \
  --sha "$HEAD_SHA"
```

Then re-run finalize with `--require-monitor` to confirm.

## Step 10: Clean Up

Remove the state file:
```bash
~/.claude/scripts/prep_pr_state.py clean
```

Print the Ship Summary from Step 9 as the final message. Add cycle-level context above it:
- Total review-fix cycles completed
- Findings found and fixed
- Quality gate results (all pass / with exceptions)

---

## Loop Termination Conditions

1. **Clean review + gates pass** → ship
2. **Max cycles reached** → surface to user
3. **User aborts** at any interaction point
4. **User ships anyway** despite remaining issues
5. **Unresolvable scope creep** → user declines to continue

## Notes

- State persists in `.claude/prep-pr-state.json` — cleaned up in Step 10
- This skill is ecosystem-agnostic: gate detection adapts to Python, Node, Rust, and Go projects
- Each review-fix cycle captures a scope snapshot for creep monitoring
- Never amend commits. Each fix gets its own commit.
- A project-level `/ship-it` (`.claude/commands/ship-it.md`) is required — there is no global fallback
