> Companion appendix to /auto-dev-review. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 3 Review — Appendix

Rare-branch procedures and design rationale extracted from
`.claude/commands/auto-dev-review.md` (#1879). Each section is reached from a
named trigger sentence in the core doc; nothing here is required on the common
path (reviewers spawn, findings adjudicate, fix loop converges inside its
budget).

---

## Parent turns and subagent turns are not symmetric

A parent's turn-end is a *pause* — the completion notification resumes it. A
**subagent's** turn-end is a *return*: its final text becomes the result, and any
work it left running in the background does not survive to a later turn.

So a subagent must finish what it started inside its own turn — a subagent that
backgrounds a long command and then ends its turn reports "done" while the
command is still running. `run_in_background` remains a real and valid `Bash`
parameter; it is only the Agent-tool spawn that no longer has it.

---

## Interactive-only adjudication gates

**Small scope + MUST_FIX → AskUserQuestion (interactive only):**
- Present MUST_FIX findings (with file, line, description, suggested fix)
- Present SHOULD_FIX findings if any
- "MUST_FIX findings block shipping. Fix and re-review, skip fixes and ship
  anyway, skip ticket, or abort?"

**Large scope (any result) → AskUserQuestion (interactive only):**
- Present full consolidated review report
- If MUST_FIX: "Fix these issues and re-review, or abort?"
- If clean or SHOULD_FIX only: "Review complete. Proceed to PR creation?"

Headless never reaches either gate: adjudication is autonomous, per the Headless
callout in the core doc.

---

## `.cw/deferred-findings.md`: round stamping, legacy files, and hard-error refusal

A round-stamped entry additionally carries `[round <N>, <recorded_at>] ` in front
of its `Rejected` bullet, and trailing `round:` / `recorded_at:` lines inside its
`DEFERRED-REVIEW-FINDINGS` entry. Both are omitted for an unstamped entry, so the
bare shape in the core doc stays exactly what a pre-#1840 file looks like.

The `Rejected` section is omitted when there are no rejections, the
`DEFERRED-REVIEW-FINDINGS` block when there are no deferrals, and the file is not
written at all when every finding was fixed and there is no prior content to
preserve — all three handled by the command, not by you.

**A file left over from before #1840** (no round/date stamps anywhere) is read and
merged like any other prior content — it is not an error, and its entries are
never back-filled with a synthetic round. The command hard-errors (exit 1, plain
message) only on content matching *neither* the current nor the pre-#1840 shape —
foreign text, a truncated block, a half-written round/date pair. That is a
refusal to overwrite records it cannot read, not a transient failure: inspect or
remove the file by hand, then re-run.

---

## Why the fix loop uses push-then-recheckout

You cannot attach a new subagent to the original implementation worktree.
Subagents without `isolation: "worktree"` inherit the main session's sandbox
(which typically excludes other worktrees), and `isolation: "worktree"` always
creates a *new* worktree. Pushing the branch and re-checking it out inside the
new sandbox is the only shape that reaches the same commits.

---

## Fallback — direct execution from the main session's worktree

Reached only if the isolation fix agent *also* hits sandbox failures (Read/Write/
Bash denied inside its own new worktree), after two subagent attempts have
failed. Slower than delegation but guaranteed to work; it is a last resort, not
a shortcut past the fix loop.

```bash
# From the main session's worktree
git fetch origin <branch-name>
git checkout -B <branch-name> origin/<branch-name>   # -B: idempotent — cw provisions this worktree on <branch-name> (#712), so plain -b would fail "already exists"
git merge origin/main --no-edit                      # refresh with main (see Step 3b.2 rationale)
# apply edits via Read/Edit/Write tools
# run quality gates
git add -- <changed files>
git commit -m "..."
git push origin HEAD:refs/heads/<branch-name>        # explicit refspec — robust if local branch was renamed
test "$(git rev-parse origin/<branch-name>)" = "$(git rev-parse HEAD)"  # verify the push landed
git checkout <original-branch>   # restore main session state
```

The Pre-exit invariant in the core doc still applies after this path runs.
