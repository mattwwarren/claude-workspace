---
description: "auto-dev Stage 0: Ticket Intake — tracker resolution, pre-flight sync check, ticket fetch, context materialization"
argument-hint: "<ticket-id> [--headless]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write"]
---

# auto-dev Stage 0: Ticket Intake

Stage 0 runs first in every pipeline invocation, interactive and headless. It resolves the active tracker, runs the pre-flight origin sync check, fetches the ticket, and materializes `.cw/context.json` for downstream per-stage files.

**Arguments:** "$ARGUMENTS"

---

## Tracker Resolution (read first — before Stage 0)

This pipeline is **tracker-agnostic**; the prose below is written in Linear
terms for historical reasons. Resolve the active tracker once per invocation
and substitute its operations everywhere.

**Resolve the tracker:** read `.claude/project-config.yaml` →
`tracking.primary.system`. Recognized values: `github-issues` or `linear`;
absent file or missing key defaults to `linear`. Repos tracking work in
GitHub Issues MUST set `github-issues`.

**Operation mapping** — wherever this document says "Linear", `get_issue`,
`list_comments`, or "post to the Linear ticket", perform the active tracker's
equivalent:

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
  Linear, you are in the wrong tracker mode — stop and use `gh`. Headless, the
  Linear OAuth prompt cannot be answered and the session stalls silently until
  reaped (2026-05-30 fanout-cascade RCA).
- All downstream "post to Linear" / "post the plan/ambiguities/premises to the
  ticket" instructions operate on the GitHub issue via `gh issue comment <n>`.
  Plan signoff markers (`<!-- plan-quality-reviewed -->` etc.) are HTML comments
  embedded in the issue comment body, exactly as in the Linear flow.
- `gh` runs against the repo at the worktree's `origin` remote — confirm with
  `gh repo view --json nameWithOwner` if ambiguous.

---

### Sentinel-Emission Discipline (applies to every EXIT below)

