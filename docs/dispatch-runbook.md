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

This fires when the local `main` is behind `origin/main`. Fix it before
dispatching:

```bash
git -C <client-repo> pull --ff-only
```

`cw dev-queue refresh-all` also fast-forwards every configured client repo,
but it refuses when the working tree has untracked files. The `git pull
--ff-only` form bypasses that refusal.

---

## 2. Enqueue

```bash
cw dev-queue add <TICKET-ID> --client <client> [--scope small|large]
```

`--scope` sets `TicketTask.scope_hint`, the operator tier override that
bypasses auto-tiering from line counts. Use it when:

- The ticket is deletion-heavy (high line count but small real scope).
- You know the tier and want to avoid a misclassification penalty.
- A prior run was mis-tiered and you are re-dispatching.

Scope hint on first dispatch is always `None` until Stage 1 sets it from the
sentinel; subsequent retries inherit the prior sentinel's `scope.tier`.

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

The default concurrency cap is `OrchestratorConfig.default_max_parallel`
(default: `1`). Per-client overrides live in `per_client_max_parallel` in
`orchestrator.yaml`.

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
  dry-runs.
- **Single-instance**: `serve` provides no pidfile or lock. Running two
  `serve` processes simultaneously creates two competing dispatch loops.
  The operator is responsible for ensuring exactly one `serve` is running
  (same responsibility as the existing `run` command).

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

Primary wave-level monitoring is now subscribe-first — see §2's operator
channel flow; this section is the single-ticket blocking-wait contract.

