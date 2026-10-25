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
until the orchestrator reconciles it — on the other freshness states: dirty checkout,
`ahead`/`diverged`, detached or non-main HEAD (see §9). To fix:

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
    the sentinel always wins). `--scope large` is the operator's own "gate
    this ticket" flag: an opted-in human gate (like `--signoff operator` and
    `--hold-finalize`) that the gate recipes never release and the
    orchestrator never approves — it notifies the human (ADR-0020).
- `-p/--priority` — higher dispatches sooner. An already-queued row's
  priority can be edited in place with `cw dev-queue move <T> -c <client>
  --priority <n>` (`--to <lane>`, alias `--lane <lane>`, re-lanes it in the
  same call) — no cancel+requeue needed; it takes effect on the next
  claim evaluation with no other action required.
- (The former `-t/--timeout` wall-clock budget override was removed with the
  process-kill timeouts — see ADR-0014. Sessions are never dispositioned on
  elapsed time; a quiet worker surfaces via the liveness distress signal
  instead.)
- `--lane` — target lane (must be declared for the client; default
  `default`). Move a pending ticket later with `cw dev-queue move <T> -c
  <client> --to <lane>` (`--lane <lane>` is an alias of `--to`).
- `--signoff operator` — require an explicit operator signoff before this
  ticket ships (see below). This is a gate the human opts into, so it stays a
  human's to clear (ADR-0020).
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
- `cw dev-queue wait` exits **4** for a signoff-parked ticket, and for every
  other human-kept gate (a finalize hold, a `scope_hint: large` approval
  gate) (§4).
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
  that clears it — an operator-initiated approve is permitted to release the
  hold (it is a gate the human opted into, so releasing it stays the human's,
  ADR-0020), while any automatic approve (the RFC 0009 auto-approve gate recipe) declines,
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
  sentinel-aware ticket monitor; several tickets with `--any` return on the
  first one to reach an exit state.
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
  - `running=N/M` is session-based (a count of DAEMON sessions in ACTIVE/IDLE
    status against the client's cap) — but it is a snapshot taken **at that
    tick**, not the current live session count as of when you read the
    footer.
  - For current live session count, use the top table or `cw status`.
  - `[ORPHAN? — N session(s) counted against the ceiling vs M RUNNING row(s)]`
    (#2142) appears when that tick counted more sessions than the top table has
    RUNNING rows for the client. The two numbers are independent metrics by
    design — `running` is session-based, because a pre-existing DAEMON session
    is real host load whether or not the queue tracks it — so an excess is not
    itself a contradiction, and ordinary snapshot lag produces it too. It is,
    however, the exact shape an uncorrelated fix-agent session makes: it holds
    a client-ceiling slot that no task row accounts for, so the client reads
    `running=2/2 cap_full` beside a single RUNNING row and stops admitting
    work. The annotation is reporting only and never changes what is admitted;
    run `cw doctor` to see whether a real session is behind it. Since #2524 it
    also names the sessions behind the excess, read from current session
    state: `no occupied row owns: <id> (<ticket>), ...` lists each
    ceiling-counted session that no `RUNNING`/`BLOCKED_ON_USER`/
    `AWAITING_OPERATOR_SIGNOFF` row is bound to, oldest first, up to five with
    a `(+K more)` suffix. A session stranded after its result was already
    routed shows up here (see `wedge/active-routed-result-stranded` below).
    When every counted session is owned, or the state file cannot be read, the
    original generic text is shown.

### Reading one ticket (`cw dev-queue show`, #2691)

`cw dev-queue show <T> [-c <CLIENT>]` prints one row in full: one
`key: value` per line, a fixed key order, every key always present (`-` when
absent), no truncation, ISO-8601 UTC timestamps. A multi-line `details:`
value continues on lines indented two spaces. It ends with `next:` and the
commands that are legal for the row as it stands, each runnable as printed
(`# ...` is a shell comment); `<paths>` and `"<why>"` are the only
placeholders. Without `-c` the one client queueing the ticket is used; a
ticket queued under several clients needs `-c`.

`add`, `move`, `requeue`, `approve` and `cancel` print the same block after
their usual confirmation line (one block per ticket), so the resulting row
is visible without a second command. The echo is best-effort: if the row
cannot be read it prints nothing and the verb's exit code is unchanged.

Fields worth knowing:

- `details:` is the bound session's full `blocker.details`; when the row
  names a session that is no longer in state it reads
  `- (session not found)`.
- `attempt_ceiling:` is the resolved lane-or-global ceiling that
  `unproductive_attempts` counts toward; `none` means the lane disables it.
- `session:` shows the bound session's status, latched liveness bucket and
  whether its daemon worker is in the live roster (`in_roster=unknown;
  roster: unreadable` when the roster file cannot be read).
- `pr:` is the last hydrated PR state; nothing is fetched live, and
  `hydrated: no` means the PR was never hydrated.
- `skip_reason:` (PENDING rows only) says why the row is not dispatching and
  where the answer came from: `source=row` (live: spawn-error backoff, the
  attempt ceiling, or the lane's circuit breaker), `source=event` (the latest
  per-ticket dispatch tick for this client and ticket) or `source=last_tick`
  (the client-wide snapshot, with the lane's counts and occupants). Event
  and snapshot answers carry their age and are marked `stale` past 90s. A
  plain operator lane pause shows as `[lane paused]`.
- `approval:` lists the approvals recorded on the row: the plan approval
  time and bound draft (`approver: not recorded` -- no approval records who
  approved), a must-fix override with its actor and reason, a scope-drift
  grant (files and the head it is bound to; cleared by the next spawn).
  `-` means none is recorded on the row. Tracker-comment approvals are not
  mirrored onto the row, so `-` does not mean the plan is unapproved.
- `other_rows:` counts other rows for the same ticket and client; the one
  shown is the one `wait` and `requeue` bind to.

`cw dev-queue tasks` shows the first of those commands in its trailing `NEXT`
column, without the `cw dev-queue` prefix and the row's ticket and client; a
gate the human opted into reads `HUMAN GATE: approve` (#2692). Every
`approve`, `requeue`, `unblock` and `move` refusal ends with the same
information: the row's status, stage and disposition, why it is parked when
that is known, and `Run: <command>` for the legal next step (see "Which verb
for which park" in §5).

`--json` emits the `TicketView` model (`cw schema show ticket-view`):
`schema_version`, `ticket_id`, `client`, `status`, `stage`, `disposition`,
`blocked_reason`, `blocker_details`, `advisory_note`, `lane`, `priority`,
`scope_hint`, `attempts`, `unproductive_attempts`, `attempt_ceiling` (null =
no ceiling), `session` (null, or `id`/`status`/`liveness_bucket`/
`in_roster`/`attention_state`; `status` null = session no longer in state),
`last_result`, `pr` (`hydrated_at` null = never hydrated), `stage_base_ref`,
`dispatch` (null unless PENDING: `skip_reason`, `source` of
`row`/`event`/`last_tick`/`none`, `as_of`, `stale`, `backoff_until`,
`lane_paused`, `lane_pause` (null unless `lane_paused`: the pause `source`,
`trigger`, `since`, `trip_streak`, `recent_errors`, `auto_resume_at`,
`auto_resume_enabled` and `resume_command`, #2696), `lane_stats`,
`lane_occupants`), `approval` (null = none on
the row; no approver field), `other_rows`, `created_at`, `completed_at`,
`next` (`command`, `purpose`). Compatibility rule: fields may be added
without a version change; renaming or removing one bumps `schema_version`.

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

This has landed (#2004): the attention stream (`cw orchestrate watch`, see
the `orchestrate-sprint` skill's Phase 4) subscribes to
`session.liveness_changed`, filtered to `stale_30m`/`stale_45m`, so a stalled
worker pages you once the reconcile liveness sweep crosses one of those
bucket boundaries. Since #2693 the stream is one persistent, silent process
under a single `Monitor` (re-armed only on the Monitor's 30-minute expiry):
it follows the inbox in process, prints one line per actionable event with
the next command to run (`| next: <cmd>`), and saves its position in the
event-bus cursor store (`events_dir()/cursors/attention-<client>-<hash>.json`)
after each printed line, so a restart or re-arm resumes exactly where it
stopped. Its **first stdout line is the resolved scope** (`WATCH | scope |
client=… | lanes=… | tickets=… | source=… | start=… | cursor=…`): the default
is the caller's focus-pointer lane, else the whole client, and `--all-lanes`
/ `--ticket` widen it. Lane-less events (liveness, phantom revert) are scoped
through the ticket's row lane, and a repeated page is suppressed until a
resolving event (transition, requeue, lane resume, dispatch tick) clears it.
A PENDING row whose blocker will not clear on its own (a tripped breaker with
no auto-resume, a paused lane, the ssh-key or disk-pressure gate) is reported
once as a `STUCK | …` line after `--stuck-after-minutes` (default 15); that
age is counted from the watcher's first sighting and resets at a re-arm, so a
still-stuck row re-pages about once per re-arm. One watcher per scope: a
second start is refused while the recorded owner pid is alive. A recycled pid
cannot be detected, so a stale record after a restart needs `--force`.
Changing the scope at a re-arm starts a new position at `start=now`. `cw
queue peek` remains valuable as a **confirmation** step — and it still covers
the narrower residual window before the first `stale_30m` crossing — but it
is no longer the only line of defense.

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
cw dev-queue wait <TICKET-ID>... --client <client> [--any] [--timeout <seconds>] \
  [--stuck-after-minutes <N>] --json
```

`--timeout` defaults to 21600 s (6 hours); `0` means no limit. Several
ticket ids need `--any` and return as soon as the first one reaches an exit
state (the result document names that ticket). `--stuck-after-minutes`
(default 15) sets when a stuck PENDING ticket exits 5. A long wait should run
as a background task, not a foreground tool call.

Internal hand-offs never exit: a `blocked` result with
`blocker.reason: fix_loop_pending_dispatch`, or a `plan_pending_approval` /
`review_pending_approval` result on a row that is still `RUNNING`, is
dispatch's own routing. The wait keeps polling until the row settles, then
the codes below apply.

Exit codes:

- `0` — `shipped` / `no_op` (or `COMPLETED` queue status)
- `1` — `scope_exceeded` / `forbidden_area` / `validation_failed` / `failed`
  dispositions, or `FAILED` / `CANCELLED` queue status
- `2` — `blocked` / `merge_pending` / `ambiguities_pending_resolution` /
  `premises_pending_verification` / `BLOCKED_ON_USER` **not** caused by a
  reap proposal, once dispatch has parked the row
- `3` — ATTENTION: transcript stale past the wait command's fixed 900 s
  reporting threshold AND worker not in daemon roster (roster absence is the
  evidence; the threshold only debounces the report — nothing is
  dispositioned); a mid-wait reap confirmed by `reap_proposed_at` on the prior
  session (#542, hardened #1557 — a bare `session_id` clear during a normal
  stage handoff no longer fires this); or a `BLOCKED_ON_USER` park that
  originated from a reap proposal (`reap_proposed_at` set on the owning
  session)
- `4` — human gate: the row is held for an action only a human can give —
  `AWAITING_OPERATOR_SIGNOFF` (§2), a finalize hold (`hold_finalize` /
  `finalize_gate: manual`), or a `scope_hint: large` approval gate (cleared
  by the operator's `cw dev-queue approve`)
- `5` — stuck: a `PENDING` ticket whose blocker will not clear on its own (a
  tripped lane breaker with no auto-resume, a paused lane, the ssh-key or
  disk-pressure gate) for `--stuck-after-minutes`, or a recorded plan/review
  approval result dispatch has not routed for 300 s (`handoff_unrouted`,
  usually a dead `cw dev-queue run`). Text mode prints `Stuck: <ticket>
  (<status>, <N>m): <reason> - <detail>` and, when there is one, `Next:
  <command>`
- `124` — hard timeout ceiling reached with no terminal or attention signal

With `--json`, the output is:

```json
{
  "ticket_id": "<id>",
  "client": "<client>",
  "status": "<queue-task-status>",
  "session_id": "<session-id>",
  "state": "terminal|attention|timeout|stuck",
  "sentinel_status": "<AutoDevResult.status or null>",
  "pr_url": "<url or null>"
}
```

A `stuck` document (exit 5) adds `reason` (the machine code: a skip reason
such as `lane_circuit_paused` / `ssh_key_gate` / `disk_pressure_gate`, the
synthetic `lane_paused`, or `handoff_unrouted` — branch on this), `detail`
(display text, do not parse), `age_seconds` (since this wait first saw the
state, not since enqueue) and `next_command` (the `cw lane resume …` remedy
for the two lane-pause codes, otherwise `null`). `status` is `pending`, or
`running` for `handoff_unrouted`, whose `sentinel_status` is the unrouted
result's status.

While a ticket waits `PENDING`, one `WAITING | #<ticket> | pending:
<skip_reason> (<source>, <age>s ago)` line per change (at most one per 30 s)
goes to **stderr**, with the stuck detail appended when the code needs
action; a `RUNNING` row mid hand-off prints `WAITING | #<ticket> | running:
dispatch hand-off <reason> (<n>s)`. stdout stays one result document. A
known limitation: the ticket view prefers a ticket's latest per-ticket
`dispatch.tick` over the client-wide one, so an old per-ticket tick can hide a
fresh client-wide ssh-key or disk-pressure gate until it ages out.

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
| `no_op` | Done. Ticket already satisfied; the orchestrator closes it as completed with a citation (`/cw-followup`). |
| `ambiguities_pending_resolution` | The orchestrator resolves the ambiguities against the ticket's sources of truth (a genuine product/scope fork is batched into one question for the human), writes the answers to a decisions file, and runs `cw dev-queue resolve <T> -c <client> --file <decisions.md>` from the orchestrator session: it records them on the row as binding resolutions and requeues at the plan stage. Posting the printed relay block (or `--post`) is an optional mirror. Fallback: post a `Pre-flight Resolutions` comment on the issue, then `cw dev-queue requeue`. |
| `premises_pending_verification` | The orchestrator verifies the flagged premises and records the results with `cw dev-queue resolve <T> -c <client> --file <decisions.md>`, which requeues at the plan stage. Fallback: record the results on the issue, then re-dispatch. |
| `plan_pending_approval` | Usually released automatically by the `auto_adopt_clean_plan` gate recipe (on by default) unless it touches a forbidden area or carries `scope_hint: large`. If it stays parked: only parks for **large** (or unresolved) scope tier — a small-tier plan advances unattended. For a forbidden-area or recipes-disabled park, the orchestrator reads the plan comment, verifies it is faithful to the ticket, then runs `cw dev-queue approve <T> -c <client>`. A `scope_hint: large` park is the operator's own opted-in human gate: the orchestrator notifies the human and does not approve it. The approval is recorded on the dev-queue row (`plan_approved_at`) and reaches the re-dispatched plan stage via `queue_metadata` on every tracker; on GitHub you may additionally pass `--post-marker` to post an audit-only `<!-- auto-dev-plan-approved: <sha> -->` comment binding the approval to the approved draft's fingerprint (the unbound `<!-- auto-dev-plan-approved -->` when no valid fingerprint is recorded). Approving a changed draft posts a fresh marker; re-approving the same draft does not duplicate it. Nothing reads the marker back as approval evidence — the evidence is the row's `plan_approved_at` and `plan_approved_fingerprint`. The flag is a no-op on Linear, where `gh` cannot reach the ticket. Advances to impl only once the plan is quality-reviewed (both signoff markers present); otherwise `approve` re-queues at plan stage to run Plan Quality Review first (#968). |
| `review_pending_approval` | Usually released automatically by the `auto_approve_clean_review` gate recipe (on by default) unless health is degraded, a forbidden area is touched, or it carries `scope_hint: large`. If it stays parked: only parks for large (or unresolved) tier. For a degraded-health, forbidden-area or recipes-disabled park, the orchestrator verifies the pushed branch diff and gates, then runs `cw dev-queue approve` to advance to FINALIZE (which creates the PR) — or ships it directly (PR + auto-merge, §9.4) and cancels the task. A `scope_hint: large` park is the operator's own opted-in human gate: the orchestrator notifies the human and neither approves nor ships it. With signoff configured, `approve` re-routes to `AWAITING_OPERATOR_SIGNOFF`; approve again (§2). |
| `merge_pending` | PR created but CI/merge gate not yet cleared (#899). Not a failure — the task parks with `pr_url` preserved; monitor/merge the PR. Do **not** re-dispatch. |
| `scope_exceeded` | Scope rejection; the orchestrator relaxes the constraint and re-dispatches, or closes the ticket only when its acceptance criteria are verified satisfied (cite the merged PR/commit in the close comment) or it is verified duplicate/obsolete (cite the superseding ticket). |
| `forbidden_area` | Forbidden-area rejection; the orchestrator updates constraints or reroutes. |
| `blocked` | Triage `blocker.reason`. Check `blocker.retry_eligible` and `blocker.recovery_hint`. `blocker.reason: "plan_unreviewable"` / `"plan_unsound"` mean plan review needs orchestrator judgment — if the pipeline bounces repeatedly on an intricate ticket, use the **spec-driven subagent escape hatch** (§7) rather than retrying. A `blocked` at FINALIZE with `blocker.reason: "agent_block"` self-heals: dispatch auto-regresses the ticket to IMPL (up to 2 regressions, #770) — no operator action. |
| `merge_gate_blocked` | A prior pipeline PR is still open. The orchestrator merges or closes it, then re-dispatches. Each consecutive park on the same gate backs the row off before it can be claimed again: 60 s, doubling to a 900 s cap (#2717). The page's `breadcrumbs` read `occurrence=N next_eligible_at=<ISO>`. `cw dev-queue requeue` bypasses the wait, and under `reap_policy: auto` the Variant B release clears it once the blocking PR is MERGED or CLOSED. The delays are fixed constants, not configurable; set the per-client `gate_block_backoff_enabled` setting to `false` to disable the rollout; the next claim ignores a gate-owned wait already stamped on a row (a usage-limit or spawn-error window still holds). While waiting, `cw dev-queue show` and lane summaries label the row `spawn_error_backoff`. |
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

Also not a sentinel status, and not even a park:
`worktree_occupied` (#2077) — a PENDING ticket's per-ticket worktree is
currently held by a live session or daemon worker, caught either before the
row is claimed (the pre-claim occupancy screen) or, if that screen's check
raced with a `HookContextConflictError` during spawn, released again with no
attempt charged. Recognizable by a `dispatch.tick` event carrying
`skip_reason=worktree_occupied` and, in the second case, by the operator
`emit` line's `OCCUPIED` prefix. Two causes need different handling. If the
occupying session is genuinely working, the condition resolves itself once it
finishes and the session must not be closed (see `cw-queue-peek`'s
row/session-mismatch guidance for how to tell this apart from a genuine
wedge). If the session already routed its result but was never completed (a
Stop with background work still running, #2458; doctor class
`wedge/active-routed-result-stranded`), it never finishes on its own:
`cw dev-queue approve` (plain or `--scope-drift`; it closes after the
approval) and `cw dev-queue requeue` (it closes before the requeue, after
pre-checking it) stop its worker, confirm the worker left the daemon roster,
and close the session (#2517). Any caller of those commands, including an
agent or orchestrator session, does this. If the stop does not take they exit
non-zero and name `claude stop <id>`; an `approve` that fails there has
already approved the ticket, so close the session with
`cw spawn close --confirmed-dead <id>` once the worker is verifiably gone
(absent from `~/.claude/daemon/roster.json`, transcript flat, no live process) instead of
re-running `approve`. A session whose background work is still draining is
left running. Approvals made by unattended paths (the auto-adopt gate recipe,
`drain`, `auto_fix_ci`) do not close it; the doctor class reports the fix.

Also not a sentinel status:
`validation_failed` — the sentinel was emitted but malformed. The queue
auto-requeues the ticket to PENDING (clearing `session_id`) until the
attempt cap, then fails it. If it recurs, inspect the raw JSON with
`cw result validate` before re-dispatching.

### Which verb for which park (#2692)

`approve` releases a gate an operator holds; `requeue` re-runs a stage;
`unblock` clears a salvage park. There is no verb that does whichever fits:
every refusal names the right one for the row (`Run: ...`), and
`cw dev-queue show` lists them all.

- Plan or review approval gate (`plan_pending_approval`,
  `review_pending_approval`, disposition `approval_gate`) without
  `scope_hint: large`: `approve`; `requeue --stage <current>` re-runs it
  instead.
- Human-kept gates — `AWAITING_OPERATOR_SIGNOFF` (`signoff: operator`), a
  finalize hold (`finalize_gate_held`: `hold_finalize` /
  `finalize_gate: manual`), a `scope_hint: large` approval gate: the
  operator runs `approve`; an agent notifies the human. `show` and every
  refusal label the row's only command `operator-only human gate; not for
  an agent to run`, never suggest a `requeue`, and `tasks` shows
  `HUMAN GATE: approve`. A sessionless one is refused by `approve` itself
  until the operator resolves it (#2718).
- `review_health_gate`, `review_artifacts_stale`, `awaiting_operator`,
  `codex_must_fix_mechanically_rejected`: `requeue` (re-runs the stage), or
  `drain --held` for every such park of the client. `approve` never releases
  them.
- `branch_behind_main`: rebase, then `requeue`. Not drained, not approved.
- `ambiguities_pending_resolution` / `premises_pending_verification`:
  at the plan stage, `resolve --file <decisions.md>` (records the answers on
  the row and requeues); otherwise answer or verify on the ticket, then
  `requeue`.
- `merge_gate_blocked` / `stale_dispatch`: merge or close the open PR, then
  `requeue`. A `merge_gate_blocked` (or `prior_pipeline_pr_open`) park also
  backs the row off, 60 s doubling to 900 s per consecutive park (#2717);
  `requeue` bypasses that wait and the `reap_policy: auto` release clears it.
  The delays are not configurable.
- `plan_scope_drift` (IMPL): `approve --scope-drift <paths>`, or `requeue`
  to re-run impl without them.
- Codex MUST_FIX park (`codex_must_fix_findings`):
  `approve --override-must-fix --reason "<why>"`, then
  `requeue --stage finalize` (§7); if the ticket or lane has a finalize
  hold, operator signoff or scope_hint: large, this step is the human's.
- Salvage park (session `reap_reason: SALVAGE_PARKED`): `unblock`, or
  `requeue`. `unblock` applies to salvage parks only.
- `PENDING`: `wait` (a `requeue` is an exit-0 no-op); `RUNNING`: `wait` or
  `cw queue peek`; `COMPLETED`/`FAILED`/`CANCELLED`:
  `requeue --from-completed|--from-failed|--from-cancelled`.

**Row lag.** A worker can record its result before the dispatch loop routes
its completion, so the row still reads `RUNNING` while `cw dev-queue wait`
already reports the park. (For a `plan_pending_approval` /
`review_pending_approval` result, `wait` now keeps waiting until dispatch
routes the row, then exits 2 or 4 — or 5 after 300 s unrouted.) `approve` and `requeue` first route such a row
the way the dispatch loop would (bounded at 10s, one stderr line saying what
was routed); if the completion is not routable yet they refuse as before,
naming `cw dev-queue wait`. When that routing just parked the row at a
human-kept gate, `approve` refuses with `its recorded sentinel was just
routed` and releases nothing: the operator reviews the row and runs a
second `approve`.

---

## 5a. Morning drain (`cw dev-queue drain --held`)

RFC 0011 A4 (#1161): a batch sibling of `cw dev-queue requeue` for Rule-5
availability parks — tickets `BLOCKED_ON_USER` with
`disposition=awaiting_operator` because dispatch could not reach the
operator or a dependency (RFC 0011 A1, #1254), not because anything is
actually broken. Resuming these one at a time (`requeue <T> -c
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

- `wedge/task-running-no-session` — reverts task to PENDING. A RUNNING row
  whose dev-queue stamp failed after its worker launched (#2502) is usually
  not left for this: reconcile adopts it onto its recorded session
  (`task.session_adopted`, #2591) once the worktree's `cw-context.json` ties
  the session to the claim, after which it is an ordinary bound row and no
  longer a wedge. A row reconcile cannot tie, for example because the
  `sessions.json` write also failed, is still reported here.
- `wedge/task-running-completed-session` — reverts task to PENDING.
- `wedge/terminal-sibling-park` — a `BLOCKED_ON_USER` row with
  `disposition=terminal_sibling` (see below) that was never claimed
  (`attempts == 0`, no `session_id`) — cancels the row. Never a PENDING
  revert: the ticket's real row already finished, so there is nothing to
  revert it *to* (reverting it just gets it re-parked `terminal_sibling` on
  the very next reconcile pass).
- `wedge/active-daemon-stale-no-sentinel` (#2078) — an `ACTIVE` session still
  present in the daemon roster with a stale transcript and no terminal
  sentinel (a plain/non-headless spawn whose harness never re-fired the Stop
  hook that would have completed it) — marks the session `COMPLETED`, stops
  the daemon surface, and reverts the owning task to PENDING.
- `wedge/active-null-liveness-orphan` (#2237) — an `ACTIVE`/`IDLE` DAEMON
  session with no daemon surface and no local-process liveness record past its
  spawn grace, holding a ceiling slot no other detector can see — advisory
  only (ADR-0014: no roster/PID/terminal-result evidence backs it). `cw doctor`
  reports it with the exact recovery command; nothing is reaped automatically.
  For an aider or opencode session the recovery is two steps (#2663): stop the
  process named in the `spawn_post_launch_failed` breadcrumb
  (`<backend> pid <pid>`), then run `cw spawn close <id>`; `cw spawn close`
  alone does not stop it. If no page names a pid (the page is best-effort),
  find the worker process in the session's worktree; if none is running, skip
  the stop step. Other backends keep the `cw spawn close <id>`
  command alone. Excludes `CODEX_BACKEND` sessions (owned by `codex_boot`,
  #2285/#2307).
- `wedge/active-routed-result-stranded` (#2524) — an `ACTIVE`/`IDLE` DAEMON
  session whose staged result a #2458 partial route already routed (the
  ticket's row advanced), with no `RUNNING`/`BLOCKED_ON_USER`/
  `AWAITING_OPERATOR_SIGNOFF` row bound to it, its worker still in the daemon
  roster, and a transcript in the 30m/45m liveness bucket. It holds a ceiling
  slot and its worktree. Reconcile has already paged it once
  (`session.needs_attention`, `paused_status=routed_result_session_stranded`).
  One case closes automatically (#2715): when the routed result is `shipped`,
  its own PR is OPEN or MERGED, and the worktree's drain stamp is readable,
  carries this session's id and is past `fix_loop_await_deadline_minutes`,
  reconcile's shipped release closes it on the next tick (same
  `reap_reason`, no queue row touched) before it is ever paged. A stamp
  written before #2715 carries no session id, so such a session stays here
  for you. Everything else is closed only by an operator, under any
  `reap_policy`. A plain
  `cw doctor` reports it; `cw doctor --reap` closes **every** session of this
  class (marks it `COMPLETED` with `reap_reason=routed_result_stranded` and
  stops its worker) and never touches a queue row. To close just one, run
  `cw spawn close --confirmed-dead <id>`; in a non-TTY session, close one
  with `cw doctor --reap --yes --routed-session-id <id>`. Either close only
  after liveness evidence for that session: the routed result is on record, its
  transcript is flat, and no live process is working in its worktree. `cw dev-queue approve` and
  `requeue` also close this session when run for the ticket (see the
  `worktree_occupied` paragraph).

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

## 6a. Answer a paused session (`cw session send`)

A worker that pauses to ask you something parks its row `BLOCKED_ON_USER` and
then sits there. Before #2212 the only way to answer was lossy: close the
session, requeue the ticket, and let a fresh worker rediscover the context you
were about to hand it. `cw session send` collapses that into one command that
preserves the transcript, the worktree, the dev-queue row, and the row's
`BLOCKED_ON_USER` status.

**Find the rows waiting on you.** `cw queue peek` now reports parked rows
alongside running ones, tagged `AWAITING_OPERATOR` with the reason they
parked:

```bash
cw queue peek --client <client>
```

An `AWAITING_OPERATOR` row is waiting on the orchestrator to triage it, not wedged — it carries no
age/idle score and is never listed under "Suggested stops." A `STOP-OR-PEEK`
row is the ambiguous case that still needs your eyes.

**Answer it.**

```bash
# Inline, positionally
cw session send <session-id> "Use the second approach; the first one breaks
the lane cap."

# Inline via the flag (same thing)
cw session send <session-id> --message "Use the second approach."

# Or from a file, for anything longer than a sentence
cw session send <session-id> --message-file ./answer.md
```

Pass exactly one of the positional MESSAGE, `--message` or `--message-file`;
two or none is a usage error and nothing is queued.

**Read what it parked on.** `cw session result <session-id>` prints the
session's `last_result` JSON; `--field PATH` (repeatable, dotted, numeric
segments index lists) prints just one value, e.g.
`cw session result <session-id> --field blocker.reason`. One `--field` prints
the bare value, several print a JSON object keyed by path, and a missing path
exits 1.

The message is appended to the session's durable mailbox first, then a resume
is attempted. The command exits 0 whenever the message was **queued**, which
is the point: a queued message survives whether or not the session could be
woken right now. Exit 1 means nothing was queued at all — unknown session,
terminal session, or bad flags.

**What it does not do.** It does not transition the dev-queue row. The row
stays `BLOCKED_ON_USER`; releasing it is still the separate, explicit action
it always was (`cw dev-queue requeue`, `cw plan approve`, etc.).

**The one case it cannot handle yet.** A session that is genuinely live and
mid-task (`RUNNING` row, `ACTIVE` session) cannot be woken — there is no way
to deliver into a running `claude --bg` turn, per #1889's spike. You will see:

```
Message <id> queued for session <id>.
Warning: not delivered yet — session is live and mid-task; live-session
delivery is deferred to #2255
```

That is the expected outcome, not a failure: the message is on disk and will
be there when the session is next resumed. See ADR-0017 for the full decision
record.

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

### Ship past a codex MUST_FIX park (#2205)

A ticket parked `BLOCKED_ON_USER` with `blocked_reason: codex_must_fix_findings`
has a blocking codex review verdict. When you decide to ship the branch anyway
(a false positive you accept, a finding a follow-up ticket will fix), record
that decision first — `requeue --stage finalize` alone no longer ships it,
because FINALIZE's MUST_FIX Override Verification step re-reads the worktree's
`.claude/review-verdict.json` and parks the row again with the same reason:

```bash
# 1. Record the override: durable on the row, audited as TICKET_APPROVED
#    (actor, reason, reviewed SHA, finding identities). Stamp-only.
uv run cw dev-queue approve <TICKET> -c <CLIENT> --override-must-fix \
  --reason "false positive: X is guarded by Y; follow-up #NNNN"

# 2. Ship it.
uv run cw dev-queue requeue <TICKET> -c <CLIENT> --stage finalize
```

If the ticket or lane has a finalize hold, operator signoff or
`scope_hint: large`, this step is the human's. `requeue --stage finalize`
moves the stage pointer past REVIEW, where those gates are evaluated, so for
such a ticket `cw dev-queue show` and `tasks` list only the override (#2692)
and an agent notifies the human instead of shipping.

The override is bound to the verdict it was given for: its `reviewed_sha` and
the `fingerprint_v1` identity of every MUST_FIX finding on it. FINALIZE honors
it only while the live verdict still has that SHA and finding set **and** HEAD
has not moved past that SHA. A new review round, a new commit, or a regress into
REVIEW (which clears the override) voids it. Record a fresh override for the
new verdict, or fix the findings. `approve --override-must-fix` refuses without
changing anything when the row is not parked for `codex_must_fix_findings`, the
verdict file is missing, unreadable, or owned by another ticket, the verdict is
not blocking, `--reason` is blank, or a MUST_FIX finding has no diff anchor
(`file: "N/A"`, no identity to bind). On a successful ship, the PR body carries
an `## Operator override` section with the actor, reason, reviewed SHA, and the
overridden findings.

**This is not `cw review settle`.** Settle writes a ledger entry that suppresses
a finding in *future* review rounds. It does nothing to a park that already
happened, and a `requeue --stage finalize` runs no new review round. Use the
override to ship past this verdict. Use settle so the next round stops raising
the finding. They can be combined.

### Settle a review finding for good (#1838)

When a codex review round raises a finding you reject, and the *next* round
re-derives the same finding and re-parks the ticket, run `cw review settle` for
it. The decision is recorded on the ticket's cw dev-queue row, which every
subsequent review pass reads first, on every tracker, mechanically (no LLM
interprets it), so it survives regress, redispatch, and worktree teardown. On a
GitHub-tracked ticket the marker it prints is also read back from the thread,
so post it there too.

**A prose adjudication comment does nothing on the codex lane.** There is no
LLM on that path to correlate "these four are settled, don't reopen" to a
specific finding — only a recorded row or a sentinel-bearing comment counts.
Free-text ingestion is a deliberate non-goal here (follow-up F1).

**Hand-authoring the marker is unsupported (#2210).** `cw review settle` is
the only supported producer, because it is the only path that records
provenance and refuses to run inside a dispatch worker. The reader enforces
that: a disposition record is **applied only when it carries the full
provenance set** — the finding's identity, an `actor`, a `recorded_at` that
parses as a UTC instant, a `reviewed_sha`, and a non-empty rationale. A record
missing any of those is ignored (never applied as a suppression, and never
rendered into the reviewer's "previously adjudicated" prompt block), logged at
WARNING, and reported on the review comment under **"Disposition records
refused (no provenance)"** so you can see that something tried to suppress a
finding and was refused. This applies to `ACCEPTED` entries too.

A marker or queue row written before #2210 carries none of that provenance. It
still loads — the fields are optional, so history is not lost — but it is no
longer applied: the finding it used to suppress will re-appear, with the
refusal reported on the comment. Re-settle it with `cw review settle`.

The same goes for a record minted before the key bound the verbatim summary
(#2210 review round 3): its key is the two-part `file::normalized summary`
shape, which fails the identity binding below. It is refused and reported, not
deleted; re-settle the finding with `cw review settle` and post the new marker.
The refused row stays in the ticket's durable ledger and is re-reported on each
review pass until the ticket ends — the fresh record is a separate key and
applies normally.

**A refused record is never written into the ledger and never replaces an
entry that is already there (#2210, round 3).** Refusal used to mean only "not
applied", so a malformed or pasted marker could still overwrite the record of a
finding you had legitimately settled. Now the ledger write itself rejects an
under-provenanced record: it can neither add a key nor evict a valid entry for
the same key, whatever its `recorded_at` says. You will see it as one WARNING in
the review log (`refusing a finding disposition ... ticket=<T>, key=<K>`) and
in the comment's refused-records section; the prior entry keeps applying. Only
`cw review settle` output replaces an existing entry, and only when the new
record is itself valid and no older than the one it replaces.

**Render the marker, don't write it (#2210).** Every blocking codex
review comment now prints a `### Settle a finding` section with one
ready-to-paste JSON payload per blocking finding, carrying the finding's
verbatim `file` and `summary` — which are the record's whole identity — plus
the `reviewed_sha` it was raised against, so nothing needs normalizing or
editing:

```bash
# Save the payload from the review comment to settle.json, then run this
# (the command refuses only inside a dispatch worker; see below):
uv run cw review settle settle.json \
  --reason "intentional tradeoff, see ADR-0012" \
  --ticket "$TICKET" -c <CLIENT> --out marker.md
# That recorded the settle on the ticket's dev-queue row. Verify it:
uv run cw review dispositions "$TICKET" -c <CLIENT>
# marker.md is the human-readable mirror: post it as its own comment
# (GitHub shown; use your tracker's own tool elsewhere).
gh issue comment "$TICKET" --repo "$REPO" --body-file marker.md
```

**`--reason` is mandatory** and must be non-blank — a suppression with no
recorded rationale is the silent silencing this record exists to prevent. It
applies to every entry; an entry's own `rationale` overrides it, so several
findings can be settled for different reasons in one call.

**`cw review settle` refuses to run inside a dispatch worker.** A settled
finding is never re-raised, so the pipeline must not be able to settle its own
reviewer's findings. The command looks for the nearest
`.claude/cw-context.json` (searched upward from cwd), which `cw` stamps for
every session it spawns:

- `headless: true` — a dispatch worker: refuses.
- anything else — `headless: false`, no context file, or a context file that
  cannot be read: proceeds. Not being able to prove the caller is not a worker
  is not a reason to refuse (ADR-0020).

Ahead of that cwd check, a `$TMPDIR` inside a headless worker's worktree
refuses too: every cw executor points a worker's `TMPDIR` there, so a worker
that changes directory is still recognized. A refusal exits non-zero and
writes nothing — no row write, no marker, no `--out` file, no event. There is no bypass
flag. A worker that has a payload hands it to the orchestrator, which runs
the command.

**Who decides what to settle.** The orchestrator (`/cw-followup`) adjudicates
each blocking finding against the ticket's sources of truth: the ticket body,
the approved plan, pre-flight resolutions, recorded operator decisions and
linked RFCs. It settles a finding that asks for out-of-scope or
already-decided work, or that does not reproduce against the code, with
`--reason` citing the source or `file:line`. It requeues a real in-scope
defect into the fix loop, and escalates to the human only a finding that raises a
product question or would grow scope beyond the plan.

**On a GitHub-tracked ticket, post the marker as its own comment, unedited.**
The reader recognises a
disposition record by *position* as well as shape: the block must open the
comment body, directly under its `## Review Finding Dispositions` title, which
is exactly what `--out` writes. A block pasted below a preamble, quoted in a
reply, or embedded in other text is not a record and is silently not applied —
you will see the finding re-appear, with nothing in the refused-records section
(nothing tried to settle anything). This is what stops a reviewer's own finding
text from minting a suppression: marker syntax inside a finding's summary,
file, evidence or contest claim is escaped on render (you will see it as
`REVIEW-FINDING\-DISPOSITIONS`), and would not parse at that position anyway.
On any other tracker the row is the record: post the marker with that
tracker's own tool for human readers, knowing no review pass reads it.

Every record it writes carries provenance: your resolved `gh` login, a UTC
timestamp stamped by the command (`recorded_at` is **rejected** as a payload
key — it is audit data, not input), the verbatim summary, and the reviewed
sha. An entry with no resolvable sha is refused; pass `--reviewed-sha <sha>`
for a hand-written payload. The command exits non-zero rather than recording an
anonymous settle if `gh api user` cannot resolve your identity.

Each settled finding emits one audit event, correlated to `--ticket`
(required): `review.finding_settled` for an `ACCEPTED`/`REJECTED` entry, or
`review.finding_disposition_reverted` for a `REVERSED` one (#2232) — the type
carries the semantic, so `cw event tail --type
review.finding_disposition_reverted --json` answers "what have I withdrawn"
without filtering settles by payload. Duplicate keys collapse newest-wins, so two
payload entries with the same file and byte-identical summary are one entry
and one event. `-` reads the
payload from stdin.

**The events are recorded first, then the row, then the marker.** If any event
cannot be recorded, the settle is refused outright — no row write, no marker on
stdout, no `--out` file, non-zero exit, and a message naming the finding and the
failure. An audit record with no effect is noise; a durable suppression with no
audit record is invisible, so the ordering favours the first. Re-run the same
payload once the event store is healthy.

When the row itself cannot be recorded, the outcome depends on the client's
tracker (`tracking.primary.system` in its `.claude/project-config.yaml`):

- **No dev-queue row for the ticket.** GitHub-tracked client: exit 0, the
  events are recorded, a stderr WARNING says the row was NOT recorded, and the
  marker prints; `cw review dispositions` shows nothing until a review pass
  reads the posted marker. Any other tracker, or an unresolvable one: exit 1
  and nothing is recorded (no event, no marker, no `--out`); enqueue the ticket
  and re-run.
- **The row write fails** (bounded-lock timeout, `OSError`, a row that vanished
  since the pre-flight). GitHub-tracked client: exit 0, events recorded, the
  same WARNING, marker printed. Any other tracker, or an unresolvable one: exit
  1; the events are recorded, but no marker and no `--out` are written; re-run
  once the cause is fixed.
- **It lands on a terminal row.** It records, on every tracker, with the stderr
  note below.

A settle recorded on a TERMINAL row is lost if the ticket is re-enqueued
(`cw dev-queue add` creates a new row and nothing is carried forward). Settle
prints a note when it lands on one; re-run it on the new row. `cw doctor` says
the same thing for a client whose tracker is not GitHub: settles are recorded
in the cw store.

The rendered marker looks like this. Every field below is required for the
record to be *applied* — a block missing any of them parses but is refused:

```markdown
## Review Finding Dispositions

<!-- REVIEW-FINDING-DISPOSITIONS
{
  "schema_version": 1,
  "dispositions": {
    "src/cw/foo.py::bug here::4bffc7a70f587f3e984f1ab5279c1211005e73b789b8a7a27faf53299b44b48a": {
      "outcome": "REJECTED",
      "rationale": "intentional tradeoff, see ADR-0012",
      "recorded_at": "2026-08-16T00:00:00Z",
      "actor": "mattwwarren",
      "reviewed_sha": "deadbee",
      "summary": "Bug here"
    }
  }
}
REVIEW-FINDING-DISPOSITIONS -->
```

**The settle section is capped.** A pass with many MUST_FIX findings prints at
most 10 payloads (and at most 12,000 characters of them); the rest are listed
compactly under the payloads, by file and trimmed summary. Write a payload for
one of those by hand and pass it to
`cw review settle <file> --reason '<why>' --ticket <ticket> -c <client>` — the identity is its
file and the verbatim summary from its MUST_FIX line in the same comment. A
hand-written *payload* is fine; a hand-written *marker* is not, because only
the command records the provenance the reader requires. A payload block is
never cut in half, so anything you
can copy out of the section is complete.

- The key is `"<file>::<normalized summary>::<digest>"`. The first two parts are
  `cw.review_debt.fingerprint_v1`'s: the summary lowercased,
  whitespace-collapsed, line/position references stripped, digit runs replaced
  by `N`. The `<digest>` is the SHA-256 (64 hex characters) of the finding's
  **exact** summary text — the key binds the verbatim summary, and the reader
  refuses a record whose stored `summary` does not hash to its key's digest, so
  a record can only ever apply to the finding it was settled for. The example
  digest above is `sha256("Bug here")`. `cw review settle` mints it from the
  payload's verbatim `summary`, so a pasted payload reproduces the key a
  **byte-identical** re-raise will hit.
- A reviewer's *reworded* re-raise no longer exact-matches: matching
  non-identical text is the claim tier's job, and it is off by default (see the
  claim-match gate in `config/CONFIG_REFERENCE.md`). Until a lane is armed the
  finding blocks again and, when it clears the matcher, is logged as
  `review.finding_claim_shadowed`. Settle the new wording too, or contest it
  with `contests_adjudication`. The reviewed sha is deliberately **not** in the
  key — a later fix commit must not orphan the record — but it is a required
  field of it.
- `outcome` is `REJECTED` or `ACCEPTED`. Only `REJECTED` suppresses the
  finding; `ACCEPTED` is recorded and shown to the reviewer but changes no gate.
- On GitHub the reader unions every marker on the thread, and the row merge is
  additive too. Post only what you are settling now.
- Changed your mind? Re-run `cw review settle` for the same finding with
  `"outcome": "ACCEPTED"` (and, on GitHub, post the new marker) — its `recorded_at` is later,
  and newest wins per key. Removing the marker does **not** un-settle anything;
  the ledger is forward-only by design. There is no per-record rollback command
  yet (tracked as a follow-up); the provenance fields exist so one can target
  exactly one record when it lands.
- A malformed block degrades to "no dispositions" and never fails the review;
  the symptom is the finding re-appearing, which is visible and correctable.
  A *well-formed but under-provenanced* block behaves the same way, and
  additionally names itself in the comment's refused-records section.
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

**Linear and other non-GitHub tickets feed this ledger through the row
(#2701).** The codex path's comment fetch is still GitHub-only, so a marker
posted on a Linear ticket is never read. `cw review settle --ticket <id> -c
<client>` writes the ledger onto the dev-queue row directly, and the review pass
reads the row first on every tracker. Post the marker with the tracker's own
tool for human readers only. Follow-up F5 (reading markers off Linear) is no
longer needed for settles.

#### The reworded re-raise, and the claim tier (#2210)

The exact key above only matches text that normalizes identically. A reviewer
that re-raises the same defect in *different words* produces a different key
and is not suppressed — which is the original complaint this ticket came from.

A second, fuzzy **claim tier** now matches a same-file MUST_FIX against a
ledger entry by shared code symbols and content-word overlap. It ships **off**,
and arming it takes two switches (the orchestrator arms them, with the
measured evidence below, per `docs/release-playbook.md`; flipping either one
back is the rollback):

```yaml
# ~/.claude-workspace/orchestrator.yaml
codex_claim_suppression_enabled: true

# ~/.config/cw/clients.yaml — on the lane, per client
lanes:
  - name: impl
    codex_review_tiers:
      claim_suppression: true
```

Both must be true. A lane that names no tier falls to a hardcoded-off floor,
and a client with no declared lanes gets a synthesised `default` lane — so
arming means declaring the lane first.

**Measure before you arm.** While the gate is closed the tier still runs and
records everything it *would* have suppressed:

```bash
cw event tail --type review.finding_claim_shadowed --json
# also: grep the INFO line "claim-tier match NOT suppressed, gate off"
```

Read those, judge each pair same-defect versus distinct-defect, and arm a lane
only after ~20 clean events on it with no distinct-defect pair among them. One
distinct-defect pair is a reason to tighten the thresholds, not to arm.
`cw event tail` reads only the live inbox; auto-prune **archives** older events
to `events/inbox.<YYYY-MM-DD>.jsonl` rather than deleting them, so read those
files for older ones (or raise `event_inbox_retention_count`). Known accepted
trade-offs — the ledger is severity-blind — are recorded in ADR-0016. Before
arming, also read [Rolling back a settle, and spotting
drift](#rolling-back-a-settle-and-spotting-drift-2232) below: being able to
inspect and undo what is settled, and having drift surfaced rather than
silently suppressed, are **arming preconditions** ADR-0016 names, and the
second of them is now enforced — a lane whose `disposition_drift_check_enabled`
resolves `false` refuses to arm its claim tier at all.

#### Contesting a settled finding (#2210)

A reviewer that believes a settled decision is now wrong sets
`contests_adjudication` on the finding to what changed, quoting the changed
code. A non-blank value removes the finding from the ledger's match set on
**both** tiers, so it stays blocking, and the review comment labels it
`_(contests prior adjudication — …)_`.

Two caveats worth knowing before you trust one:

- It is **unverified and gameable**. Nothing checks that the quoted code
  actually changed; the string is an assertion (follow-up F2). It can only fail
  toward blocking.
- It is guaranteed only on the codex **single-pass** lane and fix-loop cycle 0.
  On later fix-loop cycles `_admit_new_must_fix` does not read the field, so a
  contest on code the latest cycle did not touch is diverted to the debt ledger
  and the contest text is dropped (follow-up F6).

**One thing the pipeline hides from itself:** the `### Settle a finding`
section of a *pipeline-authored* comment (one carrying `<!-- cw-agent-authored
-->`) is stripped before the next reviewer sees the thread — a pre-filled
`"outcome": "REJECTED"` payload must not read to the next reviewer as an
operator decision. A payload **you** paste yourself is not stripped, by design.

#### Rolling back a settle, and spotting drift (#2232)

Two things #2210 left open, and ADR-0016 named as preconditions for ever
arming the claim tier, are closed.

**See what is settled.** `cw review dispositions <ticket>` lists every record
currently bound to a ticket — file, summary, outcome, who settled it, their
stated reason, when, how long ago, and against which sha:

```bash
cw review dispositions GEN-123 --client acme
cw review dispositions GEN-123 --client acme --worktree ~/work/acme --json
```

Every record is listed whatever its outcome, with an explicit OUTCOME column:
a withdrawn settle must never read as a live suppression. Only `REJECTED`
suppresses; `ACCEPTED` is a record-only annotation and `REVERSED` is a
withdrawal. With `--worktree`, the STALE column says whether each record's
file has moved since the record was settled. It reads `?` whenever the
question cannot be answered — no `--worktree`, an unreadable one, or a record
carrying no reviewed sha to compare against; `?` is never "not stale". AGE is
rendered `?` the same way for a record whose `recorded_at` is missing or
unparseable.

This reads the **dev-queue row's** ledger: `cw review settle` writes it
directly (#2701), so a settle recorded through the CLI shows up immediately,
and a marker posted on a GitHub ticket appears once a review pass has read it.
It is not a live fetch of the ticket thread. Same snapshot convention as
`cw dev-queue tasks`.

**Undo one.** Rollback is `cw review settle` with a third outcome, not a new
command. Paste the SAME `file` and `summary` the original settle used (read
them off `cw review dispositions`, or the original marker) with `"outcome":
"REVERSED"`, then post the marker it prints:

```bash
cw review settle --reason "the tradeoff no longer holds: X changed" \
  --ticket GEN-123 -c <CLIENT> --reviewed-sha "$(git rev-parse HEAD)" --out /tmp/reverse.md -
# {"entries": [{"file": "src/cw/foo.py", "summary": "<verbatim>",
#               "outcome": "REVERSED", "reviewed_sha": "<sha>"}]}
# GitHub mirror only; the row already holds the withdrawal
gh issue comment 123 --body-file /tmp/reverse.md
```

The newest-`recorded_at`-wins merge is what makes it durable, so no second
write path exists and every guard `settle` already has — the worker refusal,
the identity resolution, the audit-before-effect ordering — applies unchanged.
A reversed record matches neither tier and is never rendered into the
reviewer's "previously adjudicated" block: a withdrawal is the absence of a
decision, so telling the model one stands would be backwards. It stays visible
in `cw review dispositions`, because reversal history is part of the audit
trail.

A withdrawal is audited wherever it lands. `cw review settle` emits
`review.finding_disposition_reverted` for the record it writes, and the review
pass emits the same event when a `REVERSED` record first reaches the durable
ledger through the ticket thread's marker — so one query answers "what has
been withdrawn" regardless of which path recorded it:

```bash
cw event tail --type review.finding_disposition_reverted --json
```

**Drift is surfaced, not silently suppressed.** On every review pass, a record
that matches a finding is checked for drift: did that file change between the
sha the record was settled against and the sha being reviewed? If it did, the
record is **not applied** for that pass. The finding keeps blocking, the
posted comment reports it under "Settled findings re-raised (the code moved)"
with both shas, and an event records it:

```bash
cw event tail --type review.finding_disposition_stale --json
```

The ledger entry is **not expired** — silent expiry is the invisible act this
whole seam exists to avoid. It stays, and still applies on any pass where its
file has not moved. Diff the two shas, then either re-settle against the
current code or reverse the record.

The check fails toward surfacing: an unresolvable ref (a rebased or
force-pushed branch orphaning the settled sha) or an unreadable repository
reads as drift, so you re-settle rather than suppress against code nobody
adjudicated. It is gated by `disposition_drift_check_enabled` — global default
`true`, per-lane override — and turning it off for a lane refuses to arm that
lane's claim tier. `cw review dispositions --worktree` is not gated by it.

### Spec-driven subagent escape hatch

When the pipeline bounces on an intricate cross-module ticket (repeated
`plan_unreviewable` / `plan_unsound` / `blocked` with `review_blocked`):

1. Produce a resolved spec (from the ticket's sources of truth or the last plan comment).
2. Spawn a fresh-context, worktree-isolated subagent and hand it the spec
   directly to *execute* — skipping the plan-review gate entirely.
3. Review and ship the result with your normal gate checklist.

This keeps orchestrator context lean and avoids endless pipeline retries on
tickets that require orchestrator judgment at the planning stage.

### Liveness before state (2026-07 sprint lesson)

Task rows are authoritative for *queue* state; **transcript mtime is
authoritative for *worker* state**. Rows have been observed parked
`blocked_on_user` while sessions were actively committing (#976), and deleted
outright under a live worker (#978). Before any park/requeue decision, check
the newest `*.jsonl` under `~/.claude/projects/<slug>-dev-<T>/`:

- **< 2 min old** → session ALIVE; do NOT requeue (double-spawn risk, #919
  class). Wait for its sentinel — the #918 rescue recovers a false park.
- **flat ≥ 45 min** → dead regardless of a `running` row. Adopt-check the
  worktree, confirm the session is gone (absent from the roster, no live
  process), `cw spawn close --confirmed-dead <sid>`, requeue. Still in the
  roster with a live process → stalled-but-live: use `/cw-queue-peek`'s
  bare-close path or surface the STOP recommendation.
- In between → bounded deadline check; review/plan stages go parent-silent
  for ~20 min during subagent cycles, so a single 20-min gap is not death.

**Automated for daemon sessions (#2275).** `cw dev-queue requeue` now refuses
on its own when the daemon roster still lists a session for the ticket, even
one the row's `session_id` no longer points at, and names the live session(s)
plus a `cw spawn close <sid> --requeue` remediation. The same guard covers
`cw spawn close --requeue`, `cw dev-queue drain --held` (reported per ticket
as `skipped_live_session`) and the `auto_fix_ci` re-dispatch (which re-arms
its one-shot latch so a later tick retries). `cw spawn close --requeue`
exempts only the session it just closed; a second live session for the same
ticket is still refused. The guard fails closed, like the worktree reuse
guard (#2213): an unreadable or malformed roster refuses every one of those
paths with `daemon roster unreadable at <path>; cannot rule out a live session
for #<T>` (drain reports it as `skipped_roster_unreadable`), because nothing
can show that no session is live. The just-closed exemption does not lift this
refusal: the close still happens, but the requeue does not. Fix the roster file
and retry. A roster file that does not exist means no daemon is running, and
the requeue goes ahead. The manual transcript-mtime check above remains
necessary only for work the daemon roster does not know about
(non-daemon-origin sessions).

### CANCELLED row recovery (`--from-cancelled`)

`cw spawn close --confirmed-dead <sid>` on a **RUNNING** row transitions that
row to `CANCELLED` — not `BLOCKED_ON_USER`. `cw dev-queue requeue` normally
rejects anything but `BLOCKED_ON_USER`/`AWAITING_OPERATOR_SIGNOFF`, so a
CANCELLED row is otherwise a requeue dead-end (#1018).

**One-command path (#1889):** `cw spawn close --confirmed-dead --requeue
<sid>` (only after the liveness evidence above: absent from `~/.claude/daemon/roster.json`, transcript flat, no live process) folds the close and the requeue into a single invocation — it closes
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
dev-queue show <T> -c <CLIENT>` / the event history before requeuing it.

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
flag. Check `cw dev-queue show <T> -c <CLIENT>` / the event history
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
it reaches this flag. Check `cw dev-queue show <T> -c <CLIENT>` / the
event history before requeuing it.

### Legacy codex session recovery (`cw codex migrate-legacy`, RFC 0014 B1)

A *legacy* codex session predates RFC 0014 A2: its review ran on a thread
inside `serve`, so it carries no liveness handle and the crash-only harvest
sweep never sees it. Until B2 retires the boot sweep, only a `serve` restart
disposes of one. `cw codex migrate-legacy` recovers every such session still
live, once, so B2 can land (#2389).

**Prerequisites.** Run it after the Epic I release is installed and `serve`
has been restarted on it (RFC 0014 D-6), so no new legacy session can appear.
It may run while `serve` is up: it takes `sessions_lock` and
`dev_queue_lock` like every other writer. Close any live sibling session on
the same ticket first.

```bash
cw codex migrate-legacy          # text report
cw codex migrate-legacy --json   # {"status", "counts", "unresolved"}
```

Per session it scans the worktree for a live codex writer first, then applies
the codex harvest gate (`reap_policy: auto`, codex fix loop off, worktree
clean apart from the review verdict, HEAD unmoved): all pass → the task is
requeued (`TICKET_REQUEUED`, reason `codex_harvest_clean_requeue`), any fail →
it is parked `BLOCKED_ON_USER` (`codex_review_orphaned_at_harvest`). Either
way the session closes `COMPLETED`/`CRASHED` behind a `SESSION_COMPLETED`
audit event with `reason: codex_legacy_recovery`, `legacy: true` and the
gate's own reason in `detail`.

**Reading the result.** The marker at
`~/.local/share/cw/codex_legacy_recovery.json` is the run record. Its counts
are `scanned`, `requeued`, `parked`, `failed`, `skipped_already_handled`
(the row or session had already moved on; nothing to do) and
`skipped_writer_live`. `outcomes` holds one entry per session with its
`disposition`, `prior_status` and `prior_stage`. The command exits 0 for
`completed` and `already_completed`, and 1 for `partial`.

**Handling `partial`.** Every `unresolved` entry names a session and a
`reason`. Resolve the cause, then re-run: a re-run retries only the unresolved
sessions (plus any live one an interrupted run never reached) and leaves
resolved ones alone. There is no `--force`.

- `live_writer`: a codex process may still be writing in the worktree, or the
  process scan was inconclusive. Wait for it to exit (or stop it),
  then re-run.
- `worktree_unset`, `worktree_missing`, `context_missing`,
  `context_unreadable`: the session has no recorded worktree, the worktree is
  gone, or its `.claude/cw-context.json` is absent or unreadable, so a writer
  cannot be ruled out. Inspect the session and its worktree, close the
  session through the usual session-close path, then re-run. A session that is
  no longer live resolves as `skipped_already_handled`.
- `audit_write_failed`, `state_write_failed`: an I/O error on the event log or
  `sessions.json`. Fix the disk problem and re-run.
- `client_config_missing`: the session's client is not in `clients.yaml`.
  Restore it, or close the session, and re-run.
- `clean_probe_unavailable`: the lockless git probe was missing or stale; re-run.

Audit events are attempt records. A `state_write_failed` session can carry two
`SESSION_COMPLETED` events for one session id (the failed attempt and the
retry); the marker's `outcomes` entry for that session is authoritative.

**Reversing a disposition.** Read the session's `outcomes` entry
(`prior_status`, `prior_stage`) first. No new command exists; use the
existing ones:

- `parked`: `cw dev-queue requeue <T> --client <C>`. `cw dev-queue unblock`
  does not apply (it only clears `SALVAGE_PARKED` sessions).
- `requeued`: `cw dev-queue cancel <T> -c <C>`, then inspect the worktree
  directly, since a closed session cannot be reopened. Optionally
  `cw dev-queue requeue <T> -c <C> --from-cancelled` to put it back.

**B2 gate.** B2 may retire the boot sweep once the marker exists,
`completed_at` is set, and `unresolved` is empty. A completed marker makes
every later run a no-op that prints `already completed at <ts>`.

### Requeue at a different stage (`--stage` / `--regress`)

`cw dev-queue requeue` defaults to re-running the ticket's **current** stage.
`--stage plan|impl|review|finalize` requeues at a forward stage instead. A
**backward** target on a parked ticket (e.g. a plan-deviation review exit back
to `impl`) is a regress: since #2692 it is inferred without `--regress` and
announced on stdout before the `Requeued` line, with the new
`regress_attempts` and the evidence the regress cleared (into `plan`: the plan
approval; into `review`: any recorded MUST_FIX override). `--regress` is still
accepted and makes the regress explicit. A ticket that is already `PENDING`
(no `--stage`, or its own stage) is an idempotent no-op: `requeue` exits 0,
prints `... is already PENDING at <stage>; nothing to requeue.`, stops and
closes nothing, records no event, and names any daemon-live session for the
ticket on stderr with the `cw spawn close <sid> --requeue` that would close it.
A `PENDING` row asked for a different stage refuses and names
`cw dev-queue remove` plus `cw dev-queue add --stage`. `cw dev-queue add --stage`
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
  `cw.codex_review._context._load_operator_comments` — **operator comments
  only**: any comment the pipeline itself posted (the `<!-- cw-agent-authored -->`
  marker) is dropped, so a prior round's verdict never re-enters the next
  round's prompt (#2213). The codex path is `github-issues`-only — a `codex`
  REVIEW backend on any other tracker has no in-process fetch op and cannot
  deliver them.

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
`concierge_enabled: true`. The orchestrator runs the recipe below when
concierge is off, or when the row is refused at the attempt ceiling.

A quota window or hang loop (#979) grinds a ticket to
`attempt_cap_blocked`. The ceiling compares `unproductive_attempts` (claims
that left RUNNING with no evidence of progress), not the raw `attempts`
counter, which bumps on every claim AND stage transition and gates nothing
(#1750).

The number the park fired against is the *resolved* attempt ceiling — the
row's lane `attempt_ceiling`, or `global_attempt_ceiling` when the lane sets
none (#1751). Do not read `global_attempt_ceiling` and assume it is what
parked the row: both park events (`dispatch.tick` with
`skip_reason=attempt_cap_blocked`, and the matching
`session.needs_attention`) carry the resolved number on an `attempt_ceiling`
payload field. A lane whose operator answers every park can set
`attempt_ceiling: false` to opt out of the cap entirely — see
`config/CONFIG_REFERENCE.md`.

Reset recipe (#2695) — one command, safe with the loop running, because it
takes the dev-queue lock like every other verb:

```bash
cw dev-queue show <T> -c <CLIENT>                    # unproductive_attempts vs attempt_ceiling
cw dev-queue requeue <T> -c <CLIENT> --reset-attempts
```

`--reset-attempts` zeroes only `unproductive_attempts`; raw `attempts`, lane,
priority, stage and `scope_hint` are kept (zeroing `attempts` would skip the
`attempts <= 1` stale-`context.json` invalidation, #1030). The confirmation
line reads `...; unproductive attempts reset (was N)` and `ticket.requeued`
carries `attempts_reset: true` plus `prior_unproductive_attempts`. On a row
that is already PENDING the flag is not applied (the requeue is a no-op, and
stderr says so). `show`'s `next:` suggests the flag on any
`attempt_cap_blocked` row, and the `cw dev-queue tasks` UNPRODUCTIVE cell
reads `u/ceiling` (`3/10`; `3/none` when the lane disables the ceiling).

The old recipe (stop every loop, hand-edit `~/.local/share/cw/dev_queue.json`,
then requeue) is unsafe and no longer needed: editing that file with a loop
alive silently loses the edit to a tick's read-modify-write (observed twice,
2026-07-04).

What does not charge the counter (#2695): `ambiguities_pending_resolution`,
`premises_pending_verification` and `review_pending_approval` parks; a
`blocked` result whose `blocker.reason` is `push_auth_failed`,
`operator_unavailable`, `dependency_unmerged` or
`impl_comments_unreadable_after_regress`; and a no-worker spawn failure that is
a raw `OSError`. Residuals the ceiling still owns or no longer bounds:

- `plan_pending_approval` still charges, deliberately: `auto_adopt_clean_plan`
  (on by default; `gate_recipes_enabled`, or the lane's recipe switch, turns it
  off) can re-claim that park without a human, and the ceiling bounds an
  adopt -> re-run -> re-park cycle. A long plan tail costs one unit per
  `plan_pending_approval` park; `--reset-attempts` clears it.
- Plain `CwError` wraps from `claude --bg` (SSH or 1Password walls, adapter
  failures, CLI flag breaks, a missing binary) and `WorktreeError` (including
  a git or SSH failure inside `create_worktree`) still charge until those
  failures are typed (#2722).
- A raw-`OSError` spawn failure that is in fact deterministic per ticket is
  bounded only by the 300 s spawn-error backoff cap and the lane circuit
  breaker; no separate spawn-error ceiling exists.
- An orchestrator looping `cw dev-queue drain` or `requeue` against a standing
  `push_auth_failed` wall is unbounded, since that park no longer charges. The
  audit trail is the `ticket.requeued` and `task.transition` events (the
  latter carries `unproductive_attempts`).
- A worker that hits an auth wall at runtime and dies with no sentinel (a
  phantom or stalled revert) still charges.

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
- **#2696: the cause is shown, not just the marker.** `cw dev-queue status`
  prints a `Paused lanes:` block after the client table, even when the queue
  is empty, no tick has run, or the paused lane has no tickets (`--client`
  filters it). Each entry names who paused the lane and when (`by
  circuit_breaker (spawn errors, trip #N)`, `worktree setup errors` for a
  worktree trip, `by operator`, `declared in clients.yaml`, or `legacy, since
  unknown` for a pause recorded before #2696 with no metadata), the last five
  spawn errors (redacted and capped at 500 characters), the auto-resume time
  and the exact `cw lane resume <client> <lane>` command. `cw lane ls` prints
  the same lines under each paused row and a `pause` object in `--json`, and
  `cw dev-queue show` appends them to `skip_reason`. `status --json` carries
  them per client as `paused_lanes`, including a paused client with no tick.
- **Auto-resume.** A circuit-breaker pause lifts on its own after
  `lane_circuit_auto_resume_minutes` (default `15`; set to `0` to disable), doubling
  per consecutive trip with no spawn success in between up to
  `lane_circuit_auto_resume_max_minutes` (default 240). The resume is
  half-open: the lane's counters drop to one below their trip point, so one
  more failure of the kind that tripped it re-trips at once with the next,
  longer cool-down, and the first spawn after a resume is a probe that does
  not charge the ticket's `unproductive_attempts` (its spawn-error backoff is
  still stamped). A spawn success resets the streak. The resume emits
  `lane.resumed` with `source=auto_resume` and pages nobody; while a resume is
  scheduled the #1630 starved page above is held for the first two trips of a
  streak and resumes at the third. Operator pauses (`cw lane pause`), legacy
  pauses and `paused: true` in `clients.yaml` are never auto-resumed, and a
  breaker trip never takes over an operator pause. A lane that is also
  declared paused in `clients.yaml` stays paused after its circuit pause
  lifts; `status` then shows it as `declared in clients.yaml`.
- **Worktree errors count apart.** A `WorktreeError` (including stale-tree
  and branch-held refusals) counts on a separate per-lane counter that trips
  at 3x `lane_circuit_breaker_threshold`. Outside a half-open probe it still
  charges the claimed ticket's `unproductive_attempts`, which is the right
  bound for a ticket-local fault such as a stale tree; the aggregate bound for
  a lane-wide worktree fault is that per-lane counter, so a client-wide
  failure charges at most 3x the threshold attempts per lane before the pause
  (each client stops its lane walk after the first spawn error per tick).
- **Effective config.** Under the paused-lanes block, `status` prints one
  `effective:` line per client: the configured ceiling and where it came from
  (`override`, `declared` or `default`), each lane's cap, the breaker and
  worktree thresholds and the auto-resume knobs, as this command loaded them.
  ` [CONFIG? last tick used cap=M, configured ceiling=N; ...]` means the
  dispatch loop ran its last tick with a different ceiling: the loop keeps its
  last-good config when a reload fails (only a log warning), and `serve
  --max-parallel` replaces every ceiling. Right after an edit it can also
  show for one tick (about 30 s) before the loop reloads. `status --json`
  carries the same values as `effective`.
- A tick line reading `skip=lane_circuit_paused [resumed since this tick]` is
  a stale snapshot: no lane of that client is paused any more, and the next
  tick will dispatch.
- `BLOCKED_ON_USER` rows hold lane slots by design; a parked ticket can
  starve its lane. Requeue or remove to free the slot.
- A session wedged in `needs_salvage` with a park marker poisons every
  respawn of its ticket (claim → revert → attempts+1, plus per-tick
  `session.salvage_skipped` noise): `cw spawn close --confirmed-dead <sid>`
  clears it, once the session is verified dead (absent from `~/.claude/daemon/roster.json`, transcript flat, no live process). **Now partially mechanized** — see §11.1's
  `park_marker_poison_clear` recipe (requires `concierge_enabled: true` and
  `consecutive_salvage_skips >= 1` on a confirmed-dead session).
- `disposition: unresolved_subagent_spawn` (#1646) is **not** an ordinary
  `phantom_surface` crash — do not just requeue it. It means the worker died
  or hung with a sub-agent spawn still in flight, so committed work may exist
  behind a verification tail that never ran. **Check the worktree and branch
  before requeueing:** look for commits ahead of the base branch, and for a
  half-finished stage the transcript never reported. Once you know what
  landed, requeue at the right stage (or close the ticket, but only when its acceptance criteria are verified satisfied, citing the merged PR/commit in the close comment, or it is verified duplicate/obsolete, citing the superseding ticket). The evidence is durable — `agent_spawn_stamp.unresolved_count`
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
- `disposition: fix_dispatch_ref_unresolved` (#2209) means the fix-loop
  dispatcher found **no remote ref whose tip matches the worktree's HEAD**,
  across all three rungs of its ladder: the branch the impl session's sentinel
  reported pushing (`last_result.branch`), the local branch's configured
  `@{u}`, and the templated `origin/<prefix>/<ticket>` guess. Either nothing
  was pushed, or everything that was pushed is behind the worktree.
  `pending_fix_dispatch` is **retained** on this park — uniquely among the
  dispositions here — preserving the REVIEW round's action list so you can read
  what the fix agent was going to be told. It is **not** a resume point:
  `cw dev-queue requeue` sets the row back to PENDING, which stops matching the
  parked-row exemption (that check gates on `BLOCKED_ON_USER`), so the #2142
  stale-handoff sweep drops the retained handoff and the ticket is claimed into
  a **fresh REVIEW session**. Whether requeue should resume the handoff instead
  is #2265. **Check before requeueing:** the
  worktree's HEAD versus what `origin` actually holds, and the impl session's
  reported branch name. If the work was never pushed, push it before requeuing;
  a requeue alone will re-derive the same failure. The reason is
  escalation-eligible (§11.2) and deliberately excluded from concierge's
  auto-requeue (§11.1), on the same reasoning as `unresolved_subagent_spawn`
  above: an agent-authored branch name is not automatically safe to keep
  retrying against.
- `disposition: running_row_session_lost` (#2715) means the row was RUNNING
  and bound to a session that has **no record in `sessions.json`**, usually
  because the state write failed after the worker launched
  (`spawn_post_launch_failed`). Reconcile parked it, keeping the dangling
  `session_id` as evidence, and charged no unproductive attempt. The
  leaked-worker sweep stops any live worker with no cw session, so by the
  time you look the worker is normally gone. **Check before requeueing:** the
  ticket's worktree for unsaved work (commit and push it, or confirm it is
  junk), then run `cw dev-queue requeue <ticket> -c <client>`. Doctor class 5
  also lists this park as `wedge/blocked-on-user-dead-session` (a session
  absent from state counts as dead); its `--reap` release is the same as the
  requeue. Escalation-eligible (§11.2) and excluded from concierge's
  auto-requeue (§11.1). At most three rows park per tick; if `sessions.json`
  is empty or missing and more than three bound rows would park, reconcile
  parks none and logs a WARNING (inspect `sessions.json` before acting).
- A `session.needs_attention` with `paused_status=running_row_no_session`
  (#2715) is page-only: a RUNNING row has had **no session bound** for at
  least 30 seconds after its claim. Nothing was changed. Read the breadcrumbs
  for live same-ticket sessions, then check `cw list` and
  `cw dev-queue tasks`. A slow worktree phase or an unstamped worker looks
  like this and reconcile adopts the row once the evidence ties (#2591). Only
  when no worker is live for the ticket, release it with `cw doctor --reap`
  (class 2) or `cw dev-queue cancel` followed by
  `cw dev-queue requeue --from-cancelled`. The row's `advisory_note` reads
  `?no_session` until it is bound or claimed again.

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

### Reading lock-hold numbers (#2637)

cw records how long the **sessions** lock (`.sessions.lock`) and the
**dev-queue** lock (`.dev_queue.lock`) are held, so a slow or wedged holder
shows up as a number instead of a guess.

**What is measured.** Both locks go through one seam,
`cw.config._guarded_state_lock`. The *hold* runs from the `flock` acquisition
returning to just before the unlock. The *wait* (from just before the
acquisition to it returning) is recorded separately. The *caller* is the
first frame outside the lock plumbing, as `module:qualname` (for example
`cw.dispatch.tick:dispatch_tick`; a `mutate_state` call is labelled with the
function that called `mutate_state`). Each sessions-lock record also carries
`state_io_s`, the time spent inside `load_state`/`save_state` during that
hold, and `state_bytes`, the `sessions.json` size when the record was written.
A hold is recorded when it was **held or waited for at least 0.25 s**.

**Where to read it.**

- `cw doctor`, check `lock-holds` (also in `cw doctor --json`): each lock's
  longest completed hold in the last hour with caller and age, "at least N
  recorded", the longest wait when it reached 10 s, the aggregate state
  load/save share, and the `sessions.json` size at the longest hold.
- `cw dev-queue status` (text mode only): one footer line,
  `Longest completed lock hold, last hour: …`. `--json` is unchanged.
- The raw records: `~/.local/share/cw/lock_holds.<YYYY-MM-DD>.jsonl` (one
  file per UTC day; today's and yesterday's are kept). Every recorded hold is
  also logged at DEBUG on logger `cw._lock_hold`, before the cap below
  applies.

**Thresholds.** Recorded at or above 0.25 s held or waited; doctor warns
(advisory, `ok` stays true) at or above 10 s held or waited.

**Limits.**

- *Completed holds only.* A hold still in progress, or a process killed
  mid-hold, leaves no record. For a lock that is held right now, find the
  holder with `lsof ~/.local/share/cw/.sessions.lock` (or `.dev_queue.lock`).
  A bounded acquisition that timed out was never held and leaves no record
  either.
- *Counts are lower bounds.* Records are capped at 1 MiB per UTC day. Past
  the cap only records with a hold or wait of at least 10 s are appended, up
  to a hard ceiling of 2 MiB, and one marker line is written when the cap
  trips (another when the ceiling does). Doctor then prints "record cap
  reached; counts are lower bounds", and after the ceiling also "nothing more
  is recorded today". The DEBUG log is not capped.
- *The share is survivor-biased once the cap trips.* Past the cap only the
  10 s+ holds are kept, and past the ceiling nothing is, so the aggregate
  state load/save share is computed over the holds that survived, not over
  every hold that day.
- *Notes can be stranded.* A hold is written only once its thread holds no
  guarded lock. A dev-queue hold taken while the thread holds a guarded lock
  that does not go through this seam (the dispatch-plan lock,
  `dispatch_state_lock`, the focus lock) stays in memory until that thread's
  next sessions or dev-queue release, and is lost if the process exits first.

**Kill switch.** `CW_LOCK_HOLD_RECORD=0` (read once at process start; default
on) turns off recording and the state load/save timer in that process.
Existing sidecar files are still read by doctor and status.

**The pruning decision rule.** Use the *aggregate* state load/save share over
the window's sessions-lock holds: `state_io_fraction = sum(state_io_s) /
sum(held_s)` over the recorded sessions holds that carry a `state_io_s`
figure. It is not the longest hold's own ratio, and it covers recorded holds
only. `dev_queue` holds have no such figure ("n/a").

- **Aggregate `state_io_fraction >= 0.5`:** load/save of `sessions.json`
  dominates, so a smaller file shortens holds. Run `cw session prune`
  manually. Doctor adds the `cw session prune` pointer in exactly this case.
- **Below 0.5:** pruning will not fix long holds. Look at what the named
  callers do while they hold the lock: the in-lock work the #1232 family
  moved out, the ADR-0019 allowlist of in-lock subprocesses, and the
  bounded-wait history in #2491, #2500 and #2501.

Automatic pruning is deliberately not shipped. #2637 stays open as the
tracker for that half, and this section is its measurement input.

**Tie-in to the watchdog.** A `cw dev-queue serve` wedged on `.sessions.lock`
stops emitting `dispatch.tick`. The mainstream watchdog's dead-man's switch
pages on exactly that (see §11.3), and so does `dispatch_loop_stale` above,
but the watchdog only runs if the orchestrator ran `cw watchdog install` and
activated the timer. The lock-hold numbers then tell you who held the lock and
for how long once it was released.

### Quota walls: probe before pausing

Worker transcripts ending in "You've hit your session limit" are evidence of
a PAST window, not the current one. Before pausing a wave:
`claude -p "Reply with exactly: WORKER-OK" --model <worker-model>` — if it
answers, the wall is gone. During a genuine wall, STOP the dispatch loop
(each tick burns an attempt per pending ticket into the wall).

---

## Orchestrator permission allowlist

Docs only: cw does not write your permission settings beyond what `cw init`
already adds. `cw init` puts one broad `Bash(cw:*)` entry in
`~/.claude/settings.json` (see [`docs/INSTALL.md`](INSTALL.md)), and that entry
covers every verb below. The per-verb list is for operators who want a
narrower allowlist for the orchestrator session: these are the read-only verbs
an orchestrator runs on every monitoring pass, so allowlisting them keeps the
loop free of permission prompts without pre-approving anything that changes
state.

- `Bash(cw dev-queue tasks:*)`
- `Bash(cw dev-queue status:*)`
- `Bash(cw dev-queue show:*)`
- `Bash(cw dev-queue wait:*)`
- `Bash(cw queue peek:*)`
- `Bash(cw session result:*)`
- `Bash(cw session show:*)`
- `Bash(cw session list:*)`
- `Bash(cw event tail:*)`
- `Bash(cw orchestrate watch:*)`
- `Bash(cw review dispositions:*)`
- `Bash(cw lane ls:*)`
- `Bash(cw config show:*)`
- `Bash(cw config concurrency get:*)`
- `Bash(cw preflight:*)`

State-changing verbs are intentionally not listed: `cw lane set`,
`cw config concurrency set|clear`, `cw dev-queue approve|resolve` and
`cw review settle`. Approve those per call, or add them yourself knowing each
one mutates queue, review or lane state. The old script-based attention
watcher was removed in #2693 in favor of `cw orchestrate watch`; drop any
allowlist entry that still names it.

**Command shapes.** Orchestrator-run skills (`.claude/skills/*` and
`.claude/commands/queue-issues.md`) prescribe one plain command per Bash call:
no command substitution, backtick substitution, shell loops or jq filter
flags, because those shapes do not match a per-verb allowlist entry and are
rejected by worktree-isolation hooks. Where one command's output feeds the
next, the skill says to read the field from the printed JSON and substitute it
literally. `tests/test_orchestrator_command_shapes.py` enforces this. One fence
is exempt: the `cw-session-watch` arm-automerge recovery fence, a merge-safety
control pinned by `tests/test_arm_automerge_markdown_sites.py`, tracked in
#2753. Worker-run docs (`auto-dev*`, `prep-pr`, `ship-it`, `review-monitor`)
are out of scope.

### Orchestrator bootstrap

An orchestrator started in a fresh Claude Code worktree needs no setup step:

- **No context file.** With no `.claude/cw-context.json`, `cw review settle`,
  `cw dev-queue resolve`, `cw preflight` and `cw orchestrate watch` all run.
  Only a headless dispatch worker (`headless: true` in the nearest context
  file, or a `$TMPDIR` inside such a worktree) is refused (ADR-0020
  invariant 4). Do not hand-author a context file: hooks read it as a
  cw-spawned session.
- **Pre-flight.** `cw preflight <TICKET> -c <CLIENT>` runs the dispatch
  readiness checks and prints one JSON object (`ok`, `ticket_id`, `client`,
  `repo`, `repo_root`, `tracker`, `checks`); it exits 1 when a hard check
  failed. `-c` is required. With no `clients.yaml` the repo root is the
  nearest ancestor of the current directory holding `.git`, which in a linked
  worktree is the worktree itself. `--repo OWNER/NAME` overrides the derived
  slug.
- **Lane limits.** `cw lane set <CLIENT> <LANE> KEY=VALUE...` (or
  `<LANE> KEY=VALUE... -c <CLIENT>`; alias `cw config lane set`) sets only
  `max_parallel` (a positive integer) and `attempt_ceiling` (a positive
  integer, `false`, or `null`); `KEY=null` removes the key so the lane
  inherits. Several assignments apply together or not at all. Everything else
  is refused with the supported-key list: the gate keys `signoff` and
  `finalize_gate` (human opt-in gates, ADR-0020: agents never touch gate
  keys), `reap_policy`, the guard toggles, the codex fix-loop toggle,
  `priority`, `description` and `paused` (use `cw lane pause` /
  `cw lane resume`). Edit `clients.yaml` by hand for those.
- **Client ceiling.** `cw config concurrency set ceiling=N -c <CLIENT>` and
  `cw config concurrency clear ceiling -c <CLIENT>` set or clear one client's
  dispatch ceiling override. Without `-c` both commands keep their existing
  host-wide behavior (`max_parallel_clients`), which has no worker guard.
- **Audit.** `cw lane set` and the `-c` ceiling setter refuse inside a
  dispatch worker and print each change as `old -> new`. Each call that
  changes something emits one `lane.config_changed` event,
  `{client, lane, changes: {<key>: [<old>, <new>]}}` (`lane` is null for a
  client ceiling), after the file is written; refusals, validation failures
  and no-ops emit nothing. If the emit itself fails, the command reports that
  the change WAS applied. See [`docs/events.md`](events.md).

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
those block indefinitely until the orchestrator reconciles them. Three root causes — check
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
> stash. Investigate the discrepancy (the orchestrator does this itself) before
> dropping any content — the branch may have been overwritten or the wrong
> ticket branch identified.

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

**Fix.** This is a destructive reset. The orchestrator runs it after the
evidence check above (the `diff origin/dev/<ticket> HEAD` is empty) — the
evidence, not a human, is the gate (ADR-0020):

```bash
# Only after the diff above came back empty
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

**Fix.** Same destructive reset as §9.3 — the orchestrator runs it after the
evidence checks above (`git diff origin/main..HEAD` shows only release/changelog
churn, or nothing); the evidence, not a human, is the gate (ADR-0020):

```bash
# Only after the diff above showed release churn only
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

**Recovery.** The orchestrator runs these itself:

```bash
# 1. Create the PR (the branch already exists on origin)
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

**Recovery.** The branch is preserved; the orchestrator creates the PR and
enables auto-merge itself:

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
subsequent reconcile tick. It is NOT automatically cleared — the steps above
are the orchestrator's reset. Do not re-dispatch the ticket.

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
previously handled one ticket at a time by the orchestrator (see §7's "Attempt-cap reset" and "park marker
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
   reset" recipe for the common case; the orchestrator still runs the
   recipe (editing `dev_queue.json` directly) for a ceiling-refused row (see
   below) or when `concierge_enabled` is off.
2. **`park_marker_poison_clear`** — a row behind a session whose park marker
   (`silently_idle`/`needs_salvage`) has persisted for
   `consecutive_salvage_skips >= 1` and whose transcript is confirmed dead
   (per-stage-floor 45-minute staleness) is closed and requeued. This is the
   automated version of §7's "a session wedged in `needs_salvage`... poisons
   every respawn" note — the orchestrator no longer needs to run `cw spawn
   close --confirmed-dead <sid>` for this case.
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
succeed. Clear it with `cw spawn close --confirmed-dead <id>` once the session is
verified dead (absent from `~/.claude/daemon/roster.json`, transcript flat, no live process): that flips the
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
`unresolved_subagent_spawn` / `running_row_session_lost` / `None` while
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
`systemctl`/`launchctl`; the orchestrator runs the printed activation command
(`systemctl --user daemon-reload && systemctl --user enable --now
cw-watchdog.timer`, or `launchctl load <plist>`).

A `dispatch.tick` gap is often a `serve` stuck behind `.sessions.lock`. Once
the lock is released, "Reading lock-hold numbers (#2637)" earlier in this
runbook shows which caller held it and for how long.
