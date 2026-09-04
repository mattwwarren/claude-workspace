# cw dev-queue Dispatch Runbook

Operator procedure for driving `cw dev-queue` dispatch end-to-end: enqueue,
dispatch, monitor, handle gates, and clean up. For the sentinel/disposition
contract the monitoring step reads, see
[`docs/session-disposition.md`](session-disposition.md). For the full
`AUTO_DEV_RESULT` schema and event taxonomy, see
[`docs/headless-contract.md`](headless-contract.md).

> **This runbook is for cw developers** (the dispatch internals). For the operator-facing
> how-to — driving a sprint with cw from any repo — run **`cw guide`** (a version-matched
> guide bundled with the tool).

---

## 1. Pre-dispatch: fast-forward main

The freshness gate in `dispatch_tick` skips a ticket with:

```
WARN: main behind origin, ticket skipped
```

A **pure-behind** local `main` is auto-fast-forwarded by the gate itself
(`--auto-ff`, on by default on both `run` and `serve`; pass `--no-auto-ff`
to restore the legacy block-only behavior). The gate blocks indefinitely —
until reconciled by hand — on the other freshness states: dirty checkout,
`ahead`/`diverged`, detached or non-main HEAD (see §9). To fix by hand:

```bash
git -C <client-repo> pull --ff-only
```

`cw dev-queue refresh-all` fast-forwards every configured client repo in one
shot (untracked files are ignored — `ff-only` never rewrites the working
tree). It still refuses a repo whose checkout has uncommitted **tracked**
changes or is not on the default branch.

