---
description: "auto-dev Stage 0: Ticket Intake — tracker resolution, pre-flight sync check, ticket fetch, context materialization"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write"]
---

# auto-dev Stage 0: Ticket Intake

This is Stage 0 of the auto-dev pipeline. It runs first in every pipeline invocation, both interactive and headless. It resolves the active tracker, runs the pre-flight origin sync check, fetches the ticket, and materializes `.cw/context.json` for downstream per-stage files.

**Arguments:** "$ARGUMENTS"

---

## Tracker Resolution (read first — before Stage 0)

This pipeline is **tracker-agnostic**. The document below is written in Linear
terms for historical reasons; resolve the active tracker once at the top of
every invocation and substitute its operations everywhere.

**Resolve the tracker:** read `.claude/project-config.yaml` →
`tracking.primary.system`. Recognized values: `github-issues` or `linear`.
If the file is absent or the key is missing, default to `linear` (legacy
behavior). Repos that track work in GitHub Issues MUST set `github-issues`
(see the companion `project-config.yaml`).

**Operation mapping** — wherever this document says "Linear", `get_issue`,
`list_comments`, "post to the Linear ticket", or "Linear comment/marker",
perform the active tracker's equivalent:

| Operation | `linear` | `github-issues` |
|-----------|----------|-----------------|
| Fetch ticket body | `get_issue(<id>)` | `gh issue view <n> --json title,body,state,url,comments` |
| Fetch ticket comments | `list_comments(<id>)` | `gh issue view <n> --json comments` |
| Post a comment | Linear create-comment | `gh issue comment <n> --body <text>` (or `--body-file`) |
| Read plan/marker comments | scan Linear comments | scan `gh issue view <n> --json comments` (markers are HTML comments in the body, identical syntax) |
| Close on ship | Linear status → Done | `gh issue close <n>` — or rely on the PR's `Closes #<n>` trailer |
| Batch select (`--cycle`/`--label`/…) | `list_issues(filters)` | `gh issue list --json number,title,labels,state [--label …]` |

**`github-issues` mode — hard rules:**
- A bare integer argument (e.g. `403`) is a **GitHub issue number**. It is NOT
  free text and NOT a Linear ID. Resolve it with `gh issue view <n>`.
- **NEVER call the Linear MCP** (`get_issue` / `list_issues` /
  `mcp__plugin_linear_*` / Linear authenticate). If you are about to authorize
  Linear, you are in the wrong tracker mode — stop and use `gh`. In a headless
  run the Linear OAuth prompt cannot be answered and the session will silently
  stall until it is reaped (see the 2026-05-30 fanout-cascade RCA).
- All downstream "post to Linear" / "post the plan/ambiguities/premises to the
  ticket" instructions operate on the GitHub issue via `gh issue comment <n>`.
  Plan signoff markers (`<!-- plan-quality-reviewed -->` etc.) are HTML comments
  embedded in the issue comment body, exactly as in the Linear flow.
- `gh` runs against the repo at the worktree's `origin` remote — confirm with
  `gh repo view --json nameWithOwner` if ambiguous.

Everything below this section is unchanged in meaning; only the tracker
operations are substituted per the table above.

---

## Pre-flight: Origin Sync Check

Runs once per `/auto-dev` invocation, before Stage 0. Fails fast when the local `main` branch is not in sync with `origin/main`, so the pipeline does not spend impl + review tokens on a feature branch that will be rejected at the Stage 4 merge gate.

**Why this exists:** the #170 v2 dogfood completed all the way through impl + 5 serial reviewers in 25 minutes, then correctly exited at Stage 4 with `status: merge_gate_blocked` — because the feature branch carried two unrelated commits from local `main` that had not yet been pushed to `origin/main`. Catching the divergence here saves the 25 minutes of work.

### Step P1: Fetch and compare

