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
[`session-disposition.md §4`](session-disposition.md)). Use the purpose-built
monitor skills:

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

`cw dev-queue status` prints two distinct sections:

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
should have surfaced ATTENTION, inspect `cw dev-queue status` and the
transcript directly (see §6 in [`session-disposition.md`](session-disposition.md)).

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

---

## 8. Related docs

- [`docs/session-disposition.md`](session-disposition.md) — how to read a session's outcome from the transcript sentinel.
- [`docs/headless-contract.md`](headless-contract.md) — `AUTO_DEV_RESULT` schema, status enum, `ReapReason` taxonomy, `queue.session_reaped` bus event.
- [`docs/events.md`](events.md) — event bus reference.

---

## 9. Recovery: worktree leaks (#766) and false-failed runs (#774)

### 9.1 Symptom checklist

`cw dev-queue status` shows `pending > 0` but `claimed = 0` across multiple
ticks. The per-client footer reads something like:

```
skip_reason=freshness_gate  claimed=0  running=0
```

This means `dispatch_tick` is refusing to claim tickets because the local
`main` branch of the client repo is not clean/current relative to
`origin/main`. Two root causes — check in order.

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
