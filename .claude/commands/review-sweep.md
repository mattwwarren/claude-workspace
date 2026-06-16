---
description: "Find and review all unreviewed PRs in the repo"
argument-hint: "[--include-mine] [--include-drafts] [--dry-run]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Task"]
---

# Review Sweep

Automatically find and review the next unreviewed PR. Designed for ralph-loop: one PR per invocation, ralph-loop handles the outer loop.

**Arguments:** "$ARGUMENTS"

---

## Step 1: Query Candidates

Run the review-sweep script to find unreviewed PRs:

```bash
~/.claude/scripts/review_sweep.py query _FLAGS
```

Where `_FLAGS` maps from arguments:
- `--include-mine` → `--include-mine`
- `--include-drafts` → `--include-drafts`

The script returns a JSON array of candidate PRs sorted by size (smallest first). Each PR has: `number`, `title`, `author`, `url`, `headRefName`, `additions`, `deletions`, `size`, `head_sha`.

**If the array is empty:** No PRs to review. Output `<promise>SWEEP COMPLETE</promise>` and stop.

**If `--dry-run` is in arguments:** Print the candidate list in a readable table and stop. Do not review anything.

## Step 2: Pick the First PR

Take the first (smallest) candidate from the list. This is the PR you will review.

Note the PR's:
- `number` — for gh commands
- `head_sha` — for marking done
- `size` (additions + deletions) — for depth assessment
- `author` — compare against current GitHub user (`gh api user --jq .login`)

### Detect Self-Review

If the PR author matches the current user, this is a **self-review** (your own PR). Set `mode = "self-review"` and announce it clearly:

> "PR #N is **your PR** — running as self-review. I'll find issues and offer to fix them directly instead of posting review comments."

Otherwise, set `mode = "peer-review"` (default behavior).

## Step 3: Assess Review Depth

Determine whether this PR needs a **light** or **deep** review:

**Light review** (all conditions must be true):
- `size` < 200 (additions + deletions)
- No changes to core/auth/db files (check changed files for patterns: `**/auth/**`, `**/models/**`, `**/migrations/**`, `**/security/**`, `**/middleware/**`)

**Deep review** — anything that doesn't qualify as light.

## Step 4: Gather Context

Before spawning review agents, collect everything they need:

1. **Checkout the PR branch** (CRITICAL — prevents reading stale code from main):
   ```bash
   git fetch origin pull/<number>/head:pr-review-<number> 2>/dev/null || git fetch origin <headRefName>
   git checkout pr-review-<number> 2>/dev/null || git checkout <headRefName>
   ```
   **Verify:** `git branch --show-current` — must NOT be `main` or `master`.
2. **Get the PR diff:** `gh pr diff <number>`
3. **Get changed files:** `gh pr view <number> --json files --jq '.files[].path'`
4. **Get repo info:** `gh repo view --json nameWithOwner --jq .nameWithOwner`
5. **Read project context:** Check for `CLAUDE.md` and `ARCHITECTURE.md` in the repo root. Read them if they exist.

**After review completes** (in Step 8, after marking done): restore the previous branch:
```bash
git checkout - 2>/dev/null || git checkout main
```

## Step 5: Run the Review

### Agent Output Rules

Every review agent prompt MUST include this preamble before the output rules:

```
BRANCH VERIFICATION: You are reviewing PR #<number>. Before reading ANY source files,
run `git branch --show-current` and confirm you are NOT on main/master. If you are on
the wrong branch, STOP and report the error instead of reviewing.
```

Every review agent MUST also receive these output rules verbatim:

```
OUTPUT RULES — follow these exactly:

1. ONLY report problems that require action. No praise, no summaries, no "looks good".
2. If you find ZERO actionable issues, respond with exactly: NO_ISSUES
3. For each finding, return a JSON object on its own line with these fields:
   - "path": file path relative to repo root
   - "line": line number in the NEW version of the file (from the diff's +N line numbers)
   - "severity": "MUST_FIX" or "SHOULD_FIX"
   - "body": 1-2 sentence description of the problem, why it matters, and specific fix suggestion
4. Output ONLY these JSON lines, one per finding. No other text.
5. Be direct. No hedging. State the problem and the fix.
```

### Light Review

Use the code-review plugin approach — 3 focused Sonnet agents running in parallel:

| Agent | Focus |
|-------|-------|
| **Bug Hunter** | Scan for correctness bugs, logic errors, off-by-ones, null derefs. Ignore style. |
| **CLAUDE.md Auditor** | Check changes against CLAUDE.md rules. Only flag violations explicitly called out. |
| **Context Checker** | Read git blame/history of modified lines. Flag regressions or patterns that contradict prior fixes. |

Spawn all 3 as parallel Task agents (sonnet model, `run_in_background: true`). Each gets: the diff, changed file list, CLAUDE.md content, and the output rules above.

### Deep Review

Run all 3 Light Review agents, PLUS spawn additional agents:

| Agent | When to Spawn |
|-------|--------------|
| **silent-failure-hunter** | Always for deep reviews |
| **pr-test-analyzer** | Always for deep reviews |
| **type-design-analyzer** | Only if new types/models added (check diff for `class`, `TypedDict`, `BaseModel`, `dataclass`) |

