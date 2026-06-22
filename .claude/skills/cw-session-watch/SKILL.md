---
name: cw-session-watch
description: Reliably determine whether a cw dispatch session has ended and what its exit status was (sentinel.status, blocker.reason, PR url, dev-queue routing). Use after `cw dev-queue add` / `cw dev-queue run --once`, or any time you spawned a daemon session and need to know its outcome without manually grepping events and transcripts. Triggers on "did 167 finish", "what was the exit status", "is the session done", "check on the session".
model: haiku
---

# cw Session Watch

Reliably answer two questions about a cw dispatch session:

1. **Has it ended?** (look up the terminal event in the cw event inbox)
2. **What was the exit status?** (parse the AUTO_DEV_RESULT sentinel + the
   dev-queue routing decision)

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

## Required tool

The skill is a thin wrapper around `~/.claude/scripts/cw_session_exit.py`,
which reads `~/.local/share/cw/events/inbox.jsonl`, the dev-queue state, and
the claude transcript to produce a single structured summary.

## Execution

### Mode A — One-shot lookup (default)

Use when the session was dispatched in the past and you want to know its
current status (which may still be "running" if it hasn't terminated).

```bash
~/.claude/scripts/cw_session_exit.py --ticket <ID> --client <client>
```

`--client` is recommended when the same ticket id could exist for multiple
clients. Omit it only if you're sure there's no collision.

Alternative: look up by session id directly:

```bash
~/.claude/scripts/cw_session_exit.py --session <8-hex-session-id>
```

JSON output (for parsing in subsequent steps):

```bash
~/.claude/scripts/cw_session_exit.py --ticket <ID> --client <client> --json
```

**Exit codes:**

- `0` — terminal event found; sentinel may or may not be present
- `1` — session was spawned but has not yet terminated
- `2` — no spawn event found at all (typo'd ticket id, wrong client, or
  session never spawned)

### Mode B — Wait until done (Bash `run_in_background`)

Use when you've **just** dispatched and want a single notification when
the session terminates. The pattern is `run_in_background` with an
`until` poll loop, NOT `Monitor`.

**Why not Monitor with `tail -F | awk`:** the `tail -F | awk` pipe
buffers — `awk`'s `print; exit` puts the matching line in a block
buffer that doesn't flush before awk exits at the kernel level fast
enough for Monitor to observe. **Confirmed broken in practice
2026-05-26:** two parallel dispatches both had matching events sitting
in the inbox while Monitor's awk filter timed out at the 1-hour
deadline. `grep --line-buffered` in Monitor's example works because
grep flushes per match; `awk`'s buffering is different and the
`exit` action makes it worse.

**Use this pattern instead** (via `Bash` with `run_in_background: true`):

```bash
SID=$(python3 ~/.claude/scripts/cw_session_exit.py --ticket <T> --client <C> --json | jq -r .session_id)
until ~/.claude/scripts/cw_session_exit.py --session "$SID" --json 2>/dev/null | jq -e '.found' >/dev/null; do
  sleep 15
done
~/.claude/scripts/cw_session_exit.py --session "$SID"
```

The harness notifies you when the bash process exits. The 15s poll is
cheap (~50ms file read each tick); the script is idempotent.

Trade-off vs Monitor: 15s upper bound on detection latency, vs Monitor's
~instant-on-flush. For dispatch sessions that typically run minutes-to-hours,
15s is invisible. The reliability gain is worth it.

#### Multi-session Mode B (parallel dispatch)

When you've dispatched multiple tickets in parallel and need to wait on all of
them, resolve each session id via `cw_session_exit.py --ticket` per ticket —
**never** parse `cw list` (see "DO NOT" below).

```bash
TICKETS=(265 270 246)
CLIENT=claude-workspace
SIDS=()
for t in "${TICKETS[@]}"; do
  sid=$(~/.claude/scripts/cw_session_exit.py --ticket "$t" --client "$CLIENT" --json | jq -r .session_id)
  SIDS+=("$sid")
done
for sid in "${SIDS[@]}"; do
  until ~/.claude/scripts/cw_session_exit.py --session "$sid" --json 2>/dev/null | jq -e '.found' >/dev/null; do
    sleep 15
  done
done
# All sessions terminated; loop over SIDS again to surface each result.
for sid in "${SIDS[@]}"; do
  ~/.claude/scripts/cw_session_exit.py --session "$sid"
done
```

Why per-ticket lookup, not `cw list`: the script reads the event inbox
directly and returns the session id keyed to the ticket — stable contract,
JSON output. `cw list` is human-readable and unstable (see below).

#### DO NOT: parse `cw list` to find session ids

`cw list` is for humans. Do not script against it.

```bash
# WRONG — produces garbage that pollutes the polling loop
SESSIONS=$(cw list | grep "claude-workspace.*active" | awk '{print $NF}')
```

Why this fails:

1. `cw list` has no `--json` flag (as of 2026-05-26) and no stable column
   anchoring. The output is whitespace-aligned columns with a header row.
2. `awk '{print $NF}'` grabs the last column — typically the human-readable
   "SINCE" field (e.g. literal string `"now"` matching `"just now"`), not
   the session id.
3. `awk '{print $4}'` still parses the header row on the first hit and
   silently breaks the moment column order changes.
4. The polling loop then asks `--session now` (or some header fragment)
   forever; sessions complete in the background but the harness never gets
   a completion signal. Observed in practice 2026-05-26 on a parallel
   dispatch of #265/#270/#246 — caught only when the user noticed manually.

Use `cw_session_exit.py --ticket <id> --client <client> --json` per ticket
instead. The script's JSON output is a stable contract; `cw list` text is
not.

**Coverage gotcha:** if the session emits a `crashed:true`
`session.completed` event with no `claude_session_id` (the reconcile-race
regression — see "Salvaging crashed-but-actually-ran" below), this loop
WILL exit cleanly (the lookup script returns `found: true`), but the
sentinel will be missing. Always inspect the structured summary; if it
shows `crashed:True` with no sentinel, run the worktree-dir-scan salvage.

### Reporting the result

After a successful lookup (exit==0), surface to the user in a structured
form:

- **If `sentinel_status == "shipped"`**: PR url + fix cycles + ticket cleared.
- **If `sentinel_status == "blocked"`**: blocker.reason + recovery_hint +
  next_actions (so the user knows what unblocks it).
- **If `sentinel_status == "no_op"`**: explain why no work was needed
  (often "already done in upstream PR" or similar).
- **If `exit_event_type == "session.timed_out"` and `sentinel_status` is
  None**: session never emitted a sentinel; elapsed_seconds hit the
  headless budget. **Before recommending re-dispatch, check the impl
  branch for un-PR'd commits** — sessions routinely complete impl + push
  + run gates, then time out at the Stage 3/4 boundary (between reviewers
  finishing and `gh pr create`). Salvage path in that case is manual PR
  finalization, not redispatch. See "Salvaging timed-out impl" below.
  Additionally, inspect the `branch_state` field on the `session.timed_out`
  event (via `cw event tail --type session.timed_out --json`):
  - `"absent_no_merged_pr"` — **anomaly**: worker died before push or branch
    was force-deleted. No artifacts left. Investigate before re-dispatching;
    pure retry churn will not fix a dead worker.
  - *(key absent)* — branch still on origin, or check unavailable (fail-open).
  See [`session-disposition.md §5a`](../../../docs/session-disposition.md#5a-branch-absence-anomaly-on-session_timed_out-808)
  for the full rationale (branch-absence ≠ merged).
- **Always include `queue_status`**: this is the cw orchestrator's final
  routing decision (`completed`, `failed`, `pending` for retry). It's the
  source of truth for whether the ticket needs another dispatch tick.

### Common patterns

| Sentinel status | exit_event_type | queue_status | meaning |
|---|---|---|---|
| `shipped` | `session.completed` | `completed` | PR merged or auto-merge armed; ticket done |
| `blocked` (retry_eligible: true) | `session.completed` | `pending` | Recovery needed (e.g. sync main); will redispatch on next tick |
| `blocked` (retry_eligible: false) | `session.completed` | `failed` | Human needs to intervene; check blocker.recovery_hint |
| `no_op` | `session.completed` | `completed` | Nothing to do (e.g. already shipped upstream) |
| `validation_failed` | `session.completed` | `pending`/`failed` | Producer-side sentinel bug; check attempts vs 3-cap |
| `premises_pending_verification` | `session.completed` | `pending` | Session surfaced premises; verify and re-dispatch |
| `ambiguities_pending_resolution` | `session.completed` | `pending` | Session needs more info; resolve and re-dispatch |
| (none) | `session.timed_out` | `pending` | Headless budget exceeded. **Check impl branch first** — often impl shipped, only Stage 3/4 transition lost. Otherwise redispatch. |
| (none) | `session.completed` + `crashed:true` + `claude_session_id:null` | `pending` | **Reconcile-race regression.** Session actually ran fine; cw lost track. Scan worktree transcript dir for the latest sentinel (see "Salvaging crashed-but-actually-ran"). |

If you see a combination not in this table, surface the full sentinel + the
queue routing to the user — that's likely a bug worth filing.

## Salvaging crashed-but-actually-ran

Triggered by: `exit_event_type == "session.completed"` AND `crashed == True`
AND `claude_session_id == null`.

**Root cause (regression as of 2026-05-26):** the reconcile sweep in
`src/cw/reconcile.py` uses `claude agents --json` as its sole liveness
oracle (post-#269 multiplexer removal). A `claude --bg` spawn has a
register-with-daemon window of >1 second. If reconcile runs in the same
dispatch tick that just spawned a session, the daemon hasn't yet
registered it and reconcile marks it phantom → emits `session.completed`
with `crashed: true` and no `claude_session_id`. The actual session keeps
running and emits a real sentinel; cw just isn't listening anymore.

When you see this pattern, the actual work might be complete. Triage:

```bash
# Find the most recent transcript in the worktree's claude project dir
TX_DIR=~/.claude/projects/-home-matthew--cw-wt-<encoded-worktree>-auto-dev-<ticket>
ls -t $TX_DIR/*.jsonl | head -1
# Parse the last AUTO_DEV_RESULT sentinel from that transcript using the
# same logic as the script's parse_sentinel() (sentinel markers are
# <<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>>).
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

Before recommending a redispatch after `session.timed_out`-with-no-sentinel,
run this triage — it routinely turns a "lost session" into a 5-minute
manual PR-open:

```bash
# 1. Are there commits on the dev branch beyond main?
git -C <main-repo> log --oneline --all --since="<spawn_time>"
# Look for: dev/<ticket>-<slug> branch with commits ahead of main

# 2. If yes, verify gates locally on the impl branch
git -C <main-repo> fetch origin dev/<ticket>-<slug>
# (impl worktree at `.claude/worktrees/agent-*` may already have the branch
#  checked out — use that path directly if so)
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

- The script only knows what the event inbox has been told. If a session is
  still running but hasn't emitted Stop yet, it'll show `found: false`. The
  reconcile sweep (per-tick) is what eventually transitions a dead surface
  to `session.timed_out` — that can take up to `HEADLESS_TIMEOUT_SECONDS`
  (currently 3600s, see #265).
- The sentinel markers are `<<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>>` —
  `<<<` opens, `>>>` closes. (Counterintuitive; both `dev_status` patterns
  and ad-hoc grep usually have it backwards.) The script handles this.
- If `claude_session_id` is null on the terminal event (common for
  `session.timed_out`), no sentinel will be parsed — that's expected, not
  a script bug.
- The script does **not** mutate any cw state. It's read-only.
