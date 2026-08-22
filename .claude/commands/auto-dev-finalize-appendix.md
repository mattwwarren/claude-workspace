> Companion appendix to /auto-dev-finalize. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 4+5 Finalize — Appendix

Interactive-only procedures and design rationale extracted from
`.claude/commands/auto-dev-finalize.md` (#1879). Each section is reached from a
named trigger sentence in the core doc. Headless runs never need any of it:
Stage 5 is auto-skipped, and every AskUserQuestion gate below collapses per the
gate-collapse table.

---

## Step 4a — interactive open-PR prompt

Gather the prior PR's status, then present:

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

- **Wait** → Pause. On "continue", re-check PR status: if merged, proceed; if
  still open, re-ask.
- **Force** → Proceed with PR creation despite the open PR. **Stacked PRs are
  always created as DRAFTS** so they cannot merge ahead of the bottom of the
  stack; `/review-monitor` promotes them to ready when the parent (oldest open
  pipeline PR) merges. Later tickets stay gated on the bottom PR.
- **Abort** → Stop pipeline, summarize state.

(The **Fix** branch's spawn instruction stays in the core doc — its model pin is
guard-tested there.)

---

## Step 4b — interactive ship-summary prompt

Present the ship summary before delegating execution; this preserves the
pipeline's scope-aware approval gate, since the underlying per-project
`/ship-it` may not re-ask.

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

---

## Why Stage 4 delegates to /prep-pr

`/prep-pr` delegates to the per-project `.claude/commands/ship-it.md`, which
knows repo-specific PR conventions (template, labels, reviewers, base branch, CI
bootstrap) the pipeline shouldn't hardcode. It keeps sync-with-main and
quality-gate-rerun logic in one place instead of duplicating them across
`/auto-dev` Stage 4b and `/prep-pr` Step 1/7, and it handles PR monitor
registration in its Step 9.

The Pre-Stage Detector Guard exists for the same idempotency reason: without it,
a session dying between `/prep-pr` succeeding and Step 4d completing would
re-attempt PR creation.

---

## Stage 5: CI Wait and review-feedback handling (interactive only)

Headless runs AUTO-SKIP all of Stage 5 and return immediately after auto-merge
is enabled in Step 4d. Everything below is the interactive procedure.

### Step 5a — poll for CI

```bash
# Poll every 30 seconds for up to 10 minutes
gh pr checks <number> --watch --fail-fast 2>/dev/null
# If --watch not available, poll manually:
# Loop: gh pr checks <number> --json name,state,conclusion
# Exit when: all checks conclude, or 10 minutes elapsed
```

**If all checks pass within 10 minutes:** log "CI passing" and proceed to
Step 5b. **If checks are still pending after 10 minutes:** log "CI still running
after 10m — proceeding. Auto-merge will complete when CI passes." and proceed to
Step 5b.

**If any check fails, AskUserQuestion:**

```
CI failed on PR #<N> (<title>):

<failed check name>: <conclusion>
<failure details via: gh pr checks <number> --json name,state,conclusion,detailsUrl>

Options:
1. Fix — I'll investigate and push a fix (triggers another 10m CI wait)
2. Ignore — proceed to next ticket (auto-merge stays pending)
3. Abort — stop pipeline
```

- **Fix** → spawn the CI-fix agent named in the core doc's Step 5a bullet, then
  loop back to Step 5a. Max 2 fix attempts, then escalate.
- **Ignore** → proceed (user handles CI manually).
- **Abort** → stop pipeline.

### Step 5b — initial review feedback check

```bash
gh api repos/{owner}/{repo}/pulls/<number>/reviews --jq '.[] | select(.state != "COMMENTED" and .state != "APPROVED") | {user: .user.login, state: .state, body: .body}'
gh api repos/{owner}/{repo}/pulls/<number>/comments --jq '.[] | {user: .user.login, path: .path, line: .line, body: .body}'
```

**If no reviews, or only APPROVED/COMMENTED:** log "No review feedback requiring
action" and proceed.

**If CHANGES_REQUESTED is found, AskUserQuestion:**

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

- **Address** → spawn the feedback-address agent named in the core doc's
  Step 5b bullet, then loop back to Step 5a for the CI wait on the new push.
- **Skip** → proceed to next ticket.
- **Discuss** → draft reply comments for each piece of feedback, present them via
  AskUserQuestion before posting, then post approved replies via `gh api`.

### Step 5c — continue to next ticket

1. If more tickets in queue: loop back to **PR Hygiene Sweep** (top of ticket
   loop) for the next ticket.
2. If no more tickets: proceed to Pipeline Summary.

---

## Unavailability classifier: push sites and mirror provenance

The classifier covers three push sites the subagent's returned text can reflect,
in the order a single run hits them: (1) Step 4c.2's post-merge push (#1414),
(2) `/prep-pr`'s own Step 1 sync-with-base push (#1414), and (3) the delegated
project's `ship-it.md` initial `git push -u origin "$BRANCH"` (#1049).

The signature list in the core doc is a PROSE MIRROR of
`src/cw/unavailability.py`'s `UNAVAILABILITY_SIGNATURES`; keep the two copies in
sync — `test_unavailability_signatures_mirrored_in_prose` is the drift guard.
`MCP-github-unreachable` is deliberately not mirrored: no verified signature
exists yet; see the `src/cw/unavailability.py` module docstring.

Step 4c.5's own rebase-retry push is a separate site, checked directly by the
main session, which reuses this same signature list.

`push_auth_failed` is an open-enum addition to `blocker.reason`
(headless-contract.md §4.2 — `reason` is open by design, the same precedent as
`merge_conflict_post_push`). Consumers surface it verbatim; no parser change is
needed.
