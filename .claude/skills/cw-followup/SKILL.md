---
name: cw-followup
description: React to a completed /auto-dev session's sentinel result by performing the appropriate post-run action — close a no_op ticket with a citation, rebase + open PR for merge_gate_blocked, draft a Decisions section for ambiguities or premises pending verification, escalate a real blocker, or just confirm a shipped PR. Use when the user asks to follow up on a session, ship a blocked branch, disposition ambiguities or premises, close out an auto-dev run, or generally do "whatever the sentinel says I should do next" for a finished /auto-dev run.
---

# cw-followup

Performs the post-run action that matches a finished `/auto-dev` session's sentinel.

A headless `/auto-dev` run ends in one of several sentinel shapes. Each shape needs a different next move, which the orchestrator (the session running this skill) performs itself:

| Sentinel shape | What this skill does |
|---|---|
| `shipped` | Print PR URL, confirm auto-merge state. |
| `no_op` | Close the ticket with a comment citing the satisfying PR. |
| `merge_gate_blocked` | Rebase the feature branch onto current `origin/main`, force-push, open PR. |
| `ambiguities_pending_resolution` / `premises_pending_verification` | Answer from the ticket's sources of truth, render a Decisions section, append to the ticket body, re-dispatch. |
| `blocked` (real) | Read `blocker.reason` + `details`; triage per `recovery_hint` (re-dispatch, fix, close), escalating only a product/scope fork. |
| `plan_pending_approval` / `review_pending_approval` | Usually released by a gate recipe. If still parked, approve when within the ticket's agreed scope (never a human-kept gate: `AWAITING_OPERATOR_SIGNOFF`, `finalize_gate_held`, `scope_hint: large` — notify the human); escalate only scope growth that is a genuine product/scope fork. |
| `scope_exceeded` / `forbidden_area` | Adjudicate against the ticket's sources of truth; escalate only a genuine product/scope fork. |
| `BlockedResult` (parser couldn't validate) | Diagnose — show the validation error, transcript tail, and the raw payload. |

This skill collapses the seven branches into one prompt; the orchestrator runs each action, and only genuine product/scope forks go to the human (ADR-0020).

**Human-kept gates are the exception to every "approve it yourself" step below.** A row with status `AWAITING_OPERATOR_SIGNOFF` (`signoff: operator`), `finalize_gate_held` (a finalize force-hold: `hold_finalize` / `finalize_gate: manual`), or `scope_hint: large` is a gate the human opted into. Do not approve, release, or requeue it; notify the human instead. A large-by-size gate *without* `scope_hint: large` is not one of these; adjudicate it.

## Inputs

Accepts a single argument identifying the session:

- a short cw session id (e.g. `4f44d145`)
- a ticket number (e.g. `185` or `#185`) — resolves to the most recent matching session
- a path to a Claude JSONL transcript

Optional flags from the user:
- `--dry-run` — describe the action; do not execute side effects.
- `--auto-accept-defaults` — for ambiguities / premises, take every plan default and append a Decisions section without confirmation. Use it only when every ambiguity's default is backed by the ticket's sources of truth (ticket body, approved plan-of-record, pre-flight resolutions, recorded decisions, linked RFCs and dependency tickets), and cite the source for each; otherwise answer each one yourself or batch the genuine forks to the human.

## How it works

### Step 1 — locate and parse the sentinel

Run the bundled parser to resolve the session and pull the parsed sentinel plus the raw payload:

```bash
uv run --project "$(git rev-parse --show-toplevel)" python \
  .claude/skills/cw-followup/scripts/parse_sentinel.py \
  --ticket-id <NUMBER>
# or --session-id <SHORT> or --transcript-path <PATH>
```

The script emits one JSON object on stdout with `result_kind`, `result`, `raw_payload`, `session`, `transcript_path`, and `sentinel_found`. Capture it to a variable; the rest of the skill reads from this object.

If `sentinel_found` is false, stop and surface the transcript path + the session record. Treat this as a no-result-emitted failure (see `references/no-sentinel-patterns.md` for likely causes).

### Step 2 — branch on the effective status

As of schema v4 (issue #191), `premises_pending_verification` and `ambiguities_pending_resolution` are canonical `Status` members — they now parse as a normal `AutoDevResult`, not a `BlockedResult`. Only a status the parser has *never* heard of still routes through `BlockedResult` with `reason=status_unknown`. The skill prefers the **raw payload**'s `status` (it survives even that unknown-status case) and falls back to the validated `result.status`.

```text
effective_status =
    raw_payload.status   if raw_payload is present
  else result.status     if result_kind == "AutoDevResult"
  else result.blocker.reason
```

The branch table below uses `effective_status`.

### Step 3 — branch dispatch

#### `shipped`

The PR auto-merges via `gh pr merge --auto`. Verify it landed:

```bash
PR_URL=$(jq -r '.result.pr.url // .raw_payload.pr.url' <<<"$RESULT")
gh pr view "$PR_URL" --json mergeStateStatus,state,number,title
```

Print the merge state. If still open, suggest the review-monitor will handle it. If closed, recommend `cw orchestrate retire`.

#### `no_op`

The ticket was satisfied by a prior PR. Pull the citation from `friction_highlights`:

```bash
jq -r '.raw_payload.friction_highlights[]' <<<"$RESULT"
# look for the satisfying-PR pointer; spec uses a "satisfying_pr: <url>" highlight
```

Draft a close comment in the form:

```text
Closed as no_op by /auto-dev (session <ID>) — already satisfied by <PR URL>.
```

Close it with that citation (no confirmation needed once the satisfying PR is verified merged and the ticket's acceptance criteria are verified satisfied by it; a ticket closed as duplicate/obsolete instead cites the superseding ticket):

```bash
gh issue close <TICKET> --repo mattwwarren/claude-workspace \
  --comment "$DRAFT_COMMENT"
```

#### `merge_gate_blocked`

The branch was correctly built but `local main` diverged from `origin/main` before merge gate ran. Read `prior_pr_warnings` to see which PRs need to land first.

Default action (when prior PRs have since merged): rebase + force-push + open PR. Force-push applies only to a cw-owned branch (one the pipeline created for the ticket), only run by the orchestrator (never a headless worker), always with `--force-with-lease`, and only after the evidence holds: prior PRs merged, rebase onto `origin/main` clean, `fork_point_sha` verified, and `git -C "$WORKTREE" log "origin/$BRANCH" ^HEAD` showing no remote-only commits (run it after the rebase and before the push; the only entries allowed are the pre-rebase copies of your own commits, so confirm with `git -C "$WORKTREE" cherry -v HEAD "origin/$BRANCH"` that no `+` line carries work absent from the rebased branch). No confirmation step once that holds. A shared branch's history rewrite stays with the human. (This is recovery after a park; a worker's own `--force-with-lease` push of the branch it is working on after its own rebase — `/auto-dev-finalize`'s rebase retry, `/review-monitor` — is pipeline behavior and unchanged, ADR-0020 invariant 3.)

```bash
WORKTREE=$(jq -r '.session.worktree_path' <<<"$RESULT")
BRANCH=$(jq -r '.raw_payload.branch' <<<"$RESULT")
FORK_POINT=$(jq -r '.raw_payload.fork_point_sha' <<<"$RESULT")

git -C "$WORKTREE" fetch origin
git -C "$WORKTREE" checkout -B "$BRANCH" "origin/$BRANCH"
git -C "$WORKTREE" rebase --onto origin/main "$FORK_POINT" "$BRANCH"
git -C "$WORKTREE" push --force-with-lease origin "$BRANCH"
```

Before opening the PR, replicate `ship-it.md` Step 3a's gate check (this
recovery path has no `$TITLE` of its own, so derive it from the branch tip):

```bash
CHANGED_FILES=$(git -C "$WORKTREE" diff --name-only origin/main...HEAD)
TITLE=$(git -C "$WORKTREE" log -1 --format=%s "$BRANCH")
GATE_FIRES=false
if printf '%s' "$TITLE" | grep -qE '^(feat|fix)\('; then
  if printf '%s\n' "$CHANGED_FILES" | grep -q '^src/'; then
    GATE_FIRES=true
  fi
fi
EXTRA_LABEL_ARGS=""
```

If `$GATE_FIRES` is `true` and `CHANGED_FILES` doesn't already include
`CHANGELOG.md`, apply the same decision tree as `.claude/commands/ship-it.md`
Step 3a before opening the PR — add a real `[Unreleased]` entry by default,
or, only as a deliberate escape hatch, `gh label create no-changelog ... ||
true` and set `EXTRA_LABEL_ARGS="--label no-changelog"`. Do not duplicate or
re-derive the decision tree here; cross-reference `ship-it.md` Step 3a so the
two copies can't drift.

```bash
gh pr create --base main --head "$BRANCH" --title "$TITLE" ${EXTRA_LABEL_ARGS}  # body derived from the review summary
```

#### `ambiguities_pending_resolution` / `premises_pending_verification`

Render a Decisions section and append it to the ticket body. Pipe the parser output through `render_decisions.py`:

```bash
echo "$RESULT" | uv run --project "$(git rev-parse --show-toplevel)" \
  python .claude/skills/cw-followup/scripts/render_decisions.py \
  --auto-accept-defaults  # only when every ambiguity's default is backed by the ticket's sources of truth (cite them)
```

Without `--auto-accept-defaults`, the script leaves each decision as a fill-in stub. Answer each ambiguity / premise yourself from the ticket's sources of truth (ticket body, approved plan-of-record, pre-flight resolutions, recorded decisions, linked RFCs and dependency tickets; verify premises against the code), then substitute the answers into the stub before appending. Only a genuine product/scope fork (a question those sources cannot answer) goes to the human, batched into one `AskUserQuestion` with a recommendation for each.

To append to the ticket body without losing the existing content:

```bash
TICKET=$(jq -r '.raw_payload.ticket_id' <<<"$RESULT")
gh issue view "$TICKET" --repo mattwwarren/claude-workspace --json body --jq .body
```

Use the **Write tool** to author the concatenated result (the existing body above, plus the decisions section) to a scratch file — see CLAUDE.md's **Agent File Operations** rule.

```bash
gh issue edit "$TICKET" --repo mattwwarren/claude-workspace --body-file /tmp/body.md
```

After append, re-dispatch: `cw dev-queue add <TICKET>` (add `-c <CLIENT>` only for a multi-client setup); if the dispatch loop is idle, also run `cw dev-queue run --once` to kick it. The orchestrator re-dispatches itself (skip only under `--dry-run`).

#### `blocked` (validated, real `AutoDevResult` with `status=blocked`)

Print the blocker fields verbatim:

```bash
jq '.result.blocker' <<<"$RESULT"
# stage, reason, details
```

When `blocker.reason == "tool_denied"` (issue #182): re-dispatch is the typical recovery, but the classifier non-determinism flagged in #183 means a delay before retry is sensible. Run `cw dev-queue add <TICKET>` (optionally with `-c <CLIENT>`) after a 2-3 minute pause for the auto-mode classifier to settle; if the dispatch loop is idle, run `cw dev-queue run --once` after adding.

When `blocker.reason == "codex_must_fix_findings"` (issue #2210): `blocker.details` is the rendered review comment, and it ends in a `### Settle a finding` section carrying one fenced `json` payload per blocking finding (capped at 10; any remainder is listed compactly below them). Those payloads exist so a decision can be recorded permanently instead of the same finding re-parking the next round.

**Adjudicate each finding yourself against the ticket's sources of truth; do not hand the list to a human.** The plan was set before the ticket entered the pipeline. The ticket body, the approved plan-of-record, any `<!-- auto-dev-preflight-resolutions -->` comment, recorded operator decisions on the ticket, and linked RFCs and dependency tickets are the record of what was agreed. Read them, then put every finding in exactly one bucket:

| Bucket | When | Action |
|---|---|---|
| **Out of scope / already decided** | The finding asks for work the plan or ticket explicitly excludes, or contradicts a recorded operator decision (cite which). | Settle it `REJECTED`, with `--reason` quoting the source: "Plan §Scope excludes X (issue comment <url>)". |
| **Not reproducible** | The finding is wrong against the code. Verify it yourself, citing `file:line`. | Settle it `REJECTED`, with the evidence as the reason. |
| **Real, in-scope defect** | The finding is correct and fixing it stays inside the agreed scope. | Do **not** settle it. Requeue the ticket into the fix loop (`cw dev-queue requeue "$TICKET" -c <CLIENT> --stage impl --regress`; the park sits at REVIEW, and a backward move needs `--regress`). Nothing to ask anyone. |
| **Product or scope question** | Fixing it needs behavior the sources of truth do not decide, or grows scope beyond the plan (new files, features or contracts the ticket never asked for). | Escalate to the human: batch every such finding into one `AskUserQuestion`, each with a recommendation. This is the only bucket that reaches them. |

Never settle a finding you cannot tie to a specific source or to code evidence. That one is a product or scope question, not a rejection.

To settle: save the payload with the **Write tool** to a scratch file, run `cw review settle <file> --reason "<the cited source or evidence>" --ticket "$TICKET" --out <marker.md>`, and post it with `gh issue comment "$TICKET" --repo <REPO> --body-file <marker.md>` **as its own comment, unedited**. The reader honours a disposition block only when it opens the comment body under its `## Review Finding Dispositions` title. A block pasted under a preamble is not a record and is not applied.

`--reason` is mandatory and must be non-blank: the citation, never a placeholder. The command records your resolved `gh` login as the settling actor and emits a `review.finding_settled` audit event per finding, so the record says who silenced the finding, when, why, and against which reviewed sha. A settle can be withdrawn with `outcome: "REVERSED"` (see `cw review settle --help`).

**Where it runs.** The orchestrator runs it, from wherever it already is (main checkout, interactive cw session worktree, or any directory outside a headless worker). It refuses only inside a headless dispatch worker (`.claude/cw-context.json` with `headless: true`, or `$TMPDIR` inside such a worktree), because a worker settling its own reviewer's findings is the pipeline adjudicating itself. A worker hands the payload to the orchestrator, which runs it. Do not write a context file, and do not hand-author the `REVIEW-FINDING-DISPOSITIONS` marker. A worker never runs this command.

When every finding is settled or fixed and the branch should ship as-is (issue #2205), the path is `cw dev-queue approve "$TICKET" -c <CLIENT> --override-must-fix --reason "<the same citations>"` followed by `cw dev-queue requeue "$TICKET" -c <CLIENT> --stage finalize`. A bare `requeue --stage finalize` is not enough: FINALIZE's MUST_FIX Override Verification step re-reads `.claude/review-verdict.json` and parks the row again. The override is bound to the verdict's reviewed SHA and exact MUST_FIX finding set, so a new review round or a new commit voids it. Use it only when every remaining MUST_FIX finding is in one of the two settle buckets above. The reason is recorded on the row and in the audit event, and it is rendered into the PR body's `## Operator override` section.

When `blocker.reason` is anything else: read the Phase E retry fields the Blocker now carries (issue #174) — `retry_eligible`, `retry_delay_seconds`, and `recovery_hint`. When `retry_eligible` is true, re-dispatch after `retry_delay_seconds` (honouring `recovery_hint`); when it is false or absent, triage per `recovery_hint` yourself (requeue, regress, fix, file the follow-up, or close — a close only when the ticket's acceptance criteria are verified satisfied, citing the merged PR/commit, or it is verified duplicate/obsolete, citing the superseding ticket). Escalate to the human only a genuine product/scope fork, surfacing the blocker verbatim.

#### `plan_pending_approval`

A Large plan (more than 10 files or 500 lines) parks here. Size alone is not a reason to involve a human. The `auto_adopt_clean_plan` gate recipe (on by default) releases the park on the next reconcile tick unless the plan touches a forbidden area, `scope_hint: large` is set, or the draft is unbound or was already approved once. If it is still parked and the row carries `scope_hint: large` (or is `AWAITING_OPERATOR_SIGNOFF` / `finalize_gate_held`), that is an opted-in gate (`signoff: operator` style): do not approve it; notify the human with the plan. Otherwise decide it yourself against the ticket's sources of truth:

- **Plan stays within the ticket's agreed scope** (every file and behavior traces to the ticket, its pre-flight resolutions, or a recorded decision): approve it with `cw dev-queue approve "$TICKET" -c <CLIENT>`. Approval is the row path: it binds the draft's fingerprint, and the plan stage accepts it on re-dispatch. A prose comment such as "approved" is not evidence and the ticket would only re-park.
- **Plan grows scope or touches a forbidden area**: adjudicate against the sources of truth first (trim, split into a follow-up ticket, or approve). Only if the growth is a genuine product/scope fork, put it to the human in one batched `AskUserQuestion` with your recommendation.
- **Abandon**: when the evidence shows the ticket is a duplicate or obsolete (cite it), abandon it yourself; otherwise abandoning is a product fork for the human.

#### `review_pending_approval`

A Large review parks here after its fix loop. The `auto_approve_clean_review` gate recipe (on by default) releases it when health is PROCEED, no forbidden area was touched, and a reviewer ran, unless the row carries `scope_hint: large`. If it is still parked, read `health` and `review.*`. A row carrying `scope_hint: large` (or `AWAITING_OPERATOR_SIGNOFF` / `finalize_gate_held`) is an opted-in gate: do not approve it; notify the human with a recommendation. A degraded health or a forbidden-area touch you adjudicate yourself against the ticket's sources of truth, escalating only a genuine product/scope fork. Otherwise approve with `cw dev-queue approve "$TICKET" -c <CLIENT>`, or requeue the fix loop for an in-scope defect.

#### `scope_exceeded` / `forbidden_area`

Plan exceeded scope or touched a forbidden area. Surface the scope numbers and the forbidden-touched flag, then adjudicate against the ticket's sources of truth: trim, split into a follow-up ticket, or approve the growth with the citation. Escalate to the human only a genuine product/scope fork (batched, with a recommendation).

#### `BlockedResult` with `reason != status_unknown`

The parser blocked because the payload itself was malformed. Show the validation error verbatim and link to the transcript. Likely causes:
- `validation_failed` — producer/consumer schema drift (e.g. `plan_source: github_issue_existing` not yet in the enum). File a ticket against the parser.
- `multiple_result_blocks` — the producer emitted more than one sentinel; the first one wins by spec but the run is suspect.
- `no_result_emitted` — see `references/no-sentinel-patterns.md`.

### Step 4 — report

Print one summary line followed by any receipts (URLs, branch refs, ticket numbers). Keep it tight — caveman style:

```
followup: #185 → premises rendered, ticket body updated, ready for re-dispatch
followup: #136 → no_op, closed citing PR #154
followup: #170 → merge_gate_blocked, rebased to origin/main, PR #194 opened
```

## Salvage-ship (wedged or dead session, work complete)

The recurring case the sentinel statuses don't cover (#578): the worker
pushed its branch and emitted a clean sentinel (or finished gates), but the
session wedged before/at turn-end — task left RUNNING, or watchdog-reverted
to PENDING, while the work is done. Symptoms: transcript silent >20 min with
the sentinel (or "gates green") as the last event, session still `working`
in the daemon roster, no PR.

Recipe (validated 4× in the 1.1 waves — #387, #552, #554, #558):

1. **Verify the work before touching anything**: `git ls-remote origin | grep <ticket>`
   for the pushed branch; `git log origin/main..origin/<branch> --oneline` for the
   commit stack; read the sentinel from the transcript for the review verdict +
   open SHOULD_FIX list.
2. **Close the session**: if it is verified dead (absent from `~/.claude/daemon/roster.json`, transcript flat, no live process — "work is done" is not death evidence), `cw spawn close --confirmed-dead <short-id>` (flag before the id). A wedged session still `working` in the roster is live: the bare `cw spawn close <short-id>` is classifier-gated and needs the human to have allowlisted it (permission settings are the human's); if it is not allowlisted, surface the recommendation instead. Then **sweep the queue**
   (`cw dev-queue remove <ticket> -c <client> --all` — the task is stale however
   it was routed; the PR record becomes the source of truth).
3. **Disposition the sentinel** as if it had routed normally:
   `review_pending_approval` with SHOULD_FIX-only → assess the items; ship as-is
   (note them in the PR body as deferred follow-ups) or apply 1–4 surgical fixes
   inline in the worker's worktree (`~/.cw/wt/<hash>/auto-dev-<n>`),
   re-run the full gate suite, commit, push to the same branch.
4. **Open the PR yourself** from the sentinel's branch with auto-merge; the body
   carries the sentinel's review summary + the salvage note. Never re-dispatch a
   ticket whose work is already pushed — a fresh worker redoes the hour.

## Failure modes

- **Cannot resolve session** — print the session ref + sessions.json hint; do not guess. The session id may be wrong.
- **Transcript file missing** — likely the session record is stale (`reconcile` ran but the JSONL was rotated). Show the expected path.
- **Sentinel parses but `effective_status` is not recognized** — surface verbatim; decide whether it is a real blocker or a new producer status that should be added to the parser (file the parser ticket if so).
- **Side effects fail** (gh issue close, force-push, rebase conflicts) — stop, surface the error, do not retry silently.

## Out of scope

- Re-dispatch at N>1. For a single ticket the skill prepares the ground (decisions appended, branch rebased) and the orchestrator re-dispatches it; see `/cw-fanout` (#187) for re-dispatching at N>1.
- Creating new tickets. `/cw-followup` acts on the existing one only.
- Mutating the sentinel schema. Schema drift surfaces as `validation_failed`; fixing it is a separate ticket.

## Related

- #172 — `/cw-validate-result` (forensic read on any past session — uses the same parser).
- #171 — `/cw-smoke-test` (consumes followup as the post-dispatch step).
- #182 — `/auto-dev` no-recovery-on-deny (defines `tool_denied`).
- #183 — auto-mode classifier non-determinism (informs the retry-with-delay default).
- #184 — PushNotification on `tool_denied` (cw-side path; this skill is the orchestrator-side companion).
- #174 — Blocker field expansion (Phase E adds `retry_eligible` / `recovery_hint` — this skill will key off them when they land).
- #187 — `/cw-fanout` (re-dispatch at N>1 after followup prepares the ground).