Use `cw dev-queue wait` — the sentinel-aware monitor (#535):

```bash
cw dev-queue wait <TICKET-ID> --client <client> --timeout <seconds> --json
```

Exit codes:

- `0` — `shipped` / `no_op` (or `COMPLETED` queue status)
- `1` — `scope_exceeded` / `forbidden_area` / `FAILED` / `CANCELLED`
- `2` — `blocked` / any `*_pending_*` status / `BLOCKED_ON_USER`
- `3` — ATTENTION: transcript stale past idle budget, worker not in daemon roster
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

**Known gap (#542):** when a session is reaped mid-wait, reconcile reverts
the task to PENDING and clears `session_id`. The wait's spawn-window grace
(re-poll when `session_id` is None) cannot distinguish a fresh spawn from a
just-reaped task, so ATTENTION never fires and wait rides to the `--timeout`
ceiling (exit 124) instead. Workaround: if wait returns 124 on a run that
should have surfaced ATTENTION, inspect `cw dev-queue status` (or `cw board`
for a live view) and the transcript directly (see §6 in
[`session-disposition.md`](session-disposition.md)).

---

## 5. Handle gates by sentinel status

| Sentinel status | Action |
|---|---|
| `shipped` | Done. PR is live with auto-merge enabled; CI wait is orchestrator concern. |
| `no_op` | Done. Ticket already satisfied; close as completed. |
| `ambiguities_pending_resolution` | Resolve the ambiguities (post a `Pre-flight Resolutions` comment on the issue), then re-dispatch. |
| `premises_pending_verification` | Verify the flagged premises, record results on the issue, re-dispatch. |
| `plan_pending_approval` | Read the plan comment, verify it is faithful to the ticket, post `<!-- auto-dev-plan-approved -->` on the issue, re-dispatch for impl. |
| `review_pending_approval` | Locate the branch, verify the diff and run gates yourself, then ship (PR + auto-merge). |
| `plan_unreviewable` / `plan_unsound` | Resolve remaining plan issues. If the pipeline bounces repeatedly on an intricate ticket, use the **spec-driven subagent escape hatch** (§7) rather than retrying. |
| `validation_failed` | Transient (malformed sentinel); re-dispatch. Use `cw result validate` to inspect the raw JSON before re-dispatch if the failure recurs. |
| `blocked` | Triage `blocker.reason`. Check `blocker.retry_eligible` and `blocker.recovery_hint`. |
| `merge_gate_blocked` | A prior pipeline PR is still open. Merge or close it, then re-dispatch. |

---

## 6. Cleanup after terminal (ordering matters)

Wrong ordering leaves phantom PENDING orphans. Follow this sequence:

```bash
# 1. Mark the cw session completed
cw done <session-name>

# 2. Stop the live daemon worker (if still running)
#    cw spawn close refuses an already-completed session — step 1 first.
cw spawn close <session-id>

# 3. Remove the dev-queue task
cw dev-queue remove <TICKET-ID> --client <client> --all
```

If the task is already in a terminal queue status but wedged (e.g.,
`RUNNING` with no live session), `cw doctor --reap` detects and repairs
common wedge conditions:

- `wedge/task-running-no-session` — reverts task to PENDING.
- `wedge/task-running-completed-session` — reverts task to PENDING.

Run `cw doctor --reap --json` for machine-readable output.

---

## 7. Patterns

### Harden before dispatch

Run `/harden-ticket` on a ticket before the first dispatch to surface
ambiguities and unsound premises upfront. This eliminates the
ambiguity/plan-review whack-a-mole loop that burns dispatch cycles.

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

### Attempt-cap reset (environmental burn)

A quota window or hang loop (#979) grinds a ticket to
`attempt_cap_blocked` (`attempts` bumps on every claim AND stage transition).
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

### Circuit-breaker pause and held slots

- `cw dev-queue status` lane line shows `[PAUSED]` → the #875 per-lane
  breaker tripped on consecutive spawn errors. `cw lane resume <CLIENT>
  <lane>` resumes AND resets the counter. The pause is silent today — check
  for it whenever pending tickets sit unclaimed.
- `BLOCKED_ON_USER` rows hold lane slots by design; a parked ticket can
  starve its lane. Requeue or remove to free the slot.
- A session wedged in `needs_salvage` with a park marker poisons every
  respawn of its ticket (claim → revert → attempts+1, plus per-tick
  `session.salvage_skipped` noise): `cw spawn close <sid> --confirmed-dead`
  clears it.

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
> then de-duplicated to silence — so a persistent block leaves no live trace
> except the `skip_reason` in `cw dev-queue status`. Do not "restart the
> monitor" reflexively; read the skip_reason first. (See #908.)

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
re-accumulates an `ahead`/`diverged` state. (Recurs every release cycle; see
#908 for surfacing the block proactively instead of relying on this runbook.)

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
finalize_blocked` and a `rescue_attempted: true` marker. The branch is pushed
to origin; `gh pr create` failed transiently (permission error, usage limit, or
network blip) and the rescue loop will not retry.

**Diagnose.**

```bash
cw dev-queue status          # task shows BLOCKED_ON_USER
cw session show <ticket-id>  # last_result contains rescue_attempted: true
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

  If `CW_PR_EVENTS_HMAC_SECRET` is unset server-side, `/pr-event` accepts
  **unsigned** requests (pre-#930 behavior) and `serve()` logs a startup
  warning ("accepts unsigned requests") so the unauthenticated posture is
  visible in the server logs, not silent. **Blast radius of running
  unsigned behind a public tunnel**: any POST reaching the relay can mutate
  `pr_state` for *any* `(repo, pr_number)` currently tracked across *all*
  clients in `dev_queue.json`, with no rate limiting or origin check beyond
  JSON shape. Relaying this endpoint over the open internet without the
  secret set is not recommended.
- **Fork PRs cannot authenticate.** `pull_request`/`pull_request_review`
  events triggered by a fork-originated PR run with no access to repo
  secrets, so `secrets.CW_PR_EVENTS_HMAC_SECRET` resolves empty and the
  workflow falls through to the unsigned `curl` branch (a `::warning::`
  annotation on the run, nothing louder). This is a known, accepted
  limitation (#930) — not worked around via `pull_request_target`, since
  that would expose secrets to untrusted fork checkout content — because
  this pipeline's tracked PRs are same-repo/bot-originated, never forks.
  The poll producer still covers fork PRs on its own schedule regardless.

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
