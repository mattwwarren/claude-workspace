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
- **No / Abort** → Stop, report worktree path so the orchestrator can pick it up

---

## Why Stage 4 delegates to /prep-pr

`/prep-pr` delegates to the per-project ship-it (a command or a skill — see Step 8's probe), which
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
  loop back to Step 5a. Max 2 fix attempts, then park for the orchestrator.
- **Ignore** → proceed (the orchestrator picks up the CI failure from the open PR).
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
2. Skip — proceed to next ticket (the orchestrator addresses the feedback later)
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

## Post-push merge conflict: the `merge_conflict_post_push` sentinel

Reached from Step 4c.5 of the core doc, and only when the single auto-rebase attempt and the semantic auto-resolve both failed or were refused. A cleanly-mergeable PR never reaches here.

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
    "recovery_hint": "Orchestrator: rebase in the impl worktree (resolve conflicts, push), OR close PR #<N> and re-dispatch the ticket",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": ["manual_intervention"]
}
```

**Critical: every field above is required.** `scope`, `plan_source`, and `health` MUST carry real values from earlier stages. Emitting `null` or omitting them fails consumer schema validation and synthesizes a `validation_failed` blocker that masks the real `merge_conflict_post_push` reason.

**Producer note:** `merge_conflict_post_push` is an open-enum addition to `blocker.reason` (headless-contract.md §4.2 — `reason` is open by design). Consumers surface it verbatim; no parser change needed.

**Defense-in-depth handoff:** the blocker is `retry_eligible: true` because `/review-monitor` auto-engages on orphaned CONFLICTING PRs authored by `@me`. If it rebases successfully the orchestrator can re-dispatch this ticket; if it also fails, `recovery_hint` hands over to the orchestrator, which rebases the impl worktree itself or closes the PR and re-dispatches.

---

## Unavailability classifier: signature table, sentinel, and push sites

Reached from Step 4c.2 of the core doc, and only when the `/prep-pr` subagent
returned text that looks like an unavailability failure. A healthy run never
matches, so this whole block is rare-path. Match any signature below verbatim;
do not add or remove one from memory.

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

If any signature is present, emit the structured `blocked` sentinel below and stop — do NOT proceed to the verify-script gate or Step 4c.5.

**Sentinel template — `push_auth_failed` blocker:**

```json
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "blocked",
  "stage_reached": "stage4b_pr_create",
  "scope": {
    "tier": "<resolved scope.tier — see 'Resolve carried-through context' in the core doc>",
    "files": <count>,
    "lines_estimate": <count>,
    "lines_actual": <count>,
    "forbidden_touched": false
  },
  "plan_source": "<resolved plan_source — see 'Resolve carried-through context' in the core doc>",
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
    "details": "<matched signature + which push site, e.g. 'ship-it.md initial push: Permission denied (publickey)', 'Step 4c.2 post-merge push: Could not resolve host', or 'prep-pr.md Step 1 sync-with-base push: Authentication failed'>",
    "exception_type": null,
    "message": "git push failed authentication (SSH key locked or credentials expired)",
    "recovery_hint": "Orchestrator: refresh credentials and requeue the ticket (cw dev-queue requeue). Unlocking an SSH key passphrase (ssh-add) is the one physically-human step",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": []
}
```

**Do not add `push_auth_failed` to `FINALIZE_REGRESS_BLOCKER_REASONS`** (`auto_dev_result/schema.py`, currently `{"agent_block"}`). A locked SSH key is not fixed by re-running implementation; regressing FINALIZE→IMPL would burn `FINALIZE_REGRESS_CAP` attempts against a still-locked key. Park for the orchestrator via the sentinel above; the SSH-key passphrase unlock (`ssh-add`) is a physical-human exception — everything else in the recovery is the orchestrator's.

**cw-side classification (RFC 0011 A1, #1155):** `push_auth_failed` is retro-classified under `OPERATOR_UNAVAILABLE_BLOCKER_REASONS`, so cw tags its park `paused_status: "awaiting_operator_availability"` rather than generic `"blocked"` — a cw-side (`dispatch/routing.py`) routing change only, no producer change required.

**Push sites covered.** Three, in the order a single run hits them: (1) Step 4c.2's post-merge push (#1414),
(2) `/prep-pr`'s own Step 1 sync-with-base push (#1414), and (3) the delegated
project's `ship-it.md` initial `git push -u origin "$BRANCH"` (#1049).

The signature list above is a PROSE MIRROR of
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

---

## Step 4c re-verification failure: the `automerge_not_armed` sentinel

Reached from Step 4c's main-session re-verification in the core doc, and only
when `prep_pr_finalize.py verify --require-automerge` reported the
`automerge-enabled` check failed. The verify passing is the common path.

**Sentinel template — `automerge_not_armed` blocker:**

```json
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "blocked",
  "stage_reached": "stage5_post_create",
  "scope": {
    "tier": "<resolved scope.tier — see 'Resolve carried-through context' in the core doc>",
    "files": <count>,
    "lines_estimate": <count>,
    "lines_actual": <count>,
    "forbidden_touched": false
  },
  "plan_source": "<resolved plan_source — see 'Resolve carried-through context' in the core doc>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path — ~/.cw/wt/<hash>/auto-dev-<ticket>>",
  "pr": null,
  "pr_info": {
    "number": <pr_number>,
    "url": "<pr_url>",
    "auto_merge": false,
    "base": "main"
  },
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
    "stage": "stage5_post_create",
    "reason": "automerge_not_armed",
    "details": "Step 4c re-verification: prep_pr_finalize.py verify --require-automerge reported automerge-enabled check failed (autoMergeRequest read back null) for PR #<N>; arm-automerge: attempts=<k>/<max>, gh exit <code>, gh stderr: <verbatim gh_stderr, or 'none -- gh exited 0 but autoMergeRequest read back null'>",
    "exception_type": null,
    "message": "auto-merge was never armed after bounded retries (see details for gh's error)",
    "recovery_hint": "Orchestrator: run `prep_pr_finalize.py arm-automerge <pr-number> --repo-path <worktree> --head-sha <head-sha>` and re-verify, or merge the PR directly",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": ["manual_intervention"]
}
```

**Variants** — the template above is written for the retries-exhausted path. Fill `blocker.details` and `blocker.message` per the path that emitted the sentinel; every other field is unchanged:

- (a) Retries exhausted (`arm-automerge` exit 1): the template above as written.
- (b) No arm attempted (arm genuinely not attempted: `automerge-enabled` plus another failed check, or null `pr_number`; a denied arm is (d), not (b)): `details` are the original verify-only text — `Step 4c re-verification: prep_pr_finalize.py verify --require-automerge reported automerge-enabled check failed (autoMergeRequest read back null) for PR #<N>` with no `arm-automerge:` tail — and `message` is `auto-merge is not armed (no arm attempted: see details)`.
- (c) Invocation error (exit 2, or any code other than 0/1/3): `details` begin `arm-automerge invocation error (exit <code>, not a gh failure): <stderr>` and `message` is `arm-automerge could not run (invocation error, not a gh failure)`, which also covers an existing `.claude/project-config.yaml` whose `pr.auto_merge` cannot be determined (fail closed, #2581): the stderr names the reason.
- (d) Classifier-denied arm (#2625): the `arm-automerge` Bash call was denied by the auto-mode permission classifier, so `prep_pr_finalize.py` never started and the call has no exit status. Use it only when `pr_number` is non-null AND the denied call is the arm command (see Step 4c in the core doc for the detection gate). It applies even when other checks failed. `details` are `arm-automerge blocked by the auto-mode permission classifier before it ran (not a gh failure, not an invocation error); the classifier message, quoted verbatim, is: "Permission for this action was denied by the Claude Code auto mode classifier. Reason: <verbatim reason>". PR #<N> is open with autoMergeRequest null at head <head_sha> (checks may still be running; that does not block arming). The arm was not retried and not worked around (no direct gh pr merge). <if other checks failed: also failed: <names>>`, where `<verbatim reason>` is replaced at emit time by the actual text after `Reason:`. `message` is `auto-merge was not armed: the arm command was denied by the auto-mode permission classifier (an auto-mode classifier gate, not a gh failure)`. `recovery_hint` is exactly: ``Run `prep_pr_finalize.py arm-automerge <pr-number> --repo-path <worktree> --head-sha <head-sha>` from an operator shell (the orchestrator's own shell; idempotent, head-pinned; <head-sha> is the 40-hex head SHA), then `cw dev-queue requeue <ticket> -c <client>` or let the PR merge (cw releases the row). To stop recurrence the orchestrator can add an allow rule for that exact command in their own settings.`` Nothing more: never suggest that the worker change its own permissions, try another tool, interpreter or encoding, or merge directly. `retry_eligible: true` (the classifier is non-deterministic, claude-workspace#183; cw does not route on it).
  Here the reason stays `automerge_not_armed`: the sentinel's `pr_info` carries the PR, so cw stamps `pr_url` and releases the row on merge (Rule 5 `pr_info` stamp, `src/cw/dispatch/routing/__init__.py:959`; Variant A release, `src/cw/reconcile/tasks.py:832`; both keyed on this reason). This deliberately narrows the generic Tool-Use Denial Exit for this one call; see Step 4c.2 (#636).

**Do not add `automerge_not_armed` to `FINALIZE_REGRESS_BLOCKER_REASONS`** (`src/cw/auto_dev_result/schema.py:83`, currently `{"agent_block"}`). A failed auto-merge arm is not fixed by re-running implementation; regressing FINALIZE→IMPL would burn `FINALIZE_REGRESS_CAP` attempts against a PR that already exists and just needs re-arming. Park for the orchestrator via the sentinel above; the orchestrator runs `arm-automerge` itself.

**Producer note:** `automerge_not_armed` is an open-enum addition to `blocker.reason` (headless-contract.md §4.2 — `reason` is open by design). Consumers surface it verbatim; no parser change needed.

---

## Step 4c.2: the `auto` permission-mode limitation, and the no-`/ship-it` block

**Permission mode (known limitation, #636 — deferred):** headless workers run under `claude --bg --permission-mode auto` (`native_daemon.py` `_DEFAULT_PERMISSION_MODE`), so the `auto` classifier fires on `gh pr create` inside a worktree-isolated subagent and, with no TTY to approve, blocks `/prep-pr`. The allowlist `Bash(gh pr:*)` does NOT suppress it, and setting `bypassPermissions` on *this subagent spawn alone* is ineffective — the worker's own `auto` mode is the source. The effective fix (spawning the worker with a non-`auto` `permission_mode`) is **deferred** to RFC 0005's FINALIZE/REVIEW stages (#622/#621); until then a classifier block surfaces as a BLOCK for the orchestrator to ship.

**If the agent returns BLOCK due to "no project `/ship-it`":** The project hasn't been set up for automated PR creation. AskUserQuestion: "Project has no ship-it in any layout `/prep-pr` probes (`.claude/commands/ship-it.md`, `.claude/skills/ship-it/SKILL.md`, `.agents/skills/ship-it/SKILL.md`). Set one up now via `/setup` and resume, skip this ticket (leave branch pushed), or abort pipeline?" (Headless: BLOCK for the orchestrator to set up ship-it via `/setup`.)