```bash
REPO=$(pwd)
git -C "$REPO" fetch origin main --quiet
LOCAL_MAIN=$(git -C "$REPO" rev-parse main)
ORIGIN_MAIN=$(git -C "$REPO" rev-parse origin/main)
AHEAD=$(git -C "$REPO" rev-list --count origin/main..main)
BEHIND=$(git -C "$REPO" rev-list --count main..origin/main)
```

If `LOCAL_MAIN == ORIGIN_MAIN`, continue to Stage 0.

If they differ, branch on mode:

### Step P2 (interactive)

`AskUserQuestion`: how to resolve the divergence.

| Option | Action |
|---|---|
| Sync now | If ahead-only: `git -C "$REPO" push origin main`. If behind-only: `git -C "$REPO" pull --ff-only`. If both ahead and behind: fall through to "proceed anyway" — the human must decide. |
| Proceed anyway | Continue to Stage 0 with the local main as the fork point. The feature branch will diverge from origin; the Stage 4 merge gate will still catch it. |
| Abandon ticket | Exit without spawning any agents. No sentinel emit. |

### Step P3 (headless)

If `AHEAD == 0` and `BEHIND > 0` (behind-only divergence — the shape
`fast_forward_main`'s own guards would allow), do not declare divergence
yet. R1's dispatch-tick auto-ff (`_resolve_freshness` in
`src/cw/dispatch/gating.py`) advances the client's base checkout's `main`
independently and concurrently; give it a bounded window to land before
falling through to the blocked sentinel below.

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

No re-fetch: `ORIGIN_MAIN` was already captured from `origin/main` in Step
P1, and the shared `main` ref is what advances (via the base checkout, not
this worktree) — a plain local `rev-parse` is sufficient to observe it.
Checkpoints land at T+30s, T+60s, and T+90s.

- If `LOCAL_MAIN == ORIGIN_MAIN` after any iteration → continue to Stage 0
  (the divergence resolved itself; no sentinel emitted).