Stage 0 exits standalone with a `blocked`/`stale_dispatch` sentinel in three places below (the Origin Sync Check's Step P3, the Step 3 fetch-failure handler, and the Step 3 open-PR self-check) — none of these chain into Stage 1. Whichever fires:

**Validating is not emitting (#1890).** Before emitting any of the JSON payloads below, validate it with `cw result validate -` and wrap it in the literal `<<<AUTO_DEV_RESULT` / `AUTO_DEV_RESULT>>>` frame — the bare JSON shown inline in each EXIT below is the payload, not the wire format. Validating is not emitting: never narrate emission as a separate act from performing it. The frame must be the final characters of this same message.

**No interactive escalation, ever.** In headless mode there is no listener. Never escalate a pre-flight condition (origin divergence, an unreachable tracker, an already-open PR) by asking a question and ending your turn. Escalate exclusively via the sentinel's `blocker` field with `status: "blocked"` (or `status: "stale_dispatch"` for the open-PR case), as specified at each EXIT below.

## Pre-flight: Origin Sync Check

Runs once per `/auto-dev` invocation, before Stage 0. Fails fast when the local `main` branch is not in sync with `origin/main`, so the pipeline does not spend impl + review tokens on a feature branch the Stage 4 merge gate will reject (#170: a full 25-minute impl + 5-reviewer run exited `merge_gate_blocked` for exactly this).

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

**If they differ**, divergence handling is rare — the full procedure (Step P2
interactive, Step P3 headless with its bounded behind-only wait-and-recheck, and
the exact `blocked` / `local_main_diverged_from_origin` sentinel to EXIT with)
lives in `.claude/commands/auto-dev-intake-appendix.md`, section
"Origin Sync Check divergence handling (Steps P2 and P3)". Read it now; do not
improvise the wait cadence or the sentinel from this summary alone.

---

## Stage 0: Ticket Intake

First resolve the active tracker per **Tracker Resolution** above; the parse
rules below are tracker-aware.

1. Parse `$ARGUMENTS`:
   - **Ticket ID** → single-ticket mode, skip selection. What counts as an ID is
     tracker-dependent: `linear` takes an issue ID like `PROJ-1234`;
     `github-issues` takes a **bare integer** like `403` (or `#403`) — do NOT
     treat that as free text, and do NOT query Linear for it.
   - **Filter flags** (`--cycle`, `--project`, `--label`, `--team`, `--state`, `--assignee`, `--priority`) → batch mode
   - **Free text** (no ID pattern, no flags) → use as description directly, no tracker lookup, no existing plan
   - **Mode flag** `--headless` → suppress all AskUserQuestion calls; apply the Headless Mode gate-collapse table at every downstream decision point. Independent of the input forms above (batch mode in headless is undefined).

2. **Parse constraint flags** (used by the `/auto-debt` alias): `--scope-limit small` (reject Large tickets), `--branch-prefix <prefix>` (override the default `dev` prefix), `--forbidden <comma-separated areas>` (hard-reject tickets touching those areas).

3. **Single-ticket mode:** Fetch the issue via the **active tracker's** fetch op
   (`get_issue(<id>)` for `linear`; `gh issue view <n> --json title,body,state,url,comments`
   for `github-issues`, whose single call carries the comments too). For `linear`,
   `list_comments(<id>)` is a **mandatory op that MUST run before Step 0d** so the
   comments are in hand before context is materialized. Proceed to Stage 1.

   **Step 3 fetch-failure handling (fatal, #1156 — RFC 0011 A2):** a fetch that
   exits non-zero or returns error text instead of issue data is rare — the
   signature table to match that error text against, and the exact `blocked` /
   `operator_unavailable` sentinel to EXIT with (before spawning any agent and
   **before** the `stage.entered` (`s0_intake`) emission below), live in
   `.claude/commands/auto-dev-intake-appendix.md`, section
   "Fetch-failure signature mirror: provenance, signature table, and sentinel".
   Read it now if the fetch failed; do not add, remove, or improvise a
   signature or a sentinel field from memory.

   **Step 3 open-PR self-check (#1862) — run after a successful fetch, before
   Step 0d.** Check whether this ticket already has an open PR before doing any
   planning work:

   ```bash
   gh pr list --head "<branch-prefix>/<ticket-id>" --state open \
     --json number,url,reviewDecision,isDraft
   ```

   Use the effective branch prefix (`--branch-prefix` if given, else the
   client's `feature_branch_prefix`, default `dev`). A non-empty result means
   this ticket already has an open PR. Treat only a *reliable* answer as a hit:
   a non-zero exit, a timeout, or a missing `gh` binary is **not** evidence of a
   PR — fall through and continue the run.

   On a genuine hit, EXIT before spawning any agent with the structured
   `stale_dispatch` / `pr_already_open` sentinel. Firing this branch is rare —
   the exact sentinel and the full rationale (why the race happens, the
   fail-open reliability rule, and why `no_op`/`blocked` are both wrong here)
   live in `.claude/commands/auto-dev-intake-appendix.md`, section
   "Open-PR self-check (#1862): rationale, reliability rule, and status
   choice". Read it now if this condition applies; do not improvise the
   sentinel from this summary alone. Not a substitute for `cw`'s own
   pre-dispatch gate (`disposition: "stale_dispatch_gate"`).

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
   `$CW_SESSION` and `$TICKET` are used by every subsequent stage event emission. Source is `cw-context.json` (written by `cw` dispatch before spawning): the `CW_SESSION_ID` env var does not propagate through `claude --bg` (RFC 0001 §Row 10 gap).

4. **Batch mode** (filter flags given, interactive only) is rare — the full
   procedure lives in `.claude/commands/auto-dev-intake-appendix.md`, section
   "Batch mode (interactive only)". Read it now if `$ARGUMENTS` carried filter
   flags; do not improvise the selection flow from memory. Batch mode in
   headless is undefined.

### Step 0d: Materialize `.cw/context.json`

After ticket fetch (single-ticket mode), write the orientation context for downstream per-stage files:

```bash
mkdir -p .cw
cat > .cw/context.json << 'CWCTXEOF'
{
  "ticket_id": "<TICKET>",
  "ticket_title": "<title from ticket fetch>",
  "ticket_body": "<body from ticket fetch>",
  "comments": [{"author": "<login>", "created_at": "<ISO8601 createdAt>", "body": "<comment text>"}, "..."],
  "scope_hint": null,
  "prior_decisions": [],
  "materialized_by_session": "<session_id from .claude/cw-context.json>"
}
CWCTXEOF
```

**Populating `comments` (from a real fetch, never model initiative):** the `comments` array MUST be filled from an actual tracker fetch — never guessed, never left `[]` by default. **Each entry is an object, not a bare body string (#1794):** map the tracker's fields onto `{"author": ..., "created_at": ..., "body": ...}` **on write** — `github-issues` gives `author.login` → `author` and `createdAt` → `created_at`; `linear` gives the comment's user identity and creation timestamp. The timestamp is load-bearing: Stage 2's Pre-Stage Detector Guard compares the newest comment's `created_at` against branch HEAD's commit date to decide whether an `Auto-Dev-Stage: impl-complete` trailer is still authoritative (`.claude/scripts/check_impl_guard_staleness.py`), and a timestamp-less comment is invisible to that check.
- **`github-issues` mode:** use the `comments` returned by the Step 3 fetch (`gh issue view <n> --json title,body,state,url,comments`), which already carries `createdAt` and `author.login`; do not re-fetch.
- **`linear` mode:** `list_comments(<id>)` is a **mandatory op that MUST run before Step 0d** — run it explicitly and populate `comments` from its result. Do NOT rely on model initiative to decide whether comments are worth fetching.

**WARN on comments-fetch failure:** a comments fetch that exits non-zero or
returns malformed JSON is rare — emit an attention signal and continue with
`comments: []`. The exact `cw event record` call, why `stage.errored` is the
wrong event here, and the one failure shape this WARN cannot detect live in
`.claude/commands/auto-dev-intake-appendix.md`, section "Comments-fetch failure:
the WARN event and what it cannot detect". Read it now if the comments fetch
failed; do not improvise the event name or payload from memory.

Stamp `materialized_by_session` with the current `session_id` (from `.claude/cw-context.json`). This is what makes the idempotency guard below requeue-safe.

**Idempotency (requeue-safe):** skip re-fetch and re-write **only** if ALL of the following hold: `.cw/context.json` exists, its `ticket_id` matches, AND its `materialized_by_session` equals the current `session_id` (from `.claude/cw-context.json`). Otherwise — a different/missing `materialized_by_session` means a **new session is running against a reused worktree (i.e. a `requeue`)** — you MUST re-fetch the ticket (`gh issue view <n> --json title,body,comments`) and **overwrite** `.cw/context.json` so newly-added operator comments and resolutions reach this run. Read the existing file and compare both fields before deciding to skip.

**Changing this idempotency guard, or debugging a stale `.cw/context.json`,** is
rare — the rationale (#837) and the `.cw/context.json` vs `.claude/cw-context.json`
distinction live in `.claude/commands/auto-dev-intake-appendix.md`, section
"`.cw/context.json` idempotency: why the guard keys on the writing session
(#837)". Read it now if either applies.
