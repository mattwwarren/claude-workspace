---
description: "Full PR prep: commit, self-review, fix loop, scope check, ship"
argument-hint: "[--max-cycles N] [--skip-review] [--base <branch>] [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task", "Skill", "AskUserQuestion"]
---

# Prep PR

Full PR preparation orchestrator: commit → self-review → fix → quality gates → ship. Monitors scope creep and surfaces conflicts to the user at each step.

**Arguments:** "$ARGUMENTS"

---

## Headless Mode

`/prep-pr` is delegated to by the headless `/auto-dev` finalize stage
(`auto-dev-finalize.md` Step 4c → `/prep-pr --headless`). In headless mode there
is no TTY: **every interactive gate that would call `AskUserQuestion` is
forbidden.** Each gate below carries an explicit `**Headless:**` override — read
it in place of the interactive prompt.

**The universal rule:** a gate collapses to one of

- a **deterministic auto-action** (auto-commit, continue-with-known-state), or
- a **`HEADLESS BLOCK`** when the gate cannot be resolved without a human.

Never silently skip a step. A silently-skipped ship step is the exact defect
this mode exists to prevent (feed post / review-monitor registration dropped
with auto-merge still armed). When a gate cannot be auto-resolved, STOP and emit
a `HEADLESS BLOCK` as the **final output**, then halt — do not proceed to any
later step.

**`HEADLESS BLOCK` format** (greppable; the calling agent surfaces it as a
friction **BLOCK**, which the pipeline maps to `blocker.reason: "agent_block"`
per `claude-workspace/docs/headless-contract.md` §2 gate-collapse table, row
"Any other agent BLOCK (Plan / prep-pr / etc.)"):

```
<<<PREP_PR_BLOCK
reason: agent_block
gate: <step + gate name, e.g. "Step 7 quality gate">
details: <verbatim cause — failing gate output, conflicting files, or "no project /ship-it">
PREP_PR_BLOCK>>>
```

Do NOT fall back to shipping-anyway, reverting work, or inventing a PR yourself
to route around a block. Emitting the block and halting IS the correct headless
behavior — the orchestrator routes it to a human.

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
   - `--headless` → non-interactive mode. See **Headless Mode** below. Every
     gate that would otherwise call `AskUserQuestion` collapses to a
     deterministic action or a `HEADLESS BLOCK`. Never prompt.

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

   **Headless:** AUTO-COMMIT (selective staging, descriptive message, never
   amend) and continue. Committing pending work is non-destructive and matches
   Step 8.1; do not abort on this.

## Step 1: Sync with Base Branch

Pull the latest base branch and merge it into the current branch to ensure we're working against up-to-date code:

```bash
git fetch origin <base>
git merge origin/<base>
```

