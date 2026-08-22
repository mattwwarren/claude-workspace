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

## Blocking-findings comment rule: header, body shape, and the three triggers

Reached from Checkpoint 3a of the core doc, and only when a round is heading
for one of three exits — the mechanically-rejected MUST_FIX path (#1714), the
cycle-5 hard exit in Step 3b.5, or the 4a `plan_deviation` Exit rule. A round
that converges never reaches any of them.

**Blocking-findings comment rule (#1815).** When Stage 3 exits `blocked` with `blocker.reason: "review_blocked"` — the mechanically-rejected MUST_FIX path (#1714) or the cycle-5 hard exit in Step 3b.5 — post the still-unresolved MUST_FIX finding(s) as a tracker comment under the fixed header `## Blocking Review Findings`, the same surface `auto-dev-plan.md` uses for its `plan_unreviewable`/`plan_unsound` exits. Source the body from the structured finding data (`file`/`line_start`/`line_end`/`summary`/`suggested_fix`, plus `reviewer_role`) — one `### <reviewer_role> — MUST_FIX` sub-section per finding. No PR exists yet, so the comment posts to the ticket. Sentinel: append `blocking findings posted: review_blocked` to `friction_highlights`, mirroring `auto-dev-plan.md`'s idiom.

**Third trigger (#1817): the `plan_deviation` exit.** The same rule, header, and structured-finding source also cover Stage 3's `blocker.reason: "plan_deviation"` exit (the 4a Exit rule in the core doc). Delta vs. the two `review_blocked` sites above, which stay MUST_FIX-worded (#1714's guarantee is MUST_FIX-specific there): the `plan_deviation` trigger posts whatever NON_DEFERRABLE finding(s) caused the exit, **regardless of severity**, because 4a's NON_DEFERRABLE test is not severity-scoped.

---

## Operator-actionable findings comment rule: header, checklist format, and trigger

This is the operator-actionable findings comment rule the core doc's
Checkpoint 3a defers to. It is reached only when bucket 4 landed non-empty —
i.e. `ADJUDICATIONS` carries at least one `outcome: "operator_action"` entry.

**Operator-actionable findings comment rule (#1817).** Post those findings as a tracker comment under the fixed header `## Operator-Actionable Review Findings` — a literal markdown checklist, one line per entry, so the operator can tick items off in place:

```
- [ ] <summary> — <suggested_fix> *(<reviewer_role>)*
```

Source the body from the structured finding data, as the blocking-findings comment rule does. The entry's `rationale` is REQUIRED and must name the concrete action the operator has to take — file ticket X, link the ticket discharging criterion Y — so a checklist reader never has to open the transcript. No PR exists yet, so the comment posts to the ticket. Sentinel: append `operator actionable findings posted: review_operator_actionable` to `friction_highlights`, mirroring the `blocking findings posted: <reason>` idiom.

**Its trigger is `ADJUDICATIONS`, not `blocker.reason`.** The rule fires purely on the presence of an `operator_action` entry, whatever `blocker.reason` the stage exits with. That decouples the racing-exits case: a pass carrying both an operator-actionable finding and a separate NON_DEFERRABLE finding records `plan_deviation` as the exit reason and still posts this comment. Two comments, one exit reason, no precedence ladder.

---

## Fix loop cycle budget: escalation triggers and the cycle-5 hard exit

Reached from Step 3b.5 of the core doc, and only once the fix loop entered
cycle 3, the fix-loop friction report flagged scope growth, or the cap was
exhausted. A loop that converges inside the expected 2 cycles never reaches
here.

**Escalation triggers** (any of these counts as "an escalation event"):
- Cycle 3, 4, or 5 entered (i.e., MUST_FIX persisted past the expected 2)
- Fix-loop diff touches files outside the original Stage 1 approved plan's file list
- Fix-loop diff promotes scope tier from Small → Large (file count > 10 OR line count > 500 OR forbidden area touched)

The fix-loop agent's friction report MUST flag scope growth explicitly so the main session can decide whether the cycle counts as an escalation event. Do not let the agent silently grow scope.

**Interactive — on each escalation event:** log a one-line notice describing the trigger (e.g. `⚠ Fix loop entered cycle 3 (expected baseline is 2)`). Do NOT block on AskUserQuestion — the user can stop the pipeline between agent dispatches, and the cycle-5 hard gate is the final decision point. A deliberate trade-off against prompt fatigue: interactive cycles 3-4 run with notice-only visibility.

**Headless — on each escalation event:** append a string to `friction_highlights` (e.g. `"fix_loop_cycle_3_entered"`, `"fix_loop_scope_growth: <files>"`) AND set `health.fix_loop_escalated: true` in the structured output. Continue the loop without any AskUserQuestion. (`health.fix_loop_escalated` is distinct from `health.downgrade_applied`, which only the Headless Mode health aggregation rule sets for confidence-driven status downgrades.)

**Hard exit (cycle 5 failed to clear MUST_FIX) — applies in both modes:**
- **Interactive:** AskUserQuestion: "MUST_FIX issues persist after 5 fix cycles: [details]. Continue manually from worktree, skip ticket, or abort pipeline?"
- **Headless:** EXIT `blocked` with `blocker.reason: "review_blocked"`. The `friction_highlights` field will contain the per-cycle escalation notes from cycles 3–5; the human reviewer sees them in the structured output. Also post the still-unresolved MUST_FIX findings — verbatim — as a tracker comment per the blocking-findings comment rule above (Checkpoint 3a).

> **Maintenance note:** the cap values (`expected 2`, `hard-cap at 5`) appear in 6 locations: Step 3b.5 in the core doc, this section (multiple), the Checkpoint 3a Headless callout, the gate-collapse table rows for `S3 action list non-empty`, `S3 action list non-empty after 5 fix cycles`, and `S3 fix-loop cycle 3+`, and the `blocker.reason` table description for `review_blocked`. If you tune either value, update all locations atomically.

---

## Pre-exit invariant: what to do when the tree is dirty

Reached from the core doc's pre-exit invariant, and only when
`git status --porcelain` printed something. A clean tree is the common path.

You MUST either:
- **Commit and push** the staged changes (if they represent completed work), then emit the sentinel as normal, OR
- **Emit a `blocked` sentinel** using the full sentinel template from Stage 3 Completion in the core doc, with `blocker.reason: "dirty_tree_no_sentinel"`, `scope.tier: "small"` (required by the schema validator even on blocked — `auto_dev_result/schema.py`'s §3.3 validator rejects null at stage3_review), `blocker.details: "staged or unstaged changes exist but could not be committed and pushed before session end — emitting blocked rather than exiting silently with a dirty index and no sentinel"`, and `health.lowest_agent_confidence` set to a non-null value (the same §3.3 validator in `auto_dev_result/schema.py` requires it for stage3_review; omitting it causes schema rejection → `validation_failed` retries rather than `BLOCKED_ON_USER`).

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
