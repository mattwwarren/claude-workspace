---
name: cw-session-watch
description: Reliably determine whether a cw dispatch session has ended and what its exit status was (sentinel.status, blocker.reason, PR url, dev-queue routing). Use after `cw dev-queue add` / `cw dev-queue run --once`, or any time you spawned a daemon session and need to know its outcome without manually grepping events and transcripts. Triggers on "did 167 finish", "what was the exit status", "is the session done", "check on the session".
model: haiku
---

# cw Session Watch

Reliably answer two questions about a cw dispatch session:

1. **Has it ended?** (`cw session` reports the live status from cw state)
2. **What was the exit status?** (the AUTO_DEV_RESULT sentinel via
   `cw session result` + the dev-queue routing decision via
   `cw dev-queue status`)

This skill replaces the manual flow of grepping `inbox.jsonl` + locating the
right claude transcript + regex-extracting the sentinel block — that flow
silently misses sessions, mis-parses sentinels (the `<<<` / `>>>` markers are
ordered counterintuitively), and forgets to cross-reference dev-queue
routing.

## When to use

- After `cw dev-queue add <ticket> && cw dev-queue run --once`
- After `cw spawn ...` or any other path that creates a daemon session
- When the user asks "did 167 finish?" / "what was the exit?" / "is X done?"
- When you previously dispatched something and need to surface its outcome

Do **not** use this skill for interactive (USER-origin) sessions started via
`cw start` — those don't emit sentinels and don't go through dev-queue.

## Required tools

This skill is built entirely on first-class `cw` inspection subcommands — no
external scripts:

- `cw session list --ticket <id> --client <c> --json` — resolve a ticket to its
  session id (stable JSON contract).
- `cw session show <ref> --json` — full session state, including `status`,
  `branch`, `claude_session_id`, `worktree_path`, and `last_result` (the parsed
  sentinel).
- `cw session result <ref>` — just the `last_result` sentinel JSON (exit 1 if
  none recorded).
- `cw session wait <ref> [--until ...] [--timeout SECS]` — block until the
  session reaches a terminal status; replaces any hand-rolled poll loop.
- `cw dev-queue status --client <c> --json` — the orchestrator's per-ticket
  routing decision (`completed` / `failed` / `pending`).

All five are read-only; none mutate cw state.

### Resolving a ticket to a session id

`cw session list` **excludes terminal sessions by default** (completed /
timed_out). A session you dispatched in the past may already be terminal, so a
robust resolve tries the running set first, then each terminal status:

```bash
resolve_sid() {  # $1=ticket  $2=client
  for st in "" "--status completed" "--status timed_out"; do
    sid=$(cw session list --ticket "$1" --client "$2" $st --json | jq -r '.[0].id // empty')
    [ -n "$sid" ] && { echo "$sid"; return 0; }
  done
  return 1   # no session found for this ticket/client (typo, wrong client, never spawned)
}
```

`--client` is recommended when the same ticket id could exist for multiple
clients. Omit it only if you're sure there's no collision.

## Execution

### Mode A — One-shot lookup (default)

