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

**In-tick fetches are bounded (#2910).** Every read-only `git fetch` the tick
runs (the freshness gate's, the claim path's through `create_worktree`, and
the fix agent's) goes through one fetch gate: 15 s per call, 30 s per tick in
total, and a 120 s per-client backoff after a timeout. A fetch that times out,
or that the gate skips (`fetch skipped: tick fetch budget spent` /
`fetch skipped: client is backing off after a fetch timeout`), reads as
freshness unknown: the tick proceeds and dispatches, with no
`ticket.needs_sync` and no freshness-block latch change. The
`freshness_check_skip` WARNING is logged once per distinct failure per client
for the life of the loop process, not once per tick (the dedup is not re-armed
when a later fetch succeeds). The tick's auto-fast-forward is a local
`git merge --ff-only origin/<default>` with no timer, since the gate's fetch
has already updated that ref. Two calls stay unbounded because they mutate a
checkout: `git submodule update` on the claim path, and `refresh-all`'s
`git pull --ff-only` (an operator command). Interactive `cw start` fetches are
unbounded too.

Known limits of the gate (tracked as follow-ups filed from the #2910 PR's
deferred-review-findings block):

- **The numbers.** A 30 s fetch cap plus the default 30 s tick sleep is 60 s,
  which stays under `TICK_STALE_SECONDS` (90 s). The 120 s backoff is 4
  default ticks. The 60 s covers the gate's own `fetch`/`ls-remote` calls
  only: the changelog-repair recipe's network git carries its own 120 s
  timers outside the gate and is neither charged to the 30 s budget nor
  subject to the 120 s backoff. Aligning those read-only legs with the gate
  is tracked as a follow-up from the PR's deferred-review-findings block.
- **In-memory state.** The budget and the backoff map live in the loop
  process only. A loop restart clears the backoff, and a backoff expires by
  itself after 120 s.
- **Orphaned `ssh`.** `subprocess.run(timeout=)` kills only the `git` child.
  An `ssh` grandchild waiting on a credential prompt can outlive it, so each
  backoff expiry can raise another prompt (at most one per client per 120 s).
  Fixing it needs a process-group kill.
- **Large or slow fleets.** A fleet that spends the 30 s tick budget skips the
  clients later in the iteration order for that tick, and their freshness
  reads unknown. A skipped claim-path fetch degrades like a failed one: a
  reused worktree is not refreshed, and a missing local branch is created from
  the default start point instead of resuming pushed history.
  A fetch granted less than the 15 s per-call timeout because the tick budget
  is nearly spent can time out against a healthy remote. That timeout is
  charged to the budget and starts the client's 120 s backoff (4 default
  ticks) like any other, so its claim-path fetches are skipped too during the
  window and its freshness reads unknown.
- **Claim-path submodule update.** `git submodule update --init --recursive`
  stays unbounded (it mutates a checkout), so a credential prompt there can
  still stall the tick.

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

### PITA review-feedback handoff

`--review-feedback-file` carries one typed PITA report on a single
implementation-stage queue row. Its JSON identity is the pair `repo` and
`pr_number`; `repo` must be a GitHub `owner/repo` slug, `pr_number` must be
positive, `reviewed_sha` must be a full 40-character lowercase commit SHA, and
`captured_at` must be a timezone-bearing ISO-8601 timestamp. `report` is kept
verbatim, must contain non-whitespace text, and is limited to 64 KiB in UTF-8.
Unknown fields and invalid values are rejected without repair.

Example `/path/to/review-feedback.json`:

```json
{
  "repo": "owner/repo",
  "pr_number": 42,
  "reviewed_sha": "0123456789abcdef0123456789abcdef01234567",
  "captured_at": "2026-11-04T12:30:00Z",
  "report": "PITA report text, preserved verbatim."
}
```

Enqueue exactly one ticket at IMPL with:

```bash
cw dev-queue add <TICKET-ID> --client <client> --stage impl \
  --review-feedback-file /path/to/review-feedback.json
```

The option requires explicit `--stage impl` and exactly one ticket ID. It does
not generically bypass the open-PR dispatch gate: a pending PLAN/IMPL row whose
ticket already has an unrelated open PR can still be parked as
`stale_dispatch_gate`. A payload that identifies the exact open PR is the
explicit repair exception described below.
For an existing parked row, attach the report while requeueing it at IMPL:

```bash
cw dev-queue requeue <TICKET-ID> --client <client> --stage impl \
  --review-feedback-file /path/to/review-feedback.json
```

Before either form, read the live identity and head from the repository root,
and put those exact values in the feedback file:

```bash
gh repo view --json nameWithOwner --jq .nameWithOwner
gh pr view <PR-N> --repo <owner/repo> \
  --json number,headRefOid,headRefName,url
git rev-parse origin/<headRefName>
```

Require `number`, `url`, `headRefName`, and `headRefOid` to identify the same
`<owner/repo>#<PR-N>`, and require `headRefOid` to equal both
`reviewed_sha` and the branch tip. The attach/requeue form updates the existing
row atomically; the pre-dispatch gate releases it only when the payload's repo
and PR number identify the open PR found on that branch.

A non-green PR adopted with `--adopt-pr` needs no file: this payload is
the same `ReviewFeedback` shape that `--adopt-pr` synthesizes from the live
PR, and `--adopt-pr <N> --stage impl --review-feedback-file` takes this file
in place of the synthesis. Both share the Stage 2 guard; see "Adopt an open
human PR" below.

Both forms carry the report and its exact reviewed head. Stage 2 repeats the
live repository/PR, worktree, branch, and local-`HEAD` checks above and blocks
with `review_feedback_stale` on any mismatch; it never fetches, resets, or
re-bases to make the values match. Existing PR worktree routing remains a
separate limitation.

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
  - For current live session count, use the top table or `cw status` (which
    shows sessions as recorded: a dead worker stays counted until the next
    dispatch tick, `cw start` or `cw doctor --reap`).
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
  - Since #2516 the footer reads client-wide ticks only. The dispatch loop
    also records a per-ticket `dispatch.tick` (with a `ticket_id`) each time
    it passes over one PENDING row for a ticket-specific reason; that event
    no longer replaces the client's line, so `pending=` and `skip=` stop
    flipping between two ticks.
- **"Pending tickets held by dispatch" block** (#2516) — printed after the
  footer, only when some PENDING row has per-ticket skips. One line per row,
  per client in table order, longest run first:
  `  acme: 49: worktree_occupied x103 (last 12s ago)`. `xN` counts
  consecutive per-ticket tick events with the newest reason (normally one per
  tick); a different reason, a gap over 10 minutes or the row's creation time
  ends the run. `[stale]` marks a run whose newest event is older than 90s
  (the loop has stopped skipping it, or the row was requeued). This is where
  a ticket skipped as `worktree_occupied` every tick shows up while the
  client footer reads `skip=no_pending` with `pending=1`.

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
gate the human opted into reads `HUMAN GATE: approve` (#2692), and one that
`approve` cannot take reads `HUMAN GATE: requeue --stage <stage>` (#2760). Every
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
`next` (`command`, `purpose`, `operator_only`: true for a human-kept gate's
command, which only the human operator runs, #2760). Compatibility rule:
fields may be added without a version change; renaming or removing one bumps
`schema_version`.

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
stopped. For a human-kept gate the `| next:` field instead reads `HUMAN GATE:
cw dev-queue show <T> -c <C>`: an operator-only stop sign to notify the human,
not a command to run (#2760). Its **first stdout line is the resolved scope** (`WATCH | scope |
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

A long clean-mode review stage (a client configured `review.convergence:
clean`, #2787) is not a stalled worker while its new-finding count keeps
falling: `peek` exempts a live session from the age-only `PEEK` and `STOP`
verdicts there (see "Clean-mode review convergence" under "Review Finding Dispositions"). A stalled
confirmation round is still a `peek` signal, never a cw-imposed timeout.

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
| `review_pending_approval` | Usually released automatically by the `auto_approve_clean_review` gate recipe (on by default) unless health is degraded, a forbidden area is touched, or it carries `scope_hint: large`. If it stays parked: only parks for large (or unresolved) tier, except the finalize park (`stage_reached: "stage4a_merge_gate"`, see "Which verb for which park" below), which parks at any tier because a finalize Step 4.0b stale-tip park carries the ticket's real tier (#2911). For a degraded-health, forbidden-area or recipes-disabled park, the orchestrator verifies the pushed branch diff and gates, then runs `cw dev-queue approve` to advance to FINALIZE (which creates the PR) — or ships it directly (PR + auto-merge, §9.4) and cancels the task. A `scope_hint: large` park is the operator's own opted-in human gate: the orchestrator notifies the human and neither approves nor ships it. With signoff configured, `approve` re-routes to `AWAITING_OPERATOR_SIGNOFF`; approve again (§2). |
| `merge_pending` | PR created but CI/merge gate not yet cleared (#899). Not a failure — the task parks with `pr_url` preserved; monitor/merge the PR. Do **not** re-dispatch. |
| `scope_exceeded` | Scope rejection; the orchestrator relaxes the constraint and re-dispatches, or closes the ticket only when its acceptance criteria are verified satisfied (cite the merged PR/commit in the close comment) or it is verified duplicate/obsolete (cite the superseding ticket). |
| `forbidden_area` | Forbidden-area rejection; the orchestrator updates constraints or reroutes. |
| `blocked` | Triage `blocker.reason`. Check `blocker.retry_eligible` and `blocker.recovery_hint`. `blocker.reason: "plan_unreviewable"` / `"plan_unsound"` mean plan review needs orchestrator judgment — if the pipeline bounces repeatedly on an intricate ticket, use the **spec-driven subagent escape hatch** (§7) rather than retrying. A `blocked` at FINALIZE with `blocker.reason: "agent_block"` self-heals: dispatch auto-regresses the ticket to IMPL (up to 2 regressions, #770) — no operator action. |
| `merge_gate_blocked` | A prior pipeline PR is still open. The orchestrator merges or closes it, then re-dispatches. Each consecutive park on the same gate backs the row off before it can be claimed again: 60 s, doubling to a 900 s cap (#2717). The page's `breadcrumbs` read `occurrence=N next_eligible_at=<ISO>`. `cw dev-queue requeue` bypasses the wait, and under `reap_policy: auto` the Variant B release clears it once the blocking PR is MERGED or CLOSED. The delays are fixed constants, not configurable; set the per-client `gate_block_backoff_enabled` setting to `false` to disable the rollout; the next claim ignores a gate-owned wait already stamped on a row (a usage-limit or spawn-error window still holds). While waiting, `cw dev-queue show` and lane summaries label the row `spawn_error_backoff`. To stop the park altogether for a client, set `merge_gate_enabled: false` in `clients.yaml` (#2851, operator-only; `cw doctor` lists it as `merge-gate/<client>`); the worker then skips Stage 4a, notes `merge gate disabled by client config` in `friction_highlights`, and never emits this status. Risk: a PR that conflicts with an open sibling is created anyway, and whichever merges second needs a manual rebase. |
| `stale_dispatch` | **This** ticket already has an open, unmerged PR from an earlier dispatch (#1862) — distinct from `merge_gate_blocked`, which is about a *different* ticket's PR. The session found it and refused rather than re-implementing work already in review; `blocker.details` names the PR. Land or close that PR, then `cw dev-queue requeue <T> -c <client>`. Re-dispatching first just reproduces the refusal. |

Not a sentinel status but seen in the same `disposition` field:
`stale_dispatch_gate` — the same condition caught *before* any session was
spawned, by the pre-dispatch open-PR gate. Recognizable by
`blocked_reason: "pr_already_open_pre_dispatch"`, an empty `session_id`, and
a `dispatch.tick` event carrying `skip_reason=stale_pr_blocked`. Same
operator action as `stale_dispatch` above. The gate covers PLAN/IMPL-stage
`PENDING` rows only (a REVIEW/FINALIZE ticket legitimately has an open PR)
and fails open on any `gh` error, so it can hold a healthy ticket only if the
PR is genuinely open. The park records that PR's URL as the row's `pr_url`
when `gh` reports one (#2740): `cw dev-queue show <T> -c <client>` prints it
as `pr: #N <url>` and `cw dev-queue tasks` shows `#N`. Stamping a new URL also
resets the row's `pr_state`, since `requeue` keeps the terminal `pr_state`
hydrated for an earlier PR. A lookup without a
usable URL still parks with `pr_url` empty. `requeue` clears it, and the next
claim re-runs the gate, which reads the 300s `open_pr_probe` cache that
`requeue` does not invalidate: within 300s of the earlier probe the row can
re-park on the earlier PR (and its URL); after that window it records the PR
open then. The row is not
auto-released when that PR merges (#1902). Because it now has a `pr_url`, the
row is hydrated by `pr_hydrate` (one `gh pr view` per pass, every
`pr_hydration_interval_seconds`, default 150) and can emit `pr.*` events that
are in the default operator-forward set (`pr.merged`, `pr.ci_failed`,
`pr.review_received`, `pr.mergeable`), so you may now be notified about the
earlier PR.

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
  refusal label the row's command `operator-only human gate; not for an
  agent to run`, `show --json` flags it `operator_only: true`, and `tasks`
  shows `HUMAN GATE: approve`. When `approve` cannot take the gate, the
  labelled command is a `requeue --stage <stage>` instead (`tasks` shows
  `HUMAN GATE: requeue --stage <stage>`): at the terminal stage of the lane
  pipeline it releases the gate and ships; when no bound session is at the
  gate (none recorded, or a `scope_hint: large` approval whose session is
  not at the gate) it re-runs the stage. An agent runs neither (#2718,
  #2760).
- Finalize park (`review_pending_approval` at FINALIZE, sentinel
  `stage_reached: "stage4a_merge_gate"`): `requeue --stage review`, never
  `approve` (refused at the terminal stage) or a finalize re-run. A Step 4.0
  approval park (friction `review_approval_park: <token>`) is labelled
  operator-only: the large-tier approval is recorded at review. On a lane
  without a review stage that requeue is refused, so `cw dev-queue show`
  names the release that lane accepts instead (see "Large ticket on a lane
  with no review stage" below, #2779). A Step 4.0b
  stale-tip park (friction `review_tip_check: stale (<reason>)`, tier
  `small` or `large`) is a requeue an agent may run unless the row carries
  `scope_hint: large`: for `commits_after_clean_round` or `no_clean_round` a
  review round is needed, and for any other reason fix the push or network
  cause first. The exception is `tip_check_fetch_failed` (origin could not
  be fetched: refused credentials, an unreachable remote): it is released by
  a plain `requeue --stage finalize` once the cause is fixed, never
  `--stage review`, with the same agent-runnable / `scope_hint: large`
  labelling, since no review round can cure it (#2910). Dispatch never
  auto-completes either park, and the requeue cannot ship an unreviewed
  commit, because finalize re-checks the tip before every auto-merge arm
  (#2767, #2911).
- Which stage a review approval parks at: a `review_pending_approval`
  sentinel stamped `stage_reached: "stage3_review"` parks at REVIEW (the
  Claude-native and codex review lanes alike), where `approve` releases
  it. Only the finalize park above lives at FINALIZE, by design. A
  `stage3_review` sentinel reported by a row already at FINALIZE never
  reached the stage that row was dispatched for, so it also stays at
  FINALIZE, with the attention reason `earlier_stage_report` (#2762).
- A row whose stage is missing from its lane pipeline: `show` lists `show`
  and a plain `requeue` (`approve` and `requeue --stage` refuse it); the
  requeue is labelled operator-only for a human-kept gate (#2760).
- `review_health_gate`, `review_artifacts_stale`, `awaiting_operator`,
  `codex_must_fix_mechanically_rejected`: `requeue` (re-runs the stage), or
  `drain --held` for every such park of the client. `approve` never releases
  them.
- `branch_behind_main`: the branch conflicts with the default branch (a branch
  that is merely behind never parks here, #2979). Merge the default branch into
  it once and resolve the conflict (a merge, never a rebase or force-push),
  then `requeue`. Not drained, not approved.
- `ambiguities_pending_resolution` / `premises_pending_verification`:
  at the plan stage, `resolve --file <decisions.md>` (records the answers on
  the row and requeues); otherwise answer or verify on the ticket, then
  `requeue`.
- `merge_gate_blocked` / `stale_dispatch`: merge or close the open PR, then
  `requeue`. A `merge_gate_blocked` (or `prior_pipeline_pr_open`) park also
  backs the row off, 60 s doubling to 900 s per consecutive park (#2717);
  `requeue` bypasses that wait and the `reap_policy: auto` release clears it.
  The delays are not configurable. To turn the gate off for a client, see
  `merge_gate_enabled` (#2851) in `config/CONFIG_REFERENCE.md`.
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
  `requeue --from-completed|--from-failed|--from-cancelled`. A `COMPLETED`
  shipped row paged as `shipped_pr_checks_failed` is the exception: it is
  recovered with `recover-red-pr`, not `--from-completed` (see "Red shipped
  PR" below).

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
  transcript is flat, and no live process is working in its worktree. The
  `--routed-session-id` form closes the named session immediately, without
  waiting for the staleness bucket (#2708): it stops the worker and confirms
  it gone from the roster before flipping the session, and otherwise refuses
  with exit 1 and the reason on stderr. It does NOT verify transcript age or
  live non-subagent processes, so that liveness check stays yours to make
  before you order the close. `cw dev-queue approve` and
  `requeue` also close this session when run for the ticket (see the
  `worktree_occupied` paragraph).
- `wedge/pending-worktree-occupied` (#2516) — advisory only. A `PENDING` row
  that dispatch has skipped as `worktree_occupied` on 60 or more consecutive
  per-ticket ticks, the newest within the last 90s: a live session or daemon
  worker holds the ticket's worktree and never releases it. The finding has
  no session id; the client and the count are in its recipe. A plain
  `cw doctor` reports it on every run; `cw doctor --reap` changes nothing for
  it. Inspect with `cw dev-queue show <ticket> -c <client>`; if the holder is
  dead, `cw spawn close --confirmed-dead <id>`, then `cw dev-queue requeue` /
  `approve`. Also check `wedge/leaked-daemon-worker`. A stale run is not
  reported here: a loop that stopped ticking is the loop-liveness check's
  case.
- `wedge/operator-gated-park` (#2739) — advisory only. A `BLOCKED_ON_USER` row
  that holds a lane slot (`occupies_lane_slot`) while parked on a deliberate
  operator gate: disposition `finalize_gate_held`, `stale_dispatch_gate` or
  `attempt_cap_blocked`, or disposition `blocked` with a `codex_fix_*`
  `blocked_reason`. These rows look dead but wait on an operator, so
  `wedge/blocked-on-user-dead-session` skips them; this class is the visible
  counterpart, so a lane slot held by such a row is not left unexplained.
  A plain `cw doctor` reports it on every run; `cw doctor --reap` never
  changes the row's queue fields (status, disposition, blocked_reason,
  session_id, pr_url) through any reap selector. A session bound to the row is
  a separate surface: it is reaped only under class 6/8 (or
  `cw doctor --reap <SESSION>`) on that session's own evidence. The recipe is the row's own next
  command, as the `NEXT` column of `cw dev-queue tasks` shows it; a human-kept
  gate's command is labelled operator-only and is for the operator, not for an
  agent to run. A terminal_sibling row holds no slot and is the section
  below's case.

Run `cw doctor --reap --json` for machine-readable output.

To repair one wedged ticket without touching anything else, run
`cw doctor --reap --ticket <T> -c <C>` (#2716): it reverts, collapses,
cancels or reaps only rows and sessions of that (client, ticket), skips the
global reconcile, the leaked-worker sweep and the routed-result close, and
lists each check it ran and skipped as `scoped-reap/*`, led by
`scoped-reap/scope`, which echoes the resolved `client/ticket` and the
matched-finding count (`[WARN]` at zero, so check a mistyped `-c` or
`--ticket`; the exit code stays 0) (a class-6/8 session
whose record is unreadable or belongs to another client is skipped and named
in `scoped-reap/session-skipped`).

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
#    (actor, reason, reviewed SHA, finding identities). Stamp-only; it also
#    records an operator review approval bound to the verdict's reviewed
#    commit, so a large ticket passes finalize's approval check (#2779).
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

### Large ticket on a lane with no review stage (#2779)

A large ticket reaches finalize Step 4.0 with no review approval when its lane
pipeline has no review stage (for example `stages: [plan, impl, finalize]`):
nothing ever reviewed it, so Step 4.0 parks it `review_pending_approval`
(`review_approval_park: missing`). The usual release, `requeue --stage review`,
is refused there because review is not in the lane. `cw dev-queue show` lists
this lane's release instead, both steps operator-only:

```bash
# 1. Inspect the branch head you are about to vouch for.
git -C <ticket worktree> log --oneline

# 2. Record your approval, bound to that head as it stands. The row stays parked.
uv run cw dev-queue approve <TICKET> -c <CLIENT>

# 3. Ship it. Finalize re-checks the approval against the branch head.
uv run cw dev-queue requeue <TICKET> -c <CLIENT> --stage finalize
```

`approve` records `approver_kind: operator` bound to the worktree HEAD at
approve time (the park records no head) and prints that head. A later commit
other than a merge of the default branch parks finalize again; re-approve it
with the same command. The row records only `operator`; that this was a
review-less release is on the event log, as `ticket.approved` with
`reason: "review_less_lane_release"`. #3015 tracks recording it on the row and
binding to the parked head.

This release ships only under `review.convergence: blocking` (the default). On
a `clean`-mode client such a lane has no convergence ledger, so finalize Step
4.0b parks the row again with a stale-tip park (`no_clean_round`) after the
finalize requeue; that is the rule 0d park tracked in #3014.

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

### Clean-mode review convergence

A client configured `review.convergence: clean` (`clients.yaml`, #2787) runs the
Claude-native `/auto-dev-review` until a full review round returns no new
accepted findings, then one independent confirmation round (`cw review
confirm`, default backend `codex`), instead of stopping after a fixed number of
fix cycles. An interactive run on a `review.convergence: clean` client refuses,
so dispatch the ticket through `cw dev-queue` (`cw dev-queue add <T> -c <C>
--lane <lane>`); a headless run without a ticket id is refused the same way,
with a `blocked` / `agent_block` sentinel whose `details` carry that
instruction. A long review stage with a falling new-finding count is healthy;
the peek ladder does not apply its age-only verdicts to a live session there.

A clean-mode stop is terminal until released. Each stop, the reason it exits
with, and its release:

| Stop | Exit reason | Released by | Without a release |
|---|---|---|---|
| `max_cycles` | `review_blocked` | operator raises `review.max_cycles`, then `cw dev-queue requeue <T> -c <C>` | terminal: re-emitted until a commit lands |
| `operator_action` | `review_operator_actionable` | orchestrator runs the checklist, then `cw dev-queue requeue <T> -c <C>` | not applicable (the requeue is the signal) |
| `oscillation` | `review_not_converging` | `cw review converge operator-settle` for every recurring finding, then `cw dev-queue requeue <T> -c <C>`; or a commit | terminal while any recurring finding is unsettled |
| `no_progress` | `review_not_converging` | a commit (`cw dev-queue requeue <T> -c <C> --stage impl --regress`) | terminal, no other release |

Only a commit releases `no_progress`: a same-tip requeue re-emits it. The
`operator-settle` payload is printed ready to paste in the convergence history
comment and in `cw review converge status` (`stop.settle_payload`); it records
who settled the finding and why, is refused inside a headless worker, and can
only name a finding the ledger already recorded.

**Requeue procedure for `confirm_backend_unavailable`.** The independent
confirmation round could not run because its backend is down (codex missing, an
auth failure or another non-zero exit from the backend, a crash). Read the cause
from `cw dev-queue show <T> -c <C>`, restore the backend (installing or logging
in to codex is a host-level action the human may have to do), then run `cw
dev-queue requeue <T> -c <C>`; the ledger resumes at the confirm step with no
new fix cycle. `confirm_backend_unavailable` is uncharged, so verify the backend is restored before requeueing or draining.
Three same-cause parks at one sha are the cap; the next failure escalates to an
`agent_block`. A confirm round that outlasts the 600 s foreground Bash ceiling, fails on content on a healthy backend, or is rejected for prompt size (`input_too_large`, `file_too_large`) surfaces as `agent_block`, not as this reason.

**Confirm prompt-size rejections (#2864).** A confirm diff over the codex prompt limit is sharded per directory and reviewed shard by shard, so a large diff no longer fails the round. `input_too_large` or `file_too_large` is content-level and parks as `agent_block` with `actual_chars=<n> max_chars=<m>` in the details. For `file_too_large` split or shrink the named file, then requeue. `input_too_large` has two sources, and a bare requeue re-sends the same prompts and fails identically: (a) a rendered shard prompt still over the planner's limit (`CODEX_MAX_PROMPT_CHARS`), where the fix is on the input; or (b) a real codex size rejection of a prompt the planner thought fit, which means `CODEX_MAX_PROMPT_CHARS` is too high for the backend, so the fix is lowering that constant in code before the requeue, not the input. Setting `review.confirm_backend: claude` is an interim workaround that trades away other-family independence and can fail on size too; the Claude backend is not sharded.

**Lane review prompt-size handling (#2867).** A codex REVIEW lane's cycle-0 review (fix loop enabled or disabled) now gets the same `CODEX_MAX_PROMPT_CHARS` limit: a diff over it is sharded per directory and reviewed shard by shard, and the verdict carries one `ShardRecord` per shard (in clean mode the loop round records the shard tally, so `confirm_round_sharded` appears). A diff the planner cannot shard parks before any codex call as `agent_block` with `retry_eligible: false`, the cause (`file_too_large` naming the file, or `input_too_large`) and `actual_chars=<n> max_chars=<m>` in the details; nothing was reviewed, truncated or retried. A bare requeue fails identically: split or shrink the named file, or fix the oversize input, then requeue. A real codex size rejection of a prompt the planner thought fit stays the non-retryable `codex_review_unparseable` / `codex_review_partial` park with `codex_input_too_large` and the sizes in the details. A fix-loop re-review delta over the limit is sharded with the same planner (#2877); a delta that cannot be sharded parks as `agent_block` (`retry_eligible: false`) before any codex call for that cycle, naming the cycle, the cause, the file and the sizes. The fix commits through the failed cycle (including the commit whose delta went unreviewed) are already pushed and stay on the branch, and a requeue restarts at cycle 0 over the whole diff, which is re-planned from scratch (so it carries no inlined prior-findings overhead).

**Cycle-0 coverage gap (#2880).** A codex REVIEW lane whose cycle 0 left part of the diff unread (a reviewer role that failed, or a shard no role reviewed) and then entered the fix loop no longer exits clean when a later re-review comes back clean. A fix cycle's re-review covers only the delta since the last cycle, so it cannot clear a cycle-0 failed role or unreviewed shard; the loop parks as `codex_review_partial` (`retry_eligible` unset, no verdict comment) with `left part of the diff unread`, the failed role names and the unreviewed shard count in the details, and a `codex_cycle0_coverage_gap` WARNING in the log. This holds in every mode: blocking, `review.confirm_backend: none`, a non-independent confirm and clean mode, none of which reach a confirm round after the park. The fix is `cw dev-queue requeue <T> -c <C>`, which restarts at a fresh cycle 0 over the whole diff. In clean mode the cycle-0 round already spent a ledger cycle, so a deterministic gap (for example a shard that always times out) consumes one `max_cycles` slot per requeue until a shard-count cap lands.

**Re-review coverage gap (#2877).** A fix-loop re-review whose delta was sharded and left part of that delta unread (a reviewer role that failed in some shard, or a shard no role fully reviewed) is carried to the clean exit the same way: a later cycle's re-review covers only the changes made after it, so the loop parks as `codex_review_partial` (`retry_eligible` unset, no verdict comment) with `left part of its delta unread`, the cycle numbers, the failed role names and the unreviewed shard count in the details, and a `codex_delta_coverage_gap` WARNING in the log. When cycle 0 also left a gap, the cycle-0 sentence comes first and both WARNINGs are logged. An unsharded re-review is never carried. In clean mode the dirty cycle is recorded as an ordinary ledger round (the ledger's unread guard applies only to a would-be-clean round, so it does not fire on a dirty one) and the later clean cycle parks as `codex_review_partial` with the `left part of its delta unread` text and the `codex_delta_coverage_gap` WARNING, so the run never exits clean. The fix is `cw dev-queue requeue <T> -c <C>`.

#### Default-branch merge policy (#2979)

Three rules govern how a ticket branch meets `origin/<default-branch>` (ADR-0022):

1. **Behind is not a reason to act.** A branch (or PR) that is merely behind the default branch is never merged, rebased, force-pushed or parked for that alone. GitHub merges a clean branch itself. finalize Step 4c.5 treats `MERGEABLE` + `BEHIND` as no action and appends `behind_default_no_action` to `friction_highlights`.
2. **Merge at most once, and only on a conflict.** Every site that used to merge or rebase on "behind" first asks `cw worktree merge-probe`, and merges `origin/<default-branch>` (a plain `git merge`, never a rebase, pushed with a plain push) only when the probe reports a conflict. "At most once" is structural: after one merge `origin/<default-branch>` is an ancestor of HEAD, so every later probe says `up_to_date`. A merge that carries a conflict resolution stales the tip (see below); it is reviewed over the resolution alone by `cw review converge delta-confirm` when `verify-tip` reports `delta_confirm_eligible: true` (#2980), and by a normal full review round otherwise.
3. **A clean merge never causes a review.** The tip rule tolerates any merge of `origin/<default-branch>` that git itself would write, whatever files it touches and on every loop backend.

**The probe contract** (defined here once; every prose site cites it). `cw worktree merge-probe [--worktree PATH] --base BRANCH` asks whether merging `origin/<BRANCH>` into the worktree's HEAD would conflict. `--worktree` defaults to the current directory (`.`). It prints JSON `{"base", "base_sha", "state", "conflicted_files", "detail"}`; `base_sha` is the `origin/<BRANCH>` commit probed, so a transcript shows which tip the answer is about. States and exits: `up_to_date` (already merged) and `clean` (a merge would not conflict) exit **0**: no merge. `conflict` exits **1**: merge once and resolve. `unavailable` exits **3**: the probe could not tell (an unresolvable ref, a git error, a timeout, git older than 2.38); do not merge. **A shell site merges only when BOTH the exit code is 1 AND the JSON `state` is `conflict`** (capture stdout, read `.state`): exit 1 is also what a `cw` crash or a traceback yields, and a probe that never ran is not a conflict. Any other combination (exit 1 without a `conflict` state, exit 2, an empty or unparseable state) logs `merge_probe_unavailable` and merges nothing. It is read-only (`git merge-tree --write-tree` writes loose objects only, never the index or worktree), each git call is bounded by 10 s, and **the probe never fetches**. A stale `origin/<BRANCH>` makes it wrongly answer `up_to_date` or `clean`, so every shell site runs `git fetch origin <BRANCH>` (or `git fetch origin`) in the same fence first and captures its exit status; if that fetch fails, the fence does not probe, prints `PROBE_RC=skipped PROBE_STATE=skipped`, and the site logs `merge_probe_unavailable` and proceeds with no merge. The shell sites: `/prep-pr` Step 1, finalize's pre-PR sync, Stage 2's branch freshness and the review appendix's direct-execution fallback. The Python fix dispatch (`dispatch_fix_agent`) is the one exception: it fetches through the tick's bounded fetch gate and, when that fetch degrades (an expiry or a gate skip), still probes the local tracking ref and prepends a friction note that the check used a possibly stale ref. It merges nothing: a probe-reported conflict raises with the worktree untouched. `review-monitor` acts only on a PR GitHub already reports `DIRTY`, so it merges once without the probe.

**Friction tokens.** `behind_default_no_action` (Step 4c.5 saw `MERGEABLE` + `BEHIND` and did nothing) and `merge_probe_unavailable` (the probe exited 3, or the fetch before a shell site's probe failed, and the site proceeded with no merge).

**The tip rule.** `cw review converge verify-tip` and `finalize` accept a tip that is past the last clean round only when everything after it is a merge of `origin/<default-branch>` that git itself would write: a two-parent merge whose second parent is on the default branch, for which `git merge-tree --write-tree` of its parents exits 0 with exactly the merge commit's own tree, and no commit of the branch's own in the span. Which files the merge touched does not matter, and there is no exempt path list (the old `CHANGELOG.md` / `merge_gate_ignore_paths` exemption is gone; `merge_gate_ignore_paths` now feeds only the Stage 4a merge gate). `git merge-tree` honours `.gitattributes` merge drivers, so a `CHANGELOG.md merge=union` merge is reproduced and tolerated. A conflict-resolution merge (including a Step 4c.5 one), a hand edit in a merge, an octopus, any non-merge commit, a merge-tree timeout and any unreadable context or git state park finalize as `review_tip_check: stale`, released by `cw dev-queue requeue <T> -c <C> --stage review`, which runs a normal full review round. A conflict-resolution merge or a hand edit is first offered to the conflict-resolution round below; it parks only when that round is refused, fails, finds something or leaves `verify-tip` stale. A fetch of origin that is refused or cannot run is the reason `tip_check_fetch_failed` (#2910), released by `--stage finalize` instead. A tolerated merge shows as `default_merge_tolerated: true` in the JSON and `review_tip_check: default_merge_tolerated` in `friction_highlights`; `final_round_clean_sha` stays the reviewed commit, not the shipped HEAD. The rule itself is unchanged by the `repair_changelog_conflict` carry (#2921): that recipe is the only writer of a remapped clean sha (see the repair-fence paragraph), and for such a ledger `final_round_clean_sha` is the carried head, recorded in `clean_sha_remaps`, not a sha any round reviewed. A client that wants the strict rule for every merge sets `review.default_merge_tolerance: false` in `clients.yaml`.

`cw review converge init`, `converge status`, `verify-tip` and the REVIEW staleness gate (`review_artifacts_stale`) share the one predicate (#2861), for every loop backend: the claude-native allow-list is gone (#2979). A converged ledger whose HEAD moved past `last_clean_sha` by clean default-branch merges only stays `converged`: `init` keeps it and reports `default_merge_tolerated: true`, `status` reports `next_action: done` with the same key. Any other move reopens it as `in_progress`, so another full round runs. The staleness gate passes a ticket whose `review.reviewed_sha` is behind HEAD only when the worktree ledger is converged at that sha and the move is tolerated; it derives the tolerance from the live client config (`review.convergence: clean`, `review.default_merge_tolerance`, `default_branch`), while `init`, `status` and `verify-tip` read the same fields from the worktree's cw-context. `review.default_merge_tolerance: false` is strict on every path. The ledger and the worktree git state are worker-reachable, so the pass is not tamper-proof. The gate does not fetch `origin/<default-branch>`, so a stale local tracking ref parks the ticket where the fetching `verify-tip` might accept; that is the strict direction. `verify-tip` and `finalize` (not `init` / `status`) do better: when a move reads stale for `non_merge_commit`, `second_parent_not_on_default` or `no_default_ref`, the signature of a clean merge of default-branch commits newer than the local tracking ref, they run one bounded (10 s) `git fetch origin <default-branch>` and ask again once (the fetch names an explicit `+refs/heads/<default-branch>:refs/remotes/origin/<default-branch>` refspec, so a single-branch clone, whose configured refspec tracks only the ticket branch, still gets its tracking ref); a failed fetch keeps the stale verdict. The gate's tip check runs git under `dev_queue_lock`: every call (rev-parse, merge-base, rev-list, merge-tree) has a 10 s timeout, and the per-merge `merge-tree` loop runs under a 30 s wall-clock budget checked before each merge (so the worst case is the four git calls before the loop plus the budget plus one merge, up to three git calls of 10 s each); a timeout or a spent budget reads as stale (`merge_tree_unavailable` or unreadable, fail closed).

**Diagnosing a tip that reads stale despite a clean merge.** Run `cw review converge verify-tip` in the worktree and read `detail`: `verify-tip: not a clean merge of origin/<default-branch>: <reason>` names the first offending commit. `not_textually_clean <sha7>` means git does not reproduce that merge (a conflict resolution, a hand edit, or one of the false conflicts below); `non_merge_commit <sha7>` is a commit of the branch's own; `second_parent_not_on_default <sha7>` merged something other than the default branch; `not_two_parent <sha7>` is an octopus; `merge_tree_unavailable <sha7>` means git could not answer (a timeout, git older than 2.38); `no_default_ref`, `clean_not_ancestor`, `no_merge` and `git_unreadable` are state problems. A `not_textually_clean <sha7>` that `cw review converge delta-confirm` could review alone also carries `delta_confirm_eligible: true` and `resolution_files` (#2980); the `detail` is the same either way.

**The `branch_behind_main` REVIEW gate (#1823).** It parks a REVIEW ticket only when the merge probe reports a conflict; a branch that is merely behind, or whose merge would be textually clean, advances. The probes run in `consume_completed_sessions` before `dev_queue_lock` is taken (the precompute-outside-the-lock pattern of `dispatch/pr_gate`, ADR-0022 invariant 3), at most 20 per tick, under a 30 s budget checked before each probe (so the worst case is the budget plus one probe, up to three git calls of 10 s each), and every git error, a skipped probe and an unprobed row fail open (no park).

**The retired delta confirm round (#2918).** Its only eligible shape, a textually clean merge that overlaps the branch's files, is now always tolerated, so `next_action: delta_confirm`, `overlapping_files` and `delta_confirm_pending` are gone. A `clients.yaml` that still sets `review.default_merge_delta_confirm` keeps loading: the key is stripped with a one-time warning; delete it.

**The conflict-resolution round (#2980).** `cw review converge delta-confirm --ticket-id <T> -c <C>` reviews only what the resolver did in the one default-branch merge after the last clean round that git would not write itself: the diff from the tree `git merge-tree --write-tree` writes for its parents (conflict markers left in) to the merge commit's own tree, with the branch's own hunks in those files as context. It runs one pass on the ledger's confirm backend (codex or Claude, whatever the loop backend), records a `kind=delta` round itself (a worker cannot self-report one) and moves `last_clean_sha` to HEAD on a clean round; findings reopen the loop. Finalize Step 4.0b runs `delta-confirm` when `verify-tip` reports `delta_confirm_eligible: true`, then re-runs `verify-tip`, which alone decides. It refuses (exit 1, nothing written) with `tolerance_off` (read from the live client config, never the cw-context), `ledger_not_confirmable`, any `default_merge_rejection` span or per-merge reason, `no_resolution`, `multiple_resolutions`, `empty_resolution`, `markers_left` (an added line is a conflict marker) or `git_unreadable`; exit 3 and 4 match `cw review confirm` and record nothing, and the full review round is the fallback. It has no config key; `review.default_merge_tolerance: false` turns it off with the rest of the tolerance.

**Accepted limitations.**

- **Strict-protection repos without a merge queue.** GitHub reports `BEHIND` only when the repo requires up-to-date branches. On such a client without a queue, a `BEHIND` PR cannot merge and auto-merge armed at Step 4d waits. Rule 1 is unconditional (no per-client key). The workaround is the operator's "Update branch" button: a clean merge, which never stales a ledger.
- **False conflicts are fail-closed.** A GitHub update-branch merge, a `rerere`-resolved merge, an author-local `-X` strategy option or custom merge driver, or a git-version difference in the ort strategy can make a clean merge look hand-edited, so the tip reads stale and a normal review round runs. It is not overridable per merge, by design; the only knob, `review.default_merge_tolerance: false`, is stricter. Diagnose with `verify-tip`'s `detail` as above.
- **`/prep-pr` has no Step 4c.5 backstop.** Its `unavailable` fallback (proceed with no merge) leaves the PR's mergeability state as the only later check.
- **The codex lane's own post-loop reconcile stays strict.** `codex_clean_confirm` passes no tolerance right after a full codex loop reviewed HEAD, so a clean merge landing between that loop's end and its confirm round reopens the ledger: a narrow window, fail-closed.
- **The `branch_behind_main` gate runs only on the consume path** (the dispatch tick and `cw dev-queue wait`). The single-event settles (`cw/cli/spawn.py`, `cli/dev_queue/_settle.py`) and reconcile's sentinel recovery install no verdict, so the gate does not park there; finalize Step 4c.5 handles a conflict downstream. It also probes only rows whose `worktree_path` is stamped (USER-origin rows); dispatch-driven rows carry none, so the gate stays inert for them, as it was (#2990 tracks making it probe them).
- **PR attention follows the same behind policy (#2991, #2996).** `pr_hydrate` Row 1 and review-monitor's `merge_blocked` attention state are limited to DIRTY conflicts. A healthy BEHIND PR does not trigger `escalate_merge_block` or immediate review-monitor escalation; failing CI and change requests retain their `ci_failing` and `changes_requested` states while BEHIND. Neither path merges or rebases the default branch for BEHIND alone.

#### Review-approval claim gate (#2773)

Finalize Step 4.0 refuses to ship a `large`-tier ticket whose review approval
is missing or stale, but it runs inside the finalize worker. The claim gate
applies the same predicate before the worker starts. One function,
`cw.review_approval.evaluate_review_approval`, backs all three evaluators:
`cw review check-approval` (what Step 4.0 now calls), this gate, and the repair
recipes' fence.

- **What it checks.** A PENDING `Stage.FINALIZE` row on a lane whose pipeline
  has a `review` stage, and whose tier is `large` on the row (`scope_hint` or the stamped `computed_scope_tier`) must carry an
  approval whose `reviewed_sha` is an ancestor of the branch head, with no
  non-merge work after it and only merges of `origin/<default-branch>`. HEAD is
  the local branch ref in the client git dir, falling back to
  `origin/<branch>` only when the local ref is absent. The gate never fetches.
- **The park.** A `missing`, `stale` or `unreadable` verdict parks the row
  BLOCKED_ON_USER with disposition `review_approval_gate` and `blocked_reason`
  `review_approval_missing_pre_dispatch`, spawns no worker, charges no attempt,
  and emits `session.needs_attention` (`approval_token`, `branch`, `details`).
- **The release.** `cw dev-queue requeue <T> -c <C> --stage review`, operator
  only. It returns the ticket to review for a fresh approval, so it costs a
  full re-review. A plain finalize requeue keeps the old record and re-parks.
- **Lanes with no review stage are not gated.** That release is refused when the
  lane has no `review` stage, and the gate's park binds no session for
  `approve` to act on, so the gate skips such a row (client or lane `stages`
  without `review`). Step 4.0 in the worker parks it as before and the
  review-less release in "Large ticket on a lane with no review stage (#2779)"
  above applies.
- **Deferral.** A git timeout, `OSError` or git error does not park: the claim
  is deferred and the row stays PENDING. A WARNING names the row once; after
  five consecutive deferred ticks the hold becomes a real `unreadable` park so
  an operator is paged. A restart resets the streak, which only delays the page.
- **Per-tick cap.** At most 8 rows are probed per client per tick (10 s per git
  call). Rows past the cap, and every row after the first transient fault of a
  tick, are deferred, never claimed unevaluated.
- **No kill switch.** The gate is always on and has no config key: it only
  affects large rows lacking an approval that Step 4.0 would already park, and
  a backstop an operator can silently disable is not a backstop. If a false
  park is observed in production, file a ticket for a toggle; the relief meanwhile
  is the labelled requeue.
- **Residual gaps** (the worker's Step 4.0 still covers the first two):
  (a) a row with no tier on the row is not gated at claim (one WARNING names
  it); (b) a worker-derived large tier the row does not show; (c) an approval
  invalidated after the claim, because `arm_automerge` is unchanged; (d) a raw
  `gh pr merge --auto`; (e) a stale `origin/<default>` after a failed freshness
  fetch can read finalize's own Step 4c sync merge as `stale`; (f) a repair-path
  row with an approver kind set but `review_approved_at` None is judged by its
  plain row fields, where the gate and cw-context read it as no approval.
- **Upgrade note.** Step 4.0 needs a `cw` on PATH that has `review
  check-approval`. On a missing or older `cw` every large-tier finalize parks
  `unreadable` ("cw is missing or older than this command"); upgrade cw on
  every worker host before relying on the new doc.

#### Codex-lane clean mode (#2791)

A codex REVIEW lane on a `review.convergence: clean` client keeps its own codex
fix loop, then the detached `cw codex run` driver records that loop in the
convergence ledger (`.cw/review-convergence.json`) and runs the confirmation
round itself, so finalize's `verify-tip` finds a converged ledger. The
opencode lane drives the ledger from its own prompt (#2806) and the `local`
lane is blocked (both below).

- **Which backend runs.** With `review.confirm_backend` unset the confirm round
  is Claude: one `claude -p` call per reviewer role, `cwd` at the task worktree,
  tools `Read,Grep,Glob` only, `--permission-mode dontAsk`, empty settings
  sources (no operator or project hooks), no MCP servers, under the operator's
  own Claude login. The prompt carries the diff from the ledger's pinned diff
  base (the merge-base with the default branch), the approved plan (`.cw/plan.md`), the ticket title and body, the ticket's
  comments (every comment the pipeline did not post itself, fetched live from
  the tracker), the finding dispositions an operator already settled, and the
  repo's review configuration (reviewer agent specs, `.claude/review-extras.md`,
  the `CLAUDE.md` quality gates and ruff settings). The file tools can also
  read any file under the task worktree, untracked and gitignored files
  included. Claude Code (not cw) enforces the worktree
  boundary: a direct read of a path outside the worktree was denied on the
  `claude` version where the envelope was captured, but symlinks inside the
  worktree that point outside it were not exercised, so their targets are not
  verified as unreadable. On a codex lane the only hard boundary is the
  `review.confirm_backend` opt-out (`codex` or `none`); on an opencode lane
  only `none` avoids egress. This is
  default-on egress for every codex-lane clean client. The confirm runs on the
  client's `worker_model` (`--model <id>`) when that is set, and on Claude's
  own default model otherwise.
- **Opt-out.** `review.confirm_backend: none` or `codex` converges on the clean
  codex round alone (the sentinel records `confirm_round_opt_out` or
  `confirm_not_independent`). Nothing is sent to Claude.
- **Confirm spend is uncapped (#2812).** The Claude confirm round spends Claude
  usage under the operator's own login with no ceiling, by decision (ADR-0021).
  The bounds are `max_cycles`, the wall-clock budget (only when one is passed;
  dispatched lanes pass none) and the loop iteration backstop
  (`effective_max_cycles + 1` loop runs). The cost is the CLI's self-reported
  `total_cost_usd`, visible only in the INFO log line `claude review role ...
  finished in ... (total_cost_usd=...)`; it is not recorded in the ledger or in
  `cw review history`. To bound cost today, set `review.confirm_backend: none`
  or `review.convergence: blocking`. `cw review confirm --backend claude` is
  covered by the same decision.
- **Ledgers written before #2800.** A ledger with no `diff_base` needs no
  operator action: `cw review confirm` (like `cw review converge diff-base` and
  the codex gate) back-fills it from the live merge-base and records it; it
  exits 1 only if the base cannot be computed.
- **What a requeue does.**

| Ledger state at the requeue | What the driver does | Why |
|---|---|---|
| `awaiting_confirm` at HEAD, `review.confirm_backend` unchanged since the ledger was stamped (after a Claude outage or a round that could not complete) | skips the codex loop and runs only the Claude confirm | the clean codex round at this sha is already recorded |
| `awaiting_confirm` at HEAD, but `review.confirm_backend` now differs from the one stamped on the ledger (for example `none` or `codex` set after an outage) | re-runs the codex loop; the post-loop gate then converges on the clean loop round without calling Claude (opt-out) or confirms on the new backend | the stamped choice must not run a Claude call the client no longer consents to; the loop can park on a fresh MUST_FIX before the gate is reached |
| `in_progress` after a dirty Claude confirm at HEAD (the requeue of a park on a Claude MUST_FIX), `review.confirm_reentry` on (the default) | re-runs the codex loop seeded with that confirm round's MUST_FIX findings, then the confirm | the loop is the only fixer on this lane, so the Claude findings are fed into its first fix prompt (#2807) |
| anything else (a stop released, HEAD moved, or a dirty confirm with `review.confirm_reentry: false`) | re-runs the codex loop, unseeded, then the confirm | the codex review alone decides what the loop fixes |

- **Claude findings re-enter the loop (#2807).** After a codex loop, a dirty
  Claude confirm does not park the run: the driver re-enters the codex fix loop
  in the same session with the confirm round's accepted MUST_FIX findings as
  seeds (read from the round's persisted verdict; findings an operator settled,
  SHOULD_FIX and operator-actionable ones are left out), then runs the confirm
  again. Each seeded loop adds an INFO log line `claude confirm feedback
  re-entry` and a `claude_confirm_feedback: round=<n> findings=<k>` friction
  line. One exception: a requeue that finds the ledger `awaiting_confirm` at
  HEAD (the first table row, after a Claude outage) runs only the confirm and
  returns before any loop, so a dirty confirm there still parks with
  `codex_must_fix_findings`; the next requeue then seeds the loop from the
  persisted verdict. The re-entry stops on the ledger's own stops:
  `max_cycles` (each dirty confirm is charged one fix cycle) and oscillation
  (Claude re-finds a finding after a fix commit). The wall-clock bound applies
  only when a budget is passed (`cw codex run --wall-clock-budget-seconds`):
  then there is no re-entry with less than the fix-cycle floor left, and each
  re-entered loop gets what remains. Dispatched lanes pass no budget, so there
  the bounds are `max_cycles`, oscillation and the `effective_max_cycles + 1`
  loop-run backstop. Worst case is `effective_max_cycles + 1` loop runs (9 at
  the clean default), each with a full codex review and one Claude confirm;
  because every dirty codex pass is a ledger round (#2816, next bullet), the
  fix cycles dispatched per epoch are bounded by `max_cycles` across all of
  them, and no-progress can stop the run too. The persisted result is the last
  gate's outcome.
  `review.confirm_reentry: false`
  restores the old behavior: the run parks on the first Claude MUST_FIX and a
  requeue re-runs an unseeded, nondeterministic codex loop that can park on
  the same finding again. The key is inert in `blocking` mode and needs the
  lane's codex fix loop enabled.
- **Every codex fix-loop cycle is a ledger round (#2816).** Each dirty codex
  pass is recorded as a `loop` round and charged one fix cycle: cycle 0, and
  every re-review that still has open MUST_FIX findings. So all three
  detectors apply inside the loop, not only between loop runs: no-progress,
  oscillation and `max_cycles`. A stop parks the loop at once as
  `review_not_converging`, with the ledger's release hint in
  `blocker.details` and a `codex_loop_round_stop: decision=<d> round=<n>`
  friction line. Oscillation is stricter than before: a finding the loop
  tried to fix that the next re-review raises again at the new sha stops the
  ledger, so a clean-mode codex ticket can park sooner than it used to (an
  operator-settle releases it, per the stop table). Claude seeds are not
  recorded again at cycle 0 (the dirty confirm round already charged them).
  The final clean pass is still recorded by the post-loop gate, and a ledger
  bookkeeping failure inside the loop parks `codex_convergence_ledger_error`.
  The codex loop's own divergence guard and growth budgets remain as additional
  guards; under clean mode the cumulative growth budget is anchored at the
  run's first loop and spans every re-entry. Findings the ledger already
  settled stay settled in every loop pass. A loop that parks for another
  reason keeps the rounds it recorded. Blocking mode is unchanged: no ledger
  is read or written.
- **Claude down.** The park is `confirm_backend_unavailable` with
  `blocker.details` starting `backend=claude;`. Follow the requeue procedure
  above (restore `claude` on the serve host, then requeue); the cap and the
  `agent_block` escalation are the same. A worktree changed by the reviewer
  parks, on a codex lane, as `codex_review_worktree_mutated` instead (on an
  opencode lane `cw review confirm` exits 4 with cause `worktree_mutated` and
  the agent parks `agent_block`): inspect `git status` and
  `git log`, revert anything the reviewer process wrote, then `cw dev-queue
  requeue <T> -c <C> --stage review`.
- **`codex_convergence_ledger_error`.** A ledger, git or file step failed after
  the codex review completed. The codex verdict is still posted. A failed save
  is transient; a foreign or corrupt ledger, or a verdict that is not at HEAD,
  repeats identically, so read `blocker.details` first, fix the cause (for a
  foreign ledger, move `.cw/review-convergence.json` aside), then `cw dev-queue
  requeue <T> -c <C> --stage review`. The park is charged.
- **`--override-must-fix` is refused** on a clean client unless the worktree
  holds a converged ledger for the ticket that is clean at the verdict's
  reviewed commit (a missing, unreadable, foreign, non-converged or
  other-tip ledger refuses before any write): `verify-tip` would still park
  after the override, and an override does not converge the ledger. Exits:
  `cw dev-queue requeue <T> -c <C> --stage review` (with
  `review.confirm_reentry` on, the requeued loop is seeded with the Claude
  findings), or, for a Claude finding you judge a false positive, settle it
  first with `cw review converge operator-settle` (it matches a finding in the
  ledger's fix memory as well as a stopped ledger's recurring ones), then
  requeue: the next confirm stamps the settled finding and the round comes back
  clean. Or switch the client to `review.convergence: blocking`. `cw review
  settle` records finding dispositions on the row only; it does not converge
  the ledger.

#### Opencode-lane clean mode (#2806)

An opencode REVIEW lane on a `review.convergence: clean` client gets a separate
converge-to-clean REVIEW prompt (`cw.opencode_clean_review`); the blocking
prompt is unchanged. The opencode agent is both reviewer and fixer and drives
the convergence ledger itself, with the same `cw review converge` primitives as
the Claude-native prose stamped `--loop-backend opencode`: `config` (a guard:
anything but `clean` parks `agent_block`), `init`, `status` (it obeys
`next_action`), findings-form `record-round` loop rounds recorded before any
fix, the confirm round, then `finalize`. opencode runs fire-and-forget with no
cw wrapper process, so `finalize` / `verify-tip` is the fail-closed backstop
against a skipped step, and the independent confirm round cross-checks the
agent's own loop round.

- **Confirm default and egress.** opencode has no confirm family, so an unset
  `review.confirm_backend` resolves to `claude`: the confirm round is
  `cw review confirm --backend claude`, with the same Claude reviewer sandbox
  and prompt payload as the codex lane (above), under the operator's own Claude
  login. Explicit `claude` and `codex` are both independent of the loop.
  `confirm_backend: codex` is not an opt-out on an opencode lane: it sends the
  same context to Codex; only `none` avoids egress, and it forfeits the
  independent round (the ledger converges on the opencode agent's own loop
  round alone, so the sentinel records `rounds_run: 1` and
  `confirm_backend: "none"`). Upgrade note: a client already on
  `convergence: clean` with an opencode REVIEW lane has tickets parked on
  `clean_review_unsupported_backend`; those tickets begin Claude egress on
  their first requeue after the upgrade. Set `review.confirm_backend: none`
  before requeueing to avoid it. Confirm spend is uncapped, as on the codex
  lane (ADR-0021).
- **Which backend the prompt passes.** At the confirm step the agent reads the
  ledger-stamped backend with
  `cw review converge status | jq -r .sentinel_review.confirm_backend` and
  passes it as `--backend` (the CLI default is `codex`), because `record-round`
  accepts a confirm round only from the ledger's backend. A `none` ledger never
  reaches the confirm step; if one does, the agent parks `agent_block` without
  calling `cw review confirm`. The confirm verdict is recorded with
  `record-round --defer-should-fix`, which records each SHOULD_FIX as deferred
  (non-gating) and an undecided MUST_FIX as a fix, the codex gate's rule.
- **Foreground confirm ceiling.** The confirm is one foreground Bash call with
  the shell `timeout 600` wrapper and the tool timeout set explicitly to
  600000 ms, never backgrounded. An overrun (exit 124, or the tool timeout
  ending the call) parks `agent_block` with
  `confirm_invocation_error: exit=timeout`, never
  `confirm_backend_unavailable`. opencode's own shell-tool timeout, as read
  from the bundled source of the opencode 1.18.32 binary (not a live run): the
  tool takes an optional per-call `timeout` in milliseconds, defaults to
  120000 ms (overridable with `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS`),
  rejects only negative values and has no maximum, so the explicit 600000 ms
  should hold. This is unverified on a live run and on other opencode
  versions. If a host's opencode caps the tool below 600 s, every confirm round
  parks `agent_block` (fail-closed, never a false converged ledger) and each
  attempt still spends uncapped Claude usage, so watch the first live run.
- **Requeue and resume.** A requeued session re-runs `converge init` and obeys
  `converge status`'s `next_action`: `confirm` resumes at the confirm round,
  `done` goes straight to `finalize`, `stopped` re-emits the stop, and `fix` is
  treated as a fresh loop round (opencode loop rounds are findings-form, so
  there is no per-round verdict file to replay; the cost is at most one extra
  loop round at the same sha). Residual risk: a findings-form round persists
  counts only (no `round-<n>-verdict.json`, and no verify-fixes on this lane),
  so a recorded-but-unfixed MUST_FIX is not remembered across the resume. If
  the fresh same-sha round does not re-raise it, `record-round` can record a
  clean round, and under `confirm_backend: none` the ledger then converges on
  that loop round alone, so `verify-tip` passes at a sha that still carries the
  bug. The risk is worst under `confirm_backend: none`; with a `claude` or
  `codex` confirm backend the independent confirm round is the mitigation.
- **Typed exits.** `review_not_converging` (`oscillation`, `no_progress`;
  `retry_eligible: false`), `review_blocked` (the `max_cycles` backstop, naming
  the cap and `fix_cycles_used`), `review_operator_actionable`
  (`operator_action`), `confirm_backend_unavailable` (the confirm backend is
  down, recorded with `converge confirm-failed`, `retry_eligible: true`; a host
  without the configured confirm binary parks here) and `agent_block` for
  everything else (a confirm round that failed on a healthy backend, a usage
  error, an overrun, a `record-round` refusal, a failed guard or a missing
  `jq`). Stops quote `stop.release_hint` and `stop.settle_payload`; release
  them as on the other clean-mode lanes. The opencode lane posts no tracker
  history comment and runs no `cw signal-park`.
- **`confirm_reentry` is inert** on opencode lanes: there is no codex fix loop
  to re-enter. A dirty confirm sends the agent back to its own loop round in
  the same session.

#### Local REVIEW lanes under clean mode (#2797)

The `local` (aider) REVIEW lane writes no convergence ledger. On a clean client
a REVIEW-stage spawn on it is blocked before any agent runs, with
`clean_review_unsupported_backend`.
`cw doctor` reports such a lane ahead of time as
`[FAIL] review-convergence/<client>/<lane>`.

- **Requeue procedure for `clean_review_unsupported_backend`.** A bare `cw
  dev-queue requeue` re-dispatches into the same block. Change the config
  first: set `review.convergence: blocking` for the client, or give the lane a
  `claude-native`, `codex` or `opencode` REVIEW executor in `clients.yaml`. Confirm with
  `cw doctor` that the lane is no longer `[FAIL]`, then `cw dev-queue requeue
  <T> -c <C> --stage review`.

### Reading a review's history (cw review history, #2790)

`cw review history <T> -c <C>` replays one ticket's review round by round. Read
it before adjudicating a `review_not_converging` park, or whenever a review ran
long and you want to know where the rounds went. It is read-only: no state
lock, no subprocess, no event recorded (the event inbox is read under its own
short leaf lock, as every inbox reader does). `-c` may be omitted when only one
client queues the ticket.

The text form prints a header (`state`, `mode`, `rounds`, `converged`,
`final_clean_sha`, `stop`, `new_accepted_by_round`, `fix_cycles`,
`confirm_backend`, `deferred`, `confirm_failures`, `sources`), one indented
block per round (reviewers, reviewed sha, new findings with their
MUST_FIX/SHOULD_FIX split, fixes dispatched, findings settled that round with
their rationale, wall time), then `settled_other` (operator settles, and any
settled entry whose round has no block), `dispositions`, `voided`, `releases`
and `notes`. Every key is printed, `-` when absent; nothing is truncated. The
`deferred` header is the ledger's count, `N (text not recorded)` when only the
sentinel reports it (a blocking-mode review keeps no finding text), or `not
recorded`.

Reading the trend: `new_accepted_by_round` falling toward 0 is a review that is
converging. A series that stalls or comes back up, together with the ledger's
own `stop` (`oscillation`, `no_progress`, `max_cycles`, `operator_action`), is
a review that is not; the view adds no classifier of its own. "Fixes
dispatched" is what the ledger records: a fix cycle was dispatched for those
findings, not that each fix was verified. Wall time runs from the previous
round's record to this one, so it includes the fix cycle in between; round 1
runs from this row's own `s3_review_started` event, or is not recorded.

The Claude confirm round's spend cost is not recorded in the ledger or in this
view, so the history cannot show it (#2812; see ADR-0021). The only trace is
the INFO log line of each reviewer role.

Sources, in precedence order: the converge ledger in the ticket's worktree
(`--worktree PATH` overrides the lookup of the row's worktree, its bound
session's worktree, then the derived path), with each round's verdict file for
the severity split; when the ledger is absent, unreadable or owned by another
ticket (for example a worktree GC already removed it), the newest session
`AUTO_DEV_RESULT` of the row (its `review` block and `review_round_<n>` friction
lines); the row's `finding_dispositions` are always listed on their own. A
blocking-mode review records no per-round data, so it shows one `initial` entry
from its cycle-0 counts and a `rounds_unrecorded` note.

Events are shown only when they can be tied to this row. A ticket id is unique
per client, not globally, and the event inbox is global, so a
`stage.entered` event is used only when its `session_id` names one of the row's
sessions, and a `review.finding_voided` event only when its finding is already
in the row's ledger `settled` or `finding_dispositions` AND that record's
rationale is the voided-suppression text naming the event's operator comment
(it starts with `VOIDED_RATIONALE_PREFIX`, the comment id and a space). Its
`file`, `summary` and `severity` are then taken from the row's record, not the
event. A record whose rationale is un-prefixed (an ordinary rejection, such as
a finding rejected before it was voided) matches by content only and cannot
rule out another client's ticket with the same id, so that event is never
rendered: `notes` counts it as a content-only tie. Every other event of the
ticket id appears only as a count in `notes`, as one that could not be tied to
the row.

`--json` emits the `ReviewHistory` model (`cw schema show review-history`):
`schema_version`, `ticket_id`, `client`, `row_status` (the row's status
value), `row_stage` (the row's stage value), `in_flight` (ledger `in_progress`
or `awaiting_confirm`; with no usable ledger, a RUNNING row at the REVIEW
stage), `rounds` (each with `round`, `kind` of `loop`/`confirm`/`initial`,
`source` of `ledger`/`sentinel_friction`/`sentinel_cycle0`, `backend`,
`reviewed_sha`, `roster`, the ledger counts, `settled_total`,
`new_by_severity` (null when no verdict file was recorded or it disagrees with
the ledger), `fix_dispatched`, `clean`, `recorded_at`, `wall_seconds`, and
`settled`), `summary` (`convergence_mode`: `clean` with a usable ledger or a
clean sentinel, `blocking` with no usable ledger and a sentinel `review` block
that is blocking or omits the mode, `unknown` with neither; `rounds_run`,
`converged`, `final_clean_sha`, `ledger_status`, `stop_reason`,
`stop_identities` as `<severity>::<file>::<summary>` keys, `fix_cycles_used`,
`max_cycles`, `confirm_backend`, `new_accepted_series`, `deferred`,
`deferred_count`: `len(deferred)` when a ledger was read, else the sentinel's
`review.deferred`, null when neither was read,
`releases` (`at`, `round` = rounds recorded at the release, `reason`, `cause`,
`sha`), and `confirm_failures`: every confirm-backend outage the ledger ever
recorded, null when no ledger was read), `settled`, `dispositions` (`key`,
`file`, verbatim `summary`, `outcome` of `ACCEPTED`/`REJECTED`/`REVERSED`,
`rationale`, `actor`, `recorded_at`, `reviewed_sha`, `""` meaning not
recorded), `voided` (`severity`, `file`, `summary`, `original_rationale`,
`voided_at`, `operator_comment_id`, `event_at`), `sources` (the sources that
contributed, each once, in the order `ledger`, `verdict_files`, `sentinel`,
`row_dispositions`, `events`; `[]` means nothing was readable) and `notes`
(every degradation and every unattributed event count). Settled placement:
top-level `settled` lists every ledger entry once, in ledger order; a round's
`settled` lists only the adjudication-origin entries of that round (an
operator settle carries the number of rounds recorded when it was made, not a
round that settled it); `summary.deferred` is the `defer` subset of the
top-level `settled`. Null means the source holding that value was not read or
does not record it. Adding a field is allowed; renaming or removing one bumps
`schema_version`.

There is no `--watch`: nothing emits an event per recorded round yet, so
re-run the command to see new rounds.

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

### Red shipped PR (#2905, RFC 0016 B2)

A `COMPLETED` row whose PR was shipped with auto-merge armed can go red
*after* finalize returned: finalize skips Stage 5 headless, and CI runs minutes
to hours later. Nothing used to tell the operator. The dispatch loop now runs a
read-only sweep right after PR-state hydration (`cw.shipped_pr_signal`) and
raises one page per episode.

**What it looks like.**

- `cw orchestrate watch` shows a `session.needs_attention` line with
  `paused_status` `shipped_pr_checks_failed` and `reason="<check name>"`
  (JSON-quoted, at most five names). The line ends in the first legal next
  command, `cw dev-queue recover-red-pr <T> -c <C>`.
- `cw queue peek` lists the row as `RED_SHIPPED_PR`, naming the PR, the
  failing required checks and the command. It is never a stop candidate and
  has no transcript to peek: the session finished. Do not `cw spawn close` it.

**The command.**

```bash
cw dev-queue recover-red-pr <T> -c <CLIENT>
```

It refuses unless the row is `COMPLETED` with `disposition: shipped`, the PR is
OPEN with a live failing required check, the lane pipeline has impl and
review, the impl executor is claude-native (opencode and local ignore the
feedback; #2983) and no daemon session is live for the ticket. Then it:

1. synthesizes head-pinned CI-failure feedback with the shared synthesizer
   (`cw.pr_feedback`), reading the failing checks live;
2. disarms auto-merge on the PR and verifies by read-back from GitHub; if the
   read-back shows it still armed (or unreadable) the command refuses with the
   manual `gh pr merge <N> --disable-auto` and leaves the row untouched;
3. regresses the row to impl through the same `_stage_regress` an operator
   `--regress` uses (so `regress_attempts` and the FINALIZE regress cap count),
   attaching the feedback, and records `ticket.requeued` with
   `reason: cli_recover_red_pr`.

No queue state is hand-edited.

**Why a fix is still re-reviewed.** The regress revokes the review approval
and the pipeline walks IMPL, REVIEW, FINALIZE again. A commit past the last
clean round fails the tip rule (`commits_after_clean_round`), and finalize's
open-PR reuse path re-arms auto-merge only behind Step 4.0 and Step 4.0b.
A fix commit that turns CI green therefore cannot merge unreviewed: that is
also why the command disarms.

**The re-arm obligation.** The PR is disarmed from the moment the command
succeeds until finalize re-arms it after the recovered commit passes review.

- If the recovered round parks (`scope_drift`, `review_feedback_stale`,
  `attempt_cap_blocked`), resolve the park with the row's own `next:` line.
  The PR stays red and disarmed meanwhile, which is the safe state.
- If the row is cancelled or fails, the PR stays disarmed; a human then
  decides to fix, merge by hand or close it.
- Do not re-arm by hand (`gh pr merge --auto`) before the head has been
  re-reviewed.
- To list PRs cw disarmed, look for `ticket.requeued` events with
  `reason == "cli_recover_red_pr"` (fields `was_armed` and `disarmed`).
- If the locked row write is refused after the disarm (the row stopped being
  `COMPLETED`, its `pr_url` changed, or a live session appeared) there is no
  event: the audit record is the warning log line from `recover.py`
  (`pr_url`, `was_armed`, `disarmed=True`, `outcome=row_write_refused`) plus
  the refusal message, which says the PR is disarmed and the row is untouched.
  A retry is safe once the cause is gone.
- Persisted re-arm-owed tracking (a surfaced "red and disarmed" orphan) is not
  built here; it is tracked as #2993.

**Episode semantics.** One page per episode. An episode is the row
`COMPLETED`, the PR head SHA unchanged, and the PR stayed red (a required
check is still failing). It ends, and the alerted state resets, when the PR
turns green, when a successful required-checks lookup returns no failing
required check (an optional check that keeps the PR red does not keep the
episode open: auto-merge is no longer blocked by CI, so the `RED_SHIPPED_PR`
row disappears), when the head SHA changes (the sweep notices within about 15
minutes, via a head-only read of an alerted row), or when the row leaves
`COMPLETED` (the recovery verb does this). A lookup that fails, times out or
cannot be parsed is unknown: the row stays alerted and the sweep retries on a
backoff; nothing is cleared and nothing new is paged. A changed
required-failing set inside one episode does NOT page again; the latch is
refreshed silently. **Accepted consequence:** because an empty required list
ends the episode, a required check that fails again at the SAME head after
such an end (for example a re-run that fails again) pages as a NEW episode, a
second page for the same head SHA. The page is a pointer, not the evidence:
`recover-red-pr` re-reads the live failing checks, pinned to the head SHA, so
a check that fails later at the same head needs no page of its own.

**Off-switch.** The sweep is on by default; `shipped_red_signal_enabled: false`
in `orchestrator.yaml` stops the sweep and the page (takes effect next tick; global, not per lane).

**Known limits.**

- A required check that is cancelled-only (CANCELLED, STARTUP_FAILURE:
  infra-only by design, #1684) raises no signal, because hydrated `ci_ok`
  stays True.
- A required check that never ran is not in the status rollup, so it raises no
  signal either.
- A required check whose conclusion is STALE counts as a failure in cw
  (`ci_ok False`) but gh buckets it `pending`, so the `--required` read sees it
  as not failing and the sweep stamps an optional-only latch (re-polled every
  900 s) with no page.
- Hydrated `ci_ok == False` is an over-inclusive pre-filter (the rollup covers
  optional checks too), so the sweep always follows it with the
  `gh pr checks --required` test, and an optional-only red raises no page.
- A red to green to red at the same head SHA that hydration never observed as
  non-red counts as one episode (no second page).
- An alerted row is re-evaluated when its hydrated failing-name set changes,
  its head changes or 900 s pass (head only). A required check whose
  conclusion changes without changing the set of failing check names (for
  example a re-run that ends CANCELLED) is not seen until one of those happens;
  `recover-red-pr` then refuses "no required check is failing" and changes
  nothing.
- A PR with checks but no *required* checks makes `gh pr checks --required`
  exit 1 with "no required checks reported"; the sweep reads that as none
  failing (no page). "no checks reported" (an empty rollup) stays unknown.

**The `auto_fix_ci` recipe (#2989).** This is the opt-in review recipe, off by
default: it needs `review_recipes_enabled: true` and `auto_fix_ci: true` on the
lane or ticket. It fires once per `ci_failing` episode on ANY failing check
(not only required ones) and never on a DIRTY PR (that is `merge_blocked`). A
BEHIND PR retains its `ci_failing` attention state. On a `COMPLETED` row it requeues the row at the SAME stage
(finalize): no disarm, no synthesized feedback, no fix agent. The new sweep
fires on required failing checks of an armed PR and does not depend on the
recipe. The sweep runs before the dispatch tick, so on a lane with both armed
the page can fire and the recipe can requeue the row in the same loop
iteration; `recover-red-pr` then refuses with "row is not COMPLETED" -- do not
force it, let the recipe's finalize run finish. If the PR is still red
afterwards the row is `COMPLETED` again, the transition cleared the latch, a
fresh page fires and `recover-red-pr` works. Do not arm both on one lane; where
red-PR recovery is wanted, leave `auto_fix_ci` off. Rewiring the recipe onto
the recovery is tracked as #2989.

**Failure modes of the recovered round.** `review_feedback_stale` (the PR head
moved: generate fresh head-pinned feedback and run
`cw dev-queue requeue <T> -c <C> --stage impl --review-feedback-file <feedback.json>`
as described under "Recovering a repair row"; `recover-red-pr` applies only
to COMPLETED rows), `scope_drift` at gate 2
(`approve --scope-drift <paths>`), `attempt_cap_blocked`
(`requeue --reset-attempts`), a PR merged in the meantime (the command refuses
because the PR is not OPEN).

**Decision: regress a completed row to impl; no bounded CI check in
finalize.** A bounded check at finalize misses the failure #2905 reports (the
PR goes red after finalize returns; headless finalize skips Stage 5 and does
not poll CI), delays and holds lane slots for every green PR, is rejected by
RFC 0016's own "Repair at finalize" entry (finalize ships and does not
repair; a post-review commit invalidates the ledger), and would still need the
signal and the path back to impl. Regressing reuses the shared synthesizer,
the `review_feedback` pin, the Stage 2 live-PR guard and the `pr_gate`
release, and adds no finalize or review-gate semantics.

### COMPLETED row recovery (`--from-completed`)

A shipped, `COMPLETED` ticket can later need another pass -- e.g. its merged
PR went conflicting because a sibling ticket in the same wave merged first.
(A shipped PR that went red on a required check is a different case, with its
own verb: see "Red shipped PR" below.)
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

### Adopt an open human PR (`--adopt-pr`, RFC 0016, #2943)

An existing, open, same-repository PR that a human wrote can be driven
through review and finalize without re-planning or re-implementing it. The
`/adopt-pr` skill resolves which ticket the PR belongs to and echoes it; the
CLI primitive takes the resolved ticket:

```bash
cw dev-queue add <TICKET> --adopt-pr <N> -c <CLIENT>
```

`--adopt-pr` takes exactly one ticket. A green PR enters at `review`; a PR
with a merge conflict or failing CI enters at impl with synthesized repair
feedback (#2944, RFC 0016 B1, below). `--stage review` is accepted for a green
PR and refused for a non-green one; `--stage impl` needs
`--review-feedback-file`; any other `--stage` is rejected. Every gh read
happens before the dev-queue lock.

**Refusals** (exit 1, no row written), in precedence order: gh cannot read the
PR; a fork PR (`isCrossRepository`); a PR that is not OPEN; a head branch that
fails the row branch charset (the message names it); a malformed head SHA or a
PR from another repository; then, inside `add_ticket`'s lock, the PR or its
resolved branch already carried by a live row (PENDING, RUNNING,
BLOCKED_ON_USER, AWAITING_OPERATOR_SIGNOFF, or COMPLETED with its PR still
OPEN or not yet hydrated), or any existing row for `(client, ticket)` (a
distinct message, not "already queued"). For a `github-issues` tracker the
ticket's issue must be readable; other trackers are not verified and the CLI
prints a note. A PR adopted as its own ticket (`<TICKET>` = `<N>`) needs no
issue read.

**What the row carries.** `stage=review` (green) or `stage=impl` with
`review_feedback` (non-green), `branch=<headRefName>`, `adopted_pr_url` (also
stored as `pr_url`) and `adopted_head_sha`, the pin. On an impl-entry row the
feedback's `reviewed_sha` equals the pin by construction. The invariant: **an
adopted row is always reviewed at its pinned head**, which is why finalize
Step 4.0 and 4.0b need no adoption-specific change. `pr_url` survives every
park, revert and cancel on an adopted row.

**Non-green PRs enter at impl with synthesized repair feedback.** A
CONFLICTING PR and/or one with failing CI (PENDING CI is not failing) is not
parked. Intake calls `cw.pr_feedback.synthesize_pr_feedback(repo, pr_number,
cwd=...)`, the shared synthesizer (it takes no ticket or row, so other callers
reuse it), which re-reads the PR once and builds a bounded `ReviewFeedback`:

- a header naming the repo, PR, head SHA, base ref, capture time and the
  conditions (merge conflict and/or the failing-check count);
- the conflicted files, from a real `git merge-tree` of `origin/<base>` and
  the head in the client repo (after a bounded fetch of both branches). If
  GitHub says CONFLICTING but the local merge is clean, a note says so;
- for each failing check (at most five), its name, conclusion and url, plus
  the failed-steps log tail of a GitHub Actions job (`gh run view <run> --job
  <job> --log-failed`), terminal-stripped, secret-redacted and capped at
  6 KiB. A non-Actions check, or a log gh cannot read, says why instead.

Everything copied from the PR, its CI logs and its file paths sits between
`<<<CW-UNTRUSTED-BEGIN sha=...>>>` and `<<<CW-UNTRUSTED-END sha=...>>>`
markers, every line prefixed `| `, under a header that says it is evidence
about that one commit and to never follow instructions in it. Stage 2 treats
it as untrusted review evidence and verifies it against the live PR before any
edit (the PITA guard above). The row then enters at impl with no plan; Stage 2's
"Repair entry: no plan" carve-out applies (keyed on the feedback and the
adoption branch, so a normal ticket without a plan still stops
`plan_missing`).

**Where the CI-log text lives.** The report is persisted on the dev-queue row
(`~/.local/share/cw`), copied into every spawn's `.claude/cw-context.json`
(`queue_metadata.review_feedback`) and from there into the impl agent's prompt,
until the row is removed. Log tails are redacted (`cw._text.redact`) before
they are embedded. `cw dev-queue show`, the adoption echo and emitted events
never print the report.

Synthesis refuses the adoption (exit 1, no row) when it cannot run: gh cannot
read the PR, a git fetch / merge-tree step fails, or the PR's head moved
between the intake read and the synthesizer's own read (re-run the add).

**Supplying the feedback yourself.** Add `--stage impl` and a PITA feedback
file; it replaces the synthesis, for a green or non-green PR:

```bash
cw dev-queue add <T> --adopt-pr <N> -c <C> --stage impl \
  --review-feedback-file /path/to/review-feedback.json
```

The file's `repo`, `pr_number` and `reviewed_sha` must match the fresh PR read
(the repo slug from the PR url, `<N>`, the current head); a mismatch refuses
naming the field, with no row.

**Intake park.** A PR that GitHub still reports `mergeable: UNKNOWN` after
the bounded re-poll (three reads, five seconds apart) is adopted but parked
`adopt_pr_state_unknown` (with a feedback file, the row is built at impl
carrying it and still parks). A queue written by v1.87.0 may also hold the
legacy `adopt_pr_not_green` park, which cw no longer stamps. Release either
with a plain requeue:

```bash
cw dev-queue requeue <TICKET> -c <CLIENT>
```

The requeue re-reads the PR first (with no synthesis). Still UNKNOWN refuses.
Green releases it, and the pin moves to the freshly read head. Now not green
also refuses, because a parked review row cannot be re-routed to impl by a
requeue; the message names the recovery:

```bash
cw dev-queue remove <T> -c <C>
cw dev-queue add <T> --adopt-pr <N> -c <C>
```

Auto callers (`drain`, concierge recipes, `cw spawn close --requeue`) never
release these parks.

The requeue also reports how the new head relates to the old pin. It fetches
the branch and, when missing locally, the old pin (best-effort), then prints
`Re-pinned the adopted head OLD -> NEW`, with `(history rewritten: the new head
does not descend from the pin)` for a force-push or rewrite. When the fetch
fails the ancestry cannot be decided and the line says so instead (`unknown`);
the requeue still proceeds. The same answer is logged (`adopted_repinned_on_recheck`),
carried on `ticket.requeued` as `adopted_ancestry_result` and emitted as an
`adoption_pin` `task.transition` event with `source: requeue-recheck`. The
intake park never ran a session, so nothing reviewed is invalidated; the row
re-enters at review (`--stage` past review is refused).

**Recovering a repair row.** An impl-entry repair row is an ordinary IMPL row
for recovery. At the unproductive-attempt ceiling it parks
`attempt_cap_blocked`, and one command releases it at impl, keeping the pin,
branch and feedback:

```bash
cw dev-queue requeue <T> -c <C> --reset-attempts
```

(`cw dev-queue requeue <T> -c <C>` alone re-runs it without resetting the
counter.) If Stage 2 blocks `impl_failed` with details starting
`review_feedback_stale` (the PR's head no longer equals the feedback's
`reviewed_sha`, for example after a push), build fresh feedback against the
current head and attach it:

```bash
cw dev-queue requeue <T> -c <C> --stage impl --review-feedback-file /path/to/fresh.json
```

**Drift park and the re-pin.** Before every claim the dispatch loop compares
the PR's origin head with the pin. A head that moved outside a cw session
parks the row `adopted_head_drift`; the `session.needs_attention` event names
the pinned and observed SHAs and the capped `git log --oneline pin..head`
range. An unreadable origin head fails open (no park). Review the range, then:

```bash
cw dev-queue requeue <TICKET> -c <CLIENT> --repin-adopted-head
```

The CLI fetches the branch, then the requeue re-reads the origin head and
re-pins only when the old pin is an ancestor of it. A rewrite (force-push) or
an ancestry check that cannot be decided refuses, leaving the pin; a plain
`requeue` of a drift park is refused with the exact command above. The re-pin
lands at review or earlier, clears the row's review approval, and the review
convergence engine forces a fresh round at the next `converge init` (a
converged ledger on a moved head is reopened; a stopped one is released as
`new_commits`). Two existing engine exceptions are not overridden: a head that
moved only by clean default-branch merges (#2861, #2979) keeps a converged
ledger.

**Who moves the pin.** Automatically, in exactly two places: the session-end
settle and the repair recipe's lease push. Every RUNNING exit of an adopted row
stamps `adopted_repin_due` (a pure write; the stamp point runs under
`sessions_lock`, where ADR-0019 forbids a subprocess). At the head of the next
dispatch tick, with no lock held, `settle_adopted_pins` reads the origin head,
fetches the branch and advances the pin only when the old pin is an ancestor;
a non-descendant or a git error keeps the pin and the drift scan parks it. An
unreadable origin head defers the settle (the flag stays set, the row is
skipped by the drift scan, and a warning is logged each tick until gh reads
again). The settle runs at most 8 rows per client per tick, each with a 60s
fetch bound, and runs even when the client has no free slot; there is no
per-client toggle yet (see the follow-up below). The repair recipe's lease
push (`repair_git._push_with_lease`) advances the pin from the expected SHA to
the pushed one.

**Residual (accepted).** A foreign push that lands during a live cw session,
or in the one-tick gap before the next settle, and that descends from the pin
is absorbed by the settle (when its objects can be fetched). Those commits were
not reviewed by cw; finalize's local-HEAD versus PR-head check stays the
backstop. A non-descendant parks. A push after the settle parks.

**Drafts.** An adopted draft PR is adopted as-is. Finalize marks it ready
(`prep_pr_finalize.py ready`) immediately before arming auto-merge, because
GitHub refuses auto-merge on a draft; a failure to mark it ready blocks with
`agent_block` and never arms. Finalize's reuse path also runs the `/prep-pr`
Step 7 quality gates before arming (Step 4c.6, #2974); an adopted row whose
gate fails is covered by the known gap below; that failure is never a ship.

**Known gaps (follow-ups, not changed here):**

- The repair recipes (`repair_changelog_conflict`, `repair_mechanical_conflict`)
  have no authorship check: an adopted COMPLETED row whose PR goes
  CONFLICTING can be force-pushed by a repair when the client opted in. The pin
  follows the lease push; whether a repair may rewrite a human's PR at all is
  an open decision owned by #2944 (repair routing).
- The tick-start settle has no per-client toggle or time budget beyond its
  per-tick cap and fetch timeout.
- Reuse-path quality gates (#2974): every reuse-path PR, including every adopted
  PR, runs the quality gates before arming auto-merge. A failing gate exits
  `blocked` / `agent_block`. For an adopted row, that regresses to IMPL and
  parks there with `plan_missing`, because an adopted row enters at REVIEW and
  so usually has no `.cw/plan.md`. The park is bounded by
  `FINALIZE_REGRESS_CAP` and is never a ship. Recovery is the #2944
  repair-routing work, or fixing the branch by hand and requeueing
  (`cw dev-queue requeue <TICKET> -c <CLIENT>`; add `--repin-adopted-head`
  when the pin has drifted).

### CHANGELOG-only conflict repair (`repair_changelog_conflict`, #2771)

A shipped (`COMPLETED`) ticket's open PR goes `CONFLICTING`/`DIRTY` whenever
another PR's `CHANGELOG.md` bullet lands first. GitHub ignores the repo's
`CHANGELOG.md merge=union` driver; a local rebase does not, and after a release
it silently files bullets under the released version. The
`repair_changelog_conflict` review recipe repairs such a PR when CHANGELOG.md
is its only conflict.

**Enable.** Default off. Behind `review_recipes_enabled: true`, set it per
lane (`lanes[].review_recipes: {repair_changelog_conflict: true}`) or per
ticket. It runs only in the dispatch loop's reconcile (never from `cw start`
/ `cw doctor --reap`), handles one PR per tick, spawns no LLM session (usage
limits do not apply), and keeps an in-process latch of 3 attempts per PR (a
daemon restart permits one more fully fail-closed attempt).

**What it does.** In a detached scratch tree
(`<worktree_base>/.changelog-repair-<T>`, removed when it finishes; never the
ticket's worktree): rebuild CHANGELOG.md as main's text plus the branch's own
`[Unreleased]` bullets, rebase the other files, verify the non-CHANGELOG diff
is unchanged, run the CHANGELOG freeze gate (when `.claude/scripts/check_changelog_frozen.py`
is present in the client repo; an absent script skips the gate and is logged at
INFO, and it runs with `--require-tags` after a checked `git fetch --tags`,
from the client's copy, never the PR branch's), disarm auto-merge if the PR was
armed, `git push --force-with-lease` over the fetched head, then re-arm with
`prep_pr_finalize.py arm-automerge <n> --head-sha <new_sha>` only if the new
head passes the review-approval and tip fence.

**The fence.** A rewritten head fails finalize Step 4.0 (the recorded approval
is no longer an ancestor) and Step 4.0b (`commits_after_clean_round`) by
construction, so large-tier PRs end **unarmed** with
`session.needs_attention` (`paused_status: changelog_repair_unarmed`,
`review_tip_check: stale`, `disposition: unarmed_review_fence`). An origin
tip the fence could not read (`ls-remote` failed, timed out or returned no
ref) is reported as
`tip_check_fetch_failed` (#2910); the event token stays
`review_tip_check: stale`. Only a small tier outside clean mode re-arms, plus one clean-mode exception (#2921): the recipe re-arms a clean-mode PR whose ledger is converged at the old head when every branch commit is proven unchanged (`git range-diff --no-patch` pairs all `=`, with no dropped or added commit) and the only commit added is the rebuilt CHANGELOG commit. The recipe then remaps `last_clean_sha` to the new head and appends an audit entry to `clean_sha_remaps`, and the strict tip rule accepts that head unchanged. Any other case stays unarmed with `commits_after_clean_round`: a `!` pair (for example a CHANGELOG-touching branch commit, which the union merge can make differ), an unreadable range-diff, a missing, foreign or unconverged ledger, a `last_clean_sha` that is not the old head (including a ledger tolerated past a default merge), an unreadable origin tip, a strict `review.default_merge_tolerance: false` client, and a refused or failed ledger write. The tip rule itself is unchanged; the repair recipe is the only writer of a remapped clean sha (a narrow, proof-carrying exception to ADR-0022's "no exempt-path list"). A PR that was armed before the repair can
therefore end unarmed; the event's `auto_merge_was_armed` says so.

**Release an unarmed repair.** The row is `COMPLETED` at FINALIZE, so
`--stage review` is refused (a regress of a non-parked row, `requeue.py`; and
`--from-completed --stage review` fails the same check). Use:

```bash
# If the ticket's worktree still exists, sync it first: finalize will not
# reset over local commits origin lacks. The reset destroys local work, so run
# it ONLY when both checks print nothing: no uncommitted changes, and no local
# commit beyond the pre-repair head (<expected_sha> from pr.action_taken's
# evidence_snapshot). Otherwise stop and inspect the worktree first.
git -C <worktree> fetch origin
git -C <worktree> status --porcelain
git -C <worktree> log --oneline <expected_sha>..HEAD
git -C <worktree> reset --hard origin/<branch>   # only if both are empty
cw dev-queue requeue <T> -c <C> --from-completed
```

`--from-completed` returns the row to PENDING at FINALIZE, and finalize
re-evaluates Step 4.0 / 4.0b on the repaired head. If finalize parks again, the
`BLOCKED_ON_USER` row it leaves is released by that park's own command (for
which `requeue --stage review` is then legal). Alternatively inspect the PR and
arm it by hand: `prep_pr_finalize.py arm-automerge <n> --repo-path <repo>
--head-sha <new_sha>`.

**Recover the old head.** `pr.action_taken` carries the pre-image as
`evidence_snapshot.expected_sha` (no backup ref is pushed): from a clone that
has the object, `git push --force-with-lease origin
<expected_sha>:refs/heads/<branch>`, or use GitHub's reflog.

**Dispositions** (on `pr.action_failed`; the row is never moved, and
`escalate_merge_block` still pages once per episode):

- `non_changelog_conflict` — another file conflicts; resolve by hand or requeue.
- `conflict_unexplained` — nothing explains GitHub's CONFLICTING; inspect the PR.
- `changelog_unparseable`, `changelog_released_section_touched`,
  `changelog_ambiguous` — the branch's CHANGELOG cannot be rebuilt safely; fix
  the bullets by hand.
- `rebase_failed`, `verify_failed`, `gate_failed` — nothing was pushed; read
  `error` (a `verify_failed` carries a `range-diff --stat` diagnostic; a
  `rebase_failed` also covers an unexpected I/O or cw error before the push).
- `fetch_failed`, `scratch_worktree_failed`, `push_rejected` — transient; the
  next tick retries (up to 3 attempts). If every `push_rejected` retry fails
  after the PR was disarmed, the PR stays **DISARMED**, still `CONFLICTING` and
  NOT re-armed; recover with `cw dev-queue requeue <T> -c <C> --from-completed`
  or re-arm by hand with `gh pr merge <n> --auto`. `auto_merge_was_armed` in
  the payload reflects the armed state read on the LAST attempt, so a retry
  after a successful disarm reads `false`; the error text still says
  "auto-merge was disarmed and is NOT re-armed".
- `push_outcome_unknown` — the push timed out: compare `origin/<branch>` with
  `expected_sha` before anything else.
- `pr_head_mismatch` — the PR head moved (or is not `<prefix>/<T>`), the PR is
  not `OPEN`, or its base is not the client's default branch (the error names
  the observed `state` and `base`); nothing was pushed.
- `arm_script_missing` — install the scripts (`scripts/install-skills.sh`);
  nothing was touched.
- `disarm_failed` — the PR could not be unarmed, so nothing was pushed: check
  `gh pr view <n> --json autoMergeRequest`, run `gh pr merge <n> --disable-auto`
  by hand, then wait for the retry or requeue. If the error says `PUSHED`,
  auto-merge may still be armed against an unreviewed head: disarm it first.
- `rearm_failed` — pushed and repaired but NOT armed; release as above.
- `post_push_failed` — the push happened (`pr.action_taken` carries both
  SHAs); confirm the PR is unarmed before anything else.
- `unarmed_review_fence` — on the attention page, not here: release as above.

### Declared-path conflict repair (`repair_mechanical_conflict`, #2917)

Some conflicts are mechanical: two PRs each add a migration and both repoint
the same head pointer (`max_migration.txt`, `atlas.sum`), or both regenerate
one lockfile. The `repair_mechanical_conflict` review recipe repairs a shipped
(`COMPLETED`) ticket's `CONFLICTING`/`DIRTY` PR when **every** conflicting
path matches a glob the client declared, by running a command the client
declared.

**Enable.** Add a `mechanical_conflict_repair` entry to the client in
`clients.yaml` (see `config/CONFIG_REFERENCE.md`) and keep
`review_recipes_enabled: true`. Presence of the entry is the opt-in: with no
lane or ticket value the recipe is on for that client. A lane or ticket
`review_recipes: {repair_mechanical_conflict: false}` vetoes it; `true`
without an entry never fires (the entry carries the globs and commands).

```yaml
clients:
  acme:
    mechanical_conflict_repair:
      paths: ["migrations/**", "migrations/max_migration.txt"]
      repair_command: ["scripts/rechain_migrations.sh"]
      verify_command: ["scripts/check_single_head.sh"]
      repair_timeout_seconds: 300   # 1..600, default 300
      verify_timeout_seconds: 120   # 1..600, default 120
```

Like the CHANGELOG recipe it runs only in the dispatch loop's reconcile, one
PR per tick, with no LLM session and an in-process latch of 3 attempts per PR.

**What it does.** Fetch; require the PR `OPEN` on the default branch at the
fetched head; derive the conflict set with `git merge-tree` (park unless every
path matches a declared glob). Then, in a detached scratch tree
(`<worktree_base>/.mechanical-repair-<T>`, removed when it finishes; never the
ticket's worktree): `git merge --no-commit --no-ff origin/<default>` into the
branch, re-check that every unmerged path is in scope, run the repair command,
stage only in-scope paths, refuse leftover conflict markers, commit the merge
(`chore(merge): sync <default> and repair declared-path conflicts (#<T>)`),
verify that nothing outside the globs changed (the out-of-glob path set and
`-U0` diff of `merge-base..old-head` must equal those of
`origin/<default>..new-head`), run the verify command, disarm auto-merge if
armed, `git push --force-with-lease` over the fetched head, then re-arm only
if the fence passes. It is a merge, not a rebase: the reviewed commits stay
ancestors and there is exactly one conflict stop.

**The repair script's contract.**

- cwd is the scratch tree, mid-merge of `origin/<default>` into the branch:
  conflicted declared paths contain `<<<<<<<` / `>>>>>>>` markers, and the
  merge's clean changes are already staged.
- Resolve the conflicts by editing the tree. Do not `git add`: cw stages, by
  explicit path, every in-scope change (edits, deletions, new files). Never
  stage anything outside the globs. A tracked file edited outside them in the
  working tree parks `repair_out_of_scope`; one the script itself staged
  (`git add`) counts as the merge's own work at that step and is caught by
  the scope verify instead, which parks `verify_failed`. An untracked file
  outside the globs is ignored (logged as `mechanical_repair_untracked_ignored`).
- Environment: the daemon's environment minus every `GIT_*` variable, plus
  `CW_REPAIR_DEFAULT_REF` (`refs/remotes/origin/<default>`),
  `CW_REPAIR_MERGE_BASE`, `CW_REPAIR_BRANCH` and `CW_REPAIR_CONFLICTED_PATHS`
  (newline-joined, sorted). stdin is closed; stdout and stderr are captured.
- Exit 0 means resolved; any other exit, a timeout or a launch failure parks
  `repair_failed`. The verify command runs afterwards in the same tree at the
  committed merge, with the same environment; it must exit 0.
- Each command runs in its own process group, and the whole group is killed
  on every exit: at its timeout, on an interrupt or exception while waiting,
  and after a normal exit that left background children behind. A script must
  not rely on a background process outliving it, and must redirect the output
  of any helper it backgrounds (`helper >/dev/null 2>&1 &`) or not background
  at all. A helper that keeps the command's inherited stdout/stderr open holds
  the capture pipe, so the command is not seen to finish until its timeout
  (default 300 s repair / 120 s verify, at most 600 s each): the step then
  parks `repair_failed` / `check_failed` as a timeout even though the script
  exited 0, and the serial post-lock drain stalls for that long. Only a helper
  whose output is redirected away is killed at a normal exit, with the exit
  status kept. A descendant that left the group (`setsid`, a daemonizing tool)
  is not reached by the kill. The kill is a bounded-call deadline over the
  recipe's own un-pushed scratch child. A timeout ends in `repair_failed` /
  `check_failed` (an event plus the in-process latch); the kill touches no
  queue row, session, roster entry or ticket worktree.

**Trust.** Commands come only from `clients.yaml`, never from the PR branch.
A relative `argv[0]` containing `/` resolves against the client's git
directory (`repo_path`, or `workspace_path` for a legacy client), so a branch
cannot swap the script that repairs and validates it (absolute paths and bare
`PATH` names are used as written). That rule protects only the script file
itself:

- the commands inherit the daemon's full environment minus `GIT_*`;
- arguments that name repo files (`["python", "scripts/x.py"]`), and anything
  the script imports or reads from the tree, are branch (agent-authored)
  content running with that environment.

A relative `argv[0]` is read from that clone's **currently checked-out working
tree**: not from the default branch and not from the PR branch. cw only
fetches into it and never updates its checkout, so a script added or changed
on the default branch but not yet pulled there is missing or stale when the
recipe runs. A missing script parks `repair_failed` (repair command) or
`check_failed` (verify command) with `... could not run: ...` in `error`; a
stale one just runs its old version. Keep that checkout current, or declare an
absolute path or a bare `PATH` name.

Declare commands accordingly, and keep `paths` narrow and the verify command
strict.

**What it covers.** Textual conflicts confined to the declared paths: a head
pointer or manifest file, a lockfile, a same-named file. It does **not**
cover a silent duplicate head (two differently named migrations off the same
parent): git and GitHub see no conflict (`MERGEABLE`/`CLEAN`), so no recipe
fires; it surfaces later as a CI failure on the merged result.

*Example (illustrative only):* an Alembic- or Django-style project where each
migration file names its parent and a `max_migration.txt` names the head.
The repair script renumbers the branch's migration on top of main's newest
one, rewrites its parent and the head file; the verify command checks there
is a single head.

**The fence.** The same fence as the CHANGELOG recipe, unchanged. A large-tier
approval recorded on the pre-repair head still evaluates as `recorded` over
the repair merge commit, because the fence inspects only non-merge commits and
merge parents on `origin/<default>`. The approval therefore covers a
script-authored resolution: operators accept the declared script's output as
part of that approved diff, which is why `paths` should stay narrow and the
verify command strict. In clean mode the merge is a commit after the last
clean round (`commits_after_clean_round`), so the PR ends **unarmed** with
`session.needs_attention` (`paused_status: mechanical_repair_unarmed`,
`review_tip_check: stale`, `disposition: unarmed_review_fence`); the tip rule
is not weakened. Release it exactly as an unarmed CHANGELOG repair:
`cw dev-queue requeue <T> -c <C> --from-completed` (sync the ticket worktree
first, as above). Recover the old head from `evidence_snapshot.expected_sha`.

**With the CHANGELOG recipe.** Each recipe has its own latch. On a tick where
both are candidates for a row, the CHANGELOG recipe acts first and the row is
skipped by this one, with this recipe's latch unbumped. That includes a row
whose conflict is mechanical-only: the CHANGELOG recipe takes it first and
parks it `non_changelog_conflict` (one `pr.action_failed`, which saturates
its latch), and this recipe only runs on a later tick. That is expected and
benign: the row then continues under this recipe, so alerting on
`pr.action_failed` should expect one `non_changelog_conflict` for such a row
before the repair. A transient CHANGELOG failure (for example `fetch_failed`;
`scratch_worktree_failed`, `push_rejected` and `disarm_failed` are transient
too) does not saturate its latch, so the CHANGELOG recipe keeps the row on
each following tick, up to its 3-attempt cap, before this recipe gets a turn.
On later ticks this recipe derives the conflict set
without the CHANGELOG hidden-conflict rule (a CHANGELOG edit on both sides
must not park an in-glob conflict): a git-visible CHANGELOG conflict parks
`out_of_scope_conflict`, and a CHANGELOG-only union-hidden conflict parks
`conflict_unexplained` (it belongs to the CHANGELOG recipe).
`escalate_merge_block` still pages once per episode.

**Post-lock cost.** Repair jobs drain serially after the reconcile lock
releases. One job's worst case is `repair_timeout_seconds` +
`verify_timeout_seconds` plus the git and arm per-call timeouts: about 1200 s
and more at the caps, and a CHANGELOG repair in the same tick runs first.
The drain runs inline inside `dispatch_tick`'s reconcile step, ahead of every
client's claim and spawn and ahead of the `dispatch.tick` events, so the
**whole dispatch tick** (all clients, not just this one) waits for the job.
`cw watchdog tick` alarms "the dispatch loop may be down" when the newest
`dispatch.tick` event is older than `max(4 x tick_interval_seconds, 600 s)`.
Keep `repair_timeout_seconds` + `verify_timeout_seconds` plus about one tick
interval under that 600 s floor, or a slow repair pages as a stalled loop. The
defaults (300 s + 120 s) fit; raising the timeouts toward their 600 s caps does
not.

**Dispositions** (on `pr.action_failed`; the row is never moved). `error` may
carry up to 300 characters of the repair or verify command's own output, on
`pr.action_failed` and in the `mechanical_repair_parked` log line; judge what
your scripts print accordingly.

The deterministic dispositions below saturate this recipe's in-process latch
and are not retried until the dispatch loop restarts or the row leaves the
conflicting shape. Only `fetch_failed`, `scratch_worktree_failed`,
`push_rejected` and `disarm_failed` retry on later ticks (up to 3 attempts), as
for the CHANGELOG recipe.

- `out_of_scope_conflict` — a conflicted path is outside the declared globs
  (the paths are in `error`), before the merge or in it; nothing was touched.
  Resolve by hand, or widen `paths` if the file really is mechanical.
- `merge_failed` — the scratch merge, staging or merge commit failed, or the
  client lost its entry mid-tick (also an unexpected I/O or cw error before
  the push); nothing was pushed. Read `error`.
- `repair_failed` — the repair command exited non-zero, timed out or could
  not launch (`error` names its `argv[0]` and the output tail); fix the
  script, then restart the dispatch loop; a repair that parked does not retry
  on its own.
- `repair_incomplete` — conflict markers remain in a formerly conflicted path.
- `repair_out_of_scope` — the script edited a tracked file outside the globs
  in the working tree (a change it staged itself surfaces as `verify_failed`).
- `check_failed` — the verify command failed (same `error` shape as
  `repair_failed`); the repaired merge was not pushed.
- `verify_failed` — nothing was pushed. Three causes, told apart by `error`:
  the out-of-glob path set or diff changed across the merge (this includes a
  script that staged an out-of-glob change itself; only this cause carries a
  `range-diff --stat` diagnostic); the repair command rewrote history, so the
  repaired head does not descend from the reviewed tip or from the default
  branch; or the verify command moved HEAD off the verified merge commit.
- `conflict_unexplained`, `fetch_failed`, `scratch_worktree_failed`,
  `push_rejected`, `push_outcome_unknown`, `pr_head_mismatch`,
  `arm_script_missing`, `disarm_failed`, `rearm_failed`, `post_push_failed`
  — as for the CHANGELOG recipe above.

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
  round's prompt (#2213). The codex comment path is `github-issues`-only — a
  `codex` REVIEW backend on any other tracker has no in-process fetch op and
  cannot deliver comments. Binding constraints recorded with
  `cw review constrain` are different: they reach the codex review prompt and
  the codex fix loop from the cw row on **every** tracker, independent of the
  provenance marker and the operator login (#2964). The comment path is
  unchanged.

**Binding constraints for the codex lane (#2964).** When a send-back must
bind the codex reviewer and fix loop (for example "do not add `foo_bar`"),
save the constraint text with the Write tool to a scratch file and run
`cw review constrain <T> -c <client> --file constraints.md` from the
orchestrator session (refused only inside a headless worker). It appends one
entry to the row's `operator_constraints`; the row is the source of truth.
The codex review prompt renders it as `## Binding Operator Constraints`, and
the fix loop parses its prohibition sentences and parks a cycle that
introduces a forbidden token or mechanism (`codex_fix_constraint_violation`).
The printed stdout block is a human-readable mirror; post it with your
tracker's own tool if you want one, but nothing reads it back. Constraints are
append-only: there is no edit, supersede or revoke yet (retraction is tracked
in #2968), so record only what is binding. Each entry is capped at 16 KiB and
the row at 64 KiB. They survive requeue and regress; a fresh
`cw dev-queue add` after a terminal row starts with none. Check what is
recorded with `cw dev-queue tasks --json -c <client>`. Restart
`cw dev-queue serve` after upgrading: an older dispatch loop rewrites the
queue without the list and erases recorded constraints (`constrain` prints a
`warning:` line when the loop is stale). A review or fix loop already running
loaded its row snapshot at start and sees a new constraint on its next run.

**Plan decisions for the review lane (#2984).** The plan stage records the
approved plan's own choices on the same `operator_constraints` list: its
`## Adopted Assumptions`, `## Non-Goals` and `## Decisions`, each entry carrying
a `kind` label (`adopted_assumption`, `non_goal`, `plan_decision`; operator
entries are `operator`). The plan worker runs `cw dev-queue record-plan-decisions
<T> -c <client> --plan .cw/plan.md` at Step 1g.1. It is idempotent (an item
already on the row is a no-op; a reworded bullet appends a new entry) and the
library refuses unless the row's `stage` is `plan`, so a review or impl worker
cannot mint binding input for the lane that reviews it. Exit 0 is success; 1 is
a missing or unreadable plan, a stale or unknown dispatch loop, a row not at the
plan stage, or a missing row; 3 means some items were left unrecorded for the
32 KiB plan sub-budget (under the shared 64 KiB row cap), listed on stderr.
Over-cap items are refused whole, never truncated, and operator entries always keep
32 KiB of headroom. The worker records a `plan_decisions_not_recorded` friction
note and the stage continues. A plan promoted by `cw dev-queue approve` after a
regress into plan does not re-run Step 1g.1, so its decisions are not
re-recorded (entries from the earlier plan remain). The audit event is the same
`ticket.constraint_recorded`, with `kind` set and actor `plan`.

Plan-derived entries inform prompts only. The codex review prompt renders them
as `## Decided Scope`, the codex fix loop as a third prompt block, and the
Claude-native reviewers get a `## Decided Constraints` block. They never create
fix-loop guard rules, so only `cw review constrain` entries can park a cycle as
`codex_fix_constraint_violation`. `cw review decided <T> -c <client>` is the
read-only view: it prints every entry verbatim, grouped by kind, and is what the
Claude-native review stage inlines. An adopted row (created at review) only ever
carries operator entries, so it prints "None recorded." for the plan kinds.
Entries are append-only (retraction is #2968): a superseded plan entry stays on
the row until then.

**Park: `review_operator_actionable` from a decision contest (#2984).** A
reviewer that believes a decided constraint is itself unsafe files the finding
with `contests_decision` set to a quote of the decision plus the concrete harm.
A MUST_FIX contest is stamped `operator_actionable` and parks the run with
`review_operator_actionable`; a SHOULD_FIX contest is a non-blocking note.
Recognise it by `blocker.details` opening with `OPERATOR DECISION REQUIRED —
<n> finding(s) contest a recorded decision`; each contested finding reads
"contests decided constraint — held for the operator, not dropped". The checklist
and `operator-settle` rows of the `review_operator_actionable` park do not apply:
there is no `operator-settle` payload.
- Mixed case: if any MUST_FIX in the verdict is a contest, the whole verdict
  parks, even with an unmarked MUST_FIX beside it (the headline's second line
  counts those). No fix cycle runs while a contest is pending; the unmarked
  MUST_FIX is fixed normally on the requeue after you have decided. The
  Claude-native lane keeps its older ordering, where Step 3b fixes of unmarked
  findings may run before the park.
- Dismiss a contest you reject with `cw review settle` (the payload is in the
  comment), then `cw dev-queue requeue <T> -c <client>`. A re-litigation and a
  real contest look the same mechanically; settle the re-litigation.
- Uphold a contest by fixing the decision yourself (change the code or plan by
  hand), then settle the reviewer's re-raise as `REJECTED` so it does not park
  again, and requeue.
- Limit: an unmarked finding whose remedy conflicts with a decision is defended
  only by the prompts and the marker; nothing mechanical catches it.

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
`operator_unavailable`, `dependency_unmerged`, `confirm_backend_unavailable` or
`impl_comments_unreadable_after_regress`; and a no-worker spawn failure that is
a raw `OSError` or a missing `claude` binary (`ClaudeBinaryNotFoundError`,
#2722). Residuals the ceiling still owns or no longer bounds:

- `plan_pending_approval` still charges, deliberately: `auto_adopt_clean_plan`
  (on by default; `gate_recipes_enabled`, or the lane's recipe switch, turns it
  off) can re-claim that park without a human, and the ceiling bounds an
  adopt -> re-run -> re-park cycle. A long plan tail costs one unit per
  `plan_pending_approval` park; `--reset-attempts` clears it.
- Plain `CwError` wraps from `claude --bg` (SSH or 1Password walls, adapter
  failures, CLI flag breaks, an unparseable session id) and `WorktreeError`
  (including a git or SSH failure inside `create_worktree`) still charge.
  Classifying non-zero exits by cause is undecided, and uncharging them
  wholesale would leave a per-ticket failure bounded only by the backoff and
  the lane circuit breaker.
- A raw-`OSError` or missing-`claude`-binary spawn failure that is in fact
  deterministic per ticket is bounded only by the 300 s spawn-error backoff
  cap and the lane circuit breaker (a missing binary is lane-wide, so the
  breaker trips it); no separate spawn-error ceiling exists.
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
  It also carries `pending_skips` per client (#2516): one
  `{ticket_id, skip_reason, consecutive, first_at, last_at, stale}` object per
  PENDING row in the "Pending tickets held by dispatch" block, `Z`-suffixed
  timestamps, `[]` when none. A client with a held row but no tick and no
  paused lane still gets an entry, with null tick fields.
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
  for live same-ticket sessions, then check `cw list` (sessions as recorded,
  not reconciled) and
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
- `Bash(cw review history:*)`
- `Bash(cw lane ls:*)`
- `Bash(cw config show:*)`
- `Bash(cw config concurrency get:*)`
- `Bash(cw preflight:*)`

State-changing verbs are intentionally not listed: `cw lane set`,
`cw config concurrency set|clear`, `cw dev-queue approve|resolve`,
`cw review settle` and `cw review constrain`. Approve those per call, or add them yourself knowing each
one mutates queue, review or lane state. The old script-based attention
watcher was removed in #2693 in favor of `cw orchestrate watch`; drop any
allowlist entry that still names it.

**Command shapes.** Orchestrator-run skills (`.claude/skills/*` and
`.claude/commands/queue-issues.md`) prescribe one plain command per Bash call:
no command substitution, backtick substitution, shell loops or jq filter
flags, because those shapes do not match a per-verb allowlist entry and are
rejected by worktree-isolation hooks. Where one command's output feeds the
next, the skill says to read the field from the printed JSON and substitute it
literally. `tests/test_orchestrator_command_shapes.py` enforces this, and its
exemption map is empty. The `cw-session-watch` arm-automerge recovery fence was
the one exemption until #2753 rewrote it: it now prints the head SHA with a
plain `git rev-parse HEAD`, has the operator substitute the printed 40-hex
value into `--head-sha`, and checks the PR state with
`gh pr view ... --json state | grep -Eq`.
`tests/test_arm_automerge_markdown_sites.py` still pins its exit-code ladder,
NOT-arming default-deny and `--head-sha` pin. Worker-run docs (`auto-dev*`, `prep-pr`, `ship-it`, `review-monitor`)
are out of scope.

### Orchestrator bootstrap

An orchestrator started in a fresh Claude Code worktree needs no setup step:

- **No context file.** With no `.claude/cw-context.json`, `cw review settle`,
  `cw review constrain`, `cw dev-queue resolve`, `cw preflight` and
  `cw orchestrate watch` all run.
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
> `skip=no_pending` beside `pending>0` is not "nothing to do": cross-check
> the "Pending tickets held by dispatch" block, which names a row skipped
> every tick for a per-ticket reason such as `worktree_occupied` (#2516).

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
