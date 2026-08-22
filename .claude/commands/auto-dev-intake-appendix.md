> Companion appendix to /auto-dev-intake. Loaded only on the trigger conditions named there — never read by default.

# auto-dev Stage 0 Intake — Appendix

Rare-branch procedures and design rationale extracted from
`.claude/commands/auto-dev-intake.md` (#1879). Each section below is reached
from a named trigger sentence in the core doc; nothing here is required on the
common path (healthy fetch, in-sync `main`, no pre-existing PR).

---

## Origin Sync Check divergence handling (Steps P2 and P3)

Reached from `## Pre-flight: Origin Sync Check` in the core doc, and only when
Step P1 found `LOCAL_MAIN != ORIGIN_MAIN`.

### Step P2 (interactive)

`AskUserQuestion`: how to resolve the divergence.

| Option | Action |
|---|---|
| Sync now | If ahead-only: `git -C "$REPO" push origin main`. If behind-only: `git -C "$REPO" pull --ff-only`. If both: fall through to "proceed anyway" — the human decides. |
| Proceed anyway | Continue to Stage 0 with local main as the fork point; the Stage 4 merge gate still catches the divergence. |
| Abandon ticket | Exit without spawning any agents. No sentinel emit. |

### Step P3 (headless)

If `AHEAD == 0` and `BEHIND > 0` (behind-only divergence, the shape
`fast_forward_main`'s own guards would allow), do not declare divergence
yet: the dispatch-tick auto-ff (`_resolve_freshness` in
`src/cw/dispatch/gating.py`) advances the base checkout's `main`
concurrently. Give it a bounded window before falling through.

#### Behind-only wait-and-recheck

```bash
for _ in 1 2 3; do
  sleep 30
  LOCAL_MAIN=$(git -C "$REPO" rev-parse main)
  if [ "$LOCAL_MAIN" = "$ORIGIN_MAIN" ]; then
    break
  fi
done
```

No re-fetch. Checkpoints land at T+30s, T+60s, T+90s. `ORIGIN_MAIN` was captured
in Step P1 and the shared `main` ref is what advances (via the base checkout,
not this worktree), so a local `rev-parse` observes it. The T+90s ceiling
matches the `TICK_STALE_SECONDS` convention (3x `tick_interval_seconds`) rather
than being an independently-chosen timeout; if that convention moves, this
window should move with it.

- If `LOCAL_MAIN == ORIGIN_MAIN` after any iteration → continue to Stage 0
  (divergence resolved itself; no sentinel emitted).
- If still diverged after 3 iterations (T+90s, matching
  `TICK_STALE_SECONDS`'s 3x-`tick_interval_seconds` convention) → fall
  through to the blocked sentinel below, unchanged.

If `AHEAD > 0` (ahead-only, or both ahead and behind) — no wait. Proceed
directly to the blocked sentinel below.

EXIT with the structured `blocked` sentinel before any agent is spawned:

```json
{
  "status": "blocked",
  "stage_reached": "stage1_pre_flight",
  "blocker": {
    "stage": "pre_flight",
    "reason": "local_main_diverged_from_origin",
    "details": "local_main=<sha>, origin_main=<sha>, ahead=<n>, behind=<n>",
    "message": "Local main is not in sync with origin/main; pipeline aborted before impl",
    "recovery_hint": "Push or rebase local main, then re-dispatch",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": ["sync_local_main"]
}
```

`retry_eligible: true` per ADR-0002 — the orchestrator MAY re-dispatch once the divergence is resolved (typically one `git push origin main` or `git pull --ff-only`). `retry_delay_seconds: null` because no time-based backoff helps; the gate clears when the human acts. `local_main_diverged_from_origin` is an open-enum addition to `blocker.reason` (headless-contract.md §4.2 — `reason` is open by design); consumers surface it verbatim, no parser change needed.

---

## Fetch-failure signature mirror: provenance, signature table, and sentinel

Reached from Step 3 of `## Stage 0: Ticket Intake` in the core doc, and only
when the primary ticket fetch exits non-zero or returns error text instead of
issue data. Match that error text against the signature table below before
doing anything else; do not add or remove a signature from memory.

- Auth-failure:
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

On a family match, EXIT before spawning any agent and **before** the
`stage.entered` (`s0_intake`) emission, with the structured `blocked` sentinel:

```json
{
  "status": "blocked",
  "stage_reached": "stage1_pre_flight",
  "blocker": {
    "stage": "pre_flight",
    "reason": "operator_unavailable",
    "details": "<matched signature + fetch op, e.g. 'gh issue view: Could not resolve host'>",
    "message": "Ticket fetch failed: operator/dependency currently unreachable",
    "recovery_hint": "Resolve the underlying network/auth/GitHub-availability issue, then re-dispatch",
    "retry_eligible": true,
    "retry_delay_seconds": null
  },
  "next_actions": ["manual_intervention"]
}
```

The signature list above is a PROSE MIRROR of
`src/cw/unavailability.py`'s `UNAVAILABILITY_SIGNATURES`; keep the two copies in
sync — `test_unavailability_signatures_mirrored_in_prose` is the drift guard.

`MCP-github-unreachable` is deliberately not mirrored — no verified signature
exists yet; see the `src/cw/unavailability.py` module docstring.

The EXIT must happen before spawning any agent and **before** the
`stage.entered` (`s0_intake`) emission: a fetch that never succeeded has no
stage-entry to correlate against.

`next_actions` must be `["manual_intervention"]`. The only other legal member of
`_PRE_FLIGHT_BLOCKED_NEXT_ACTIONS` (`auto_dev_result/schema.py`) is
`sync_local_main`, which is the Origin Sync surface and wrong here.
`reason: "operator_unavailable"` is already in
`OPERATOR_UNAVAILABLE_BLOCKER_REASONS`, so no schema change is required. A fetch
failure matching no signature is not handled by this block — fall through and
let the ordinary failure path report it.

---

## Open-PR self-check (#1862): rationale, reliability rule, and status choice

**Why the check exists.** A dispatch can succeed, push a branch, and open a PR
while its queue row is never advanced past PLAN/IMPL (the session died before
its sentinel landed, or the sentinel was never harvested). The next dispatch
then re-plans and re-implements a ticket whose work is already sitting in an
open, unmerged PR.

**Which prefix to probe.** Use the effective branch prefix (`--branch-prefix` if
given, else the client's `feature_branch_prefix`, default `dev`) — the same key
`cw` itself probes. A non-empty result means this ticket already has an open PR.

**Reliability rule — fail open.** Treat only a *reliable* answer as a hit: a
non-zero exit, a timeout, or a missing `gh` binary is **not** evidence of a PR.
Fall through and continue the run, exactly as `cw`'s own gate fails open. Do not
gate a refusal on an unreliable signal.

**The sentinel.** On a genuine hit, EXIT before spawning any agent with the
structured `stale_dispatch` sentinel:

```json
{
  "status": "stale_dispatch",
  "stage_reached": "stage1_pre_flight",
  "branch": null,
  "pr": null,
  "blocker": {
    "stage": "stage1_pre_flight",
    "reason": "pr_already_open",
    "details": "<PR number, URL, and review state, e.g. 'PR #1899 (https://github.com/o/r/pull/1899) is open, reviewDecision=REVIEW_REQUIRED'>",
    "message": "Ticket already has an open, unmerged PR from an earlier dispatch",
    "recovery_hint": "Land or close the PR, then unblock the ticket (cw dev-queue unblock)",
    "retry_eligible": false,
    "retry_delay_seconds": null
  },
  "next_actions": []
}
```

**Why these field values.** `pr` must stay null — this run did not create that
PR, and the schema rejects a non-null `pr` on this status. The discovered PR's
identity belongs in `blocker.details`; that string is the operator's whole
triage signal. `next_actions` must be empty, because the status is a
terminal-reject status. Do NOT report `no_op` (nothing is complete — the PR is
unmerged) or `blocked` (nothing is broken — the PR is healthy, just not this
session's to duplicate).

**Relationship to `cw`'s own gate.** This is not a substitute for `cw`'s
pre-dispatch gate, which catches the same condition before a session is even
spawned. This check covers the paths that gate does not — an interactive
`/auto-dev` run, and a resume that re-enters intake with the row already
claimed.

---

## Batch mode (interactive only)

Batch mode is undefined in headless mode; this procedure runs only from an
interactive `/auto-dev` invocation carrying filter flags.

- Call the tracker's batch-select op with the provided filters (`list_issues`
  for `linear`; `gh issue list --json number,title,labels,state [--label …]`
  for `github-issues`). Omitted filters default: `state` → `"Todo"`,
  `assignee` → `"me"`.
- For each issue, read the description to check for existing plan content and
  estimate a scope hint from its keywords.
- Present a numbered list (`N. <ID>: <title> [~<Small|Large>, has plan|no plan]`).
- **AskUserQuestion:** "Select tickets to process (e.g., '1,3' or 'all'), or 'abort':"
- Build an ordered queue from the selection. Order matters — tickets process in
  the order specified.

---

## Comments-fetch failure: the WARN event and what it cannot detect

Reached from Step 0d of the core doc, and only when the comments fetch exits
non-zero or returns malformed JSON. Emit an attention signal and continue with
`comments: []`:

```bash
cw event record session.needs_attention \
  --payload '{"reason": "comments_fetch_failed", "ticket_id": "<n>", "session_id": "<from .claude/cw-context.json>"}'
```

Do NOT emit `stage.errored` for this: `STAGE_ERRORED` events are ignored by `orchestrate.py`'s `_derive_last_stage_by_session`, so it would never surface as operator attention. `session.needs_attention` is the signal the operator sees.

**Known limitation (intentionally not WARNed):** a comments fetch that **succeeds but returns empty for a ticket that actually has comments** is NOT detectable from within this stage without a second independent source of the true comment count. The `comments_fetch_failed` WARN above covers only hard failures (non-zero exit / malformed JSON).

---

## `.cw/context.json` idempotency: why the guard keys on the writing session (#837)

`requeue` reuses the worktree (`create_worktree(..., allow_dirty_reuse=True)`),
so a stale `.cw/context.json` survives across runs. A `ticket_id`-only guard
skipped re-fetch on every requeue, and operator resolutions never reached the
plan stage. Keying the skip on the writing session re-fetches on requeue while
keeping the within-session fetch-once optimization.

`.cw/context.json` is distinct from `.claude/cw-context.json` (written by
`cw dispatch`; `session_id` and `ticket_id` only). Per-stage files read
`.cw/context.json` for full ticket orientation; the `CW_SESSION`/`TICKET`
bootstrap reads `.claude/cw-context.json`.