- **If merge succeeds cleanly** → push and verify:
  ```bash
  BRANCH=$(git branch --show-current)
  git push origin HEAD:refs/heads/"$BRANCH"
  git fetch origin "$BRANCH"
  test "$(git rev-parse origin/"$BRANCH")" = "$(git rev-parse HEAD)"
  ```
  This `git push` has no `timeout` wrapper — no push site in this file family does (#1414 R9: accepted residual risk, consistent with `ship-it.md`'s existing push and `auto-dev-finalize.md`'s Step 4c.2 push).
  - **On success** → proceed to Step 2.
  - **On push failure or verify mismatch**: ask the user:
    > "Failed to push the merged branch to origin (<error>). Retry the push, or abort /prep-pr?"
    - **Retry** → re-attempt the push once.
    - **Abort** → stop /prep-pr.

    **Headless:** emit `HEADLESS BLOCK` (`gate: "Step 1 sync-with-base push"`, `reason: agent_block` per this file's fixed convention, `details:` the verbatim push failure output). Do NOT retry automatically in headless mode and do NOT proceed to Step 2 — an unavailability condition (auth/network) needs the operator, not a blind retry.
- **If merge conflicts** → surface the conflicting files to the user:
  > "Merge conflicts with `<base>`. Conflicting files: [list]. Resolve before continuing?"
  - **Yes** → help resolve conflicts, commit the merge
  - **Abort** → `git merge --abort`, stop /prep-pr

  **Headless:** run `git merge --abort` to restore a clean tree, then emit a
  `HEADLESS BLOCK` (`gate: "Step 1 sync-with-base"`, `details:` the conflicting
  file list). Do NOT attempt an autonomous conflict resolution — a mis-resolved
  merge is worse than a surfaced block.

## Step 2: Detect Quality Gates

Resolve the backing script before anything else. The checked-out repo's copy is the source of truth: the installed `~/.claude/scripts/...` path is current only when claude-workspace's `scripts/install-skills.sh` has linked it there, and a stale copy from any other checkout silently lacks Step 7's `gate-timeout` / `gate-elapsed` subcommands (#2090). Probe the repo layouts first and the installed path last, and STOP on a stale hit rather than improvising:

```bash
PREP_PR_STATE=""
for candidate in .claude/scripts/prep_pr_state.py scripts/prep_pr_state.py "$HOME/.claude/scripts/prep_pr_state.py"; do
  if [ -f "$candidate" ]; then PREP_PR_STATE="$candidate"; break; fi
done
if [ -z "$PREP_PR_STATE" ]; then
  echo "prep_pr_state.py not found (probed .claude/scripts/, scripts/, ~/.claude/scripts/)"; exit 1
fi
if ! grep -q 'gate-timeout' "$PREP_PR_STATE"; then
  echo "STALE: $PREP_PR_STATE predates #1432 (no gate-timeout subcommand) — reinstall it or use the repo copy"; exit 1
fi
echo "PREP_PR_STATE=$PREP_PR_STATE"
```

Shell state does not persist between `Bash` tool calls: every later `"$PREP_PR_STATE"` invocation in this file means the path printed here — substitute it literally, or re-run the resolver at the top of the same call. Never fall back to a bare `~/.claude/scripts/prep_pr_state.py`; if the resolver reports STALE, surface that verbatim (headless: `HEADLESS BLOCK`, `gate: "Step 2 script resolution"`) instead of picking timeouts yourself.

Run the resolved script to auto-detect quality gates:

```bash
"$PREP_PR_STATE" detect-gates
```

This scans for `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod` and checks the project's `CLAUDE.md` for `## Quality Gates` overrides. `CLAUDE.md` remains the canonical contract filename even when this workflow is reused from Codex.

Store the result — you'll run these gates in Step 7.

## Step 3: Capture Initial Scope Snapshot

```bash
"$PREP_PR_STATE" snapshot --base <base> --max-cycles <N>
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

**Headless:** AUTO — fix all MUST_FIX findings via Step 6; note SHOULD_FIX
findings (do not fix, do not block) and continue. Never abort on review
findings alone. (In the `/auto-dev` pipeline this step is unreachable —
finalize invokes `/prep-pr --skip-review`, having already run scope-aware
review in Stage 3 — so this override only applies to standalone headless use.)

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
   "$PREP_PR_STATE" snapshot --base <base>
   ```

2. **Check for scope creep:**
   ```bash
   "$PREP_PR_STATE" check-scope
   ```

3. **If scope creep is detected** (file count +30%, line count +50%, new non-test files, new directories):

   Surface to the user with specific metrics:
   > "Scope creep detected: file count grew by X%, line count by Y%. New files: [list]. Continue, revert fixes, or abort?"

   Options:
   - **Continue** — proceed with expanded scope
   - **Revert** — `git revert` the fix commits
   - **Abort** — stop entirely

   **Headless:** CONTINUE with the expanded scope and record the creep metrics
   (grown file/line counts, new files) so the caller can echo them into
   `friction_highlights`. Do NOT revert (destructive to just-applied fixes) and
   do NOT abort — the scope grew from resolving this run's own review findings.

4. **Conflict detection:** If a fix introduced new findings that didn't exist before (regressions), flag this to the user:
   > "Fix cycle may have introduced new issues. Review recommended before continuing."

## Step 7: Run Quality Gates

Run each gate detected in Step 2, in order.

Before running each gate, fetch its timeout ceiling:
```bash
"$PREP_PR_STATE" gate-timeout <gate-name>
```
This returns `foreground_ceiling_s` (when to switch this gate to background) and `poll_ceiling_s` (total wall-clock budget once backgrounded before declaring the result lost).

For each gate:
1. Record the start time: capture `date -u +%Y-%m-%dT%H:%M:%SZ` as `<started>`.
2. Run the gate command via the `Bash` tool with `timeout` set to `foreground_ceiling_s * 1000` (ms; the Bash tool accepts up to 600000ms).
   - **If it completes within the ceiling** → proceed with the existing pass/fail handling (autofix retry, or ask/HEADLESS-BLOCK below).
   - **If the foreground call itself times out** → this is the deliberate "switch to background" trigger. Re-issue the *same* command via `Bash` with `run_in_background: true`. The tool returns immediately with a shell id and an output-file path; record `<output_file>`.
3. Once backgrounded, the harness passively notifies this session when the command exits (success, failure, or crash) — this is the primary detection path. Do not idle-wait; continue other Step-7 work if any, and let the notification arrive.
4. **Liveness / early-block check** (bounded re-checks, not a dedicated poll tool): at intervals (e.g. every few minutes of elapsed session time), `Read` `<output_file>` to confirm the gate is still producing output (progress = alive), and call:
   ```bash
   "$PREP_PR_STATE" gate-elapsed --started <started> --ceiling-seconds <poll_ceiling_s>
   ```
   - **If a completion notification arrives before the ceiling is exceeded** → treat as authoritative: read the final output/exit code and apply the existing pass/fail handling (autofix retry, or ask/HEADLESS-BLOCK below).
   - **If `exceeded: true` and no completion notification has arrived** → the backgrounded command's result is lost or stalled (dead backgrounding path, not merely slow). Emit the `gate_timeout` block below **immediately** — do not wait for the remaining stage budget. Note: unlike every other block in this file, `reason:` here is `gate_timeout`, not the fixed `agent_block` literal — this is a deliberate, nested-only forward-compat marker (the sentinel's top-level `blocker.reason` still collapses to `agent_block` per the existing gate-collapse rule; `gate_timeout` and the gate name survive verbatim inside `blocker.details`):
     ```
     <<<PREP_PR_BLOCK
     reason: gate_timeout
     gate: <gate name, e.g. "mypy">
     details: foreground ceiling <foreground_ceiling_s>s exceeded at <started>; backgrounded; poll ceiling <poll_ceiling_s>s elapsed with no completion notification and no output growth in <output_file> since <last-observed-timestamp>. Last captured output: <tail of output_file>
     PREP_PR_BLOCK>>>
     ```
     This applies uniformly to all 8 quality gates detected by Step 2/`detect-gates` (uv lock check, ruff check, ruff format, mypy, pre-commit, pytest units, pytest integration, diff-cover), not only mypy/pytest/pre-commit. Known limitation: the pytest-units and pytest-integration gates currently derive the same gate name (`pytest`), so a `gate: pytest` block cannot distinguish which of the two stalled from that field alone — use the block's own `<output_file>` tail to disambiguate.
   - This is an **interactive-mode override too**: outside `--headless`, surface the same block content as a friction BLOCK to the user rather than silently parking — a lost gate result is never something to wait out quietly.
5. If it **fails** (not timed out) and has an autofix command (e.g., ruff, eslint):
   - Run the autofix command
   - Re-run the original check to verify it passes
   - Commit autofix changes
6. If it **fails** (not timed out) without autofix (e.g., mypy, tsc, pytest):
   - Report the failure output
   - Ask: "Gate [name] failed. Attempt to fix, or ship anyway?"

   **Headless:** attempt a fix loop (back to Step 4 within `--max-cycles`). If
   the gate still fails after the fix loop is exhausted, emit a `HEADLESS BLOCK`
   (`gate: "Step 7 quality gate"`, `details:` the verbatim failing gate output).
   NEVER "ship anyway" — shipping past a failing quality gate violates the
   quality floor and is exactly the silent-degradation this mode forbids.

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

  **Headless:** emit a `HEADLESS BLOCK` (`gate: "Step 7 max review cycles"`,
  `details:` the remaining-issues summary). Do NOT "ship anyway" past unresolved
  MUST_FIX issues and do NOT proceed to Step 8.

## Step 8: Ship

1. Commit any remaining uncommitted changes (staged autofix results, etc.)
2. Check for a project-level ship-it. **A project ship-it is a command in some
   repos and a skill in others — probe every supported layout, never just the
   command path.** A repo whose ship-it is a skill has a working ship-it; a
   single-path `test -f .claude/commands/ship-it.md` reports it missing and
   sends this step down the STOP/BLOCK branch for a repo that can ship fine.

   ```bash
   for candidate in .claude/commands/ship-it.md \
                    .claude/skills/ship-it/SKILL.md \
                    .agents/skills/ship-it/SKILL.md; do
     [ -f "$candidate" ] && echo "found $candidate -> $(cd "$(dirname "$candidate")" && pwd -P)/$(basename "$candidate")"
   done
   ```

   The resolved path is printed because `.claude/skills/ship-it` is commonly a
   symlink to `.agents/skills/ship-it` — two hits with one resolved path are
   one ship-it, not two.

   - **If a command file was found** (`.claude/commands/ship-it.md`): Read it and follow those instructions step-by-step. Execute every step that produces side effects (push, gh pr create, slack post, monitor register). Reading the file is not the same as running it.
   - **If a skill was found** (`…/skills/ship-it/SKILL.md`): invoke it with the
     `Skill` tool (`ship-it`) so its bundled `references/` load the way the
     skill expects. If this runtime has no `Skill` tool, Read the `SKILL.md`
     and follow it step-by-step, resolving its relative paths against the
     skill's own directory. Same rule as the command form: every side-effecting
     step must actually run.
   - **If both a command and a distinct skill were found**: prefer the command
     (`.claude/commands/ship-it.md`), and say in the Ship Summary which one ran
     and which was skipped.
   - **If both skill paths were found and they resolve to *different* files**
     (no symlink between them): prefer `.claude/skills/ship-it/SKILL.md` — the
     path the runtime itself loads — and name the shadowed `.agents/` copy in
     the Ship Summary. Never merge or run both.
   - **If none of the layouts matched**: **STOP.** Tell the user:
     > "This project has no ship-it — probed `.claude/commands/ship-it.md`, `.claude/skills/ship-it/SKILL.md`, and `.agents/skills/ship-it/SKILL.md`. Create a project-level ship-it that knows your repo's PR conventions, branch naming, and CI setup. The generic global one was removed because it caused more problems than it solved."
     >
     > Do NOT fall back to any global ship-it. Do NOT try to create a PR yourself. The user must set up a project-specific ship-it first.

**Headless:** propagate headless-ness into the delegated ship-it execution
— this is the delegation hop where the original defect surfaced.
- **If no project-level ship-it exists in any probed layout** → emit a
  `HEADLESS BLOCK` (`gate: "Step 8 ship"`, `details: "no project /ship-it"`)
  instead of the interactive STOP message. (`auto-dev-finalize.md` Step 4c's
  subagent instruction already expects and handles this specific BLOCK cause.)
  Do not emit this BLOCK on the strength of the command path alone — run the
  full probe first.
- **If it exists** → while following it step-by-step, treat the whole
  delegated run as headless: any interactive step in that file (its own
  `AskUserQuestion`, a confirmation prompt, a "proceed?" gate) converts to the
  same rule as this command — auto-resolve if the deterministic action is
  unambiguous, otherwise emit a `HEADLESS BLOCK` (`gate: "Step 8 ship-it: <that
  step>"`). A side-effecting step (push, `gh pr create`, feed/slack post,
  monitor register) must run or BLOCK — it must **never** be silently skipped.
  If the project ship-it itself accepts a `--headless`/non-interactive flag,
  pass it through.

Pass through relevant arguments when invoking the project-level ship-it:
- `--draft` if the user's original arguments included it
- `--title` if provided

## Step 9: Finalize — Verify Ship & Emit Summary

**This step is the contract that proves /ship-it actually ran.** Do not skip it. Do not paraphrase it. Run the script. Prefer the checked-out repo copy when available (`.claude/scripts/prep_pr_finalize.py`, then `scripts/prep_pr_finalize.py` — the same layout order Step 2 uses for `prep_pr_state.py`); otherwise `~/.claude/scripts/...` is acceptable.

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
- **Headless:** after attempting the missing sub-step once, if `verify` still
  exits non-zero, emit a `HEADLESS BLOCK` (`gate: "Step 9 finalize verify"`,
  `details:` the verbatim failed checks). Never emit a Ship Summary that claims
  success when `verify` failed.

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
"$PREP_PR_STATE" clean
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
6. **Headless `HEADLESS BLOCK`** → an interactive gate could not be auto-resolved (conditions 3–5 have no interactive equivalent in headless mode; they collapse to a block or a deterministic action per each step's **Headless:** override)

## Notes

- State persists in `.claude/prep-pr-state.json` — cleaned up in Step 10
- This skill is ecosystem-agnostic: gate detection adapts to Python, Node, Rust, and Go projects
- Each review-fix cycle captures a scope snapshot for creep monitoring
- Never amend commits. Each fix gets its own commit.
- A project-level ship-it is required — there is no global fallback. It may be a command (`.claude/commands/ship-it.md`) or a skill (`.claude/skills/ship-it/SKILL.md`, `.agents/skills/ship-it/SKILL.md`)
- `--headless` (see **Headless Mode**) is set by `auto-dev-finalize.md` Step 4c for the headless `/auto-dev` pipeline. It replaces every `AskUserQuestion` gate with a deterministic action or a `HEADLESS BLOCK`; nothing side-effecting is ever silently skipped.
