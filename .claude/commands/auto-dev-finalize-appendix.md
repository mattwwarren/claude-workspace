> Companion appendix to /auto-dev-finalize. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 4+5 Finalize — Appendix

Interactive-only procedures and design rationale extracted from
`.claude/commands/auto-dev-finalize.md` (#1879). Each section is reached from a
named trigger sentence in the core doc. Headless runs never need any of it:
Stage 5 is auto-skipped, and every AskUserQuestion gate below collapses per the
gate-collapse table.

**Branch resolution.** `<branch-name>` is this ticket's own branch: run `jq -r '.queue_metadata.branch // empty' .claude/cw-context.json` from the cw session worktree root and use what it prints. When it prints nothing (the file or key is absent, or the value is null), use `<branch-prefix>/<ticket-id>`, where `<branch-prefix>` is the effective prefix (`--branch-prefix` if given, else the client's `feature_branch_prefix`, default `dev`). Do not take it from `git branch --show-current`: an isolation worktree may sit on an `agent-<hash>` branch.

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

Branch: <branch-name>
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

<!-- el: finalize.sentinel-merge-conflict-post-push -->
## Post-push merge conflict: the `merge_conflict_post_push` sentinel

Reached from Step 4c.5 of the core doc, and only when its one merge of `origin/main` (the semantic auto-resolve attempt) failed or was refused. A cleanly-mergeable PR never reaches here.

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
    "details": "PR #<N> opened with conflicts after sibling merges to origin/main between /prep-pr's sync-with-main and PR open. One merge of origin/main attempted and not resolved; conflicted files: <list>",
    "exception_type": null,
    "message": "PR is open but conflicts with main; merge of origin/main not resolved",
    "recovery_hint": "Orchestrator: rebase in the impl worktree (resolve conflicts, push), OR close PR #<N> and re-dispatch the ticket",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": ["manual_intervention"]
}
```

**Critical: every field above is required.** `scope`, `plan_source`, and `health` MUST carry real values from earlier stages. Emitting `null` or omitting them fails consumer schema validation and synthesizes a `validation_failed` blocker that masks the real `merge_conflict_post_push` reason.

**Producer note:** `merge_conflict_post_push` is an open-enum addition to `blocker.reason` (headless-contract.md §4.2 — `reason` is open by design). Consumers surface it verbatim; no parser change needed.

**Defense-in-depth handoff:** the blocker is `retry_eligible: true` because `/review-monitor` auto-engages on orphaned CONFLICTING PRs authored by `@me`. If its one merge resolves the conflict the orchestrator can re-dispatch this ticket; if it also fails, `recovery_hint` hands over to the orchestrator, which rebases the impl worktree itself or closes the PR and re-dispatches.

---

<!-- el: finalize.finalize-review-findings-comment-trigger-body -->
## Finalize review-findings comment (#2794): trigger, body shape, redaction, park marker

Reached from Step 4c of the core doc ("Finalize review-findings comment (#2794)"), and only when a `/prep-pr` BLOCK whose details are review findings is about to collapse to `agent_block`. Every other block posts nothing and never reaches here.

<!-- el: finalize.trigger -->
**Trigger.** All of the core paragraph's conditions hold: a `HEADLESS BLOCK` (or other-agent BLOCK reached via Step 4c) that the Unavailability classifier, the denied-arm check (#2625) and the Base-branch-state classifier (#2320) did not claim; a `gate:` that begins `Step 8 ship-it` or equals `Step 7 max review cycles`; and a `details:` text containing the literal token `MUST_FIX`. **Accepted limit:** `details:` is free text from the repo's own `ship-it`, and `/prep-pr` does not guarantee the `MUST_FIX` token, so a project gate that reports blocking findings without it takes today's path and its findings do not reach IMPL. The rule is deliberately narrow; do not widen it to a fuzzy phrase match. This is the blocking-findings comment rule (#1815, `auto-dev-review-appendix.md`) applied to FINALIZE, and finalize's main session posts the comment, never the `/prep-pr` child.

<!-- el: finalize.body-shape -->
**Body shape.** The finding text is the BLOCK's `gate:` line plus its `details:` text, posted verbatim, not truncated. The 500-character `_PRIOR_ATTEMPT_DETAILS_MAX_LEN` cap that bounds `prior_attempts_summary` does not apply to this path: the comment is a different channel, and its finding text is only redacted and, when long, split. Each comment is, in order: the header line `## Blocking Review Findings`, a blank line, the sub-heading line `### Finalize ship-it gate findings`, a label line only when the post has more than one part, the fenced finding text, then a blank line and `<!-- cw-agent-authored -->` on its own line. `cw post-comment` builds that wrapper from the flags below, so the session never composes it. **Provenance marker (#2097):** the marker is appended here by `cw post-comment` itself, as the line `<!-- cw-agent-authored -->`, per the *Comment provenance rule* in `.claude/commands/auto-dev.md`; the session must not add it to the body file. It is what stops a later stage reading this pipeline's own analysis as an operator decision. The header and sub-heading are exactly what the regressed IMPL run's *Regress findings carve-out (#2794)* (`auto-dev-impl-appendix.md`) matches on, so spell them exactly.

