---
name: cw-fanout
description: Multi-ticket parallel dispatch with a monitoring handoff — takes a list of tickets, runs pre-flight per ticket, enqueues the batch, starts the cw dispatch loop, then watches the wave via the queue-peek ladder and the session.needs_attention / session.timed_out event bus until every ticket is terminal. Use when the user wants to enqueue several tickets and then monitor the queue in one motion. Triggers on "fan out these tickets", "dispatch a wave", "enqueue these and watch them", "run /auto-dev on N tickets and monitor", "dispatch and babysit the queue".
---

# cw-fanout

Carries a batch of tickets from **"enqueue these"** to **"now monitoring the
queue"** in one orchestrated motion. It is the N-way companion to
`/cw-smoke-test` (which does the same enqueue → dispatch → monitor arc for a
single ticket, one tick) and the active driver that sits in front of
`/cw-queue-peek` (the in-flight health ladder).

Pure orchestration — it composes existing pieces and adds no new parsing or
validation logic:

- **pre-flight** reuses `cw-smoke-test/scripts/preflight.py` (per ticket).
- **dispatch** uses `cw dev-queue add` + `cw dev-queue run`.
- **wave lifecycle** uses the bundled `scripts/wave_status.py` (is the batch done?).
- **in-flight health** uses `cw queue peek` (WAIT/PEEK/STOP).
- **attention** uses the `session.needs_attention` / `session.timed_out` event bus.

## When to use

- The operator has several ready tickets and wants them dispatched in parallel,
  then watched as a single wave.
- A meta-test spray across N tech-debt tickets where each should run `/auto-dev`
  headless and surface only when it needs a human.
- Re-dispatching at N>1 after `/cw-followup` has prepared the ground (decisions
  appended, branches rebased).

Do **not** use this skill for:

- A single ticket end-to-end validation — use `/cw-smoke-test`.
- Acting on one finished session's sentinel — use `/cw-followup`.
- A forensic read of one session — use `/cw-validate-result`.
- Interactive (USER-origin) sessions started via `cw start` — those don't go
  through dev-queue.

## Inputs

One or more ticket ids (e.g. `201 202 203` or `#201 #202`).

Optional flags:
- `--client <NAME>` — cw client for the queue (default `claude-workspace`).
- `--max-parallel <N>` — override the per-client concurrency cap for this wave.
- `--skip-preflight` — run pre-flight in advisory mode (report, do not drop).
- `--dry-run` — pre-flight only; do not enqueue or dispatch.
- `--no-monitor` — enqueue + start dispatch, then hand back to the operator
  without entering the monitoring phase.

## How it works

### Step 1 — batch pre-flight

Run the bundled checker once per ticket (it is the single source of truth for
readiness; never re-implement it):

```bash
uv run --project "$(git rev-parse --show-toplevel)" python \
  .claude/skills/cw-smoke-test/scripts/preflight.py \
  --ticket-id <NUMBER> --client <CLIENT>
```

Each call emits one JSON object with `ok` (bool) and `checks`. Aggregate into a
table: one row per ticket, `ok` plus any failing hard checks. Then:

- **Default:** drop tickets whose `ok` is false from the wave. Print the dropped
  rows with their failing checks. Continue with the survivors.
- **`--skip-preflight`:** keep every ticket; print a warning banner listing the
  failed checks so the operator sees the wave launched in degraded mode.
- **No survivors:** stop. Nothing to dispatch.

On `--dry-run`, print the table and stop here.

### Step 2 — enqueue the survivors

`cw dev-queue add` accepts the whole batch in one call:

```bash
cw dev-queue add <T1> <T2> <T3> -c <CLIENT>
```

Record the exact ticket set you enqueued — that is the **wave set** every later
step keys off. Tickets dropped in Step 1 are not part of it.

### Step 3 — start the dispatch loop

```bash
cw dev-queue run --max-parallel <N>   # omit --max-parallel to use the configured cap
```

`cw dev-queue run` (without `--once`) **loops forever** — it does not exit when
the queue drains. Each tick it `reconcile()`s (which drives the idle/timeout
watchdog and emits the attention events), spawns workers up to the concurrency
cap, and consumes completion events. So:

- **Launch it in the background** and capture the moment you started it as the
  event-bus cursor for Step 4.
- Wave completion is detected from queue state (Step 4), **not** from this
  process exiting.
- If the loop is already running (an operator wave is in flight), do not start a
  second one — reuse it.

