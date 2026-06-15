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
| Fetch ticket body | `get_issue(<id>)` | `gh issue view <n> --json title,body,state,url` |
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
   (`get_issue(<id>)` for `linear`; `gh issue view <n> --json title,body,state,url`
   for `github-issues`). Proceed to Stage 1.

   **Headless only — initialize correlation context and emit `stage.entered` (`s0_intake`):**
   ```bash
   CW_CTX=".claude/cw-context.json"
   CW_SESSION=$(jq -r '.session_id // "unknown"' "$CW_CTX" 2>/dev/null || echo "unknown")
   TICKET=$(jq -r '.ticket_id // ""' "$CW_CTX" 2>/dev/null || echo "")
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
  "prior_decisions": []
}
CWCTXEOF
```

**Idempotency:** if `.cw/context.json` already exists and `ticket_id` matches, skip re-fetch and re-write. Read the existing file and confirm the `ticket_id` field matches before skipping.

**Note:** `.cw/context.json` is distinct from `.claude/cw-context.json` (written by `cw dispatch`, contains `session_id` and `ticket_id` only). Per-stage files read `.cw/context.json` for full ticket orientation; the `CW_SESSION`/`TICKET` bootstrap reads `.claude/cw-context.json`.