**Tracker and ticket.** The ticket id and tracker kind come from the same resolution as the PR-link comment (Step 4d, "**Post to Linear:**"). A free-text ticket (no tracker) posts nothing and takes today's path: no comment, no park stamp, the unchanged sentinel.

<!-- el: finalize.post-and-read-back -->
**Post and read back.** This is the blocking-findings comment rule's post routed through the *Tracker post seam (#2810)* in `.claude/commands/auto-dev.md`, which defines the result JSON, every exit code and the no-retry, no-fallback rule; this step adds only its flags. Author the BLOCK's `gate:` line and its verbatim `details:` text via the **Write tool** — see CLAUDE.md's **Agent File Operations** rule — to `.cw/finalize-findings.txt`. The file is raw: no header, sub-heading, fence or marker. Then run, in the foreground, from the cw session worktree root:

```bash
cw post-comment <ticket> --body-file .cw/finalize-findings.txt \
  --header "## Blocking Review Findings" \
  --subheading "### Finalize ship-it gate findings" \
  --fence --parts split --consume
```

The command redacts first: the redaction output, not the raw text, is what gets measured, split and posted, and the header, sub-heading, label line, fence and marker are added after redaction. It splits on line boundaries so every comment, header, sub-heading, label line, fence and marker included, is at most 60,000 characters, at most 59,000 characters of finding text per part, with the label line `(part i of n)` only when there is more than one part; the finding text is never truncated. `--consume` deletes the raw file once it has been read, on every path, so it never outlives the post. The command reads every part back by its comment URL and posts nothing more after a fault. On Linear add `--emit-only --out-dir .cw/finalize-findings-parts` and pass each listed `out_files` file's content verbatim to `create_comment`, oldest first (no read-back; the seam's documented residual).