- If still diverged after 3 iterations (T+90s, matching
  `TICK_STALE_SECONDS`'s 3x-`tick_interval_seconds` staleness convention) →
  fall through to the blocked sentinel below, unchanged.

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

`retry_eligible: true` per ADR-0002 — the orchestrator MAY re-dispatch once the user resolves the divergence (typical resolution is a single `git push origin main` or `git pull --ff-only`). `retry_delay_seconds: null` because no time-based backoff helps; the gate clears when the human acts.

**Producer note:** `local_main_diverged_from_origin` is an open-enum addition to `blocker.reason` (per headless-contract.md §4.2 — `reason` is open by design). Consumers surface it verbatim; no parser change needed.

---

## Stage 0: Ticket Intake

First resolve the active tracker per **Tracker Resolution** above. The parse
rules below are tracker-aware.

1. Parse `$ARGUMENTS`:
   - **Ticket ID** → single-ticket mode, skip selection. What counts as an ID
     depends on the tracker:
     - `linear`: a Linear issue ID like `PROJ-1234`.
     - `github-issues`: a **bare integer** like `403` (a GitHub issue number) —
       or `#403`. Do NOT treat this as free text; do NOT query Linear for it.
   - **Filter flags** (`--cycle`, `--project`, `--label`, `--team`, `--state`, `--assignee`, `--priority`) → batch mode
   - **Free text** (no ID pattern, no flags) → use as description directly, no tracker lookup, no existing plan
   - **Mode flag** `--headless` → suppress all AskUserQuestion calls; apply gate-collapse table from the Headless Mode section for all downstream decision points. Independent of the input forms above (can combine with Linear ID, filters, or free text — though batch mode behavior in headless is undefined per the Headless Mode out-of-scope notes).

2. **Parse constraint flags** (used by `/auto-debt` alias):
   - `--scope-limit small` → reject tickets classified as Large
   - `--branch-prefix <prefix>` → override default `dev` branch prefix
   - `--forbidden <comma-separated areas>` → hard-reject tickets touching these areas

3. **Single-ticket mode:** Fetch the issue via the **active tracker's** fetch op
   (`get_issue(<id>)` for `linear`; `gh issue view <n> --json title,body,state,url,comments`
   for `github-issues` — the single call carries the comments too). For `linear`,
   `list_comments(<id>)` is a **mandatory op that MUST run before Step 0d** (see
   Step 0d) so the comments are in hand before context is materialized. Proceed to
   Stage 1.

   **Step 3 fetch-failure handling (fatal, #1156 — RFC 0011 A2):** if the primary
   fetch above (whichever op the active tracker resolved to) exits non-zero or
   returns error text instead of issue data, match that error text against the
   signature table below before doing anything else. This list is a PROSE MIRROR
   of `src/cw/unavailability.py`'s `UNAVAILABILITY_SIGNATURES` (mirror-comment
   pattern: `cw.dev_queue.lifecycle._PLAN_SPEC_MARKER` mirroring
   `gh._PLAN_MARKER`); keep the two copies in sync, see
   `test_unavailability_signatures_mirrored_in_prose` for the drift guard:

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

   (`MCP-github-unreachable` is deliberately not mirrored here — no verified
   signature exists yet, see the module docstring in `src/cw/unavailability.py`.)

   On a family match, EXIT before spawning any agent and **before** the
   `stage.entered` (`s0_intake`) emission below — a fetch that never succeeded
   has no meaningful stage-entry to correlate a `s0_intake` event against
   (same precedent as the Step P3 Origin Sync block above) — with the
   structured `blocked` sentinel:

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

   `next_actions` **must** be `["manual_intervention"]` — `sync_local_main` is
   the only other legal member of `_PRE_FLIGHT_BLOCKED_NEXT_ACTIONS`
   (`auto_dev_result/schema.py`) and is semantically wrong here (that's the Origin
   Sync surface). `reason: "operator_unavailable"` is already a member of
   `OPERATOR_UNAVAILABLE_BLOCKER_REASONS` (`auto_dev_result/schema.py`) — no schema
   change required.

   A fetch failure that matches no signature (unrecognized error text) is not
   handled by this block — it falls through to whatever undefined behavior
   already existed for an unmatched fetch failure; this ticket only closes the
   classified-failure gap, per R5 (no interactive branch is added here).

   **Headless only — initialize correlation context and emit `stage.entered` (`s0_intake`):**
   ```bash
   CW_CTX=".claude/cw-context.json"
   if [[ ! -f "$CW_CTX" ]]; then
     echo "FATAL: $CW_CTX not found — headless invariant violated; stage events will not correlate." >&2
     exit 1
   fi
   CW_SESSION=$(jq -r '.session_id' "$CW_CTX")
   TICKET=$(jq -r '.ticket_id' "$CW_CTX")
   cw event record stage.entered \
     --correlation-id "$TICKET" \
     --payload "{\"session_id\":\"$CW_SESSION\",\"ticket_id\":\"$TICKET\",\"stage\":\"s0_intake\",\"started_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" || true
   ```
   `$CW_SESSION` and `$TICKET` are used by all subsequent stage event emissions. Source is `cw-context.json` (written by `cw` dispatch before spawning) — `CW_SESSION_ID` env var does not propagate through `claude --bg` (RFC 0001 §Row 10 gap).

4. **Batch mode:**
   - Call the tracker's batch-select op with the provided filters (`list_issues`
     for `linear`; `gh issue list --json number,title,labels,state [--label …]`
     for `github-issues`). Apply defaults for omitted filters:
     - `state` defaults to `"Todo"` if not provided
     - `assignee` defaults to `"me"` if not provided
   - For each issue in the result:
     - Read description to check for existing plan content
     - Estimate scope hint from description keywords
   - Present a numbered list:
     ```
     Found N tickets matching filters:
      1. PROJ-123: Fix login timeout [~Small, has plan]
      2. PROJ-456: Refactor user service [~Large, no plan]
      3. PROJ-789: Add retry logic [~Small, no plan]
     ```
   - **AskUserQuestion:** "Select tickets to process (e.g., '1,3' or 'all'), or 'abort':"
   - Build ordered queue from selection. Order matters — tickets process in the order specified.

