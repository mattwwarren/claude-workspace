# cw dev-queue Dispatch Runbook

Operator procedure for driving `cw dev-queue` dispatch end-to-end: enqueue,
dispatch, monitor, handle gates, and clean up. For the sentinel/disposition
contract the monitoring step reads, see
[`docs/session-disposition.md`](session-disposition.md). For the full
`AUTO_DEV_RESULT` schema and event taxonomy, see
[`docs/headless-contract.md`](headless-contract.md).

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

## 4. Monitor

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