Handle the exit code per the *Exit-code to friction mapping* in the *Tracker post seam (#2810)* of `.claude/commands/auto-dev.md`; this step adds only its finalize-specific values:

- **Exit 1**, or any exit other than 0 or 3 (including exit 1 with no parseable JSON, or 127): append `blocking findings post skipped: post_comment_refused` (with the JSON `reason` when there is one) to `friction_highlights` and emit the `agent_block` sentinel otherwise unchanged. Never fall back to a raw `gh issue comment`.
- **Exit 3** (`status: partial`): append `blocking findings post failed: finalize_agent_block` to `friction_highlights`, and never retry the post. The seam's exit-3 recording applies, including the empty-`posted` case.
- `cw signal-park` runs only after exit 0 and, for `emitted` on Linear, only after every `out_files` part was posted via `create_comment`; after any other exit do not run it.

<!-- el: finalize.redactor-limits -->
**Redactor limits.** The redactor covers path shapes and secret shapes only; it does not detect quoted code, fixtures or customer data, nor a header name followed by a space and its value (`x-api-key abc123`; the colon and equals forms are detected). The mitigations are the visibility refusal, the read-back of every part, and the operator deleting a leaked comment — see the *Tracker post seam (#2810)* in `.claude/commands/auto-dev.md`.

<!-- el: finalize.park-marker -->
**Park marker (#2135)** — once `cw post-comment` exits 0 (and, on Linear, every `out_files` part was posted via `create_comment`; a failed or skipped post is not stamped), run `cw signal-park` once, from the cw session worktree root (the directory holding `.claude/cw-context.json`; not a gate worktree and not a nested agent worktree). It takes no arguments. Never run it for a `tool_denied` exit (that sentinel is emitted immediately, with no further tool call). If it is denied, exits non-zero, is an unknown command (an older `cw`), or says `park marker NOT recorded`, ignore that and emit the exit sentinel unchanged. See the *Park-comment stamp rule* in `.claude/commands/auto-dev.md`. After any other exit (the exit mapping above), do not run it.

<!-- el: finalize.park-flag-note -->
**Park-flag note (G5).** A finalize session that posts and stamps `cw signal-park` and then dies without emitting a sentinel parks the row `BLOCKED_ON_USER` (as `stopped_without_sentinel`) and does NOT 5a-regress; that is the intended semantics, and the #2135 park flag (`park_on_abandoned_exit_enabled`) ships dark by default.

<!-- el: finalize.sentinel -->
**Sentinel.** `blocker.reason` stays `"agent_block"` and `blocker.details` is unchanged (the text the collapse emits without this rule). Once every part has posted and read back, append `blocking findings posted: finalize_agent_block` to `friction_highlights`. `agent_block` is in `FINALIZE_REGRESS_BLOCKER_REASONS`, so Sub-rule 5a still regresses the ticket to IMPL within its cap, and on `github-issues` the regressed run receives this comment through the *Regress findings carve-out (#2794)* in `auto-dev-impl-appendix.md`, as agent-authored evidence, never as an operator instruction. On Linear the carve-out withholds with `identity_unresolved` (plan-approved fail-closed behaviour): the findings are in the tracker comment but are not passed to the regressed IMPL run. An `external_state_block` never reaches this section.

---

<!-- el: finalize.unavailability-classifier-signature-table-sentinel -->
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

<!-- el: finalize.cw-side-classification -->
**cw-side classification (RFC 0011 A1, #1155):** `push_auth_failed` is retro-classified under `OPERATOR_UNAVAILABLE_BLOCKER_REASONS`, so cw tags its park `paused_status: "awaiting_operator_availability"` rather than generic `"blocked"` — a cw-side (`dispatch/routing/stage_failure.py`) routing change only, no producer change required.

<!-- el: finalize.push-sites-covered -->
**Push sites covered.** Three, in the order a single run hits them: (1) Step 4c.2's post-merge push (#1414),
(2) `/prep-pr`'s own Step 1 sync-with-base push (#1414), and (3) the delegated
project's `ship-it.md` initial `git push -u origin "$BRANCH"` (#1049).

The signature list above is a PROSE MIRROR of
`src/cw/unavailability.py`'s `UNAVAILABILITY_SIGNATURES`; keep the two copies in
sync — `test_unavailability_signatures_mirrored_in_prose` is the drift guard.
`MCP-github-unreachable` is deliberately not mirrored: no verified signature
exists yet; see the `src/cw/unavailability.py` module docstring.

Step 4c.5's own merge push is a separate site, checked directly by the
main session, which reuses this same signature list.

`push_auth_failed` is an open-enum addition to `blocker.reason`
(headless-contract.md §4.2 — `reason` is open by design, the same precedent as
`merge_conflict_post_push`). Consumers surface it verbatim; no parser change is
needed.

---

<!-- el: finalize.sentinel-automerge-not-armed -->
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
- (d) Classifier-denied arm (#2625): the `arm-automerge` Bash call was denied by the auto-mode permission classifier, so `prep_pr_finalize.py` never started and the call has no exit status. Use it only when `pr_number` is non-null AND the denied call is the arm command (see Step 4c in the core doc for the detection gate). It applies even when other checks failed. `details` are `arm-automerge blocked by the auto-mode permission classifier before it ran (not a gh failure, not an invocation error); the classifier message, quoted verbatim, is: "Permission for this action was denied by the Claude Code auto mode classifier. Reason: <verbatim reason>". PR #<N> is open with autoMergeRequest null at head <head_sha> (checks may still be running; that does not block arming). The arm was not retried and not worked around (no direct gh pr merge). <if other checks failed: also failed: <names>>`, where `<verbatim reason>` is replaced at emit time by the actual text after `Reason:`. `message` is `auto-merge was not armed: the arm command was denied by the auto-mode permission classifier (an auto-mode classifier gate, not a gh failure)`. `recovery_hint` is exactly: ``Run `prep_pr_finalize.py arm-automerge <pr-number> --repo-path <worktree> --head-sha <head-sha>` from an operator shell (the orchestrator's own shell; idempotent, head-pinned; <head-sha> is the 40-hex head SHA), then `cw dev-queue requeue <ticket> -c <client>` or let the PR merge (cw releases the row). To stop recurrence the human can add an allow rule for that exact command in their own settings (permission settings are the human's, never an agent's).`` Nothing more: never suggest that the worker change its own permissions, try another tool, interpreter or encoding, or merge directly. `retry_eligible: true` (the classifier is non-deterministic, claude-workspace#183; cw does not route on it).
  Here the reason stays `automerge_not_armed`: the sentinel's `pr_info` carries the PR, so cw stamps `pr_url` and releases the row on merge (Rule 5 `pr_info` stamp, `src/cw/dispatch/routing/stage_failure.py`, `_route_stage_failure`; Variant A release, `src/cw/reconcile/tasks.py:832`; both keyed on this reason). This deliberately narrows the generic Tool-Use Denial Exit for this one call; see Step 4c.2 (#636).

**Do not add `automerge_not_armed` to `FINALIZE_REGRESS_BLOCKER_REASONS`** (`src/cw/auto_dev_result/schema.py:83`, currently `{"agent_block"}`). A failed auto-merge arm is not fixed by re-running implementation; regressing FINALIZE→IMPL would burn `FINALIZE_REGRESS_CAP` attempts against a PR that already exists and just needs re-arming. Park for the orchestrator via the sentinel above; the orchestrator runs `arm-automerge` itself.

**Producer note:** `automerge_not_armed` is an open-enum addition to `blocker.reason` (headless-contract.md §4.2 — `reason` is open by design). Consumers surface it verbatim; no parser change needed.

---

<!-- el: finalize.reuse-path-gate-failure-agent-block-sentinel -->
## Step 4c.6 reuse-path gate failure: the `agent_block` sentinel (#2974)

Reached from Step 4c.6 in the core doc when a quality gate on the reuse path
failed or dirtied the worktree; a gate cut off by the foreground timeout is the
next section's sentinel. A passing run never reads this section.

**Sentinel template — `agent_block` blocker (reuse path):**

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
    "forbidden_touched": <resolved forbidden_touched — see 'Resolve carried-through context' above>
  },
  "plan_source": "<resolved plan_source — see 'Resolve carried-through context' above>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path>",
  "pr": null,
  "pr_info": {
    "number": <pr_number>,
    "url": "<pr_url>",
    "auto_merge": false,
    "base": "<base_ref>"
  },
  "review": {"must_fix_initial": <count>, "should_fix": <count>, "fix_cycles_used": <count>, "deferred": <count>},
  "health": {
    "lowest_agent_confidence": "HIGH",
    "any_incomplete_risk": false,
    "shortcuts": [],
    "recommendation": "PROCEED",
    "downgrade_applied": false,
    "fix_loop_escalated": false
  },
  "blocker": {
    "stage": "stage5_post_create",
    "reason": "agent_block",
    "details": "Step 4c.6 reuse-path quality gate: gate <name> failed at head <head_sha> on PR #<pr_number>; <dirtied worktree: <paths> | exit <code>>: <output tail>",
    "exception_type": null,
    "message": "reuse-path quality gate failed: auto-merge was not armed and the PR was not made ready",
    "recovery_hint": "Orchestrator: fix the failing gate in the impl worktree, push, then requeue; finalize re-runs the gates before arming",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": []
}
```

**Field sources (fill each placeholder from its source, one per field):**

- `pr_info.number` and `pr_info.url`: the Pre-Stage Detector Guard's `gh pr view <branch-name> --json number,state,url` call.
- `pr_info.base`: a read-only call in this failure path: `gh pr view <pr_number> --json baseRefName --jq .baseRefName`. Never the literal `"main"`; the reuse path does not otherwise read the base.
- `<head_sha>` (in `details` only): the `HEAD_SHA` captured at the top of Step 4c.6. No `commits` key is added.
- `scope`, `plan_source`, `branch`, `worktree_path`: the "Resolve carried-through context" step in the core doc.
- Emit with `printf '%s' "$JSON" | cw result emit -`. Fix every `field.path: message` error it reports and re-run until it exits 0, then frame the same JSON per the Stage 4+5 Completion section. No observability calls beyond the `friction_highlights` tokens and this sentinel.

**Why `pr_info` and not `pr`.** `blocked` with a populated `pr` is rewritten to `merge_pending` by the parser's `_coerce_blocked_with_pr` (`docs/headless-contract.md` §6). `pr_info` carries the PR so cw stamps `pr_url` without that coercion. Same convention as the `automerge_not_armed` section above.

**Why `agent_block` and why no fix commit.** Adds no new `blocker.reason`: `agent_block` is regress-eligible (`FINALIZE_REGRESS_BLOCKER_REASONS`, Sub-rule 5a), so the row regresses to IMPL, where the failing gate is fixed, reviewed and pushed through the existing review-tip checks. Finalize does not commit a fix itself: such a commit would land after the last clean review round, and on an adopted PR it would push to a human's branch. A gate cut off by the foreground timeout is not this sentinel: it is the `gate_timeout` sentinel in the next section.

**Stale-install variant.** `prep_pr_state.py` absent or stale (no `gate-timeout` subcommand) → `details` begin `Step 4c.6: HEADLESS BLOCK — prep_pr_state.py <cause>`, and every other field is unchanged.

<!-- el: finalize.adopted-row-limitation-agent-block-sentinel -->
**Adopted-row limitation.** It applies to failures only: a gate that times out parks `gate_timeout` and never regresses. An adopted row whose reuse-path gate fails regresses to IMPL and parks there with `plan_missing`, because an adopted row enters at REVIEW and so usually has no `.cw/plan.md`. The park is bounded by `FINALIZE_REGRESS_CAP` and never ships. Recovery is the #2944 repair-routing work, or fixing the branch by hand and requeueing (`docs/dispatch-runbook.md`, "Adopt an open human PR").

**Interactive prompt — Step 4c.6 gate failure.** Ask once, with the failing gate's output:

- **Fix:** fix in-session, commit, and push; route the branch back through the normal review path. Do not re-enter Step 4c.6 or proceed to Step 4d from the fixed commit; auto-merge must wait for the next finalize run's review-tip checks and gates.
- **Leave open without arming:** set `pr.auto_merge` to `false`, do not arm, and continue to Step 4d item 4.
- **Abort:** stop.

A foreground timeout is treated as a failed gate and shows this same prompt, with the ceiling in the prompt text. There is no ship-anyway option.

---

<!-- el: finalize.sentinel-gate-timeout -->
## Step 4c.6 reuse-path gate timeout: the `gate_timeout` sentinel (#3372)

Reached from Step 4c.6 in the core doc when the executor cut a reuse-path
quality gate call off at its foreground ceiling and the worktree is still
clean. What counts as a timeout is defined once, under "Headless ceiling" in
Step 4c.6 of the core doc; this section only builds the sentinel. A gate that
failed or dirtied the worktree is the previous section's `agent_block`
sentinel.

**Sentinel template — `gate_timeout` blocker (reuse path):**

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
    "forbidden_touched": <resolved forbidden_touched — see 'Resolve carried-through context' above>
  },
  "plan_source": "<resolved plan_source — see 'Resolve carried-through context' above>",
  "branch": "<branch-name>",
  "worktree_path": "<session worktree path>",
  "pr": null,
  "pr_info": {
    "number": <pr_number>,
    "url": "<pr_url>",
    "auto_merge": false,
    "base": "<base_ref>"
  },
  "review": {"must_fix_initial": <count>, "should_fix": <count>, "fix_cycles_used": <count>, "deferred": <count>},
  "health": {
    "lowest_agent_confidence": "HIGH",
    "any_incomplete_risk": false,
    "shortcuts": [],
    "recommendation": "PROCEED",
    "downgrade_applied": false,
    "fix_loop_escalated": false
  },
  "blocker": {
    "stage": "stage5_post_create",
    "reason": "gate_timeout",
    "details": "Step 4c.6 reuse-path quality gate timeout: gate <gate name exactly as detect-gates printed it> was cut off by the executor's foreground timeout (foreground ceiling <N>s, started <started>) at head <head_sha> on PR #<pr_number>; not backgrounded (headless); not a gate failure. Gates not run: <names or none>. Auto-merge was left as found (no gh pr merge was run). Last captured output: <tail>",
    "exception_type": null,
    "message": "reuse-path quality gate was cut off by the foreground timeout; not a gate failure; auto-merge left as found",
    "recovery_hint": "Orchestrator: confirm required CI is green at the PR head; in the row's worktree (worktree_path), at that head, run the timed-out gate by hand with no time limit; if it passes on a clean tree, run `prep_pr_state.py gate-pass record <gate>` there; then `cw dev-queue requeue <ticket> -c <client>`. The requeue keeps the row's worktree and its gitignored gate-pass stamp (.claude/prep-pr-state.json), and finalize skips a gate whose stamp matches the same head on a clean tree. The stamp holds only while the head is unchanged and the worktree is not removed (worktree GC keeps an open PR's worktree; it removes only a clean worktree whose PR merged or closed; no other removal path was verified); otherwise finalize will re-run the gate and may park again, and the orchestrator re-records the pass.",
    "retry_eligible": false,
    "retry_delay_seconds": null
  },
  "next_actions": []
}
```

**Field sources:** the same as the failure section above (`pr_info` from the
Pre-Stage Detector Guard and the read-only `baseRefName` call, `<head_sha>`
from the `HEAD_SHA` captured at the top of Step 4c.6, the rest from "Resolve
carried-through context"). The gate name is copied exactly as `detect-gates`
printed it, so `gate-pass record <gate>` takes it verbatim. Emit with
`printf '%s' "$JSON" | cw result emit -` and frame it per the Stage 4+5
Completion section, as above.

**Why not `agent_block`.** A gate the executor cut off did not fail: a fresh
IMPL session cannot make it faster, and on an adopted PR a regress strands the
row at `plan_missing`. `gate_timeout` routes to the generic park: the row parks
`BLOCKED_ON_USER` at FINALIZE, is never regressed to IMPL, charges no attempt,
and gets `pr_url` stamped from `pr_info`, so cw releases the row if the PR
merges on its own (auto-merge was left as found). `retry_eligible: false`: a
bare retry would time the same gate out again.

**Do not add `gate_timeout` to `FINALIZE_REGRESS_BLOCKER_REASONS`**
(`src/cw/auto_dev_result/schema/_vocab.py`, which stays `{"agent_block"}`).

**Producer note:** `gate_timeout` is an open-enum addition to `blocker.reason`
(headless-contract.md §4.2): no `schema_version` bump and no parser change.

---

## Step 4c.2: the `auto` permission-mode limitation, and the no-`/ship-it` block

<!-- el: finalize.permission-mode -->
**Permission mode (known limitation, #636 — deferred):** headless workers run under `claude --bg --permission-mode auto` (`native_daemon.py` `_DEFAULT_PERMISSION_MODE`), so the `auto` classifier fires on `gh pr create` inside a worktree-isolated subagent and, with no TTY to approve, blocks `/prep-pr`. The allowlist `Bash(gh pr:*)` does NOT suppress it, and setting `bypassPermissions` on *this subagent spawn alone* is ineffective — the worker's own `auto` mode is the source. The effective fix (spawning the worker with a non-`auto` `permission_mode`) is **deferred** to RFC 0005's FINALIZE/REVIEW stages (#622/#621); until then a classifier block surfaces as a BLOCK for the orchestrator to ship.

**If the agent returns BLOCK due to "no project `/ship-it`":** The project hasn't been set up for automated PR creation. AskUserQuestion: "Project has no ship-it in any layout `/prep-pr` probes (`.claude/commands/ship-it.md`, `.claude/skills/ship-it/SKILL.md`, `.agents/skills/ship-it/SKILL.md`). Set one up now via `/setup` and resume, skip this ticket (leave branch pushed), or abort pipeline?" (Headless: BLOCK for the orchestrator to set up ship-it via `/setup`.)
