---
description: "Ship the current branch as a PR with auto-merge enabled (claude-workspace project ship-it)"
argument-hint: "[--base <branch>] [--title <value>]"
allowed-tools: ["Bash", "Read"]
---

# Ship It (claude-workspace)

Project-level ship-it for the `claude-workspace` (cw) repo. Runs after `/prep-pr` finishes its self-review and quality gates. Creates a PR, enables auto-merge, registers a monitor, and runs the finalize verification.

**Arguments:** "$ARGUMENTS"

Quality gates (ruff/mypy/pytest) are handled by `/prep-pr` Step 7 — do not re-run them here. This file covers push, PR creation, auto-merge, monitor registration, and finalize only.

---

## Step 1: Parse arguments

Default base to `main`. Override with `--base <branch>` if provided in `$ARGUMENTS`. Extract `--title <value>` into `EXPLICIT_TITLE` if provided:

```bash
EXPLICIT_TITLE=""
if printf '%s' "$ARGUMENTS" | grep -qE '(^| )--title '; then
  EXPLICIT_TITLE=$(printf '%s' "$ARGUMENTS" | sed 's/.*--title //' | sed 's/ --.*//')
fi
```

## Step 2: Confirm branch is pushed

```bash
BRANCH=$(git branch --show-current)
git push -u origin "$BRANCH" 2>&1
```

If push fails (e.g., diverged), BLOCK — do not force-push without explicit user approval.

## Step 3: Create the PR

Read the latest commit message and recent commits to draft a PR title + body.

```bash
RANGE_BODY=$(git log --format='- %s' "origin/main..HEAD")

# Tier 1: Explicit --title override (passed from /prep-pr --title)
LAST_RESORT_TITLE=""
if [ -n "$EXPLICIT_TITLE" ]; then
  TITLE="$EXPLICIT_TITLE"
else
  # Tier 2: First substantive conventional-commit (excludes chore/style/revert)
  TITLE=$(git log --reverse --format='%s' origin/main..HEAD \
    | grep -E '^(feat|fix|docs|refactor|test|build|perf|ci)(\(.*\))?:' \
    | head -1)

  if [ -z "$TITLE" ]; then
    # Tier 3: Ticket title fallback (reuses existing CLOSES_TRAILER jq/cw-context pattern)
    if [ -f .claude/cw-context.json ]; then
      TICKET_ID=$(jq -r '.ticket_id // empty' .claude/cw-context.json 2>/dev/null)
      if printf '%s' "$TICKET_ID" | grep -qE '^[0-9]+$'; then
        TITLE=$(gh issue view "$TICKET_ID" --json title -q .title | cut -c1-72)
      fi
    fi
  fi

  if [ -z "$TITLE" ]; then
    # Tier 4: Last resort — HEAD subject (pre-fix behavior)
    TITLE=$(git log --format='%s' -1)
    LAST_RESORT_TITLE=true
  fi
fi
```

Build a `Closes #<n>` trailer when this is an orchestrated run (#491). `cw`
drops the ticket id in `.claude/cw-context.json` for every auto-dev worktree, so
read it from there — no branch-name parsing. Only emit the trailer for a
GitHub-numeric id (so the PR auto-closes the issue and feeds the
`closedByPullRequestsReferences` linkage that reconcile/doctor merged-detection
keys on). Interactive runs (no `cw-context.json`) and Linear ids (closed via
Linear status, not GitHub) get no trailer:

```bash
CLOSES_TRAILER=""
if [ -f .claude/cw-context.json ]; then
  TICKET_ID=$(jq -r '.ticket_id // empty' .claude/cw-context.json 2>/dev/null)
  if printf '%s' "$TICKET_ID" | grep -qE '^[0-9]+$'; then
    CLOSES_TRAILER="Closes #${TICKET_ID}"
  fi
fi
```

Create the PR with `gh pr create`:

```bash
gh pr create \
  --base "${BASE:-main}" \
  --head "$BRANCH" \
  --title "$TITLE" \
  --body "$(cat <<EOF
## Summary

$RANGE_BODY

## Test plan

- [ ] ruff check — zero violations
- [ ] mypy — zero type errors
- [ ] pytest — 100% pass rate
- [ ] No regressions in dispatch, reconcile, or other affected modules

${CLOSES_TRAILER}
${LAST_RESORT_TITLE:+
> Title derived from HEAD commit — verify it is correct.
}🤖 Shipped via /prep-pr + project /ship-it
EOF
)"
```

If PR creation fails, BLOCK with the `gh` error verbatim.

## Step 4: Enable auto-merge

```bash
PR_NUMBER=$(gh pr view --json number -q .number)
gh pr merge "$PR_NUMBER" --auto --squash
```

If auto-merge fails, BLOCK — the PR exists but auto-merge isn't on; don't silently leave it unset.

## Step 5: Register PR monitor

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

If registration fails, report the error but do not block — monitor is advisory. If `/prep-pr` Step 9 requires it via `--require-monitor`, it will catch the gap and retry.

## Step 6: Run finalize verification

This is the contract that proves /ship-it actually completed its side effects. /prep-pr will re-run this and require the JSON to show `status: "ok"` and a non-null `pr_number`.

```bash
~/.claude/scripts/prep_pr_finalize.py verify --require-automerge --json
```

Print the JSON output verbatim — do not summarize.

If the script exits non-zero, BLOCK with the JSON. Do not paper over failures.

---

## Failure modes

- **Push fails (diverged):** BLOCK. User must rebase or merge main first.
- **PR creation fails:** BLOCK with the `gh` error verbatim.
- **Auto-merge enable fails:** BLOCK — the PR exists but auto-merge isn't on; don't silently leave it unset.
- **Finalize verification fails:** BLOCK with the JSON; do not paper over.

No fallbacks. No silent retries. Errors surface to the user via /prep-pr's BLOCK handling.
