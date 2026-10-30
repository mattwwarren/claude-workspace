---
name: cw-fanout
description: Multi-ticket parallel dispatch with a monitoring handoff — takes a list of tickets, runs pre-flight per ticket, enqueues the batch, starts the cw dispatch loop, then watches the wave via the queue-peek ladder and the session.needs_attention / session.timed_out event bus, closes gate tickets inline via /cw-followup without hand-rolling, and drives the wave to terminal in one orchestrated motion. Use when the user wants to enqueue several tickets and then monitor the queue in one motion. Triggers on "fan out these tickets", "dispatch a wave", "enqueue these and watch them", "run /auto-dev on N tickets and monitor", "dispatch and babysit the queue".
---

# cw-fanout

Carries a batch of tickets from **"enqueue these"** to **"now monitoring the
queue"** in one orchestrated motion. It is the N-way companion to
`/cw-smoke-test` (which does the same enqueue → dispatch → monitor arc for a
single ticket, one tick) and the active driver that sits in front of
`/cw-queue-peek` (the in-flight health ladder).

Pure orchestration — it composes existing pieces and adds no new parsing or
validation logic:

- **pre-flight** reuses `cw preflight <T> -c <C>` (per ticket).
- **dispatch** uses `cw dev-queue add` + `cw dev-queue run`.
- **wave lifecycle** uses the bundled `scripts/wave_status.py` (is the batch done?).
- **in-flight health** uses `cw queue peek` (WAIT/PEEK/STOP).
- **attention** uses the operator channel (`task.transition` /
  `session.needs_attention` pushes), falling back to the `session.needs_attention`
  / `session.timed_out` event bus when the channel is unavailable.

## When to use

- There are several ready tickets and wants them dispatched in parallel,
  then watched as a single wave.
- A meta-test spray across N tech-debt tickets where each should run `/auto-dev`
  headless and surface to the orchestrator when they need attention (the human
  only for a genuine product/scope fork).
- Re-dispatching at N>1 after `/cw-followup` has prepared the ground (decisions
  appended, branches rebased).

Do **not** use this skill for:

- Several tickets that are one design or share a merge order — that is a
  *swing*: one parent ticket with the children inlined (`/graduate-plan` swing
  track), enqueued as one row. Fanning those out produces N workers re-deriving
  one design and a last PR nobody can review as a system.
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

Run the built-in command once per ticket, each as its own Bash call (it is the
single source of truth for readiness; never re-implement it):

```bash
cw preflight <T1> -c <CLIENT>
```

```bash
cw preflight <T2> -c <CLIENT>
```