A persistently gated client is no longer fully silent: after
`freshness_block_attention_threshold` (default 5) consecutive blocked ticks,
a `session.needs_attention` event fires with `paused_status:
freshness_block_escalated` (#974) — visible on `cw event tail` and the
operator channel.

---

## 2. Enqueue

```bash
cw dev-queue add <TICKET-ID> [<TICKET-ID> ...] --client <client> \
  [--scope small|large] [--priority <n>] \
  [--lane <lane>] [--signoff operator] [--hold-finalize] \
  [--stage plan|impl|review|finalize]
```

- `-s/--scope` sets `TicketTask.scope_hint` (default `None`). One effect:
  - **Escalate-only approval gating**: at a `plan_pending_approval` /
    `review_pending_approval` sentinel, a `large` hint forces the operator
    approval gate, and a `small` hint is used only when the sentinel omits
    its own tier — a hint can ADD the gate, never remove it (`large` from
    the sentinel always wins).
- `-p/--priority` — higher dispatches sooner.
- (The former `-t/--timeout` wall-clock budget override was removed with the
  process-kill timeouts — see ADR-0014. Sessions are never dispositioned on
  elapsed time; a quiet worker surfaces via the liveness distress signal
  instead.)
- `--lane` — target lane (must be declared for the client; default
  `default`). Move a pending ticket later with `cw dev-queue move <T> -c
  <client> --to <lane>`.
- `--signoff operator` — require an explicit operator signoff before this
  ticket ships (see below).
- `--hold-finalize` — stop this ticket before an unattended finalize (see
  below). A boolean switch, not a value option.
- `--stage plan|impl|review|finalize` — enqueue the ticket directly at a
  given pipeline stage instead of the default `plan` (GitHub #1682). Shares
  the exact same vocabulary and per-client pipeline-membership check as
  `requeue --stage` — see "Requeue at a different stage" (§7) for the
  vocabulary's origin. An unrecognized value fails loudly at parse time (exit
  code 2, no row inserted); a value not declared in the client's pipeline
  raises before the row is written. Closes a recovery gap: a row removed with
  `cw dev-queue remove` can be re-added directly at the stage it was parked
  at, instead of `add` → `cancel` → `requeue --from-cancelled --stage <x>`.

### The operator-signoff gate (RFC 0007 Phase 3, #990)

A ticket with signoff configured parks at the ship checkpoint — the
REVIEW→FINALIZE transition — in queue status `AWAITING_OPERATOR_SIGNOFF`
instead of advancing unattended. Resolution precedence: per-ticket
(`add --signoff operator`) > per-lane (`LaneConfig.signoff`) > global
(`default_signoff` in `orchestrator.yaml`, default `"none"` = no gate).

- Clear the gate with `cw dev-queue approve <T> -c <client>`. Approving a
  REVIEW-stage plan/review gate on a signoff-configured ticket re-routes it
  to `AWAITING_OPERATOR_SIGNOFF` instead of advancing — run `approve` a
  second time to clear it.
- `cw dev-queue wait` exits **4** for a signoff-parked ticket (§4).
- A signoff-parked row is escalation-eligible (§11.2) but is NOT eligible
  for re-dispatch until approved.

### The proactive finalize hold (RFC 0011 A3, #1160)

A ticket with the hold armed stops at the same ship checkpoint — the
REVIEW→FINALIZE transition — but parks in queue status `BLOCKED_ON_USER` with
disposition `finalize_gate_held`, rather than `AWAITING_OPERATOR_SIGNOFF`.
Resolution precedence: per-ticket (`add --hold-finalize`) > per-lane
(`LaneConfig.finalize_gate`) > global (`default_finalize_gate` in
`orchestrator.yaml`, default `"auto"` = no hold).

Use it when you want a ticket to reach a reviewable state and then *stop*,
full stop — as opposed to `--signoff`, which is a second signature slot on a
ticket you already intend to ship.

Precedence, exactly:

- flag on (any config / probe / tier) → **HELD**, including Small-scope
- flag off, lane/global `finalize_gate: manual` → **HELD**, including Small-scope
- flag off, config `auto`/unset, availability probe unavailable → held by the
  RFC 0011 A5 probe's own mechanism, not this one
- flag off, config `auto`/unset, probe available → **unchanged** (Large blocks
  at the approval/signoff gate as today; Small auto-advances as today)
- hold armed **and** `--signoff operator` set → a single park,
  `BLOCKED_ON_USER` / `finalize_gate_held`; the hold wins outright and the row
  is never double-parked

Operating notes:

- Release via `cw dev-queue approve <T> -c <client>`. That is the only command
  that clears it — a *human* approve is permitted to release the hold, while
  any automatic approve (the RFC 0009 auto-approve gate recipe) declines,
  changes nothing, and emits `gate.auto_approve_held` instead.
- `cw dev-queue drain` will NOT release it: drain deliberately covers only the
  RFC 0011 A1 `awaiting_operator` availability parks (RFC 0011 A4 R11).
- To reject rather than release, `cw dev-queue requeue --stage <earlier>
  --regress` moves the row backward as usual.

### After you enqueue — monitor with the skills, not raw status

If you (operator or agent) add tickets to the queue, **you own watching them to
terminal.** Don't hand-read `cw dev-queue status` att/status columns — they are
pipeline mechanics and misread easily (a rising `att` + a `running → pending`
flip is normal stage advancement, **not** churn; see
[`session-disposition.md §4`](session-disposition.md)).

**Primary: subscribe to the operator channel.** With `cw queue-channel serve`
running and the `cw-operator` MCP server wired into `.mcp.json` (see the
[operator channel](operator-channel.md) doc for the wiring), terminal
`task.transition` pushes (read `disposition` off the payload) and
`session.needs_attention` pushes arrive on `<channel source="cw-operator">` —
drive the wave-watch loop off those pushes and the primary path needs zero
poll turns. A `cancelled` transition also forwards through the channel but
always carries `disposition: null`; handle it as its own case ("cancelled, no
disposition to report"), not an error.

### Fallback: Poll Ladder

Use this ladder when `cw queue-channel serve` is down, `cw-operator` isn't
wired into `.mcp.json`, or you need recovery forensics — see
[§7 "Liveness before state"](#liveness-before-state-2026-07-sprint-lesson) for
why transcript mtime, not queue rows, is authoritative for worker state on
this path. Use the purpose-built monitor skills:

- **In-flight wave watch** → the **`cw-queue-peek`** skill (`cw queue peek
  --client <client> [--json]`). Per running task it parses the last sentinel
  into `stage`/`status` and emits a WAIT / PEEK / STOP recommendation via the
  peek-stop ladder. Do not cancel a worker on a single att bump — confirm the
  ladder says STOP first.
- **Block on one ticket to terminal** → **`cw dev-queue wait`** (§4 below) — the
  sentinel-aware single-ticket monitor.
- **Terminal exit / attention state** → `cw event tail --type
  session.needs_attention --type session.timed_out`, or the **`cw-session-watch`**
  skill for the exit-event classification.
- **Post-mortem the sentinel a finished session produced** → the
  **`cw-validate-result`** skill.
- **`session.timed_out` with `branch_state: absent_no_merged_pr`** — the
  worker died before pushing (or the branch was force-deleted); this is an
  anomaly, not an ordinary slow timeout. Investigate the worker before letting
  it churn through retries. See [`session-disposition.md §5a`](session-disposition.md)
  for the full `branch_state` vocabulary.

If you are scripting a long-running monitor loop, prefer driving it off `cw
queue peek --json` (the recommend ladder) over re-deriving health from raw task
fields.

---

## 3. Dispatch

```bash
# Dispatch all pending tasks (up to the concurrency cap)
cw dev-queue run

# Dispatch one tick and exit
cw dev-queue run --once

# Override concurrency cap for this run
cw dev-queue run --max-parallel <n>
```

The default concurrency cap is `OrchestratorConfig.default_ceiling`
(default: `1`). Per-client overrides live in `per_client_ceiling` in
`orchestrator.yaml`; `max_parallel_clients` additionally limits how many
clients are eligible per tick (default: no limit). The legacy
`default_max_parallel` / `per_client_max_parallel` keys are deprecated
aliases that still load with a one-time warning.

`OrchestratorConfig.host_session_budget` (#1444, default: `null` = off) adds
a further, fleet-wide ceiling: the total number of DAEMON sessions running
concurrently across every client on the host, folded into the per-client
admission math ahead of the per-client cap. A client gated by it (rather
than its own `per_client_ceiling`) gets `skip_reason=host_capacity_gated`
in its `dispatch.tick` event — distinguishable from `cap_full` (that
client's own ceiling) via `cw event tail --type dispatch.tick`. Excess
PENDING work is never killed or rejected by this gate; it simply waits for
a later tick once budget frees up. A stalled session parked
BLOCKED_ON_USER / AWAITING_OPERATOR_SIGNOFF by reconcile does not count
against the budget, so an unresolved "ghost" session cannot permanently
strand a slot.

`OrchestratorConfig.disk_pressure_gate_enabled` (#1887, default: `true`)
adds a claim-time preflight probe of each client's worktree-base mount:
when free space drops below `disk_pressure_min_free_gb` (default `5.0`
GB), that client is held PENDING for the tick with
`skip_reason=disk_pressure_gate` instead of risking a session spawning
onto an already-filling disk.

To dispatch in a planned order rather than raw priority, produce a
DispatchPlan first: `cw dev-queue plan -c <client>` spawns a one-shot
planner session and persists the plan; `run --use-plan` (also on `serve`)
then respects its ordering when claiming tasks.

### Self-healing dispatch (`cw dev-queue serve`)

`cw dev-queue run` exits when the dispatch loop returns or crashes. For
unattended, long-running pipelines use `serve` instead — it wraps `run` in
a supervisor that restarts on crash with exponential backoff:

```bash
# Self-healing dispatch (runs until Ctrl-C or a clean stop)
cw dev-queue serve

# Limit to 5 automatic restarts, then give up
cw dev-queue serve --max-restarts 5

# All run options are also available on serve
cw dev-queue serve --max-parallel 2 --client my-client --quiet
```

Restart behaviour:

- **Clean exit / Ctrl-C**: supervisor exits without restarting.
- **Crash**: supervisor waits (5s → 10s → 20s → 40s → 60s cap) then
  restarts the loop. Backoff resets to 5s after a healthy run (≥ 60s).
- **Crash window cap**: ≥ 5 crashes within 300s → `sys.exit(1)` with a
  CRITICAL log. Use `cw doctor` to investigate the underlying cause.
- `serve` does **not** accept `--once`; use `run --once` for single-tick
  dry-runs. Note that `run --once` **also contends for the single-instance
  lock** below — a dry-run tick is refused while any loop is running.
- **Single-instance lock** (#1362): both `run` and `serve` acquire one
  **global** advisory `fcntl.flock` (`~/.local/share/cw/.dispatch_loop.lock`)
  for their full lifetime — there is no `--client` keying, so exactly one
  dispatch loop runs per machine/state-dir regardless of which client(s) it
  scopes to. A second launch of either command (including `run --once`) fails
  fast, exiting non-zero with a message naming the holder's PID and command,
  e.g. `dispatch loop already running (pid 3212239: cw dev-queue serve) —
  stop it first or use --force`. The lock is advisory and kernel-released:
  a `SIGKILL`ed or crashed loop leaves **no stale lock** (the kernel drops the
  flock when the process's fd table is torn down), so no manual cleanup is
  ever needed.
- **`--force`**: both `run` and `serve` accept a bare `--force` flag that
  bypasses the lock entirely (via `contextlib.nullcontext()`) and logs a
  WARNING on every use. It exists only to override a genuinely wedged or
  foreign holder; running two real loops concurrently re-introduces the
  per-process state divergence (in-memory dedup sets, usage-limit windows)
  the lock exists to prevent, so use it deliberately and sparingly.

### Lane serialization — cli.py and other shared files

Tickets that touch the same file conflict at merge. `cli.py` is the
most common collision point. Identify which pending tickets touch it and
**run them one at a time**: dispatch the first, wait for it to clear, then
enqueue and dispatch the next.

Do not pre-enqueue all tickets and rely on `--max-parallel 1` if you want
strict sequencing — `run --once` claims ALL pending tasks up to the cap
simultaneously. The safe pattern:

```
enqueue ticket A → dispatch → wait for terminal → enqueue ticket B → dispatch
```

### Double-spawn-at-cap

`run --once` claims all pending tasks up to `per_client_max_parallel` in one
tick. If you enqueue N tickets and run once with `--max-parallel N`, all N
spawn simultaneously. To strictly sequence, do not pre-enqueue the next
ticket; wait for the prior task to reach a terminal state before adding the
next.

---

### Reading the status output

For a live, human-readable cockpit use `cw board` (lane × stage panels, with
`--detail` for a session-grouped worktree-contention view) — it is the primary
interactive read surface. `cw dev-queue status` remains the parseable snapshot
for scripting and field reads; it prints two distinct sections:

- **Top table** — live queue state. Each row reflects the current task record
  (PENDING / RUNNING / COMPLETED / FAILED columns) at the moment the command
  runs. This is the authoritative view of what is in the queue right now.

- **"Last dispatch tick per client:" footer** — a historical snapshot from the
  most recent `DISPATCH_TICK` event stored in the event history. It can be
  stale, especially after idle periods or when no dispatch has run since the
  last queue mutation.
  - `running=N/M` reflects that tick's grant math — how many workers were
    granted vs. the cap at dispatch time — **not** the current live session
    count.
  - For current live session count, use the top table or `cw status`.

---

## 4. Monitor

This section covers the **single-ticket blocking wait**. For multi-ticket wave
monitoring use the `cw-queue-peek` skill; for terminal/attention state use `cw
event tail` or the `cw-session-watch` skill (see the breadcrumb under §2).

### 4.0 Two kinds of monitoring — you need both

Every mechanism in this section and in §2 is a **negative** check: it reports a
failure the pipeline noticed and named (`session.needs_attention`,
`session.timed_out`, `reap_proposed`, a non-zero `wait` exit code). That leaves
one gap, and it is the gap most likely to waste an afternoon.

**A worker that spawns and then stalls emits nothing.** It claims the ticket,
registers in the daemon roster, increments `attempts`, reports `RUNNING`, and
then produces no further output. No attention event fires, because nothing
failed in a way the pipeline can name. On a status table it is indistinguishable
from a healthy worker doing slow work.

One case deserves naming, because it is the one most likely to cost you hours:
**a session waiting on a subagent will not page you at all, at any age.**
`session_unresponsive` fires only when there is no terminal sentinel *and* no
pending subagent spawn at the transcript tail (see
[`events.md`](events.md)) — so an outstanding spawn suppresses the distress
signal outright rather than for a bounded window. A fix-loop or review agent
that is dispatched and never reports back therefore leaves its parent silent and
unflagged indefinitely, with the queue row still reading `RUNNING`.

That suppression is deliberate: a parent legitimately goes quiet while a
subagent works, and paging on it would be noise. The gap is that it has no upper
bound. Until it does, `peek` is the only thing that will show you.

The **positive** check is `cw queue peek`:

```bash
cw queue peek --client <client>
```

Read `idle_m` (minutes since the session's last transcript **record**) against
`age_m` (minutes since spawn):

- `idle_m` well below `age_m` → the worker is producing output. Healthy.
- `idle_m ≈ age_m` → it has done nothing since spawning. This is the signature
  of a stalled or dead worker regardless of what the queue row says.
- Peek renders its own `WAIT` / `STOP-OR-PEEK` / `STOP` recommendation from
  these, plus branch and PR state.

Run it at checkpoints — after a gate, after a merge, before reporting that a
wave is healthy — not on a timer. Event-driven monitoring covers the named
failures; peek covers the unnamed one.

This has landed (#2004): the attention monitor (`scripts/attention_monitor.sh`,
see the `orchestrate-sprint` skill's Phase 4) now subscribes to
`session.liveness_changed`, filtered to `stale_30m`/`stale_45m`, so a stalled
worker pages you once the reconcile liveness sweep crosses one of those
bucket boundaries. `cw queue peek` remains valuable as a **confirmation**
step — and it still covers the narrower residual window before the first
`stale_30m` crossing — but it is no longer the only line of defense.

> **Do not hand-roll a liveness check.** `peek` already computes `idle_m`
> correctly from parsed transcript records. Substitutes based on file mtimes
> are wrong in two ways that both fail *silently*: a file's `mtime` can advance
> after its last record was written (so a silent session reads as live — see
> #1795), and `find -newermt` parses its argument in **local** time, so passing
> a UTC timestamp on a non-UTC host produces a cutoff hours in the future that
> matches nothing and yields a confident, false all-clear.

If you are driving the event bus directly rather than through a skill, note
that `cw event tail --lane <lane>` scopes the stream to that lane and drops
every other lane's events. That is intended for two operators sharing a client;
if you are the only one watching, omit it — a lane filter can only lose you
events.

Primary wave-level monitoring is now subscribe-first — see §2's operator
channel flow; this section is the single-ticket blocking-wait contract.

Use `cw dev-queue wait` — the sentinel-aware monitor (#535):

```bash
cw dev-queue wait <TICKET-ID> --client <client> --timeout <seconds> --json
```

Exit codes:

- `0` — `shipped` / `no_op` (or `COMPLETED` queue status)
- `1` — `scope_exceeded` / `forbidden_area` / `validation_failed` / `failed`
  dispositions, or `FAILED` / `CANCELLED` queue status
- `2` — `blocked` / any `*_pending_*` status / `BLOCKED_ON_USER` **not**
  caused by a reap proposal
- `3` — ATTENTION: transcript stale past the wait command's fixed 900 s
  reporting threshold AND worker not in daemon roster (roster absence is the
  evidence; the threshold only debounces the report — nothing is
  dispositioned); a mid-wait reap confirmed by `reap_proposed_at` on the prior
  session (#542, hardened #1557 — a bare `session_id` clear during a normal
  stage handoff no longer fires this); or a `BLOCKED_ON_USER` park that
  originated from a reap proposal (`reap_proposed_at` set on the owning
  session)
- `4` — `AWAITING_OPERATOR_SIGNOFF`: ticket parked for an explicit operator
  signoff before it ships (§2; clear with `cw dev-queue approve`)
- `124` — hard timeout ceiling reached with no terminal or attention signal

With `--json`, the output is:

```json
{
  "ticket_id": "<id>",
  "client": "<client>",
  "status": "<queue-task-status>",
  "session_id": "<session-id>",
  "state": "terminal|attention|timeout",
  "sentinel_status": "<AutoDevResult.status or null>",
  "pr_url": "<url or null>"
}
```

`sentinel_status` is non-null only when the sentinel was read directly from
the transcript (i.e., the sentinel fired before reconcile updated queue
status). `state=terminal` from a queue-status fast-path leaves
`sentinel_status: null`.

**Mid-wait reap handling (#542, hardened #1557):** the wait loop tracks the
first non-None `session_id` it observes; if a later poll sees `session_id`
cleared, it does **not** treat that bare transition as proof of a reap — a
normal inter-stage handoff clears `session_id` the same way. It surfaces
ATTENTION (exit `3`) only when the prior session also carries
`reap_proposed_at` (looked up in state on the old session id), confirming
reconcile actually reaped the session and reverted the task. Otherwise (no
evidence, or the old session has been pruned from state entirely) it falls
through to the same spawn-window grace used for fresh spawns and continues
polling toward the `--timeout` ceiling. If wait does return `124`, inspect
`cw dev-queue status` (or `cw board` for a live view) and the transcript
directly (see §6 in [`session-disposition.md`](session-disposition.md)).

---

## 5. Handle gates by sentinel status

| Sentinel status | Action |
|---|---|
| `shipped` | Done. PR is live with auto-merge enabled; CI wait is orchestrator concern. |
| `stage_complete` | No action — ordinary staged advance (one of HARDEN/PLAN/IMPL/REVIEW finished cleanly); dispatch auto-advances the ticket to the next stage. Seeing it parked is abnormal — see §6/§7 recovery. |
| `no_op` | Done. Ticket already satisfied; close as completed. |
| `ambiguities_pending_resolution` | Resolve the ambiguities (post a `Pre-flight Resolutions` comment on the issue), then re-dispatch (`cw dev-queue requeue`). |
| `premises_pending_verification` | Verify the flagged premises, record results on the issue, re-dispatch. |
| `plan_pending_approval` | Only parks for **large** (or unresolved) scope tier — a small-tier plan advances unattended. Read the plan comment, verify it is faithful to the ticket, then `cw dev-queue approve <T> -c <client>`. The approval is recorded on the dev-queue row (`plan_approved_at`) and reaches the re-dispatched plan stage via `queue_metadata` on every tracker; on GitHub you may additionally pass `--post-marker` to post the `<!-- auto-dev-plan-approved -->` audit comment (it is a no-op on Linear, where `gh` cannot reach the ticket). Advances to impl only once the plan is quality-reviewed (both signoff markers present); otherwise `approve` re-queues at plan stage to run Plan Quality Review first (#968). |
| `review_pending_approval` | Only parks for large (or unresolved) tier. Verify the pushed branch diff and gates, then `cw dev-queue approve` to advance to FINALIZE (which creates the PR) — or ship manually (PR + auto-merge) and cancel the task. With signoff configured, `approve` re-routes to `AWAITING_OPERATOR_SIGNOFF`; approve again (§2). |
| `merge_pending` | PR created but CI/merge gate not yet cleared (#899). Not a failure — the task parks with `pr_url` preserved; monitor/merge the PR. Do **not** re-dispatch. |
| `scope_exceeded` | Scope rejection; close the ticket or relax the constraint, then re-dispatch. |
| `forbidden_area` | Forbidden-area rejection; update constraints or reroute. |
| `blocked` | Triage `blocker.reason`. Check `blocker.retry_eligible` and `blocker.recovery_hint`. `blocker.reason: "plan_unreviewable"` / `"plan_unsound"` mean plan review needs human judgment — if the pipeline bounces repeatedly on an intricate ticket, use the **spec-driven subagent escape hatch** (§7) rather than retrying. A `blocked` at FINALIZE with `blocker.reason: "agent_block"` self-heals: dispatch auto-regresses the ticket to IMPL (up to 2 regressions, #770) — no operator action. |
| `merge_gate_blocked` | A prior pipeline PR is still open. Merge or close it, then re-dispatch. |
| `stale_dispatch` | **This** ticket already has an open, unmerged PR from an earlier dispatch (#1862) — distinct from `merge_gate_blocked`, which is about a *different* ticket's PR. The session found it and refused rather than re-implementing work already in review; `blocker.details` names the PR. Land or close that PR, then `cw dev-queue requeue <T> -c <client>`. Re-dispatching first just reproduces the refusal. |

Not a sentinel status but seen in the same `disposition` field:
`stale_dispatch_gate` — the same condition caught *before* any session was
spawned, by the pre-dispatch open-PR gate. Recognizable by
`blocked_reason: "pr_already_open_pre_dispatch"`, an empty `session_id`, and
a `dispatch.tick` event carrying `skip_reason=stale_pr_blocked`. Same
operator action as `stale_dispatch` above. The gate covers PLAN/IMPL-stage
`PENDING` rows only (a REVIEW/FINALIZE ticket legitimately has an open PR)
and fails open on any `gh` error, so it can hold a healthy ticket only if the
PR is genuinely open.

Also not a sentinel status:
`validation_failed` — the sentinel was emitted but malformed. The queue
auto-requeues the ticket to PENDING (clearing `session_id`) until the
attempt cap, then fails it. If it recurs, inspect the raw JSON with
`cw result validate` before re-dispatching.

---

## 5a. Morning drain (`cw dev-queue drain --held`)

RFC 0011 A4 (#1161): a batch sibling of `cw dev-queue requeue` for Rule-5
availability parks — tickets `BLOCKED_ON_USER` with
`disposition=awaiting_operator` because dispatch could not reach the
operator or a dependency (RFC 0011 A1, #1254), not because anything is
actually broken. Resuming these by hand one at a time (`requeue <T> -c
<client>` per ticket) doesn't scale once a client accumulates several
overnight.

```bash
# Preview what would resume, no mutation:
cw dev-queue drain --held --client <client> --dry-run

# Resume every availability-park ticket for a client, at each one's own
# current stage:
cw dev-queue drain --held --client <client>

# Restrict to a single lane:
cw dev-queue drain --held --client <client> --lane <lane>
```

Exit code is `0` iff every selected ticket drained (or the selection was
empty, or `--dry-run`); nonzero iff any ticket failed (e.g. its status
raced away from `BLOCKED_ON_USER` between the drain's selection snapshot
and the per-ticket requeue — every outcome is still echoed individually
before the batch-level failure is raised).

**`drain --held` does NOT release A3 force-holds** (`disposition
=finalize_gate_held`, #1160's proactive stop-before-finalize) — those
gate on caller provenance and only release via `cw dev-queue approve
<ticket> -c <client>`. The morning routine is therefore two commands: this
one for availability parks, `approve` per deliberate force-hold (see §5's
`review_pending_approval` / `plan_pending_approval` rows above).

---

## 6. Cleanup after terminal (ordering matters)

Wrong ordering leaves phantom PENDING orphans. Follow this sequence:

```bash
# 1. Close the live daemon worker. This stops the daemon surface, marks the
#    session COMPLETED, and atomically CANCELs the owning RUNNING queue task
#    so reconcile cannot revert it to PENDING and re-spawn it (#317).
cw spawn close <session-id>

# 2. Remove the dev-queue task
cw dev-queue remove <TICKET-ID> --client <client> --all
```

`cw spawn close` **refuses a session that is already COMPLETED** — do not
`cw done` it first. If the session is already completed (e.g. the worker
exited cleanly), skip straight to `cw dev-queue remove`; use `cw done
<session-name>` only for a session that never had a live daemon surface to
close.

If the task is already in a terminal queue status but wedged (e.g.,
`RUNNING` with no live session), `cw doctor --reap` detects and repairs
common wedge conditions:

- `wedge/task-running-no-session` — reverts task to PENDING.
- `wedge/task-running-completed-session` — reverts task to PENDING.
- `wedge/terminal-sibling-park` — a `BLOCKED_ON_USER` row with
  `disposition=terminal_sibling` (see below) that was never claimed
  (`attempts == 0`, no `session_id`) — cancels the row. Never a PENDING
  revert: the ticket's real row already finished, so there is nothing to
  revert it *to* (reverting it just gets it re-parked `terminal_sibling` on
  the very next reconcile pass).

Run `cw doctor --reap --json` for machine-readable output.

#### `terminal_sibling` duplicate rows (GitHub #2100)

A `disposition=terminal_sibling` `BLOCKED_ON_USER` row is a duplicate:
`park_terminal_sibling_tasks` parks any `PENDING` row whose `(client,
ticket_id)` already has a terminal (`COMPLETED`/`CANCELLED`) sibling. The
usual source is a PR-watcher recipe (e.g. `auto_fix_ci`) re-dispatching a
ticket whose queue row is already terminal; as of #2100 that recipe
requeues the ticket's own row in place instead of minting a fresh one, so a
genuinely new `terminal_sibling` duplicate should be rare going forward —
this section covers cleaning up an existing one.

`cw doctor --reap` auto-cancels the narrow, safe shape above
(never-claimed, no session). A row with claim history (`attempts > 0`) is
left alone — inspect it before deciding, then remove it directly with the
`--status`/`--disposition` selectors on `cw dev-queue remove` (combine them
to target the exact row without `--all`, which would also delete the
ticket's legitimate row):

```bash
cw dev-queue remove <TICKET-ID> --client <client> \
  --status blocked_on_user --disposition terminal_sibling
```

### Bulk cleanup of stale terminal rows (`cw dev-queue prune`)

`cw dev-queue remove` handles one ticket at a time. To retire a backlog of
long-terminal rows in one pass, use `cw dev-queue prune`, which deletes rows
whose `completed_at` (or `created_at`, for rows that never got one — e.g.
CANCELLED) is strictly older than `--older-than` days:

```bash
# Preview only — this is the default; nothing is deleted without --confirm
cw dev-queue prune --client <client>

# Same preview, said out loud
cw dev-queue prune --client <client> --dry-run

# Actually delete
cw dev-queue prune --client <client> --older-than 90 --confirm

# Widen the status set, or cross the tenant boundary
cw dev-queue prune --client <client> --status completed,failed,cancelled --confirm
cw dev-queue prune --all-clients --confirm
```

- **Nothing is deleted unless you pass `--confirm`.** `--dry-run` wins if both
  are given. A `--confirm` run computes its candidate set once under the same
  dev-queue lock the dispatch loop takes, and prints exactly the rows it
  removed — it cannot silently delete more than it reports.
- **`--client` is required unless you pass `--all-clients`.** Prune never
  scopes across every client just because you forgot a flag.
- **RUNNING, BLOCKED_ON_USER, and AWAITING_OPERATOR_SIGNOFF rows are never
  pruned, at any age** — that is live or operator-parked work. Naming one in
  `--status` is an error, not a silent skip. Release those the normal way
  (`drain --held`, `approve`, `requeue`) first.
- **PENDING is opt-in only**: prunable solely when you name `--status pending`
  *and* a single `--client`. It is never in the default status set
  (`completed`) and is refused outright with `--all-clients`.
- Every removed row emits a `task.deleted` event with `reason=operator_prune`,
  so the deletion is auditable in `cw event` history.

This is unrelated to `cw event prune`, which trims the event log itself.

---

## 7. Patterns

### Harden before dispatch

Run `/harden-ticket` on a ticket before the first dispatch to surface
ambiguities and unsound premises upfront. This eliminates the
ambiguity/plan-review whack-a-mole loop that burns dispatch cycles.

#### Resolutions are binding only with the marker

The plan stage does not recognise resolutions by their heading. It greps the
**live-fetched** ticket comments and body for the literal string
`<!-- auto-dev-preflight-resolutions -->`, and only a source carrying that
marker is injected into the plan agent's prompt as a `## Binding Pre-flight
Resolutions` list of constraints. `/harden-ticket` appends the marker
automatically.

**A hand-written resolutions comment without the marker is advisory, not
binding — and nothing reports that.** The comment still reads as authoritative
to a human, and plan agents frequently comply anyway on their own judgment,
which is what makes the omission so hard to notice. Ordinary tickets often
converge fastest by dispatching first and answering the consolidated park, so
the hand-written path is common — append the marker when you use it.

Two consequences worth knowing:

- **Exactly one marker-bearing source per ticket.** Two trip a multi-marker
  gate. When re-resolving on a later round, post one consolidated comment
  restating every prior resolution plus the new ones, and leave the earlier
  unmarked comments as history. A marker-bearing body section is authoritative
  when both channels carry it.
- **"Missing `## Pre-flight Resolution Conformance` section" is a false
  positive when no marker exists.** The plan is explicitly instructed to *omit*
  that section when nothing was injected, so a reviewer flagging its absence is
  applying a rule that does not apply. Check for a marker-bearing source before
  acting on that finding — the reviewer cannot see whether injection happened.

### Settle a review finding for good (#1838)

When a codex review round raises a finding you reject, and the *next* round
re-derives the same finding and re-parks the ticket, post a
`REVIEW-FINDING-DISPOSITIONS` marker comment on the ticket. It is read
mechanically on every subsequent review pass — no LLM interprets it — and is
persisted onto the queue row, so it survives regress, redispatch, and worktree
teardown.

Each key is `"<file>::<normalized summary>"`, where the normalized summary is
the finding's summary lowercased, whitespace-collapsed, with line/position
references stripped and every digit run replaced by `N`
(`cw.review_debt.fingerprint_v1`). Compute it rather than normalizing by hand
— the review comment's `### Debt — recorded, not blocking` section prints only
the summary half of the same fingerprint, not the `file::` prefix:

```bash
uv run python -c 'from cw.review_debt import fingerprint_v1; print("::".join(fingerprint_v1("src/cw/foo.py", "Bug at line 42")))'
# -> src/cw/foo.py::bug
```

```markdown
## Review Finding Dispositions

<!-- REVIEW-FINDING-DISPOSITIONS
{
  "schema_version": 1,
  "dispositions": {
    "src/cw/foo.py::bug here": {
      "outcome": "REJECTED",
      "rationale": "intentional tradeoff, see ADR-0012",
      "recorded_at": "2026-08-16T00:00:00Z"
    }
  }
}
REVIEW-FINDING-DISPOSITIONS -->
```

- `outcome` is `REJECTED` or `ACCEPTED`. Only `REJECTED` suppresses the
  finding; `ACCEPTED` is recorded and shown to the reviewer but changes no gate.
- Changed your mind? Re-post the marker with a later `recorded_at` — newest
  wins per key. Removing the marker does **not** un-settle anything; the ledger
  is forward-only by design.
- A malformed block degrades to "no dispositions" and never fails the review;
  the symptom is the finding re-appearing, which is visible and correctable.
- Because the identity is not evidence-anchored, a suppression does not lapse
  when the code moves. Every suppression therefore prints itself on the review
  comment (`suppressed — rejected: finding … re-adjudicate if the code at this
  location has changed`) and emits
  `review.finding_disposition_suppressed`. If you see one against code that has
  since been rewritten, re-adjudicate it.

Distinct from `cw review check-voided`'s `VOIDED-REVIEW-FINDINGS` record
(#1814), which the Claude-native session mints from your prose and which lapses
as soon as the cited evidence changes. Use this one when you want the decision
to *stick*.

### Spec-driven subagent escape hatch

When the pipeline bounces on an intricate cross-module ticket (repeated
`plan_unreviewable` / `plan_unsound` / `blocked` with `review_blocked`):

1. Produce a resolved spec manually (or from the last plan comment).
2. Spawn a fresh-context, worktree-isolated subagent and hand it the spec
   directly to *execute* — skipping the plan-review gate entirely.
3. Review and ship the result with your normal gate checklist.

This keeps orchestrator context lean and avoids endless pipeline retries on
tickets that require human judgment at the planning stage.

### Liveness before state (2026-07 sprint lesson)

Task rows are authoritative for *queue* state; **transcript mtime is
authoritative for *worker* state**. Rows have been observed parked
`blocked_on_user` while sessions were actively committing (#976), and deleted
outright under a live worker (#978). Before any park/requeue decision, check
the newest `*.jsonl` under `~/.claude/projects/<slug>-dev-<T>/`:

- **< 2 min old** → session ALIVE; do NOT requeue (double-spawn risk, #919
  class). Wait for its sentinel — the #918 rescue recovers a false park.
- **flat ≥ 45 min** → dead regardless of a `running` row. Adopt-check the
  worktree, `cw spawn close <sid> --confirmed-dead`, requeue.
- In between → bounded deadline check; review/plan stages go parent-silent
  for ~20 min during subagent cycles, so a single 20-min gap is not death.

### CANCELLED row recovery (`--from-cancelled`)

`cw spawn close <sid> --confirmed-dead` on a **RUNNING** row transitions that
row to `CANCELLED` — not `BLOCKED_ON_USER`. `cw dev-queue requeue` normally
rejects anything but `BLOCKED_ON_USER`/`AWAITING_OPERATOR_SIGNOFF`, so a
CANCELLED row is otherwise a requeue dead-end (#1018).

**One-command path (#1889):** `cw spawn close --confirmed-dead --requeue
<sid>` folds the close and the requeue into a single invocation — it closes
the session, then (if a `ticket_id` resolves from the session name) requeues
the ticket to PENDING at its current stage, same as the two-step recipe
below. This is the recommended path for the common case: a stranded RUNNING
session whose session name still encodes a resolvable ticket. Prefer the
manual two-step recipe only when you need to close without immediately
requeuing (e.g. inspecting the row first).

Manual two-step recipe: requeue it explicitly with the escape hatch, which
moves it back to PENDING at its current stage and clears
`session_id`/`stage_base_ref`:

```bash
cw dev-queue requeue <T> -c <CLIENT> --from-cancelled
```

§11.1's `cancelled_row_restore` concierge recipe already auto-handles this
when the worktree has committed work ahead of base and `concierge_enabled:
true`. Both the manual `--from-cancelled` flag and `--requeue` above cover
the remaining cases: zero commits ahead of base, a missing/pruned worktree,
or concierge disabled. `--requeue` is safe to pass even when concierge is
enabled and could win the race — its underlying `requeue_ticket()` call
raising `RequeueStateError` is followed by one fresh read; if that read
shows the row already landed on PENDING/RUNNING (the concierge recipe having
resolved it first), `--requeue` treats that as success and no-ops rather
than erroring. See also: Attempt-cap reset (below) — a different terminal
condition (`attempt_cap_blocked` on a parked row), not a CANCELLED row.

**Caveat:** both `--from-cancelled` and `--requeue` accept *any* CANCELLED
row, regardless of why it was cancelled — the row carries no record of
provenance by the time it reaches either flag. If the ticket may have been
deliberately cancelled (e.g. via `cw dev-queue cancel` as a duplicate or
superseded ticket) rather than stranded by `spawn close`, check `cw
dev-queue tasks -t <T> -c <CLIENT>` / the event history before requeuing it.

### FAILED row recovery (`--from-failed`)

A ticket's row can land on `FAILED` even when the underlying session
actually completed clean — the row itself just has no requeue path once
FAILED, since `cw dev-queue requeue` normally only accepts
`BLOCKED_ON_USER`/`AWAITING_OPERATOR_SIGNOFF` (#1190).

Fix: requeue it explicitly with the escape hatch, which moves it back to
PENDING at its current stage and clears `session_id`/`stage_base_ref`
(mirrors `--from-cancelled` above):

```bash
cw dev-queue requeue <T> -c <CLIENT> --from-failed
```

**Caveat:** `--from-failed` accepts *any* FAILED row, regardless of why it
failed — the row carries no record of provenance by the time it reaches this
flag. Check `cw dev-queue tasks -t <T> -c <CLIENT>` / the event history
before requeuing it to confirm the failure isn't a genuine, still-unresolved
defect.

### COMPLETED row recovery (`--from-completed`)

A shipped, `COMPLETED` ticket can later need another pass -- e.g. its merged
PR went conflicting because a sibling ticket in the same wave merged first.
`cw dev-queue requeue` normally only accepts
`BLOCKED_ON_USER`/`AWAITING_OPERATOR_SIGNOFF`, so a COMPLETED row is
otherwise a requeue dead-end short of deleting and re-adding it -- which
would lose its `attempts`/cost history (#2023).

Fix: requeue it explicitly with the escape hatch, which moves it back to
PENDING at its current stage and clears `session_id`/`stage_base_ref` while
preserving the row's attempt/cost history (mirrors `--from-cancelled` /
`--from-failed` above):

```bash
cw dev-queue requeue <T> -c <CLIENT> --from-completed
```

**Caveat:** `--from-completed` accepts *any* COMPLETED row, regardless of why
the recovery is needed -- the row carries no record of provenance by the time
it reaches this flag. Check `cw dev-queue tasks -t <T> -c <CLIENT>` / the
event history before requeuing it.

### Requeue at a different stage (`--stage` / `--regress`)

`cw dev-queue requeue` defaults to re-running the ticket's **current** stage.
`--stage plan|impl|review|finalize` requeues at a forward stage instead; add
`--regress` to allow a **backward** target on a blocked ticket (e.g. a
plan-deviation review exit back to `impl`). `cw dev-queue add --stage`
(§2, GitHub #1682) shares this same `--stage` vocabulary for enqueuing a
*new* row directly at a stage, rather than requeuing an existing one.

#### Review-stage send-back: what actually reaches the worker (#1730)

Posting a send-back comment on the ticket and then requeuing does **not**, on
its own, guarantee the worker reads it. Stage 0 does not re-run between
pipeline stages, so `.cw/context.json`'s cached `comments` array is only as
fresh as the last stage that rewrote it. What each stage does on re-entry:

- `plan` — live-fetches comments and body on every invocation
  (`auto-dev-plan.md`, "Comments and body are live, not cached"). A send-back
  always lands.
- `impl` — live-fetches comments on every invocation and rewrites the cached
  array (`auto-dev-impl.md`, #1794). A send-back always lands.
- `review` — live-fetches comments on every invocation (#1730), on **both**
  execution backends: `claude-native` inlines them into every reviewer prompt
  as Business Context, and `codex` inlines them via
  `cw.codex_review._context._load_operator_comments`. The codex path is
  `github-issues`-only — a `codex` REVIEW backend on any other tracker has no
  in-process fetch op and cannot deliver them.

A requeue that lands at `review` additionally stamps
`TicketTask.pending_operator_comment` when it arrived via `--regress` (or Rule
5a's FINALIZE self-heal). That marker rides into the worker's
`queue_metadata` at spawn and tells the review stage to treat the fetched
comments as a **binding** adjudication input rather than background context;
it is cleared once a REVIEW-stage spawn has consumed it, so it survives an
intervening IMPL spawn.

A review-stage requeue that cannot deliver comments degrades loudly
(`requeue.review_delivery_degraded` event, carrying `reason` / `backend` /
`tracker`, **forwarded to the operator channel by default** — #1730) and
proceeds — it **does not block the transition**. This is deliberate: `impl`
hard-exits on a missing plan while `review` and `finalize` degrade, and a
hard-fail guard here would invert that codified asymmetry (see
`src/cw/dev_queue/requeue.py`'s `_apply_requeue_stage`). Watch for the event
with `cw event tail` after any send-back requeue; if it fires, the reviewer
ran without your comment and its verdict should be read accordingly.

**#1717 coordination.** `pending_operator_comment` is stamped at the shared
`_stage_regress` chokepoint alongside #1794's `regressed_into_stage`. #1717's
`finalize_regress_branch_head` is designed to stamp at the same point; per
the Unified Re-entry Contract on #1730/#1717, each marker is written and
cleared independently, and whichever ticket lands second owns the compose
test. Both markers now exist and coexist at that seam. In one
`_stage_regress` call it stamps `pending_operator_comment` unconditionally
(gate at the consumption site: `dispatch/claim.py` clears it only at a
REVIEW-stage spawn) and, when the regress origin was `FINALIZE`,
`finalize_regress_branch_head` from the pre-clear `stage_base_ref` (#1717's
repeat detector reads-and-clears it at the first REVIEW re-entry,
`dispatch/regress_repeat.py`). Neither write clobbers the other's field and
clearing either leaves the other untouched — the compose case (a re-entry
that is simultaneously a same-branch-head repeat *and* carries a pending
send-back) is covered by `tests/test_dispatch.py`'s
`TestUnifiedReentryContractCompose`.

None of the three per-arrival markers currently survives a spawn that dies
with no sentinel ever emitted — each clears at (or shortly after) the spawn
that consumes it, not at the point the marker was actually acted on. GitHub
#1801 evaluated this gap for `regressed_into_stage` specifically and
accepted it as a documented limitation rather than changing the clear
timing (see the field's comment in `src/cw/models/tasks.py` for the full
reasoning). Whether `pending_operator_comment` and
`finalize_regress_branch_head` share the same exposure is an open,
out-of-scope question if this is ever revisited.

### Attempt-cap reset (environmental burn)

See also: CANCELLED row recovery (above) — a different terminal condition
(a CANCELLED row from `spawn close` on a RUNNING task), not an
attempt-ceiling park.

**Now partially mechanized** — see §11.1's `false_park_requeue` recipe,
which auto-requeues the common `stalled_retry_cap_parked` case when
`concierge_enabled: true`. The manual recipe below is still needed when
concierge is off, or when the row is refused at the attempt ceiling.

A quota window or hang loop (#979) grinds a ticket to
`attempt_cap_blocked` (`attempts` bumps on every claim AND stage transition).

The number the park fired against is the *resolved* attempt ceiling — the
row's lane `attempt_ceiling`, or `global_attempt_ceiling` when the lane sets
none (#1751). Do not read `global_attempt_ceiling` and assume it is what
parked the row: both park events (`dispatch.tick` with
`skip_reason=attempt_cap_blocked`, and the matching
`session.needs_attention`) carry the resolved number on an `attempt_ceiling`
payload field. A lane whose operator answers every park can set
`attempt_ceiling: false` to opt out of the cap entirely — see
`config/CONFIG_REFERENCE.md`.

Reset recipe — the file-edit steps are only safe with ZERO loops alive:

```bash
pkill -f "cw dev-queue run"        # TaskStop on a wrapper does NOT reliably
pgrep -f "cw dev-queue run"        # kill the python child — verify EMPTY
# edit ~/.local/share/cw/dev_queue.json: attempts=0, disposition=null
# re-read the file to verify the write landed
cw dev-queue requeue <T> -c <CLIENT>
cw dev-queue run &                  # restart, verify fresh tick (no [STALE])
```

Editing that file with a loop alive silently loses the edit to a tick's
read-modify-write (observed twice, 2026-07-04).

**`[BLOCKED — codex review, ticket <T>, <N>s]` is not `[STALE]` (#1742).**
When a client's tick line carries that annotation instead, the dispatch loop
is alive and an executor is mid-review; the number is how long *that review*
has been running, not how long since the last tick. It is expected and
non-actionable: **do not restart the loop.** A restart kills the review's
daemon thread mid-flight, and it takes every other client's in-flight review
with it. `cw doctor` suppresses its `loop-liveness` warning for the same
reason. Wait for the annotation to clear; only a tick line still reading
`[STALE — no tick in Ns]` (no marker) means the loop may genuinely be dead.

### Circuit-breaker pause and held slots

- `cw dev-queue status` lane line shows `[PAUSED]` → the #875 per-lane
  breaker tripped on consecutive spawn errors. `cw lane resume <CLIENT>
  <lane>` resumes AND resets the counter. The trip emits a `lane.paused` bus
  event (`cw event tail`) but no push notification — check for it whenever
  pending tickets sit unclaimed. **#1630:** once tripped, a lane with
  stranded PENDING work also pages via a recurring
  `session.needs_attention` (`paused_status=lane_circuit_paused`,
  `session_id=lane:<client>/<lane>`) — immediately on first detection, then
  every `lane_starved_notify_interval_minutes` (default 15) while it stays
  starved, so `cw lane resume` is not the only way to find out.
- `BLOCKED_ON_USER` rows hold lane slots by design; a parked ticket can
  starve its lane. Requeue or remove to free the slot.
- A session wedged in `needs_salvage` with a park marker poisons every
  respawn of its ticket (claim → revert → attempts+1, plus per-tick
  `session.salvage_skipped` noise): `cw spawn close <sid> --confirmed-dead`
  clears it. **Now partially mechanized** — see §11.1's
  `park_marker_poison_clear` recipe (requires `concierge_enabled: true` and
  `consecutive_salvage_skips >= 1` on a confirmed-dead session).
- `disposition: unresolved_subagent_spawn` (#1646) is **not** an ordinary
  `phantom_surface` crash — do not just requeue it. It means the worker died
  or hung with a sub-agent spawn still in flight, so committed work may exist
  behind a verification tail that never ran. **Check the worktree and branch
  before requeueing:** look for commits ahead of the base branch, and for a
  half-finished stage the transcript never reported. Once you know what
  landed, requeue at the right stage (or close the ticket if the work is
  already good). The evidence is durable — `agent_spawn_stamp.unresolved_count`
  in the worktree's `.claude/cw-context.json`, with `last_stamped_at` giving
  the time the spawn began.
- **This one class parks even on a `reap_policy: auto` lane.** That is a
  deliberate, documented exception (see
  [`docs/session-disposition.md` §6b](session-disposition.md)), not a bug in
  your lane config: `auto`'s silent PENDING revert would re-run a session over
  possibly-committed work with nobody looking, which compounds the exact
  failure the stamp exists to catch. Everything else on an `auto` lane keeps
  reverting as configured. The reason is escalation-eligible (§11.2), so a row
  left parked here still pages after 45 minutes; it is deliberately excluded
  from concierge's auto-requeue (§11.1) for the same reason the override
  exists.

### Dispatch-loop staleness page (`dispatch_loop_stale`)

A `session.needs_attention` with `paused_status=dispatch_loop_stale` (#1875)
says: **this client has pending work, its last `dispatch.tick` is older than
90s, and it is not sitting behind a live executor-blocked marker.** It recurs
every `dispatch_stale_notify_interval_minutes` (default 15) until the
condition clears. `breadcrumbs` carries `pending=<n> age_s=<n>`; the client
is in the `client` field (`ticket_id` is null — this signal is client-scoped,
not ticket-scoped).

Triage in this order — the page does *not* tell you which of these it is:

1. **Did the loop process die?** `cw doctor` (its `loop-liveness` check is the
   on-demand form of this exact predicate) plus a process check for the
   `cw dev-queue serve` / `run` process. Dead → restart it:
   `cw dev-queue run`.
2. **Is a scoped `serve` starving this client?** If a loop is running but was
   started as `cw dev-queue serve -c OTHER`, it holds the #1362 singleton
   lock and will never tick this client. Look for the boot-time
   `scoped serve (client=…)` WARNING in that process's log. Fix by restarting
   it unscoped, or rescoping it once the scoped client drains.
3. **Is it legitimately blocked?** A paused lane or a tripped circuit breaker
   halts dispatch without stopping the ticks, so this page usually means the
   loop itself is not running — but a lane paused *and* a loop that exited is
   a real combination. `cw dev-queue status` shows `[PAUSED]` lanes;
   `cw lane resume <CLIENT> <lane>` clears one.

Do **not** treat a recurrence as a new incident: the recurrence cadence is the
signal deliberately re-firing while the condition persists, exactly like the
`lane_circuit_paused` page above. The debounce stamp clears itself the moment
the client ticks again, so the next genuine episode pages immediately.

### Quota walls: probe before pausing

Worker transcripts ending in "You've hit your session limit" are evidence of
a PAST window, not the current one. Before pausing a wave:
`claude -p "Reply with exactly: WORKER-OK" --model <worker-model>` — if it
answers, the wall is gone. During a genuine wall, STOP the dispatch loop
(each tick burns an attempt per pending ticket into the wall).

---

## 8. Related docs

- [`docs/session-disposition.md`](session-disposition.md) — how to read a session's outcome from the transcript sentinel.
- [`docs/headless-contract.md`](headless-contract.md) — `AUTO_DEV_RESULT` schema, status enum, `ReapReason` taxonomy, `queue.session_reaped` bus event.
- [`docs/events.md`](events.md) — event bus reference.

---

## 9. Recovery: worktree leaks (#766) and false-failed runs (#774)

### 9.1 Symptom checklist

> **First diagnostic when the dispatcher is up but nothing dispatches.** If
> `serve`/`run` is alive yet tickets sit `pending` with free slots, check
> `cw dev-queue status` skip_reason **before** suspecting the monitor, the
> daemon roster, or lane caps. A healthy dispatcher that emits a fresh
> per-client tick snapshot (not `[STALE …]`) but claims nothing is almost
> always gated, not hung. The freshness WARN is emitted **once per ticket**
> to the `serve` stdout (invisible under `--quiet` + background redirect) and
> then de-duplicated to silence — so short of the escalation latch (a
> `session.needs_attention` with `paused_status: freshness_block_escalated`
> after 5 consecutive blocked ticks, #974), a persistent block leaves no live
> trace except the `skip_reason` in `cw dev-queue status`. Do not "restart
> the monitor" reflexively; read the skip_reason first. (See #908.)

`cw dev-queue status` shows `pending > 0` but `claimed = 0` across multiple
ticks. The per-client footer reads something like:

```
skip_reason=freshness_gate  claimed=0  running=0
```

This means `dispatch_tick` is refusing to claim tickets because the local
`main` branch of the client repo is not clean/current relative to
`origin/main`. The gate auto-fast-forwards a **pure-behind** main and clears
itself; it does **not** auto-resolve `ahead`/`diverged`/dirty/non-main-HEAD —
those block indefinitely until reconciled by hand. Three root causes — check
in order (§9.2 dirty checkout, §9.3 worker-diverged, §9.3b release/merge
artifacts).

---

### 9.2 Dirty-main-checkout variant (#766)

**What happened.** A `cw dev-queue run` worker wrote files into the main
checkout instead of (or in addition to) its assigned worktree. `git status`
inside the client repo shows modified or staged tracked files whose paths
match an in-flight ticket's scope.

**Diagnose.**

```bash
# 1. Confirm the main checkout is dirty
git -C <client-repo> status

# 2. Identify the in-flight ticket branch that owns those files
git -C <client-repo> diff HEAD origin/dev/<ticket> -- <paths>
```

If step 2 produces **empty diff**, the leaked content is a byte-identical
duplicate of what is already on `origin/dev/<ticket>`. The worker's real
work is safe on the remote branch; the main-checkout copy is droppable.

**Fix.**

```bash
# Stash the leaked files (label for traceability)
git -C <client-repo> stash push -m "#766 leak" -- <paths>

# Verify clean
git -C <client-repo> status
# → nothing to commit, working tree clean

# Verify not diverged
git -C <client-repo> rev-list --left-right --count HEAD...origin/main
# → 0    0   (ahead=0, behind=0)
```

Once main is clean the freshness gate clears on the next tick. Resume the
dispatch loop normally (`cw dev-queue run`).

> **If the diff is non-empty** (leaked files differ from the branch), do NOT
> stash. Surface the discrepancy to the operator before dropping any content
> — the branch may have been overwritten or the wrong ticket branch identified.

---

### 9.3 Clean-but-diverged-main variant (#766)

**What happened.** The worker committed directly onto local `main` instead of
its worktree branch. `git status` is clean but `rev-list` shows local-only
commits:

```bash
git -C <client-repo> rev-list --left-right --count HEAD...origin/main
# → 3    0   (3 ahead, 0 behind — local commits exist that origin/main lacks)
```

**Diagnose.** Confirm the local commits are byte-identical to the ticket
branch — they should not introduce any net-new content:

```bash
git -C <client-repo> diff origin/dev/<ticket> HEAD
# → empty (no diff means local commits are a safe duplicate)
```

**Fix.** This is a destructive reset. It requires explicit operator
approval — do not run it from an automated agent or in auto-mode without
a human sign-off:

```bash
# Explicit approval required before this step
git -C <client-repo> reset --hard origin/main
```

Verify afterwards:

```bash
git -C <client-repo> status
# → nothing to commit, working tree clean

git -C <client-repo> rev-list --left-right --count HEAD...origin/main
# → 0    0
```

The worker's real work is safe on `origin/dev/<ticket>`. Resume the dispatch
loop after the reset.

Related issues: #766 (root cause), #786 (usage-limit re-spawn churn that
amplifies leak frequency), #787 (diff-cover skipped pre-PR).

---

### 9.3b Release/merge-artifact divergence (not a worker leak)

**What happened.** `main` is `diverged` (ahead AND behind) but **no worker
leaked** — the local ahead-commits are release-tooling or merge artifacts: a
local `chore(release): vX.Y.Z` that was superseded by the squash-merged
release PR on origin, plus stray `Merge branch 'main'` commits. There is **no
`origin/dev/<ticket>` to diff against** (the §9.3 test does not apply), so the
worker-leak diagnosis dead-ends here.

**Diagnose.** Confirm the local ahead-commits introduce **no net-new content**
versus origin — i.e. all real work is already on `origin/main`:

```bash
# What origin has that local lacks (the real work you must NOT discard):
git -C <client-repo> log --oneline HEAD..origin/main

# What local adds over origin — should be only release/merge artifacts:
git -C <client-repo> log --oneline origin/main..HEAD

# The content delta local adds over origin. For a safe reset this should show
# ONLY version-bump/changelog churn (or be empty) — never unique feature work:
git -C <client-repo> diff origin/main..HEAD
```

If the only delta is release/changelog churn (or the diff shows local merely
*missing* origin's newer commits), the ahead-commits are droppable.

**Fix.** Same destructive reset as §9.3 — **explicit operator approval
required; auto-mode classifiers will (correctly) block `reset --hard`, so the
operator runs it by hand**:

```bash
# Explicit approval required
git -C <client-repo> reset --hard origin/main
git -C <client-repo> rev-list --left-right --count HEAD...origin/main   # → 0  0
```

**Then RESTART `serve`** so the editable install reloads the now-current `main`
(Python won't hot-reload), and verify a representative merged symbol imports.
Re-running PRs that auto-merge re-introduce a pure-*behind* state, which the
gate auto-fast-forwards — keep local `main` ff-pulled between waves so it never
re-accumulates an `ahead`/`diverged` state. (Recurs every release cycle; the
#974 escalation latch — `freshness_block_escalated` after 5 consecutive
blocked ticks — now surfaces a persistent block proactively.)

---

### 9.4 Manual-finalize for false-failed runs (#774)

**Symptom.** A task is marked `failed` or `no_sentinel` in the queue, but
inspection of the transcript shows the review actually passed. Look for a
`tool_result` block containing:

```json
{
  "status": "review_pending_approval",
  "health": { "recommendation": "PROCEED" },
  "next_actions": ["create_pr"]
}
```

And `origin/dev/<ticket>` has pushed commits (the work is complete).

**Why it happens.** The session was reaped or interrupted after the review
sentinel fired but before the orchestrator processed it. The queue recorded
`failed`/`no_sentinel` from the reap, losing the sentinel result.

**Recovery.**

```bash
# 1. Create the PR manually (the branch already exists on origin)
gh pr create \
  --base main \
  --head dev/<ticket> \
  --title "<ticket title>" \
  --body "Manual finalize — sentinel showed PROCEED (#774)"

# 2. Enable squash auto-merge
gh pr merge <PR-number> --squash --auto

# 3. Once the PR merges, reap the stale queue task
cw dev-queue cancel <ticket> --client <client>
```

Do **not** re-dispatch the ticket. Re-dispatching risks a second worker
picking up an already-complete ticket and hitting the same drift that caused
the false-failure in the first place.

Related issues: #774 (false-failed sentinel), #766 (worktree leak that can
co-occur), #786 (re-spawn churn), #787 (diff-cover skipped pre-PR).

---

### 9.5 Manual PR for a tombstoned finalize-blocked session (#816)

**Symptom.** A task is stuck at `blocked_on_user` with `paused_status:
finalize_blocked` and a `rescue_attempted: true` marker. By this point the
branch is reliably on origin — the post-merge push in `auto-dev-finalize.md`
Step 4c.2 and `/prep-pr`'s own Step 1 push (both added by #1414) land the
branch before the quality-gate window this scenario's transient `gh pr
create` failure (permission error, usage limit, or network blip) occurs in
— and the rescue loop will not retry.

**Diagnose.**

```bash
cw dev-queue status           # task shows BLOCKED_ON_USER
cw session show <session-id>  # last_result contains rescue_attempted: true
                              # (session id from cw dev-queue tasks -t <T>)
```

Verify the branch exists on origin:

```bash
gh pr list --head dev/<ticket-id>
# or
git ls-remote origin dev/<ticket-id>
```

**Recovery.** The branch is preserved; create the PR and enable auto-merge
manually:

```bash
# 1. Create the PR
gh pr create \
  --base main \
  --head dev/<ticket-id> \
  --title "<ticket title>" \
  --body "Manual finalize — rescue_attempted tombstone (#816)"

# 2. Enable squash auto-merge
gh pr merge <PR-number> --squash --auto

# 3. Once the PR merges, retire the stale queue task
cw dev-queue cancel <ticket-id> --client <client>
```

The `rescue_attempted` tombstone prevents duplicate PR creation on every
subsequent reconcile tick. It is NOT automatically cleared — the above manual
steps are the operator self-service reset. Do not re-dispatch the ticket.

Related issues: #812 (finalize-blocked detection), #816 (tombstone hardening).

---

## 10. Push producer: webhook relay (GitHub #930)

`cw_pr_events_server`'s `POST /pr-event` endpoint accepts pushed PR lifecycle
events from GitHub Actions, feeding the exact same
`apply_pr_state_observation` persist/diff/emit path as the poll producer
(`cw.pr_hydrate.hydrate_pr_states`, #929). This is a **latency** optimization
on top of the poll producer, not a replacement for it — see the degradation
contract in §10.4.

### 10.1 Relay tunnel setup (operator infrastructure, not code)

`cw_pr_events_server` binds to `127.0.0.1` by default and is not meant to be
exposed directly to the internet. GitHub Actions runners cannot reach a
`127.0.0.1` endpoint on your machine, so a relay tunnel is required between
the Actions workflow and wherever `cw pr-channel serve` is actually running.
Two options, either works — this repo does not prescribe or automate either:

- **[smee.io](https://smee.io)**: create a channel at smee.io, run
  `smee --url <channel-url> --target http://127.0.0.1:8788/pr-event` on the
  machine running `cw pr-channel serve`, and set the smee channel URL as the
  `CW_PR_EVENTS_RELAY_URL` repo variable.
- **[cloudflared](https://github.com/cloudflare/cloudflared)**: run
  `cloudflared tunnel --url http://127.0.0.1:8788` (quick tunnel, no account
  needed for testing) or a named tunnel for a stable URL, and set the
  resulting `https://*.trycloudflare.com` (or your custom domain) URL as
  `CW_PR_EVENTS_RELAY_URL`.

Either way, the relay's job is just to forward POST bodies unmodified from
the public internet to the local `/pr-event` endpoint — it does not need to
understand the payload shape.

### 10.2 Secret wiring

**Set `CW_PR_EVENTS_HMAC_SECRET` on the server BEFORE provisioning the relay
tunnel from §10.1.** Standing up the tunnel first, even briefly, opens an
unauthenticated internet-facing window (see the blast-radius note below).

Two repo-level settings gate the workflow (`.github/workflows/pr-events.yml`):

- **`CW_PR_EVENTS_RELAY_URL`** (repo **variable**, not secret — it's a URL,
  not sensitive): the relay's public ingress URL from §10.1. The workflow's
  post-event job is entirely skipped (`if: vars.CW_PR_EVENTS_RELAY_URL !=
  ''`) when this is unset — repos that haven't provisioned a relay simply
  keep relying on the poll producer.
- **`CW_PR_EVENTS_HMAC_SECRET`** (repo **secret**): shared secret for
  request signing. Set it identically as:
  - a GitHub Actions repo secret (`gh secret set CW_PR_EVENTS_HMAC_SECRET`),
    consumed by the workflow to compute the `X-Cw-Signature` header, and
  - an environment variable on the machine running `cw pr-channel serve`
    (`export CW_PR_EVENTS_HMAC_SECRET=...` before `serve()` starts), consumed
    by `cw.pr_events_auth.verify_signature` to validate incoming requests.

  If `CW_PR_EVENTS_HMAC_SECRET` is unset server-side, `/pr-event`
  **default-denies unsigned requests with 401** (#1127) and `serve()` logs a
  startup INFO line noting the safe posture. To restore the old open
  behavior (pre-#1127, unsigned requests accepted), the operator must pass
  `cw pr-channel serve --allow-unsigned` explicitly; doing so also flips the
  startup log to a WARNING so the weakened posture is visible, not silent.
  **`--allow-unsigned` never bypasses a configured secret** — when
  `CW_PR_EVENTS_HMAC_SECRET` *is* set, HMAC verification is enforced
  unconditionally regardless of the flag. **Blast radius of running
  `--allow-unsigned` behind a public tunnel**: any POST reaching the relay
  can mutate `pr_state` for *any* `(repo, pr_number)` currently tracked
  across *all* clients in `dev_queue.json`, with no rate limiting or origin
  check beyond JSON shape. Relaying this endpoint over the open internet
  without the secret set (or with `--allow-unsigned`) is not recommended.
- **Fork PRs cannot authenticate.** `pull_request`/`pull_request_review`
  events triggered by a fork-originated PR run with no access to repo
  secrets, so `secrets.CW_PR_EVENTS_HMAC_SECRET` resolves empty and the
  workflow falls through to the unsigned `curl` branch (a `::warning::`
  annotation on the run, nothing louder). This is a known, accepted
  limitation (#930) — not worked around via `pull_request_target`, since
  that would expose secrets to untrusted fork checkout content — because
  this pipeline's tracked PRs are same-repo/bot-originated, never forks.
  The poll producer still covers fork PRs on its own schedule regardless.
  Note the server-side auth posture this hits: when the server *has* a
  secret configured, that unsigned `curl` request already failed closed
  (401) before #1127 — unrelated, pre-existing behavior. When the server has
  **no** secret configured, #1127 means it now also fails closed by default
  (the same 401), whereas before it would have been silently accepted. Either
  way the fork-PR push path is a no-op; the poll producer is what actually
  covers it.

### 10.3 Config

No `cw` config file changes are needed — `OrchestratorConfig` does not gain a
relay-URL field (nothing host-side reads it; the relay URL lives only in the
GitHub Actions repo variable above). The only host-side state is the
`CW_PR_EVENTS_HMAC_SECRET` environment variable read at request-handling
time.

### 10.4 Degradation contract

If the relay tunnel goes down, GitHub Actions runs fail to reach
`/pr-event`, or `CW_PR_EVENTS_RELAY_URL` is simply unset: **no events are
silently lost.** The poll producer (`hydrate_pr_states`, gated by
`pr_hydration_interval_seconds`, default 150s) independently re-derives the
same PR state on its own schedule and emits the same `pr.*` events through
the same `apply_pr_state_observation` chokepoint — push is a latency
optimization on top of an already-complete poll producer, not a dependency
of it. The one caveat: a `COMMENTED` review's `pr.review_received` event
(#930's carve-out — it never mutates `pr_state`, so the poll producer's diff
has nothing to compare against) is push-only; if the relay is down when a
`COMMENTED` review lands, that specific notification is missed, though the
review itself remains visible via `gh pr view`.

---

## 11. Concierge & Watchdog (RFC 0008 capstone, #1015)

Two independent daemon-side additions that mechanize/backstop patterns
previously handled by hand (see §7's "Attempt-cap reset" and "park marker
poisons every respawn" notes above — the concierge partially mechanizes
both).

### 11.1 Mechanical recovery reactor (`cw.reconcile.concierge`)

Runs inside every reconcile tick (both branches of `_reconcile_locked`), but
is **opt-in**: nothing fires unless `concierge_enabled: true` is set in
`orchestrator.yaml` (default `false`), mirroring `reap_policy`'s own
fail-safe default. See `config/CONFIG_REFERENCE.md` for the config surface.

Three recipes, each individually toggleable via `concierge_recoveries`:

1. **`false_park_requeue`** — a row parked `stalled_retry_cap_parked` (or
   with no disposition at all) whose owning session is confirmed dead
   (absent from the daemon roster, transcript flat) is requeued to PENDING
   at its current stage. This is the automated version of §7's "Attempt-cap
   reset" recipe for the common case; the manual recipe (editing
   `dev_queue.json` directly) is still needed for a ceiling-refused row (see
   below) or when `concierge_enabled` is off.
2. **`park_marker_poison_clear`** — a row behind a session whose park marker
   (`silently_idle`/`needs_salvage`) has persisted for
   `consecutive_salvage_skips >= 1` and whose transcript is confirmed dead
   (per-stage-floor 45-minute staleness) is closed and requeued. This is the
   automated version of §7's "a session wedged in `needs_salvage`... poisons
   every respawn" note — `cw spawn close <sid> --confirmed-dead` is no
   longer required by hand for this case.
3. **`cancelled_row_restore`** — a CANCELLED row whose worktree still has
   committed work ahead of its base branch is restored to PENDING, so work
   is never silently lost to a stray cancel.

Both recipe 1 and recipe 2 gate on `unproductive_attempts` being below the
resolved attempt ceiling (the row's lane `attempt_ceiling`, or
`global_attempt_ceiling` when the lane sets none — #1751; a lane with
`attempt_ceiling: false` has no ceiling and is never refused here). At the
ceiling, the row is refused and left parked rather than requeued — that
refusal is itself an escalation-eligible state (see 11.2) for
`stalled_retry_cap_parked` rows, so an operator still gets paged rather than
the ticket silently spinning forever. Every recovery emits a
`concierge.recovered` event (audit-trail only, not forwarded to the operator
channel by default) **before** mutating the row — see `docs/events.md`.

Recipe 1 (`false_park_requeue`) also has an internal churn backoff (#1030):
if a row's *previous* recovery produced a session that died within seconds
of spawn (dead-on-arrival — active lifespan under 2 minutes), the recipe
arms an exponential deferral (5 min → 1 hr cap) before it will consider that
row again, emitting `concierge.recovery_backoff_armed`; the requeue still
happens on every detect, this only defers the *next* cycle. Missing
evidence (no session record, or an unlocatable transcript) never arms the
backoff — a legitimately-stalled row is never penalized, and evidence-less
churn stays covered by the 11.2 escalation latch below.

Recipe 1 additionally **refuses** (not merely defers) a requeue that has
already been proven impossible (#1674). A DAEMON session that dies under the
default `reap_policy: signal_only` never gets its `Session.status` flipped,
so the `.claude/cw-context.json` it left in the worktree keeps failing every
reuse with `HookContextConflictError`. The dispatch claim path records the
blocking session on the row (`hook_context_conflict_session_id`); when the
row's currently-resolved session **is** that session and its status is still
non-terminal, recipe 1 leaves the row parked and emits
`concierge.hook_context_conflict_refused` instead of requeuing — otherwise
every cycle burns another `attempts` increment for a spawn that cannot
succeed. Clear it with `cw spawn close --confirmed-dead <id>`: that flips the
session's status (it never changes its id), which is exactly what makes the
refusal predicate go False on the next concierge cycle — the ticket then
requeues normally with no separate unblock step. A fresh session superseding
the old one by id clears it the same way, and any successful spawn wipes the
recorded id.

### 11.2 Durable escalation latch (`cw.reconcile.escalation`)

Runs **unconditionally** every reconcile tick (not gated by
`concierge_enabled`) — a `TicketTask` sitting in the escalation-eligible set
(disposition ∈ `ambiguities_pending_resolution` / `plan_pending_approval` /
`review_pending_approval` / `stalled_retry_cap_parked` / `silently_idle` /
`idle_stall` / `wall_clock_budget` / `phantom_surface` /
`unresolved_subagent_spawn` / `None` while
`BLOCKED_ON_USER`, or any `AWAITING_OPERATOR_SIGNOFF`/`FAILED` row) for more
than 45 minutes fires one `operator.escalation` event — a single page per
parked episode, not a repeat-every-tick alarm. See `docs/events.md` for the
full formula and `docs/operator-channel.md` for its default-forwarded status.

### 11.3 Mainstream watchdog (`cw watchdog`)

A standalone, one-shot `cw watchdog tick` command meant to run from a
per-user systemd timer (Linux) or launchd agent (macOS) — **independently**
of the `cw dev-queue run` dispatch loop, so an operator still gets paged even
when that loop itself is down. Three checks per tick: (1) the same durable
escalation sweep as 11.2 (fires a desktop notification for anything newly
escalated), (2) a dead-man's-switch on `dispatch.tick` event recency, and
(3) park-marker cycling (a `BLOCKED_ON_USER` row whose session's
`consecutive_salvage_skips` has crossed `salvage_skip_attention_threshold`).
Detections append to `~/.local/share/cw/watchdog.log` (log-on-detection
only) and fire a desktop notification (`notify-send` on Linux, `osascript`
on macOS).

Install/manage the timer/agent:

```bash
cw watchdog install     # writes the unit file(s); prints the activation command
cw watchdog status      # shows whether the unit file(s) are installed
cw watchdog uninstall   # removes the unit file(s)
cw watchdog tick        # run one tick manually (also what the timer/agent invokes)
```

`install` only writes the systemd `.service`/`.timer` files under
`$XDG_CONFIG_HOME/systemd/user/` (falling back to `~/.config`) or the
launchd `.plist` under `~/Library/LaunchAgents/` — it does not itself run
`systemctl`/`launchctl`; run the printed activation command
(`systemctl --user daemon-reload && systemctl --user enable --now
cw-watchdog.timer`, or `launchctl load <plist>`) yourself.