On `--no-monitor`, stop here: report the started loop + the wave set and hand
back to the operator.

### Step 4 — monitor the wave

This is the handoff `/cw-queue-peek` was built to receive. Drive it from
**checkpoints**, not a busy `sleep` poll — react to events and re-check state.

**a. Wave lifecycle — am I done?**

```bash
python3 .claude/skills/cw-fanout/scripts/wave_status.py <T1> <T2> <T3> \
  --client <CLIENT> --json
```

Exit 0 / `"terminal": true` when every wave ticket is terminal
(`completed` / `failed` / `cancelled` / `blocked_on_user`, or removed from the
queue). `"in_flight"` lists the tickets still `pending`/`running`;
`"needs_attention"` lists `blocked_on_user` tickets (a session paused for the
operator). Loop the rest of Step 4 until this reports terminal.

**b. Attention signals — does anything need me now?**

Tail the event bus with a durable consumer cursor (resumes where it left off):

```bash
cw event tail --since fanout-mon \
  --type session.needs_attention \
  --type session.timed_out \
  --type session.completed --json
```

On `session.needs_attention` (paused-for-input, user-directed blocked, or
exited-without-sentinel) or `session.timed_out`: surface the ticket + session
immediately and pause for the operator. Do not silently re-dispatch. When the
event is `session.timed_out`, check the `branch_state` field: `"absent_no_merged_pr"`
means the worker died before push (an anomaly worth investigating), while
`"present"` is an ordinary slow timeout. See [`session-disposition.md §5a`](../../docs/session-disposition.md).

**c. In-flight health — is a running session stuck?**

For the `in_flight` tickets, run the peek-stop ladder:

```bash
cw queue peek --client <CLIENT>
```

Follow `/cw-queue-peek`: act on STOP / STOP-OR-PEEK rows (stuck post-PR-merge,
retry loop, approaching the 60-min ceiling) per its ladder. Always
`cw spawn close <session_id>` **before** `cw dev-queue remove`.

Surface progress sparingly — one line when the wave shrinks or a ticket flips
to needs-attention; otherwise stay quiet. The operator can `cw watch` for the
live board.

### Step 5 — report + disposition

When `wave_status.py` reports terminal, stop the background dispatch loop (if no
other wave is queued behind it), then print a per-ticket disposition table.
Suggest the right follow-up per ticket:

- `completed` → `/cw-validate-result --ticket-id <N>` to confirm what shipped.
- `blocked_on_user` → `/cw-followup --ticket-id <N>` to disposition it.
- `failed` / dropped → surface the reason; let the operator decide.

## Output shape

End with one summary line per wave, caveman-tight:

```
fanout: client=claude-workspace wave=[201,202,203] → 2 shipped, 1 blocked_on_user (#202)
  #202 needs attention → /cw-followup --ticket-id 202
fanout: wave=[210,211] → all shipped; 0 need attention
```

## Failure modes

- **Pre-flight drops the whole wave** — print the failing rows; do not enqueue.
  Offer `--skip-preflight` if the operator wants to override.
- **Dispatch loop not running** — the watchdog only ticks while `cw dev-queue
  run` is alive; if the operator killed it, the wave stalls silently. Re-launch
  it (Step 3) and note the gap.
- **A ticket sits `pending` forever** — concurrency cap is saturated by other
  clients, or the freshness gate keeps rejecting it (stale `main`). Surface
  `cw dev-queue status` + `cw doctor`; suggest `cw dev-queue refresh-all`.
- **`needs_attention` storm** — many tickets pause at once (often the same
  ambiguity across a batch). Disposition one via `/cw-followup`, then re-dispatch
  the rest with the same decision rather than answering each separately.

## Out of scope

- Generating or rebasing the per-ticket branches — `/cw-followup` owns that.
- Mutating the sentinel schema or the dispatch internals.
- Continuous standing-queue operation — this skill drives one wave to terminal;
  for an always-on queue, leave `cw dev-queue run` up and watch `cw watch`.

## Related

- `/cw-smoke-test` — single-ticket end-to-end dogfood (the N=1 form of this).
- `/cw-queue-peek` — in-flight WAIT/PEEK/STOP ladder (Step 4c).
- `/cw-validate-result` — forensic read on a finished ticket (Step 5).
- `/cw-followup` — act on a finished/blocked ticket's sentinel (Step 5).
- `cw event tail --type session.needs_attention --type session.timed_out` —
  the durable attention bus this skill watches in Step 4b.
