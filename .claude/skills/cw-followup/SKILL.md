---
name: cw-followup
description: React to a completed /auto-dev session's sentinel result by performing the appropriate post-run action — close a no_op ticket with a citation, rebase + open PR for merge_gate_blocked, draft a Decisions section for ambiguities or premises pending verification, escalate a real blocker, or just confirm a shipped PR. Use when the user asks to follow up on a session, ship a blocked branch, disposition ambiguities or premises, close out an auto-dev run, or generally do "whatever the sentinel says I should do next" for a finished /auto-dev run.
---

# cw-followup

Performs the post-run action that matches a finished `/auto-dev` session's sentinel.

A headless `/auto-dev` run ends in one of several sentinel shapes. Each shape needs a different next move from a human:

| Sentinel shape | What this skill does |
|---|---|
| `shipped` | Print PR URL, confirm auto-merge state. |
| `no_op` | Close the ticket with a comment citing the satisfying PR. |
| `merge_gate_blocked` | Rebase the feature branch onto current `origin/main`, force-push, open PR. |
| `ambiguities_pending_resolution` / `premises_pending_verification` | Render a Decisions section, append to the ticket body, suggest re-dispatch. |
| `blocked` (real) | Surface `blocker.reason` + `details`; suggest re-dispatch or escalate. |
| `plan_pending_approval` / `review_pending_approval` | Print plan or review findings; offer to approve, fix-loop, or abandon. |
| `scope_exceeded` / `forbidden_area` | Surface and stop; ticket needs a human design decision. |
| `BlockedResult` (parser couldn't validate) | Diagnose — show the validation error, transcript tail, and the raw payload. |

Today the human does each of these by hand. This skill collapses the seven branches into one prompt.

## Inputs

Accepts a single argument identifying the session:

- a short cw session id (e.g. `4f44d145`)
- a ticket number (e.g. `185` or `#185`) — resolves to the most recent matching session
- a path to a Claude JSONL transcript

Optional flags from the user:
- `--dry-run` — describe the action; do not execute side effects.
- `--auto-accept-defaults` — for ambiguities / premises, take every plan default and append a Decisions section without confirmation.

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

Confirm with the user unless `--auto-accept-defaults` is set, then:

```bash
gh issue close <TICKET> --repo mattwwarren/claude-workspace \
  --comment "$DRAFT_COMMENT"
```

#### `merge_gate_blocked`

The branch was correctly built but `local main` diverged from `origin/main` before merge gate ran. Read `prior_pr_warnings` to see which PRs need to land first.

Default action (when prior PRs have since merged): rebase + force-push + open PR. Confirm with the user before force-push.

```bash
WORKTREE=$(jq -r '.session.worktree_path' <<<"$RESULT")
BRANCH=$(jq -r '.raw_payload.branch' <<<"$RESULT")
FORK_POINT=$(jq -r '.raw_payload.fork_point_sha' <<<"$RESULT")

git -C "$WORKTREE" fetch origin
git -C "$WORKTREE" checkout -B "$BRANCH" "origin/$BRANCH"
git -C "$WORKTREE" rebase --onto origin/main "$FORK_POINT" "$BRANCH"
git -C "$WORKTREE" push --force-with-lease origin "$BRANCH"
gh pr create --base main --head "$BRANCH"  # body derived from the review summary
```

#### `ambiguities_pending_resolution` / `premises_pending_verification`

Render a Decisions section and append it to the ticket body. Pipe the parser output through `render_decisions.py`:

```bash
echo "$RESULT" | uv run --project "$(git rev-parse --show-toplevel)" \
  python .claude/skills/cw-followup/scripts/render_decisions.py \
  --auto-accept-defaults  # only when the user opted in
```

Without `--auto-accept-defaults`, the script leaves each decision as a fill-in stub. Read each ambiguity / premise to the user, capture their answers inline, then substitute them into the stub before appending.

To append to the ticket body without losing the existing content:

```bash
TICKET=$(jq -r '.raw_payload.ticket_id' <<<"$RESULT")
gh issue view "$TICKET" --repo mattwwarren/claude-workspace --json body --jq .body > /tmp/body.md
cat /tmp/decisions.md >> /tmp/body.md
gh issue edit "$TICKET" --repo mattwwarren/claude-workspace --body-file /tmp/body.md
```

After append, suggest re-dispatch: `cw dev-queue add <TICKET>` (add `-c <CLIENT>` only for a multi-client setup); if the dispatch loop is idle, also run `cw dev-queue run --once` to kick it. Do not auto-dispatch — re-dispatch belongs to the user.

#### `blocked` (validated, real `AutoDevResult` with `status=blocked`)

Print the blocker fields verbatim:

```bash
jq '.result.blocker' <<<"$RESULT"
# stage, reason, details
```

When `blocker.reason == "tool_denied"` (issue #182): re-dispatch is the typical recovery, but the classifier non-determinism flagged in #183 means a delay before retry is sensible. Recommend `cw dev-queue add <TICKET>` (optionally with `-c <CLIENT>`) with a 2-3 minute pause for the auto-mode classifier to settle; if the dispatch loop is idle, run `cw dev-queue run --once` after adding.

When `blocker.reason` is anything else: read the Phase E retry fields the Blocker now carries (issue #174) — `retry_eligible`, `retry_delay_seconds`, and `recovery_hint`. When `retry_eligible` is true, recommend re-dispatch after `retry_delay_seconds` (surfacing `recovery_hint`); when it is false or absent, treat as human-escalation and surface verbatim.

#### `plan_pending_approval`

The plan stage finished with friction reports; the user gates whether to proceed. Print the friction highlights and the plan-stage review, then offer:
- approve → re-dispatch with `--proceed-from-plan` or equivalent;
- modify → edit the ticket body, re-dispatch from scratch;
- abandon → close the ticket.

#### `review_pending_approval`

Stage 3 review wants the user's eyes. Print `review.should_fix` and `review.must_fix_initial`, plus any health concerns. Offer:
- approve as-is and ship → invoke `/ship-it` in the worktree;
- spawn a fix loop → re-dispatch;
- abandon.

#### `scope_exceeded` / `forbidden_area`

Plan exceeded scope or touched a forbidden area. Surface the scope numbers and the forbidden-touched flag. The ticket needs a human design decision — do not act.

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
2. **Close the session** (`cw spawn close <short-id>`) and **sweep the queue**
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

- **Cannot resolve session** — print the session ref + sessions.json hint; do not guess. The user may have the wrong session id.
- **Transcript file missing** — likely the session record is stale (`reconcile` ran but the JSONL was rotated). Show the expected path so the user can confirm.
- **Sentinel parses but `effective_status` is not recognized** — surface verbatim; ask the user to confirm whether to treat it as a real blocker or as a new producer status that should be added to the parser.
- **Side effects fail** (gh issue close, force-push, rebase conflicts) — stop, surface the error, do not retry silently.

## Out of scope

- Re-dispatching to `/auto-dev`. The skill prepares the ground (decisions appended, branch rebased) but the user owns the dispatch trigger. Pair with `/cw-fanout` (#187) when re-dispatching at N>1.
- Creating new tickets. `/cw-followup` acts on the existing one only.
- Mutating the sentinel schema. Schema drift surfaces as `validation_failed`; fixing it is a separate ticket.

## Related

- #172 — `/cw-validate-result` (forensic read on any past session — uses the same parser).
- #171 — `/cw-smoke-test` (consumes followup as the post-dispatch step).
- #182 — `/auto-dev` no-recovery-on-deny (defines `tool_denied`).
- #183 — auto-mode classifier non-determinism (informs the retry-with-delay default).
- #184 — PushNotification on `tool_denied` (cw-side path; this skill is the human-side companion).
- #174 — Blocker field expansion (Phase E adds `retry_eligible` / `recovery_hint` — this skill will key off them when they land).
- #187 — `/cw-fanout` (re-dispatch at N>1 after followup prepares the ground).