Use when the session was dispatched in the past and you want to know its
current status (which may still be running if it hasn't terminated).

```bash
SID=$(resolve_sid <ID> <client>) || { echo "no session for <ID>"; exit 2; }
cw session show "$SID" --json        # status + last_result (sentinel) + branch
cw session result "$SID"             # sentinel JSON only (exit 1 if none yet)
cw dev-queue status --client <client> --json   # per-ticket routing decision
```

**Reading the `status` field** from `cw session show`:

- `active` / `idle` / `backgrounded` — still running; no terminal outcome yet.
- `completed` — terminal; inspect `last_result` for the sentinel.
- `timed_out` — terminal; headless budget exceeded (see the timed-out salvage
  below before recommending re-dispatch).

`cw session show` exits 1 if the ref doesn't resolve; `resolve_sid` returning
non-zero means no spawn was ever recorded for that ticket/client.

### Mode B — Wait until done

Use when you've **just** dispatched and want a single notification when the
session terminates.

**Primary: subscribe to the operator channel.** Instead of blocking on a poll,
subscribe to `cw-operator` (see the
[operator channel](../../../docs/operator-channel.md) doc for wiring) and
react to the terminal `task.transition` push for this ticket — read
`disposition` off its payload for the outcome (`shipped` / `blocked` /
`no_op` / etc., see the Common Patterns table below), then confirm with `cw
session show "$SID" --json` as usual. **Handle `cancelled` as its own case:**
a `cancelled` `task.transition` forwards through the channel but always
carries `disposition: null` — that means the session was cancelled with
nothing to report, not an error and not a value to silently coerce into some
other outcome.

### Fallback: Poll Ladder

Use when the channel server is down. Resolve the id once (it's still active),
then block on `cw session wait` — it polls cw state internally every few
seconds, so no manual `until` loop and no `Monitor` + `tail -F | awk` pipe.

> **Why not a hand-rolled poll loop or `Monitor` + `awk`:** `awk`'s block
> buffering + `exit` action means a matching event can sit unflushed while
> Monitor times out (confirmed broken in practice 2026-05-26: two parallel
> dispatches both had matching inbox events while the awk filter hit the
> 1-hour deadline). `cw session wait` is the blessed path — it reads cw state
> directly and returns the moment the status flips.

Run it via `Bash` with `run_in_background: true`; the harness notifies you
when the process exits:

```bash
SID=$(cw session list --ticket <T> --client <C> --json | jq -r '.[0].id // empty')
cw session wait "$SID" --timeout 3600     # exit 0 completed, 1 timed_out, 124 hard timeout
cw session show "$SID" --json
```

Set `--timeout` to the headless budget (currently 3600s, see #265); the
default is 300s, which is too short for a dispatch session.

#### Multi-session Mode B (parallel dispatch)

When you've dispatched multiple tickets in parallel and need to wait on all of
them, resolve each session id via `cw session list --ticket` per ticket —
**never** parse `cw list` (see "DO NOT" below).

```bash
TICKETS=(265 270 246)
CLIENT=claude-workspace
SIDS=()
for t in "${TICKETS[@]}"; do
  SIDS+=("$(cw session list --ticket "$t" --client "$CLIENT" --json | jq -r '.[0].id // empty')")
done
for sid in "${SIDS[@]}"; do
  cw session wait "$sid" --timeout 3600
done
# All sessions terminated; loop over SIDS again to surface each result.
for sid in "${SIDS[@]}"; do
  cw session show "$sid" --json
done
```

Why per-ticket lookup, not `cw list`: `cw session list --json` returns the
session id keyed to the ticket — a stable contract. `cw list` is
human-readable and unstable (see below).

#### DO NOT: parse `cw list` to find session ids

`cw list` is the human table view. Do not script against it — use
`cw session list --json` (or `cw session list --ticket ... --json`), which is
the structured contract.

```bash
# WRONG — produces garbage that pollutes the polling loop
SESSIONS=$(cw list | grep "claude-workspace.*active" | awk '{print $NF}')
```

Why this fails:

1. `cw list` is whitespace-aligned columns with a header row and no stable
   anchoring.
2. `awk '{print $NF}'` grabs the last column — typically the human-readable
   "SINCE" field (e.g. literal `"now"`), not the session id.
3. `awk '{print $4}'` still parses the header row on the first hit and
   silently breaks the moment column order changes.
4. The polling loop then asks `wait now` (or some header fragment) forever;
   sessions complete in the background but the harness never gets a completion
   signal. Observed in practice 2026-05-26 on a parallel dispatch of
   #265/#270/#246 — caught only when the user noticed manually.

Use `cw session list --ticket <id> --client <client> --json` per ticket
instead. Its JSON output is a stable contract; `cw list` text is not.

**Coverage gotcha:** if the session emits a `crashed:true` `session.completed`
with no `claude_session_id` (the reconcile-race regression — see "Salvaging
crashed-but-actually-ran" below), `cw session wait` WILL still exit (the status
flips to `completed`), but `last_result` will be absent. Always inspect
`cw session show --json`; if it shows `status: completed` with a null
`claude_session_id` and no `last_result`, run the worktree-dir-scan salvage.

### Reporting the result

After a successful lookup, surface to the user in a structured form. Read the
sentinel from `last_result` (via `cw session show --json` or `cw session
result`):

- **If `status == "shipped"`**: PR url + fix cycles + ticket cleared.
- **If `status == "blocked"`**: blocker.reason + recovery_hint + next_actions
  (so the user knows what unblocks it).
- **If `status == "no_op"`**: explain why no work was needed (often "already
  done in upstream PR" or similar).
- **If the queue transition is `cancelled`**: this is an operator-initiated
  task cancel (`cw spawn close` / `cancel_ticket`), not a sentinel outcome —
  `disposition` is always null. Report "cancelled, nothing to report" rather
  than waiting on a sentinel or treating the null as an error.
- **If the session `status` is `timed_out` and no sentinel was recorded**:
  the session never emitted a sentinel; elapsed time hit the headless budget.
  **Before recommending re-dispatch, check the impl branch for un-PR'd
  commits** — sessions routinely complete impl + push + run gates, then time
  out at the Stage 3/4 boundary (between reviewers finishing and `gh pr
  create`). Salvage path in that case is manual PR finalization, not
  redispatch. See "Salvaging timed-out impl" below.
  Additionally, inspect the `branch_state` field on the `session.timed_out`
  event (via `cw event tail --type session.timed_out --json`):
  - `"absent_no_merged_pr"` — **anomaly**: worker died before push or branch
    was force-deleted. No artifacts left. Investigate before re-dispatching;
    pure retry churn will not fix a dead worker.
  - *(key absent)* — branch still on origin, or check unavailable (fail-open).
  See [`session-disposition.md §5a`](../../../docs/session-disposition.md#5a-branch-absence-anomaly-on-session_timed_out-808)
  for the full rationale (branch-absence ≠ merged).
- **Always include the dev-queue routing**: `cw dev-queue status --client
  <client> --json` is the cw orchestrator's final routing decision
  (`completed`, `failed`, `pending` for retry). It's the source of truth for
  whether the ticket needs another dispatch tick.

### Common patterns

| Sentinel status | session status | queue routing | meaning |
|---|---|---|---|
| `shipped` | `completed` | `completed` | PR merged or auto-merge armed; ticket done |
| `blocked` (retry_eligible: true) | `completed` | `pending` | Recovery needed (e.g. sync main); will redispatch on next tick |
| `blocked` (retry_eligible: false) | `completed` | `failed` | Human needs to intervene; check blocker.recovery_hint |
| `no_op` | `completed` | `completed` | Nothing to do (e.g. already shipped upstream) |
| `validation_failed` | `completed` | `pending`/`failed` | Producer-side sentinel bug; check attempts vs 3-cap |
| `premises_pending_verification` | `completed` | `pending` | Session surfaced premises; verify and re-dispatch |
| `ambiguities_pending_resolution` | `completed` | `pending` | Session needs more info; resolve and re-dispatch |
| (none) | `timed_out` | `pending` | Headless budget exceeded. **Check impl branch first** — often impl shipped, only Stage 3/4 transition lost. Otherwise redispatch. |
| (none) | `completed` + `claude_session_id: null` | `pending` | **Reconcile-race regression.** Session actually ran fine; cw lost track. Scan worktree transcript dir for the latest sentinel (see "Salvaging crashed-but-actually-ran"). |
| (none — always `disposition: null`) | any | `cancelled` | Operator-initiated task cancel, not a sentinel outcome. Report "cancelled, nothing to report"; don't wait on a sentinel. |

If you see a combination not in this table, surface the full sentinel + the
queue routing to the user — that's likely a bug worth filing.

## Salvaging crashed-but-actually-ran

Triggered by: `cw session show --json` reporting `status: completed` AND a null
`claude_session_id` AND no `last_result`.

**Root cause (regression as of 2026-05-26):** the reconcile sweep in
`src/cw/reconcile/` uses `claude agents --json` as its sole liveness
oracle (post-#269 multiplexer removal). A `claude --bg` spawn has a
register-with-daemon window of >1 second. If reconcile runs in the same
dispatch tick that just spawned a session, the daemon hasn't yet
registered it and reconcile marks it phantom → records a `completed` status
with `crashed: true` and no `claude_session_id`. The actual session keeps
running and emits a real sentinel; cw just isn't listening anymore.

When you see this pattern, the actual work might be complete. Triage:

```bash
# Find the most recent transcript in the worktree's claude project dir.
# cw session show --json gives worktree_path; the transcript dir mirrors it.
TX_DIR=~/.claude/projects/-home-matthew--cw-wt-<encoded-worktree>-auto-dev-<ticket>
ls -t $TX_DIR/*.jsonl | head -1
# Parse the last AUTO_DEV_RESULT sentinel from that transcript (sentinel
# markers are <<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>>).
```

If a recent transcript exists with a valid sentinel, the dispatched work
completed normally — cw's tracking is just wrong. Treat the sentinel as
authoritative: act on `shipped` / `blocked` / `premises_pending_*` etc.
exactly as the routing table prescribes.

If the worktree has commits ahead of main on `dev/<ticket>-<slug>`,
run the timed-out-impl salvage steps below to finalize the PR manually.

**Filing the regression:** if this fires, link the report to the reconcile
race tracking ticket (search open issues for "reconcile race phantom
spawned" or similar). The fix is to either (a) add a registration grace
window before reconcile reaps a session, or (b) check `claude agents --json`
+ verify-by-PID-still-alive before declaring phantom.

## Salvaging timed-out impl

Before recommending a redispatch after a `timed_out`-with-no-sentinel session,
run this triage — it routinely turns a "lost session" into a 5-minute
manual PR-open:

```bash
# 1. Are there commits on the dev branch beyond main?
git -C <main-repo> log --oneline --all --since="<spawn_time>"
# Look for: dev/<ticket>-<slug> branch with commits ahead of main

# 2. If yes, verify gates locally on the impl branch
git -C <main-repo> fetch origin dev/<ticket>-<slug>
# (impl worktree at `.claude/worktrees/agent-*` may already have the branch
#  checked out — `cw session show --json` reports worktree_path; use it if so)
cd <impl-worktree>
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest tests/ -q

# 3. If gates green, open the PR with auto-merge
gh pr create --base main --head dev/<ticket>-<slug> \
  --title "<commit subject>" --body "<body>"
gh pr merge <PR#> --squash --auto

# 4. Clear the ticket from dev-queue (PR will close it on merge)
cw dev-queue remove <ticket> -c <client>
```

This pattern fires whenever the dispatch session is in Stage 3 (reviewers
running) or between Stage 3 and Stage 4 (`gh pr create`) when the
reconcile sweep kills it. The impl + tests + commits + push all completed;
only the PR-open step is missing. The orphaned worktree at
`.claude/worktrees/agent-*` will still hold the branch checked out.

If commits exist but gates fail, the planner-side work was incomplete —
redispatch is correct, but consider posting the partial state to the issue
as context so the next attempt doesn't redo the same exploratory work.

## Notes / caveats

- `cw session` reports what cw state knows. If a session is still running but
  hasn't emitted Stop yet, `status` shows `active`/`idle` and `cw session
  result` exits 1 (no result yet). The reconcile sweep (per-tick) is what
  eventually transitions a dead surface to `timed_out` — that can take up to
  `HEADLESS_TIMEOUT_SECONDS` (currently 3600s, see #265).
- The sentinel markers are `<<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>>` —
  `<<<` opens, `>>>` closes. (Counterintuitive; ad-hoc grep usually has it
  backwards.) cw parses and stores the result in `last_result`, so prefer
  `cw session result` over re-parsing the transcript.
- If `claude_session_id` is null on a terminal session (common for
  `timed_out`), no sentinel will have been recorded — that's expected, not a
  bug.
- `cw session list/show/result/wait` and `cw dev-queue status` are all
  read-only; none mutate cw state.