### Step 0d: Materialize `.cw/context.json`

After ticket fetch (single-ticket mode), write the orientation context to `.cw/context.json` for downstream per-stage files:

```bash
mkdir -p .cw
cat > .cw/context.json << 'CWCTXEOF'
{
  "ticket_id": "<TICKET>",
  "ticket_title": "<title from ticket fetch>",
  "ticket_body": "<body from ticket fetch>",
  "comments": ["<comment 1>", "<comment 2>", "..."],
  "scope_hint": null,
  "prior_decisions": [],
  "materialized_by_session": "<session_id from .claude/cw-context.json>"
}
CWCTXEOF
```

**Populating `comments` (from a real fetch, never model initiative):** the `comments` array MUST be filled from an actual tracker fetch — never guessed, never left `[]` by default:
- **`github-issues` mode:** use the `comments` returned by the Step 3 fetch (`gh issue view <n> --json title,body,state,url,comments`). The single Step 3 call already carries them; do not re-fetch.
- **`linear` mode:** `list_comments(<id>)` is a **mandatory op that MUST run before Step 0d** — run it explicitly and populate `comments` from its result. Do NOT rely on model initiative to decide whether comments are worth fetching.

**WARN on comments-fetch failure:** if the comments fetch exits non-zero or returns malformed JSON, emit an attention signal and continue with `comments: []`:
```bash
cw event record session.needs_attention \
  --payload '{"reason": "comments_fetch_failed", "ticket_id": "<n>", "session_id": "<from .claude/cw-context.json>"}'
```
Do NOT emit `stage.errored` for this: `STAGE_ERRORED` events are deliberately ignored by `orchestrate.py`'s `_derive_last_stage_by_session`, so a `stage.errored` here would never surface as operator attention. `session.needs_attention` is the signal the operator sees.

**Known limitation (intentionally not WARNed):** a comments fetch that **succeeds but returns empty for a ticket that actually has comments** is NOT detectable from within this stage without a second independent source of the true comment count, so it is intentionally left un-WARNed. The `comments_fetch_failed` WARN above covers only hard failures (non-zero exit / malformed JSON), not a well-formed empty result.

Stamp `materialized_by_session` with the current `session_id` (read it from `.claude/cw-context.json`). This is what makes the idempotency guard below requeue-safe.

**Idempotency (requeue-safe):** skip re-fetch and re-write **only** if ALL of the following hold: `.cw/context.json` exists, its `ticket_id` matches, AND its `materialized_by_session` equals the current `session_id` (from `.claude/cw-context.json`). Otherwise — a different/missing `materialized_by_session` means a **new session is running against a reused worktree (i.e. a `requeue`)** — you MUST re-fetch the ticket (`gh issue view <n> --json title,body,comments`) and **overwrite** `.cw/context.json` so newly-added operator comments and resolutions reach this run. Read the existing file and compare both `ticket_id` and `materialized_by_session` before deciding to skip.

> **Why:** `requeue` reuses the worktree (`create_worktree(..., allow_dirty_reuse=True)`), so a stale `.cw/context.json` survives across runs. A `ticket_id`-only guard skipped re-fetch on every requeue, so operator resolutions added as comments between requeues never reached the plan stage (GitHub #837). Keying the skip on the writing session re-fetches on requeue while preserving the within-session heavy-fetch-once optimization.

**Note:** `.cw/context.json` is distinct from `.claude/cw-context.json` (written by `cw dispatch`, contains `session_id` and `ticket_id` only). Per-stage files read `.cw/context.json` for full ticket orientation; the `CW_SESSION`/`TICKET` bootstrap reads `.claude/cw-context.json`.