`-c` is required (there is no default client). The repo is derived from the
client's resolved repo root (#2158) — omit `--repo` by default, and pass
`--repo OWNER/NAME` only when that automatic derivation fails (e.g. no
resolvable GitHub `origin` remote on the client's repo).

Each call emits one JSON object with `ok` (bool) and `checks`. Aggregate into a
table: one row per ticket, `ok` plus any failing hard checks. Then:

- **Default:** drop tickets whose `ok` is false from the wave. Print the dropped
  rows with their failing checks. Continue with the survivors.
- **`--skip-preflight`:** keep every ticket; print a warning banner listing the
  failed checks so the wave's degraded launch is on record.
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

**Primary: the one `cw orchestrate watch` Monitor.** Arm a single Monitor on
`cw orchestrate watch -c <CLIENT> --all-lanes` (or `--ticket <T>` per wave
ticket when you deliberately scope it), exactly as `/orchestrate-sprint`
Phase 4 describes; if an orchestrator session already armed one whose scope
covers the wave, reuse it and never arm a second. It prints one line per
event that needs you, ending in `| next: <cmd>`, and nothing while the wave
is healthy. Each line is what drives Step 4b below. Never use ScheduleWakeup,
cron or sleep polling on top of it. `wave_status.py` (4a) is still the
terminal-state confirmation for the whole batch; the Monitor is what tells
you *when* to re-check it.

If the session is subscribed to the `cw-operator` channel instead (see the
[operator channel](../../../docs/operator-channel.md) doc for wiring), its
`<channel source="cw-operator">` pushes play the same role: terminal
`task.transition` pushes (read `disposition` off the payload; a `cancelled`
transition always carries `disposition: null`, so handle it as its own case)
and `session.needs_attention` pushes. Use one signal source, not both.

**a. Wave lifecycle — am I done?**

```bash
python3 .claude/skills/cw-fanout/scripts/wave_status.py <T1> <T2> <T3> \
  --client <CLIENT> --json
```

Exit 0 / `"terminal": true` when every wave ticket is terminal
(`completed` / `failed` / `cancelled` / `blocked_on_user`, or removed from the
queue). `"in_flight"` lists the tickets still `pending`/`running`;
`"needs_attention"` lists `blocked_on_user` tickets (a session paused for the
operator).

The monitoring loop continues until `wave_status.py` reports `terminal=true AND
needs_attention is empty`. A `terminal=true` report with non-empty
`needs_attention` does NOT exit the loop — it means all remaining in-flight
tickets are `blocked_on_user` for gate resolution. Proceed immediately to Step
4b to close those gates, then re-evaluate.

The loop control logic is:

```
LOOP:
  report = wave_status.py <T1> <T2> ... --client <C> --json   # <WAVE> = the full ticket set
  if report.terminal and report.needs_attention is empty:
    EXIT LOOP → proceed to Step 5
  for each ticket in report.needs_attention:
    classify and close gate (Step 4b)
  tail event bus for new attention signals (Step 4b)
  run peek-stop ladder (Step 4c)
  wait for the next Monitor line
```

**b. Attention signals — does anything need me now?**

With the Monitor armed (the primary path above), attention signals arrive as
watch lines and drive straight into the gate-closure logic below — no polling
needed. If neither the watch nor the channel is available (see the Fallback
below), tail the event bus with a durable consumer cursor instead, at
checkpoints (it resumes where it left off):

```bash
cw event tail --since fanout-mon \
  --type session.needs_attention \
  --type session.timed_out \
  --type session.completed --json
```

**On `session.needs_attention`**: detect the gate type from the ticket's
disposition field, then close it inline. Run:

```bash
cw dev-queue tasks --ticket <T> -c <CLIENT> --json
```

and read `disposition` from the first row of the printed JSON (treat a missing
or null `disposition` as unknown). Call that value the gate.

If the gate is one of `plan_pending_approval`, `review_pending_approval`,
`ambiguities_pending_resolution`, `premises_pending_verification`:

1. Run `/cw-followup --ticket-id <T>` inline. It resolves the gate against
   the ticket's sources of truth and reaches the operator only for a genuine
   product or scope question.
2. Check if the ticket was removed from the queue (do this before any
   approve/requeue attempt to avoid spurious errors):
   ```bash
   cw dev-queue tasks --ticket <T> -c <CLIENT> --json
   ```
   If it prints an empty array `[]` (ticket no longer in queue): print
   `gate abandoned: #<T> (<GATE>) → operator removed ticket; wave continues`
   and skip sub-step 5 below (no re-dispatch needed).
3. Re-check status after followup: run the same
   `cw dev-queue tasks --ticket <T> -c <CLIENT> --json` and read `status` from
   the first row of the printed JSON.
4. If that status is still `blocked_on_user`:
   - `plan_pending_approval` → `cw dev-queue approve <T> -c <CLIENT>` when
     the plan stays within the ticket's agreed scope. Approve binds the
     draft's fingerprint to the row. An unreviewed plan goes back to PLAN as
     PENDING (#968 same-stage requeue) and runs Plan Quality Review
     there; a reviewed one advances to impl. Do NOT post a prose approval
     comment: it is not plan-approval evidence, and the ticket re-parks.
     Before approving a Large plan, grep its text for the literal strings the
     resolutions mandated — a `[SATISFIED]` conformance row is a claim, not
     evidence (#929 round 1 shipped one with the mandated field list absent).
     A plan that grows scope beyond the ticket: adjudicate against the
     ticket's sources of truth (trim, split, or approve with the citation);
     only a genuine product/scope fork goes to the human.
   - `review_pending_approval` →
     `cw dev-queue approve <T> -c <CLIENT>` (this arm is sound: review →
     finalize; if session not found, fall back to
     `cw dev-queue requeue <T> -c <CLIENT>`)
   - `ambiguities_pending_resolution` / `premises_pending_verification` at
     the plan stage → save the answers with the Write tool and run
     `cw dev-queue resolve <T> -c <CLIENT> --file <decisions.md>`: it records
     them on the row as binding resolutions and requeues it, with no tracker
     comment needed. At any other stage, answer **as an issue COMMENT** (the
     scanner's response-check is blind to body edits — #980) while ALSO
     keeping the binding resolutions folded in the issue BODY under its
     single `auto-dev-preflight-resolutions` marker, then
     `cw dev-queue requeue <T> -c <CLIENT>`
5. On successful re-dispatch: print
   `gate closed: #<T> (<GATE>) → re-dispatched, watching` — resume the loop.

If the gate is non-gate / unknown, or `session.timed_out` fires without a
recognized gate disposition: triage it yourself right away. Read the blocker
and the session's transcript tail / `branch_state`, then act per
`/cw-followup` (requeue, approve, regress, `cw spawn close`, file the
follow-up, or close the ticket), each destructive act only after the evidence
`/cw-followup` and `/cw-queue-peek` name for it (`cw spawn close
--confirmed-dead <id>` only for a verified-dead session; a ticket close only
with the satisfying merged PR/commit or the superseding ticket cited).
Escalate to the human only a genuine product/scope fork. Never re-dispatch
without first reading the blocker.

**Human-kept gates are never closed inline.** A row with status
`AWAITING_OPERATOR_SIGNOFF`, `finalize_gate_held` (a finalize force-hold:
`hold_finalize` / `finalize_gate: manual`), or `scope_hint: large` is a gate
the human opted into. Do not approve, release, or requeue it, in the recognized
gate loop above or in this fallback — notify the human and keep the wave
going. (A large-by-size gate *without* `scope_hint: large` is not one of
these; adjudicate it.)

When the event is `session.timed_out`, check the `branch_state` field:
`"absent_no_merged_pr"` means the worker died before push (an anomaly worth
investigating); absent key means the branch is present or the check was
unavailable (ordinary slow timeout).
See [`session-disposition.md §5a`](../../docs/session-disposition.md#5a-branch-absence-anomaly-on-session_timed_out-808).

Resume the monitoring loop after handling any attention signal.

**b2. Nothing claiming? Check the circuit breaker and held slots.**

If pending tickets sit unclaimed across ticks:
- `cw dev-queue status` lane lines — a `[PAUSED]` marker means the per-lane
  circuit breaker (#875) tripped on consecutive spawn errors (a quota window
  does this). Remedy: `cw lane resume <CLIENT> <lane>` — resume also resets
  the consecutive-error counter. The watch reports a pending ticket on a
  paused lane as a `STUCK` line after `--stuck-after-minutes` (default 15);
  with no watch armed, this check is on you.
- BLOCKED_ON_USER rows **hold lane slots by design** — a parked ticket can
  starve the lane. Requeue or remove it to convert a dead slot into a
  working one.
- `disposition=attempt_cap_blocked` after an environmental outage (quota
  window, hang loop): once the cause is fixed, run
  `cw dev-queue requeue <T> -c <CLIENT> --reset-attempts`. It zeroes the
  unproductive-attempt counter and requeues in one step, with the loop left
  running. Never hand-edit the queue store for this.

**c. In-flight health — is a running session stuck?**

For the `in_flight` tickets, run the peek-stop ladder:

```bash
cw queue peek --client <CLIENT>
```

Follow `/cw-queue-peek`: act on STOP / STOP-OR-PEEK rows (stuck post-PR-merge,
retry loop, approaching the 60-min ceiling) per its ladder, using the close
form and death checks it names (`cw spawn close --confirmed-dead <session_id>`
only for a verified-dead session). Always close the session **before**
`cw dev-queue remove`.

Nothing changed means no output. Emit one CKPT line only when something
changed (the wave shrinks, a ticket flips to needs-attention, a gate closes);
otherwise stay quiet. `cw watch` shows the live board.

### Fallback: Poll Ladder

Use this when neither the `cw orchestrate watch` Monitor nor the operator
channel (`cw queue-channel serve`, wired into `.mcp.json`) is available, or
you need recovery forensics — it's also the `cw event tail` command
referenced in 4b above. Even here, never use ScheduleWakeup or a sleep loop:
re-check at checkpoints (after a gate, after a merge, before you report).

**Monitor LIVENESS, not just queue state.** Queue rows, the roster, and
`wave_status` output can all lag or lie (false parks on live sessions — #976;
rows deleted mid-run with no event — #978; silent worker deaths produce NO
queue transition, ever). The 2026-07-03/05 sprint's queue-transition-only
monitor had to be bumped twice. The wave monitor MUST watch three
signals per ticket:

1. **Queue transitions** — the `wave_status.py` diff (4a above).
2. **Transcript idle-time LIVE/STALE flips** — the elapsed time since the
   newest `*.jsonl`'s **last parsed record** (what `cw queue peek`'s
   `idle_min` reports), not the file's `mtime` — mtime can advance well
   after the last record was written and reads as "live" on a session that
   has gone silent (#1795). STALE when idle time > 15-20 min while the
   ticket is non-terminal (use ~20 min for review stages — reviewer
   subagents are parent-silent for long stretches).
3. **New worktree commits** — `git -C <wt> rev-parse --short HEAD` changes
   (positive progress proof: silence + LIVE = genuinely working).

**Announce initial state when arming the monitor** — a change-only monitor
started against an already-stale world silently baselines the outage and
never fires.

**The liveness decision ladder (before ANY park/requeue action):**
- **Idle time < 2 min** (last transcript record, not mtime) → the session
  is ALIVE regardless of row status. Do NOT requeue (double-spawn, the
  #919 class). Wait for its sentinel; the #918 late-sentinel rescue
  recovers a falsely-parked row.
- **Idle time ≥ 45 min flat** (or two consecutive 20-min stale checks with
  no new records and no queue movement) → dead regardless of a `running`
  row. Adopt-check, then verify death (absent from
  `~/.claude/daemon/roster.json`, transcript flat, no live process — "work is
  done" is not death evidence), then
  `cw spawn close --confirmed-dead <sid>` (flag before the id) and
  requeue/re-add. Still in the roster with a live process → stalled-but-live:
  use `/cw-queue-peek`'s bare-close path or surface the STOP recommendation.
- In between → arm a bounded one-shot deadline check (resume-or-dead),
  don't guess.

### Step 5 — report + disposition

When `wave_status.py` reports terminal, stop the background dispatch loop (if no
other wave is queued behind it), then print a per-ticket disposition table.
Suggest the right follow-up per ticket:

- `completed` → `/cw-validate-result --ticket-id <N>` to confirm what shipped.
- `blocked_on_user` (gate-type — gate still open at wave end) → run
  `/cw-followup --ticket-id <N>` yourself to disposition it.
- `blocked_on_user` (non-gate — paused-for-input, user-directed blocked, or
  exited without sentinel) → triage it: read the blocker and transcript tail,
  then requeue / approve / regress / close per the evidence. The dispatch loop
  never blindly retries it; you read the blocker first. Escalate only a
  genuine product/scope fork.
- `failed` / dropped → read the reason and act (requeue, file the follow-up,
  or close); escalate only a genuine product/scope fork.

## Output shape

End with one summary line per wave, caveman-tight:

```
fanout: client=claude-workspace wave=[201,202,203] → 2 shipped, 1 blocked_on_user (#202)
  #202 needs attention → /cw-followup --ticket-id 202
fanout: wave=[210,211] → all shipped; 0 need attention
```

Gate-loop resolution mid-wave looks like:

```
gate closed: #204 (plan_pending_approval) → re-dispatched, watching
gate closed: #205 (ambiguities_pending_resolution) → re-dispatched, watching
fanout: client=claude-workspace wave=[204,205,206] → all shipped; 0 need attention
```

## Failure modes

- **Pre-flight drops the whole wave** — print the failing rows; do not enqueue.
  Re-run with `--skip-preflight` only when every failing check is one whose
  `detail` names a condition `cw doctor` reports as a known-benign warning
  (e.g. stale linkage drift on `cw_backend_healthy`); never when a failing
  check is `ticket_open`, `no_open_pr_for_ticket`, `not_already_queued`,
  `agents_present`, `client_repo_resolved`, or `repo_resolved`. State the
  checks and the benign evidence in the warning banner.
- **Dispatch loop not running** — the watchdog only ticks while `cw dev-queue
  run` is alive; if it was killed, the wave stalls silently. Re-launch
  it (Step 3) and note the gap.
- **A ticket sits `pending` forever** — concurrency cap is saturated by other
  clients, or the freshness gate keeps rejecting it (stale `main`). Surface
  `cw dev-queue status` + `cw doctor`; run `cw dev-queue refresh-all`.
- **Gate-abandon** — the ticket is removed mid-gate (e.g. the issue is closed
  or it is dropped from the queue). The skill prints
  `gate abandoned: #<T> (<GATE>) → operator removed ticket; wave continues` and
  resumes the loop. No re-dispatch is attempted.
- **Approve-session-not-found** — `cw dev-queue approve <T>` raises
  `ApproveGateError` when the target session is no longer in the daemon roster
  (e.g. the session died before the approval). Fall back to
  `cw dev-queue requeue <T>` to re-enter the ticket via a fresh session. On a
  `plan_pending_approval` gate that requeue records no approval, so the plan
  stage re-parks once; the `auto_adopt_clean_plan` gate recipe then releases
  that fresh park on the next reconcile tick (unless it touches a forbidden
  area or carries `scope_hint: large`, which is an opted-in gate).
- **`needs_attention` storm** — many tickets pause at once (often the same
  ambiguity across a batch). For known gate types, the inline loop calls
  `/cw-followup` per gate ticket in sequence — if multiple tickets share the
  same ambiguity, the orchestrator provides the same answer on each invocation.
  For non-gate blocked tickets, disposition one via `/cw-followup`, then
  `cw dev-queue requeue <T>` each remaining ticket with the same decision rather
  than answering each separately.

## Out of scope

- Generating or rebasing the per-ticket branches — `/cw-followup` owns that.
- Mutating the sentinel schema or the dispatch internals.
- Continuous standing-queue operation — this skill drives one wave to terminal;
  for an always-on queue, leave `cw dev-queue run` up and watch `cw watch`.

## Related

- `cw orchestrate watch` under one Monitor — primary attention-signal source
  for Step 4 monitoring (`/orchestrate-sprint` Phase 4).
- [operator channel](../../../docs/operator-channel.md) — the alternative
  push source; the `cw event tail` bus is the fallback when neither is up.
- `/cw-smoke-test` — single-ticket end-to-end dogfood (the N=1 form of this).
- `/cw-queue-peek` — in-flight WAIT/PEEK/STOP ladder (Step 4c).
- `/cw-validate-result` — forensic read on a finished ticket (Step 5).
- `/cw-followup` — act on a finished/blocked ticket's sentinel (Step 4b inline, Step 5).
- `cw dev-queue approve` — releases a scope-gated ticket (binding the plan
  draft's fingerprint on a plan gate); used in Step 4b gate closure. The
  `auto_adopt_clean_plan` / `auto_approve_clean_review` gate recipes do the
  same automatically on the next reconcile tick for any Large park that
  touches no forbidden area.
- `cw dev-queue requeue` — re-enters a `blocked_on_user` ticket as `pending`;
  used for ambiguity/premises gates and as the approve-session-not-found fallback.
- `cw event tail --type session.needs_attention --type session.timed_out` —
  the durable attention bus this skill watches in Step 4b.
