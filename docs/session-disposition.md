# Session Disposition Contract

How to read a `cw` session's outcome correctly. The three gotchas in this
document each caused a wrong disposition during the 2026-06-10/11 sprint.

---

## 1. Authoritative source of truth

The terminal `AUTO_DEV_RESULT` sentinel in the session transcript is the
authoritative outcome. Do not rely on:

- `Session.last_result` — lags reconcile.
- Queue task status — lags reconcile.
- Daemon roster presence — cannot distinguish finished from stalled.

Read the sentinel via `_parse_sentinel_from_transcript` (`src/cw/cli.py`),
which uses `extract_block` + `parse_stdout` from `cw.auto_dev_result`. Never
apply a raw-text regex to the transcript file.

Transcript location is resolved by `_locate_session_transcript(session)` in
`src/cw/reconcile.py` (surface_ref-prefix glob, #541):

1. If `session.claude_session_id` is set: `<project_dir>/<csid>.jsonl` directly.
2. Else if `session.surface_ref` is set: newest `<project_dir>/<surface_ref>*.jsonl`
   with `mtime > session.started_at`.
3. Otherwise: None.

Do NOT fall back to an unscoped `*.jsonl` glob — that silently reads a
different session's transcript in a reused worktree.

---

## 2. The three gotchas

### Gotcha 1 — Fixture/example blocks ≠ the terminal emit

A worker's transcript can contain `<<<AUTO_DEV_RESULT … >>>` blocks it wrote
as test fixtures or copied from `auto-dev.md` for sentinel-related tickets.
A naive "first match" false-terminates.

**Rule:** take the **last** block that parses to a real terminal `AutoDevResult`,
and only trust it after the worker has left the daemon roster.

Note: `_parse_sentinel_from_transcript` itself scans forward and returns the
**first** block with sentinel framing. That is safe on its `signal_stop` call
path (it fires at process exit, when the real emit is normally the only
sentinel present) — but it is NOT safe for manual disposition of
sentinel-related tickets whose transcripts contain fixture blocks. When
dispositioning manually, reuse its JSON-decoding walk
(`_iter_assistant_text_blocks` + `extract_block`/`parse_stdout`) but take the
final parseable block, not the first.

### Gotcha 2 — Sentinels are JSON-escaped in the transcript

Claude stores session transcripts as JSONL. Each line is a JSON object; the
`assistant` event's `message.content[].text` field holds the model output as
a JSON string, so the sentinel's quotes and newlines are `\"` and `\n` in the
raw file. A regex over the raw bytes misses sentinels that are valid only
after decoding.

**Rule:** `json.loads` each transcript line, extract the `text` field, then
run `extract_block` against the decoded text. `_parse_sentinel_from_transcript`
does this via `_iter_assistant_text_blocks`. Never regex the raw JSONL.

### Gotcha 3 — `claude_session_id` is often None on first lookup

`session.claude_session_id` is populated by the backfill inside `reconcile()`,
which fires only after the first reconcile tick. Before that tick, the field
is None.

**Rule:** locate the transcript via `_locate_session_transcript(session)`
(`src/cw/reconcile.py`) — it handles `claude_session_id=None` via the
surface_ref-prefix glob (§1). Note that `_parse_sentinel_from_transcript`
takes `(cwd, claude_session_id)` and returns None when the csid is None — so
for a csid-less session, resolve the path first (or derive the csid from the
transcript filename via `_csid_from_transcript`), then parse.

---

## 3. Sentinel status → operator action

| Status | Action |
|---|---|
| `shipped` | Done. PR live with auto-merge enabled. |
| `no_op` | Done. Ticket already satisfied; close as completed. |
| `ambiguities_pending_resolution` | Resolve ambiguities posted on the issue; re-dispatch. |
| `premises_pending_verification` | Verify flagged premises, record on issue; re-dispatch. |
| `plan_pending_approval` | Read the plan comment, post `<!-- auto-dev-plan-approved -->`; re-dispatch for impl. |
| `review_pending_approval` | Review the pushed branch diff, run gates, ship (PR + auto-merge). |
| `merge_gate_blocked` | Prior pipeline PR still open; merge or close it; re-dispatch. |
| `scope_exceeded` | Scope rejection; close or relax constraint. |
| `forbidden_area` | Forbidden-area rejection; update constraints or reroute. |
| `validation_failed` | Transient (malformed sentinel); re-dispatch. Use `cw result validate <json>` to inspect the raw payload if it recurs. |
| `blocked` | Triage `blocker.reason`, `blocker.retry_eligible`, `blocker.recovery_hint`. |

For the full status-enum semantics and `blocker` shape, see
[`docs/headless-contract.md §4`](headless-contract.md).

---

## 4. Attempts and status transitions are mechanics, not outcomes

A dev-queue task's `attempts` counter and its `running` ↔ `pending`
transitions are **pipeline mechanics, not health signals**. Reading them as
churn is a recurring false alarm — it cost a manual transcript investigation on
2026-06-21 (#817).

- **`attempts` increments on every stage transition.** auto-dev runs as staged
  sessions (plan → impl → review → ship); each stage completes by emitting an
  `AUTO_DEV_RESULT` sentinel (§1), after which the dispatcher bumps `attempts`
  and re-spawns for the next stage. So `att=4` can mean "advanced through four
  healthy stages," not "retried four times."
- **`running → pending` is the between-stage requeue.** A pure-plan session
  leaves no impl commits; the orchestrator reverts the task to `pending` and the
  next tick respawns it into the impl stage. This is correct, not a stall.

**The discriminator is the sentinel, not the counter.** An attempt bump is a
*healthy advance* if the just-ended session emitted a terminal `AUTO_DEV_RESULT`
(§1). It is *churn* only when sessions die **without** a sentinel **and** leave
**no new worktree commits and** die fast — i.e. sustained no-progress, not a
single bump. Three conditions, all required: no-sentinel + no-artifact +
fast-death.

**Use `cw queue peek` for the in-flight verdict**, not raw `cw dev-queue
tasks`. Peek resolves the transcript, parses the last sentinel into
`stage`/`status`, and emits a WAIT / PEEK / STOP recommendation via the
peek-stop ladder (see the `cw-queue-peek` skill). `att ≥ 3` and
long-stall-without-PR are already encoded there — don't re-derive them by hand.

**Transcript resolution** (#817). Peek resolves the transcript via the
session's `worktree_path` (loaded from `CW_STATE`, not the task row — dispatch
writes `worktree_path` to the Session but not to the TicketTask). This works
for any `feature_branch_prefix`: dispatch workers whose project dirs are named
after the worktree path (e.g. `…-dev-817`) are found by
`claude_project_dir(worktree_path)`, not by the old `auto-dev-{ticket_id}`
substring match that only matched `auto-dev-` prefix clients. Within the
project dir, resolution tries (1) exact `claude_session_id` match, (2)
`surface_ref`-prefix glob with mtime-after-`started_at` stale guard, (3)
newest `*.jsonl` as a degraded fallback when ids are not yet backfilled.

**Degraded-signal fallback.** When no worktree path is available for the
session and the heuristic name search also fails, peek returns null
`stage`/`status`/`age`/`idle` and a bare `PEEK` ("no transcript timestamps —
verify session is alive"). That is a **blind** signal, not a stall. If you
encounter it, scan the worktree's claude project dir manually: the newest
`*.jsonl`, whether its line count is still growing (liveness), and its last
parseable `AUTO_DEV_RESULT` (progress).

---

## 5. The orphan condition

A session that emits a terminal sentinel (`shipped` / `no_op`) but is reaped
as idle *before* `reconcile()` consumes the sentinel leaves its dev-queue task
reverted to **PENDING** — a phantom task for finished work. A subsequent
dispatch tick will re-spawn it.

**Verify true state:**

1. Read the transcript sentinel directly (§1 above).
2. Check the PR or issue for completion evidence.
3. If the work is already done, clean up manually:
   ```bash
   cw done <session-name>
   cw dev-queue remove <TICKET-ID> --client <client> --all
   ```

**Observability:** reconcile emits `queue.session_reaped` on the queue-events
bus (`cw event tail`) whenever it disposes of a session. The `reason` field
uses the `ReapReason` taxonomy; see the "queue.session_reaped Bus Event"
section of [`docs/headless-contract.md`](headless-contract.md) for the full
`ReapReason` table. Two hardening measures make false reaps less likely:

- **Confirm-before-reap** (`OrchestratorConfig.idle_confirm_observations`,
  default `2`): reconcile requires `session.idle_observation_count` to reach
  this threshold across consecutive watchdog ticks before dispositing. A single
  stale tick no longer triggers an immediate reap.
- **Widened liveness windows** (#544/#548): the per-tier idle-watchdog budgets
  are wider, reducing the window where a healthy-but-slow session looks idle.

If a reap occurs and you need to force reconcile to re-examine state:

```bash
cw doctor --reap
```

### 5a. Branch-absence anomaly on `SESSION_TIMED_OUT` (#808)

When a session times out with no sentinel and no merged PR, the reaper checks
whether the feature branch still exists on origin and annotates the
`SESSION_TIMED_OUT` event with a `branch_state` field:

- `"present"` — ordinary slow timeout; the branch is still on origin.
- `"absent_no_merged_pr"` — **anomaly**: no merged PR and the branch is gone.
  This means the worker died before pushing (or the branch was force-deleted).
  It is categorically different from a slow timeout: the worker left no
  artifacts. Investigation is warranted; do not let it churn silently through
  retries without understanding why the push never happened.
- *(key omitted)* — branch check was unavailable or did not run (fail-open).

**Critical invariant:** `"absent_no_merged_pr"` **never** routes a session to
COMPLETED. The session still times out and the task reverts to PENDING. Signal
#1 (`pr_is_merged_for_ticket`, §5 cross-ref) is the only safe completion
signal; branch-absence alone is not (#808 security finding). See also
[`docs/dispatch-runbook.md`](dispatch-runbook.md) for the operator breadcrumb.

---

## 6. Cross-references

- [`docs/dispatch-runbook.md`](dispatch-runbook.md) — full end-to-end dispatch procedure.
- [`docs/headless-contract.md`](headless-contract.md) — `AUTO_DEV_RESULT` schema, status enum, `ReapReason` taxonomy, `queue.session_reaped` event.
- `src/cw/cli.py:_parse_sentinel_from_transcript` — transcript sentinel reader.
- `src/cw/reconcile.py:_locate_session_transcript` — transcript path resolver.
- `src/cw/reconcile.py:_csid_from_transcript` — claude_session_id derivation.
