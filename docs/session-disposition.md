# Session Disposition Contract

How to read a `cw` session's outcome correctly. The four gotchas in this
document each caused a wrong disposition: the first three during the
2026-06-10/11 sprint, the fourth while triaging a fix-loop park (#1729).

---

## 1. Authoritative source of truth

The terminal `AUTO_DEV_RESULT` sentinel in the session transcript is the
authoritative outcome. Do not rely on:

- `Session.last_result` — lags reconcile.
- Queue task status — lags reconcile.
- Daemon roster presence — cannot distinguish finished from stalled.

Read the sentinel via `_parse_sentinel_from_transcript`
(`src/cw/cli/_sentinels.py`), which uses `extract_block` + `parse_stdout`
from `cw.auto_dev_result`. Never apply a raw-text regex to the transcript
file.

Transcript location is resolved by `_locate_session_transcript(session)` in
`src/cw/reconcile/_shared.py` (surface_ref-prefix glob, #541):

1. If `session.claude_session_id` is set: `<project_dir>/<csid>.jsonl` directly.
2. Else if `session.surface_ref` is set: newest `<project_dir>/<surface_ref>*.jsonl`
   with `mtime > session.started_at`.
3. Otherwise: None.

Do NOT fall back to an unscoped `*.jsonl` glob — that silently reads a
different session's transcript in a reused worktree.

---

## 2. The four gotchas

### Gotcha 1 — Fixture/example blocks ≠ the terminal emit

A worker's transcript can contain `<<<AUTO_DEV_RESULT … >>>` blocks it wrote
as test fixtures or copied from `auto-dev.md` for sentinel-related tickets.
A naive "first match" false-terminates.

**Rule:** take the **last** block that parses to a real terminal `AutoDevResult`,
and only trust it after the worker has left the daemon roster.

`_parse_sentinel_from_transcript` now implements exactly this: it walks every
sentinel-bearing block in transcript order, keeps the **last** one that
parses, and skips the illustrative example sentinel from the skill prompt
(`is_documented_example`, #591) so a worker quoting the docs never
false-terminates. Other fixture blocks a worker writes can still parse — the
last-wins rule plus the roster check is what protects against those.

### Gotcha 2 — Sentinels are JSON-escaped in the transcript

Claude stores session transcripts as JSONL. Each line is a JSON object; the
`assistant` event's `message.content[].text` field holds the model output as
a JSON string, so the sentinel's quotes and newlines are `\"` and `\n` in the
raw file. A regex over the raw bytes misses sentinels that are valid only
after decoding.

**Rule:** `json.loads` each transcript line, extract the `text` field, then
run `extract_block` against the decoded text. `_parse_sentinel_from_transcript`
does this via `_iter_sentinel_text_blocks` (`cw._util`), which scans both
assistant text blocks AND `tool_result` blocks — a worker may emit the
sentinel via `cat <<EOF`, landing it in Bash stdout rather than assistant
text (#731). Never regex the raw JSONL.

### Gotcha 3 — `claude_session_id` is often None on first lookup

`session.claude_session_id` is populated by the backfill inside `reconcile()`,
which fires only after the first reconcile tick. Before that tick, the field
is None.

**Rule:** locate the transcript via `_locate_session_transcript(session)`
(`src/cw/reconcile/_shared.py`) — it handles `claude_session_id=None` via the
surface_ref-prefix glob (§1). Note that `_parse_sentinel_from_transcript`
takes `(cwd, claude_session_id)` and returns None when the csid is None — so
for a csid-less session, resolve the path first (or derive the csid from the
transcript filename via `_csid_from_transcript`), then parse.

### Gotcha 4 — A `cycleN-review-verdict.json` diagnostics snapshot is not automatically the session's final disposition

A codex fix loop persists one full `ReviewVerdict` snapshot **per cycle** under
the session's diagnostics bundle dir (`cycle0-review-verdict.json`,
`cycle1-...`, …, #1739). Only one of them is the verdict the returned
`Blocker.details` was actually rendered from. Opening `cycle0-...json` "by
habit" reads a file whose `rejected_must_fix` may legitimately be empty while
the reported blocker cites a MUST_FIX that a *later* cycle's re-review
mechanically rejected (#1729) — the two disagree, and neither the filename nor
the numbering says which one is authoritative. The highest-numbered file is not
a safe substitute either: a fix-invocation failure or scope violation parks
before that cycle's re-review ever persists a snapshot.

**Rule:** read `is_terminal_snapshot` (#1763). Exactly one snapshot per session
carries `true`; every superseded intermediate carries `false`. Cross-check
against the `friction_highlights` pointer, which now names the specific file
(`cycle-1 MUST_FIX findings snapshot persisted (cycle1-review-verdict.json)
[diagnostics: …]`) rather than just the bundle directory.

Two paths deliberately leave every snapshot `false`, and both are correct:

- **Unparseable re-review** (`codex_review_unparseable`): the park's details
  come from the reviewer-failure formatter, not from any persisted verdict.
- **Cycle-0 mechanical rejection** (`codex_must_fix_mechanically_rejected`
  before any fix cycle): the fix loop never engages, so no snapshot is written
  at all.

In both cases the sentinel — not a snapshot — remains the source of truth (§1).

**Caveat — the field defaults `true`.** `is_terminal_snapshot` defaults to
`true` on `ReviewVerdict` (fail-toward-trust), so any snapshot written before
#1763 shipped, or by any future caller that doesn't know to stamp it `false`,
will read as terminal/authoritative regardless of whether it actually is. Only
the codex fix loop's per-cycle persist explicitly stamps `false` at write time.
A `cycleN-review-verdict.json` from a session that predates this field carries
no reliable signal either way — cross-check the sentinel (§1) rather than
trusting `is_terminal_snapshot=true` on an old file at face value.

---

## 3. Sentinel status → operator action

| Status | Action |
|---|---|
| `shipped` | Done. PR live with auto-merge enabled. |
| `stage_complete` | No action — one pipeline stage (HARDEN/PLAN/IMPL/REVIEW) finished cleanly; dispatch auto-advances to the next stage. Not a terminal outcome. |
| `no_op` | Done. Ticket already satisfied; close as completed. |
| `ambiguities_pending_resolution` | Resolve ambiguities posted on the issue; re-dispatch. |
| `premises_pending_verification` | Verify flagged premises, record on issue; re-dispatch. |
| `plan_pending_approval` | Parks only for **large** (or unresolved) scope tier — small-tier plans advance unattended. Read the plan comment, post `<!-- auto-dev-plan-approved -->`, then `cw dev-queue approve`. Advances to impl only once quality-reviewed, else re-queues at plan stage (#968). |
| `review_pending_approval` | Parks only for large (or unresolved) tier. Review the pushed branch diff, run gates, then `cw dev-queue approve` (advances to FINALIZE, which ships) — or ship manually (PR + auto-merge). With signoff configured, `approve` re-routes to `AWAITING_OPERATOR_SIGNOFF`; approve again to clear. |
| `merge_pending` | PR created, CI/merge gate not yet cleared (#899). Not a failure — monitor/merge the PR (`pr_url` is preserved on the task); do not re-dispatch. |
| `merge_gate_blocked` | Prior pipeline PR still open; merge or close it; re-dispatch. |
| `scope_exceeded` | Scope rejection; close or relax constraint. |
| `forbidden_area` | Forbidden-area rejection; update constraints or reroute. |
| `blocked` | Triage `blocker.reason`, `blocker.retry_eligible`, `blocker.recovery_hint`. At FINALIZE, `blocker.reason: "agent_block"` auto-regresses the ticket to IMPL for self-heal (up to 2 times, #770). |

`validation_failed` is not a sentinel status but appears in the same
disposition field when the emitted sentinel is malformed: the queue
auto-requeues the ticket to PENDING until the attempt cap. Use
`cw result validate` to inspect the raw payload if it recurs.

A ticket parked in queue status `AWAITING_OPERATOR_SIGNOFF` (RFC 0007
Phase 3) is not a sentinel outcome either — it is the operator ship gate at
REVIEW→FINALIZE; clear it with `cw dev-queue approve` (`cw dev-queue wait`
exits 4 for it).

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
as idle *before* `reconcile()` consumes the sentinel can leave its dev-queue
task reverted to **PENDING** — a phantom task for finished work. A subsequent
dispatch tick will re-spawn it.

This is now a residual case, not the common path: reconcile's salvage logic
reads the transcript sentinel before dispositioning a phantom/stalled
session — an emitted terminal status is honored (`SALVAGE_TERMINAL_STATUSES`,
#372/#431) and an emitted `stage_complete` is routed through the
stage-advance path (#716) rather than reverted as a crash. And under the
default `reap_policy: signal_only` (ADR-0006), detection parks the task
`BLOCKED_ON_USER` with a reap proposal instead of destructively reverting —
the PENDING revert requires `reap_policy: auto` (or `cw doctor --reap`) plus
an unlocatable/unparseable transcript.

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
`ReapReason` table.

> **ADR-0014 note:** since the process-kill-timeout removal, only
> evidence-driven dispositions exist — roster-absence phantoms, recorded
> terminal results, dead local PIDs, and explicit operator commands. The
> idle watchdog, wall-clock budgets, confirm-before-reap counter, and
> liveness veto described in older revisions of this document are gone; a
> quiet-but-live worker now surfaces via the liveness distress signal
> (`session.needs_attention` with `paused_status=session_unresponsive`)
> and is never dispositioned automatically.

If you need to force reconcile to re-examine state:

```bash
cw doctor --reap
```

### 5a. Branch-absence anomaly on `SESSION_TIMED_OUT` (#808) — historical

> **Historical (ADR-0014):** `SESSION_TIMED_OUT` is no longer produced —
> nothing times sessions out. This section is kept for reading *old* event
> logs and legacy `TIMED_OUT` rows in persisted state.

When a session timed out with no sentinel and no merged PR, the reaper checked
whether the feature branch still exists on origin and annotates the
`SESSION_TIMED_OUT` event with a `branch_state` field:

- `"absent_no_merged_pr"` — **anomaly**: no merged PR and the branch is gone.
  This means the worker died before pushing (or the branch was force-deleted).
  It is categorically different from a slow timeout: the worker left no
  artifacts. Investigation is warranted; do not let it churn silently through
  retries without understanding why the push never happened.
- *(key omitted)* — every other case: branch still on origin, branch check
  unavailable, or check did not run (fail-open).

**Critical invariant:** `"absent_no_merged_pr"` **never** routes a session to
COMPLETED. The session still times out and the task reverts to PENDING. Signal
#1 (`pr_is_merged_for_ticket`, §5 cross-ref) is the only safe completion
signal; branch-absence alone is not (#808 security finding). See also
[`docs/dispatch-runbook.md`](dispatch-runbook.md) for the operator breadcrumb.

---

## 6. The disposition-never-null invariant (#976)

Every operator-facing `BLOCKED_ON_USER` park carries a non-null
`TicketTask.disposition`. Before #976, three reconcile park/reroute paths
left `disposition=None` on the parked row: the idle watchdog's
silently-idle park (`idle.py`), and the SIGNAL_ONLY reroute-to-BLOCKED_ON_USER
path shared by the stalled/idle/phantom sweeps (via `_apply_queue_mutations`
in `reconcile/_shared.py`). A handful of other sites (`salvage.py`'s
LOW-path flag, `tasks.py`'s terminal-sibling park, and two config-error
fallbacks in `dispatch.py`'s `_stage_advance_unchecked`) had the same bare
`transition_task_status(task, QueueItemStatus.BLOCKED_ON_USER)` gap.

Every one of these now passes an explicit `disposition=` kwarg, drawn from
the existing `ReapReason` enum (`cw.models`) or the private reason constants
in `cw.reconcile._shared` (`_SILENTLY_IDLE_REASON`, `_STALLED_CAP_PARKED_REASON`,
`_GH_CHECK_BLOCKED_REASON`, `_NEEDS_SALVAGE_REASON`) — never a new literal
where one already existed:

| Park/reroute path | Disposition stamped |
|---|---|
| idle watchdog silently-idle park *(historical, ADR-0014)* | `_SILENTLY_IDLE_REASON` ("silently_idle") |
| stalled-sweep SIGNAL_ONLY reroute *(historical, ADR-0014)* | `ReapReason.WALL_CLOCK_BUDGET` |
| idle-sweep SIGNAL_ONLY reroute *(historical, ADR-0014)* | `ReapReason.IDLE_STALL` |
| phantom-sweep SIGNAL_ONLY reroute (clean crash) | `ReapReason.PHANTOM_SURFACE` |
| phantom gh-check-blocked route | `_GH_CHECK_BLOCKED_REASON` |
| salvage LOW-path flag *(historical, ADR-0014)* | `_NEEDS_SALVAGE_REASON` |
| terminal-sibling park (`tasks.py`) | `ReapReason.TERMINAL_SIBLING` |
| unknown client / invalid pipeline stage (`dispatch.py`) | `"unknown_client"` / `"invalid_stage_config"` (deliberately excluded from concierge/escalation eligibility — config errors, not recoverable states) |
| mechanically-rejected MUST_FIX park (`dispatch/routing.py`, #1714) | `REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION` ("codex_must_fix_mechanically_rejected") — stamped directly by `_park_must_fix_mechanically_rejected`, Rule 5's only reason-keyed override, rather than derived via `_hold_aware_disposition`. Escalation-eligible and drain-eligible; deliberately excluded from `HOLD_DISPOSITIONS` and from concierge's false-park requeue |

`cw.reconcile.escalation`'s `_ELIGIBLE_DISPOSITIONS` and
`cw.reconcile.concierge`'s `_FALSE_PARK_ELIGIBLE_DISPOSITIONS` were updated
to track these newly-non-null values so a ceiling-refused row in one of
these classes still surfaces to the operator instead of silently sticking.

### 6a. The liveness veto (#976, #1277, #1445) — historical

> **Historical (ADR-0014):** the stalled sweep's parks — and therefore the
> veto that bounded them — were removed with the process-kill timeouts. Kept
> for reading old `session.park_vetoed` events. The distress role the veto
> played (surfacing a still-live worker to the operator instead of killing
> it) is now the default behavior for every quiet worker, via the liveness
> distress signal.

The stalled sweep's pending park is additionally **vetoed** — suppressed
entirely, no disposition stamped, no queue mutation — when the session's
freshly-classified liveness bucket (`_classify_liveness_bucket`,
`cw.reconcile.liveness`) is `LivenessBucket.LIVE` at the moment the park would
otherwise fire. This stops the sweep from parking a session that is still
visibly making progress just because its budget expired. Since #1277 the veto
applies to **both** park sites: the ordinary wall-clock-budget revert
(`ReapReason.WALL_CLOCK_BUDGET`) **and** the retry-cap park
(`ReapReason.STALLED_RETRY_CAP_PARKED`, reached once `task.attempts >= cap`).

The veto is **bounded** (#1445). Each granted veto increments the session's
`consecutive_park_vetoes` latch; the veto is only granted while that count is
below `OrchestratorConfig.park_veto_cap` (default 2). Once the cap is reached
the veto stops firing and the pending park proceeds — and at **parity** across
both cap-fire sites an immediate `session.needs_attention` is emitted this same
tick so a still-live worker that has exhausted its veto budget surfaces to the
operator rather than looping silently. The retry-cap park emits it via its
existing path (`paused_status=stalled_retry_cap_parked`); the wall-clock-budget
SIGNAL_ONLY reroute emits it via a dedicated escalation loop
(`paused_status=wall_clock_budget`) that adds only the notification — the task
still routes to `BLOCKED_ON_USER` via the ordinary silent queue mutation, with
no daemon-stop or worktree removal. A "genuinely stale" session (bucket not
`LIVE`) is never misreported as a cap-fire, even if its counter happens to sit
at the cap. The counter resets for free per pipeline episode (each episode is a
fresh `Session`).

A vetoed candidate emits `session.park_vetoed` (see
[`docs/events.md`](events.md)) — carrying the post-increment
`consecutive_vetoes` — instead of `session.reap_proposed` /
`session.needs_attention`, and the session simply continues running —
the sweep re-evaluates it again next tick until the veto cap is hit.

---

## 7. Cross-references

- [`docs/dispatch-runbook.md`](dispatch-runbook.md) — full end-to-end dispatch procedure.
- [`docs/headless-contract.md`](headless-contract.md) — `AUTO_DEV_RESULT` schema, status enum, `ReapReason` taxonomy, `queue.session_reaped` event.
- [`docs/events.md`](events.md) — `session.park_vetoed` and the full orchestrator event-bus reference.
- `src/cw/cli/_sentinels.py:_parse_sentinel_from_transcript` — transcript sentinel reader.
- `src/cw/reconcile/_shared.py:_locate_session_transcript` — transcript path resolver.
- `src/cw/reconcile/_shared.py:_csid_from_transcript` — claude_session_id derivation.