**Do NOT** spawn `code-simplifier` or `comment-analyzer` — those aren't actionable review feedback.

Each additional agent gets the same diff and output rules.

### Confidence Filtering

After all agents return, for each finding, spawn a parallel Haiku agent to score confidence (0-100):

- **0**: False positive, doesn't hold up to scrutiny, or pre-existing issue
- **25**: Might be real, but likely a nitpick or not verifiable
- **50**: Real issue but minor; won't happen often in practice
- **75**: Verified real issue, will be hit in practice, or directly violates CLAUDE.md
- **100**: Definitely real, confirmed with evidence, high impact

**Discard findings scoring below 75.**

## Step 6: Present Findings for Approval

**MANDATORY: Never act without user approval.**

After confidence filtering, present a summary to the user:

1. PR number, title, and link
2. Review depth (light/deep)
3. **Mode** (self-review or peer-review)
4. A table of surviving findings: file, line, severity, agent source, 1-line body

### Peer-Review Mode

Proposed action: "Approve (no issues)" or "Request changes (N issues)"

Ask: **"Post this review to PR #N, or adjust?"**

- **If approved** → proceed to Step 7
- **If user wants edits** → adjust findings per feedback, re-present
- **If user declines** → skip to Step 8 (mark done without posting)

### Self-Review Mode

Frame findings as things to fix, not review comments to post:

> "Found N issues in your PR. Want me to fix them directly, or just note them for you?"

Options:
- **Fix all** → check out the branch, apply fixes, commit, push. Then mark done in Step 8.
- **Fix MUST_FIX only** → fix critical issues, skip SHOULD_FIX
- **Just note them** → print the table, skip to Step 8 (no review posted, no fixes)
- **Post as self-review anyway** → fall through to peer-review Step 7

When fixing, follow the same pattern as `/prep-pr` Step 6: commit each fix separately, never amend.

## Step 7: Post the Review

Use the `post_review.py` script to submit a proper pull request review with inline file comments. This handles JSON escaping, validation, and fallback for bad line numbers.

### If findings remain after filtering:

1. Use the **Write tool** to create `/tmp/pr_review_comments.json` with the comments JSON array. Each finding is an object with `path`, `line`, `body`. Prefix each body with `**[Agent Name]**`. Do NOT use bash heredocs — the Write tool handles JSON content without shell escaping issues.

2. Post the review:

```bash
~/.claude/scripts/post_review.py <number> \
  --event REQUEST_CHANGES \
  --body "Found N issues.

🤖 Generated with [Claude Code](https://claude.ai/code)

<sub>If this review was useful, react with 👍. Otherwise, 👎.</sub>" \
  --comments-file /tmp/pr_review_comments.json
```

3. Clean up: `rm -f /tmp/pr_review_comments.json`

If the script reports failed comments (bad line numbers), retry them as file-level comments by removing the `line` field.

### If no findings (or all filtered out):

```bash
echo '[]' | ~/.claude/scripts/post_review.py <number> \
  --event APPROVE \
  --body "No issues found. Checked for bugs and CLAUDE.md compliance.

🤖 Generated with [Claude Code](https://claude.ai/code)"
```

### Register for Monitoring

After posting the review, register for ongoing monitoring:

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
REPO_PATH=$(git rev-parse --show-toplevel)
```

Get our thread IDs from `gh review view <number> --json`, filter to threads we authored in the last few minutes. Then:

```bash
~/.claude/scripts/review_monitor.py register <number> \
  --role reviewer \
  --repo "$REPO" \
  --repo-path "$REPO_PATH" \
  --sha <head_sha> \
  --review-id <review_id> \
  --threads <thread_ids> \
  --thread-details '<JSON>'
```

## Step 8: Mark Done

After posting the review:

```bash
~/.claude/scripts/review_sweep.py mark <number> <head_sha> "<summary>" --depth <light|deep>
```

Where `<summary>` is like "3 issues, requested changes" or "approved, no issues".

## Step 9: Report

Print a summary:
- PR number and title
- Review depth (light/deep)
- Outcome: approved or requested changes (N issues)
- Link to the PR

End normally. Ralph-loop will invoke `/review-sweep` again for the next PR.

---

## Ralph-Loop Integration

```bash
/ralph-loop "/review-sweep" --completion-promise "SWEEP COMPLETE" --max-iterations 10
```

- **One PR per iteration**: Each invocation reviews exactly one PR, then exits.
- **Completion signal**: When no candidates remain, output `<promise>SWEEP COMPLETE</promise>`.
- **State persistence**: The state file tracks what's been reviewed, preventing duplicate work across iterations.

## Notes

- The state file is at `.claude/.review-sweep-state.json` in the repo root. It tracks reviewed PRs by HEAD SHA so updated PRs get re-reviewed.
- Merge-back PRs (only merge commits, no unique work) are auto-skipped and recorded in state.
- PRs are sorted by size so quick reviews happen first.
- The script uses `gh` CLI for all GitHub interaction — ensure it's authenticated.
- Reviews use GitHub's pull request review API, not plain comments. Findings appear as inline annotations on the diff.
