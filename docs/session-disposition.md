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
`src/cw/reconcile/_shared/_transcripts.py` (surface_ref-prefix glob, #541):

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
sentinel-bearing transcript block in order and keeps the **last** real result.
The last real sentinel block wins inside a single transcript block as well as
across blocks (`parse_last_block_per_chunk`, #2515), so a block that quotes an
earlier result no longer collapses to `multiple_result_blocks`. Unresolved
placeholder blocks and the illustrative example sentinel from the skill prompt
(`is_documented_example`, #591) are skipped before `parse_stdout`, so a worker
quoting the docs never false-terminates. Blocks naming a different ticket than
the session's own are skipped too: reconcile passes the session's ticket id, and
the cli scan reads it from `cw-context.json` (failing closed with no sentinel
when none is available). Other fixture blocks a worker writes can still parse —
the last-wins rule plus the roster check is what protects against those.

### Gotcha 2 — Sentinels are JSON-escaped in the transcript

Claude stores session transcripts as JSONL. Each line is a JSON object; the
`assistant` event's `message.content[].text` field holds the model output as
a JSON string, so the sentinel's quotes and newlines are `\"` and `\n` in the
raw file. A regex over the raw bytes misses sentinels that are valid only
after decoding.

**Rule:** `json.loads` each transcript line, extract the `text` field, then
parse the decoded text for sentinel blocks (never the raw line).
`_parse_sentinel_from_transcript`
does this via `_iter_sentinel_text_blocks` (`cw._util`), which scans both
assistant text blocks AND `tool_result` blocks — a worker may emit the
sentinel via `cat <<EOF`, landing it in Bash stdout rather than assistant
text (#731). Never regex the raw JSONL.

### Gotcha 3 — `claude_session_id` is often None on first lookup

`session.claude_session_id` is populated by the backfill inside `reconcile()`,
which fires only after the first reconcile tick. Before that tick, the field
is None.

**Rule:** locate the transcript via `_locate_session_transcript(session)`
(`src/cw/reconcile/_shared/_transcripts.py`) — it handles `claude_session_id=None` via the
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
| `plan_pending_approval` | Usually released automatically by the `auto_adopt_clean_plan` gate recipe (on by default) unless it touches a forbidden area or carries `scope_hint: large`. If it stays parked: parks only for **large** (or unresolved) scope tier — small-tier plans advance unattended. Read the plan comment, then `cw dev-queue approve` (records `plan_approved_at` and the approved draft's fingerprint on the row — tracker-neutral; `--post-marker` additionally posts an audit-only `<!-- auto-dev-plan-approved: <sha> -->` comment on GitHub — a changed draft gets a fresh marker, the same draft does not duplicate, and nothing reads it back as approval evidence). Advances to impl only once quality-reviewed, else re-queues at plan stage (#968). |
| `review_pending_approval` | Usually released automatically by the `auto_approve_clean_review` gate recipe (on by default) unless health is degraded, a forbidden area is touched, or it carries `scope_hint: large`. If it stays parked: parks only for large (or unresolved) tier. Review the pushed branch diff, run gates, then `cw dev-queue approve` (advances to FINALIZE, which ships) — or ship manually (PR + auto-merge). With signoff configured, `approve` re-routes to `AWAITING_OPERATOR_SIGNOFF`; approve again to clear. |
| `merge_pending` | PR created, CI/merge gate not yet cleared (#899). Not a failure — monitor/merge the PR (`pr_url` is preserved on the task); do not re-dispatch. |
| `merge_gate_blocked` | Prior pipeline PR still open; merge or close it; re-dispatch. |
| `scope_exceeded` | Scope rejection; close or relax constraint. |
| `forbidden_area` | Forbidden-area rejection; update constraints or reroute. |
| `blocked` | Triage `blocker.reason`, `blocker.retry_eligible`, `blocker.recovery_hint`. At FINALIZE, `blocker.reason: "agent_block"` auto-regresses the ticket to IMPL for self-heal (up to 2 times, #770). |
| `stale_dispatch` | This ticket already has an open, unmerged PR from an earlier dispatch — the session found it and refused rather than duplicating work already in review (#1862). `blocker.details` names the PR. Land or close that PR, then `cw dev-queue requeue <T> -c <client>`. Do **not** re-dispatch first: you will just get the same refusal. |

Since #1927 the `stale_dispatch` park carries its own PR-state source, so the
manual `requeue` above is only the *default*-policy instruction. Each tick,
`cw` registers the blocking PR named in `blocker.details` as a watched PR and
hydrates it alongside every other tracked PR. Once that PR is observed
`MERGED`, the park is re-validated exactly like the other stale-gate variants
(ADR-0006): under `reap_policy: signal_only` (the default) the row stamps
`stale_gate_detected_at` and emits `SESSION_REAP_PROPOSED` but stays
`BLOCKED_ON_USER` — you still run the `requeue`; under `reap_policy: auto` for
that lane the row self-releases to `PENDING` and re-dispatches with no
operator action. Closing (rather than merging) the blocking PR is not
observed as a clearing event — that case still needs the manual `requeue`.

`stale_dispatch_gate` is not a sentinel status but appears in the same
disposition field: it is the *code-side* twin of `stale_dispatch`, stamped
when `cw`'s pre-dispatch open-PR gate refused to spawn a session at all
(`blocked_reason: "pr_already_open_pre_dispatch"`, no session id, empty
breadcrumbs). Same operator action — the difference is only whether an agent
ever ran. See [`docs/headless-contract.md`](headless-contract.md)'s
"The `stale_dispatch_gate` Disposition" section.

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

**Known ID-less sessions get no fallback** (#2417). When the task's Session row
exists in `CW_STATE` and both `claude_session_id` and `surface_ref` are null,
peek skips step (3), the legacy ticket-name heuristic, and the newest-`*.jsonl`
idle scan. In a reused worktree those would hand the current session an
earlier stage's transcript and its `stage_complete` sentinel. The row is blind
instead: `PEEK-BLIND`, null `stage`/`status`/`age`/`idle`, null
`jsonl_idle_min`, and reason `blind — never started`, or `blind — no Claude
transcript (local process handle present)` when the Session carries a
`local_liveness` handle (a local process such as codex may be running). Exact
`claude_session_id` and `surface_ref` matches, the OpenCode log, and the case
of no matching Session row are unchanged.

**Degraded-signal fallback.** When no worktree path is available for the
session and the heuristic name search also fails, peek returns null
`stage`/`status`/`age`/`idle` and `PEEK-BLIND` ("no resolvable transcript;
none found"). That is a **blind** signal, not a stall. If you
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
> and is never dispositioned automatically. The Stop-hook abandoned-exit
> park (#2135, §6c) joins that evidence-driven set: it fires on the park
> marker the worker itself recorded, never on elapsed time, and mutates only
> the dev-queue row.

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
in `reconcile/_shared/_routing.py`). A handful of other sites (`salvage.py`'s
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
| phantom-sweep unresolved-subagent-spawn reroute (#1646) | `_UNRESOLVED_SUBAGENT_SPAWN_REASON` ("unresolved_subagent_spawn") — a clean *or* dirty crash whose worktree still carries an unresolved spawn stamp. Takes precedence over both `PHANTOM_SURFACE` and `dirty_worktree`, and **overrides `reap_policy: auto`**. See §6b |
| phantom gh-check-blocked route | `_GH_CHECK_BLOCKED_REASON` |
| Stop-hook abandoned-exit park (#2135) | `_STOPPED_WITHOUT_SENTINEL_REASON` ("stopped_without_sentinel") — the Stop hook saw the worker's recorded `park_comment_marker` and no sentinel. See §6c |
| salvage LOW-path flag *(historical, ADR-0014)* | `_NEEDS_SALVAGE_REASON` |
| terminal-sibling park (`tasks.py`) | `ReapReason.TERMINAL_SIBLING` |
| unknown client / invalid pipeline stage (`dispatch.py`) | `"unknown_client"` / `"invalid_stage_config"` (deliberately excluded from concierge/escalation eligibility — config errors, not recoverable states) |
| mechanically-rejected MUST_FIX park (`dispatch/routing.py`, #1714) | `REVIEW_MUST_FIX_MECHANICALLY_REJECTED_DISPOSITION` ("codex_must_fix_mechanically_rejected") — stamped directly by `_park_must_fix_mechanically_rejected`, Rule 5's only reason-keyed override, rather than derived via `_hold_aware_disposition`. Escalation-eligible and drain-eligible; deliberately excluded from `HOLD_DISPOSITIONS` and from concierge's false-park requeue |
| fix-dispatch unresolvable-remote-ref park (`fix_dispatch.py`, #2209) | `_FIX_DISPATCH_REF_UNRESOLVED_REASON` ("fix_dispatch_ref_unresolved") — stamped by `_park_for_unresolved_ref` when no candidate in the reported/upstream/templated remote-ref ladder has a tip matching worktree HEAD. Escalation-eligible; excluded from `HOLD_DISPOSITIONS`, `DRAIN_DISPOSITIONS`, and concierge's false-park requeue — and, unlike every other row in this table, does NOT clear `pending_fix_dispatch` on park, retaining the REVIEW round's action list as evidence for the operator. That retention is not a resume point: `cw dev-queue requeue` sets the row PENDING, the retained handoff is then dropped by the #2142 stale-handoff sweep, and the ticket is claimed into a **fresh REVIEW session** (#2265 decides whether requeue should resume the handoff instead) |

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

### 6b. Unresolved sub-agent spawns (#1646)

A worker can die — or pause forever — while a sub-agent spawn is still in
flight. That failure is invisible to every transcript-based signal: no terminal
sentinel is written, no `tool_result` is ever recorded, and the surface simply
stops. Reconstructing it after the fact from the transcript is guesswork, and
the generic `phantom_surface` disposition it used to land under says only "the
surface is gone" — which is also what a clean, harmless crash looks like.

**Mechanism.** `spawn._write_hook_context` seeds an `agent_spawn_stamp` object
into the worktree's `.claude/cw-context.json` (schema v5):

```json
"agent_spawn_stamp": { "unresolved_count": 0, "last_stamped_at": null }
```

A `PreToolUse` hook (`cw agent-spawn-pre`) increments `unresolved_count` before
a sub-agent spawn starts; a `PostToolUse` hook (`cw agent-spawn-post`)
decrements it when the spawn returns. Both are matched on
`spawn._AGENT_TOOL_MATCHER`. If the worker dies between the two, the count stays
above zero **on disk, in its own worktree** — durable state, not an inference.
The phantom sweep reads it via `reconcile._shared._read_unresolved_subagent_spawn`
during candidate classification and stamps
`_UNRESOLVED_SUBAGENT_SPAWN_REASON` instead of `PHANTOM_SURFACE` /
`dirty_worktree`.

It is a **counter, not a flag**: Claude Code can dispatch several sub-agent
`tool_use` blocks in one assistant turn, so two Pre hooks can fire before either
Post does, and a boolean would lose the second spawn. Decrements floor at zero,
so a Post with no matching Pre (a reused worktree, a hook wired mid-flight)
cannot swallow the next real crash.

**Matcher name.** The subagent tool reports `tool_name: "Agent"` — captured
empirically from a live hook payload, *not* taken from the prose, which calls it
the `Task` tool. The matcher is an anchored alternation over both names: the
alternation is version robustness, and the anchor keeps unrelated names like
`TaskStop` from pairing a spurious increment with a spurious decrement.

**Fail-open, in one direction only.** A missing worktree, a missing or pre-v5
context, malformed JSON, a non-dict payload, a wrong-typed count, or any other
error all read as `False` — an ordinary phantom. The asymmetry is deliberate:
this disposition also overrides `reap_policy: auto` (below), so a false positive
parks a healthy ticket, which is strictly worse than losing one crash's
precision. The hooks themselves never block a tool call and never exit non-zero;
lock contention is a bounded non-blocking retry that skips the stamp rather than
stalling the live worker's turn.

**`reap_policy: auto` override.** This one class always parks
`BLOCKED_ON_USER`, even on a lane configured `reap_policy: auto` — a deliberate,
documented exception to that policy's silent PENDING revert. `auto` is opt-in
per lane and this crash class is rare, but silently retrying a session that has
committed work behind a verification tail that never ran compounds exactly the
failure the stamp exists to catch. The override runs *before*
`resolve_reap_policy` is consulted at all (`phantom/core.py`), so the AUTO branch
cannot reclaim the candidate. The pre-existing merged-PR / gh-blocked fast path
still wins ahead of it: if the work already landed, how the session died no
longer matters.

**Escalation, not requeue.** The reason is escalation-eligible (its own union
term in `reconcile.escalation._ELIGIBLE_DISPOSITIONS`) so splitting it off
`phantom_surface` does not cost these rows their operator page. It is
deliberately **not** in `_REAP_ELIGIBLE_DISPOSITIONS_BASE`, which would hand it
to concierge's false-park requeue — re-running a session over possibly-committed
work without a human ever looking is the precise outcome this ticket forbids.

**Not to be confused with the salvage machinery.** `_NEEDS_SALVAGE_REASON` /
`TicketTask.salvage_no_sentinel_at` look like they cover this and do not: that
producer was deleted outright by ADR-0014 and the reason is marked *historical*
in the table above. Nothing sets it. Do not wire new no-sentinel detection into
it. Related: #1630, #1625.

---

### 6c. Stop-hook abandoned-exit park (#2135)

A headless worker that posts its park/blocker comment to the tracker and then
stops without emitting an `AUTO_DEV_RESULT` sentinel used to leave its row
`RUNNING` until the liveness ladder noticed it 45 minutes later. `cw
signal-stop` can now route that row itself.

> **Ships dark — default off.** The park is a state-mutating auto-actor, so it
> is gated by `park_on_abandoned_exit_enabled` in `orchestrator.yaml` (default
> `false`) plus a per-lane / per-ticket `park_on_abandoned_exit` map whose
> floor is `false`. With the park disabled, a sentinel-less Stop defers
> exactly as it did before #2135 and the marker is not even read. The
> Stop hook checks its preconditions cheapest-first — headless DAEMON session,
> empty `background_tasks`, a `RUNNING` dev-queue row for the session — and
> only then resolves the flag, so a session with no row to park never reads
> config at all. The resolved config is memoized per `(client, lane)` for the
> (short-lived) hook process, and every failure resolves to *disabled*: an
> unreadable or invalid `orchestrator.yaml` / `clients.yaml`, an unreadable
> `dev_queue.json`, a client absent from `clients.yaml`, a lane that client
> never declares, and an absent lane entry all defer, log once at WARNING with
> the names and error class, and never raise out of the hook. The undeclared-lane
> gate runs *ahead* of all three resolver tiers, so a per-ticket
> `park_on_abandoned_exit` override cannot open a lane nobody armed.
> Arming it is an operator action — see
> [`config/CONFIG_REFERENCE.md`](../config/CONFIG_REFERENCE.md)'s *Abandoned-Exit
> Park Enablement*.

Once armed, the park fires on four-part evidence:

1. the Stop fired with **no pending background tasks** (the existing
   `background_tasks` guard in `signal_stop` establishes this — with one
   narrow exception since #2458: a Stop with pending background work reaches
   the park when the session holds a staged `cw result emit` result that
   fails reconstruction, see §6d);
2. **no sentinel** was parsed from the transcript;
3. the worktree's `.claude/cw-context.json` carries a `park_comment_marker`
   matching the current cw session id, the ticket id, and the **`RUNNING`
   row's stage** — written by the worker itself with `cw signal-park` after
   its park comment posted; and
4. **no** `AUTO_DEV_RESULT` framing text — not even an unpaired open marker, a
   placeholder, or a #1692-discarded frame — appears in the transcript at or
   after the marker's `posted_at`.

This is **evidence-driven, not a timer** — the same family as ADR-0014's
"What remains" (roster-absence phantoms, recorded terminal results,
emitted-sentinel routing). `posted_at` is never compared to a wall clock: no
age, no expiry, no threshold. Its only non-audit use is as the ordering pivot
in conjunct 4, a comparison that can only ever *suppress* a park.

It mutates the dev-queue row only: `RUNNING →
BLOCKED_ON_USER`, `disposition="stopped_without_sentinel"`, no
`blocked_reason`, and **no `unproductive_attempts` charge** (the park post is
positive evidence the stage did its work).

The **session is left ACTIVE** and the daemon worker is not stopped, so a
late sentinel still routes through the #918 rescue in
`_apply_sentinel_to_task` — the row keeps its `session_id` precisely so that
rescue can re-find it. The park is therefore reversible.

Like `gh_check_blocked`, the disposition is in **neither** concierge's
`_REAP_ELIGIBLE_DISPOSITIONS_BASE` (auto-requeue would re-run a stage the
operator was just asked to look at) **nor** escalation's eligibility set.

The liveness sweep's `session_unresponsive` distress signal is **suppressed**
for a row in this state whose `session_id` matches the session being
classified — it already paged through its own `session.needs_attention`.
Signal-only and per tick: bucket latching and `session.liveness_changed` are
unaffected, every other disposition still pages, and once the row is requeued
(its status leaves `BLOCKED_ON_USER`) the signal applies again.

**Producer contract and limits.** The marker is a *recorded claim by the
worker* that it posted its park comment and is taking that exit — not an
observation by cw that any comment exists. cw never reads the tracker, which
is what makes the evidence tracker-agnostic (GitHub and Linear alike). What
follows from that:

- **One wired stage.** Only the plan stage's consolidated park stamps today:
  step 3a of `.claude/commands/auto-dev-plan-appendix.md`, after the single
  `## Pending Verification Scan` comment that `ambiguities_pending_resolution`,
  `premises_pending_verification`, `plan_pending_approval`,
  `deferred_stub_unresolved` and `ambiguity_scan_unconverged` all share. The
  impl and review park paths do not stamp yet (#2228); no impl exit posts a
  tracker comment at all, so there is nothing there for the marker to mean.
- **Crash window.** A worker that dies between deciding its exit and running
  `cw signal-park` leaves no marker, and the Stop hook defers exactly as it did
  before #2135 — the row then waits for the `stale_45m` liveness signal or an
  operator.
- **No marker means defer**, and so does a malformed one (treated as absent,
  silently — the writer is cw code, so the only routes here are a hand edit or
  corruption).
- **A missing or unreadable transcript defers.** Conjunct 4 is negative
  evidence: without a clean read, a late frame cannot be ruled out, so an
  unreadable file and a torn final line both suppress the park.
- **A `Read` of a stage doc after the stamp also defers**, because the quoted
  frame literal lands in a `tool_result` the guard sees. That fails toward
  pre-#2135 behavior.
- **Stale markers do not count.** `cw.spawn` rewrites `cw-context.json`
  wholesale on every dispatch, which is the *only* staleness guard — the
  session-id and ticket-id checks compare that file with itself. A `cw bg`
  followed by `cw resume` of the same daemon session re-enters under the same
  session id **without** rewriting the file, so a marker stamped earlier in the
  session can still cover the row. That path is a documented limit, not closed
  in code.
- **cwd mismatch defers.** A stamp run from a subdirectory, a gate worktree or
  a nested agent worktree finds no `.claude/cw-context.json`, fails open, and
  the hook reads no marker.

**Operator recovery.** Requeue a parked row with `cw dev-queue requeue`. `cw
dev-queue approve` **refuses** it (`_not_at_approval_gate`,
`src/cw/dev_queue/approval.py`: the row has neither a scope-gated `last_result`
nor an approval-gate disposition) — including when the parked comment was
asking for plan approval, which is the common plan-stage case. The parked
session stays ACTIVE, so a requeue may hit `HookContextConflictError` while it
is still live; close it first with `cw spawn close --confirmed-dead`.

---

### 6d. Stop-hook sentinel-unroutable attention (#2458)

A worker that runs `cw result emit` stages its result on the session
(`last_result`, `last_result_source=emit_cli`) before its turn ends. Three
authorities can route that staged result to the dev-queue row:

- **The Stop hook.** `signal_stop` routes a staged emit_cli result on the next
  Stop, **including one whose `background_tasks` is still non-empty**. A
  lock-free peek at the session decides whether anything is staged. With
  nothing staged, the hook defers exactly as it always has (issue #151). With
  a staged result, the task is routed immediately, but the session's own
  completion (`SESSION_COMPLETED`, daemon stop) waits until the background work
  drains. The one exception is a `BlockedResult` that lands the row
  terminal-`FAILED` (#1273). That worker is provably leaked, so its daemon is
  stopped at once.
- **The idle sweep (backstop).** A live DAEMON session holding a staged
  emit_cli result is routed as `ROUTE_EMITTED_SENTINEL` once
  `sentinel_unrouted_check_seconds` (default 300 s, measured from session
  start) has passed. This catches the shape the hook cannot: an upstream
  `claude --bg` async-completion wakeup that is dropped entirely (#1889), so
  that no further Stop ever fires for the session. It holds off while the
  worktree's `agent_spawn_stamp` shows background work outstanding and was
  last refreshed by a deferring Stop within `fix_loop_await_deadline_minutes`
  (default 30), because that session is draining normally and stopping its
  daemon would kill the subagent (#151). The sweep audits the
  staged result rather than re-emitting it through the door. A #1031
  stage-mismatch refusal leaves the row and the live session untouched, and
  stamps the #1149 refusal marker (`paused_status=sentinel_stage_mismatch_refused`)
  in place of the staged result so the candidate is not re-offered; only the
  emit's `session.result_emitted` audit event (status and payload digest)
  remains of the staged result. Since #2513 that refusal first pages once
  (`session.needs_attention`, `paused_status=sentinel_stage_mismatch_live_session`,
  recovery `cw spawn close --requeue <id>`), as the stalled and phantom sweeps'
  refusals do; the marker is stamped only after the page landed, so a failed
  page is retried next tick. That audit trace is best-effort since #2465: if
the event inbox is unwritable the append is skipped and only the logged
`payload_digest` records the staged result.
- **A stranded routed session (#2524).** Once the Stop hook has routed a
  staged result while `background_tasks` was still non-empty, it merges a
  `sentinel_partial_route_consumed` marker into `last_result`. From then on
  neither the idle sweep (the result is no longer "staged and routable") nor
  the stalled sweep (which skips emit_cli results) completes the session; only
  a later Stop does. If that Stop never fires, the session stays ACTIVE,
  holding a ceiling slot and its worktree. Reconcile pages it **once**
  (`session.needs_attention`, `paused_status=routed_result_session_stranded`)
  when its worker is still in the roster, no RUNNING/BLOCKED_ON_USER/
  AWAITING_OPERATOR_SIGNOFF row is bound to it, its transcript sits in the
  30m/45m liveness bucket and no background work is still draining. Nothing
  closes it automatically, under any `reap_policy`. `cw doctor` reports it as
  `wedge/active-routed-result-stranded`; close it with
  `cw spawn close --confirmed-dead <id>` (this one session) or
  `cw doctor --reap` (every session of the class). Either way the already
  advanced row is left alone.
- **`cw spawn close`.** Closing a DAEMON session routes a staged result first.
  The #317 cancel runs only when nothing is staged, the staged dict does not
  reconstruct, or the route is refused.

**The `sentinel_unroutable` page.** `sentinel_unroutable` is a narrower net
than either routing authority. It fires when an emit_cli-staged, headless
Stop hook (with or without `background_tasks` outstanding) reaches its
resolution step and lands in none of the known bails, meaning the staged
result failed reconstruction **and** the transcript fallback found no
sentinel either. In that case `signal_stop` logs a WARNING (session id,
ticket id, `last_result_source`) and fires `session.needs_attention` with
`paused_status=sentinel_unroutable`. The `cw.result` warning just before it
names the pydantic field errors.

It is **not** fired for the other bails, each of which already has its own
signal:

- a #1031 stage-mismatch refusal (`sentinel.stage_mismatch`);
- a `BlockedResult` landing terminal (#1273, daemon stopped);
- the §6c abandoned-exit park (`stopped_without_sentinel`).

It is the dropped-wakeup and `background_tasks`-defer shapes that the Stop
hook's routing and the idle sweep now close between them. This page is
defense in depth for the rarer case of a Stop hook that *did* run but found
the staged result unroutable.

It mutates **only** the event stream. There is **no task-row mutation**,
unlike §6c's park. The row stays `RUNNING`, so a later Stop, the idle sweep,
or an operator can still route it. Nothing is reaped, and the row is not
concierge-eligible (there is no false park to requeue).

**Signal-only, not escalation-eligible.** `sentinel_unroutable` is the same
class as the liveness sweep's `session_unresponsive` page. It is not a
member of `cw.reconcile.escalation._ELIGIBLE_DISPOSITIONS` and must never be
added there: `_is_escalation_eligible` only considers a `BLOCKED_ON_USER`
row with an eligible disposition or an `AWAITING_OPERATOR_SIGNOFF`/`FAILED`
row, and this page leaves the row `RUNNING`, so the entry would be dead code.

**Not suppressed in the liveness sweep.** Unlike §6c, the liveness sweep's
`session_unresponsive` distress signal is **not** withheld for this state.
That suppression keys on a `BLOCKED_ON_USER` row carrying
`stopped_without_sentinel`, and this row stays `RUNNING`. A quiet session
holding an unroutable staged result can therefore page through both signals.

**Operator recovery.** Read the staged result with `cw session result
<session>`, and read the `cw.result` warning for the failing fields. The row
is still `RUNNING`, so `cw dev-queue requeue` does not accept it yet. For an
unreconstructable result, `cw spawn close` cancels the row as before; then
`cw dev-queue requeue --from-cancelled` re-runs the stage.

### 6e. The liveness dead-session page (#2153)

The liveness sweep's distress signal (`session_unresponsive` and its sibling
reasons in [`docs/events.md`](events.md)) pages a quiet session **once per
death**, not once an hour. The first time a roster-present (or unobservable,
#2417) session sits at the top staleness bucket with no sentinel and no
in-deadline subagent, it emits one `session.needs_attention` and one push, and
stamps an evidence key on the session. Every
`liveness_attention_renotify_interval_minutes` the sweep re-evaluates it; it
pages again only if the evidence changed: a different reason, a newer
content-bearing transcript record that did not revive the session, or a change
in the owned queue row's status. Trailing metadata records do not count.
Recovery below the top bucket clears the key, so the next death pages again.

**The wording is evidence, not a verdict.** The page says "evidence suggests
this session is dead (confirm before closing)", names the last record of the
session's own transcript (its type, timestamp and, for an `API Error`, a
redacted snippet) and how long the transcript has been flat, then hands over
the remedy:

```bash
# Only after you have confirmed the session is dead:
cw spawn close --confirmed-dead <session_id>
cw dev-queue requeue <ticket> -c <client> --from-cancelled
```

The same two steps fold into one command: `cw spawn close --confirmed-dead
--requeue <session_id>`. A session with no ticket gets only the close command.
`--confirmed-dead` is the operator's assertion; cw never makes it. **No timer
acts on the page**: nothing closes, requeues or reaps the session
automatically (ADR-0014).

**Where the page stays visible.** After the board's and the orchestrate
digest's 24-hour event window ends, two surfaces still show the condition,
each for a narrower set of sessions than the sweep pages:

- `cw dev-queue tasks` shows `dead_session_paged` in the ATTENTION column for a
  row whose own session is ACTIVE or IDLE, latched at the top bucket, with a
  stamped key. It covers ticket-owned sessions only. The cell is display only
  and can outlast the page's distress (for example a sentinel landing while
  the session stays at the top bucket).
- `cw doctor` (wedge class `wedge/active-daemon-stale-no-sentinel`) carries
  the same evidence and commands in its recipe, but lists only **ACTIVE**,
  roster-present sessions whose transcript it can locate. The sweep also
  pages IDLE sessions and unobservable (`session_age`) sessions, which doctor
  does not list.

IDLE sessions with no ticket row, unobservable sessions without one, and
ticket-less daemon sessions have no surface after the 24-hour window; the
event log still holds the page.

---

## 7. Cross-references

- [`docs/dispatch-runbook.md`](dispatch-runbook.md) — full end-to-end dispatch procedure.
- [`docs/headless-contract.md`](headless-contract.md) — `AUTO_DEV_RESULT` schema, status enum, `ReapReason` taxonomy, `queue.session_reaped` event.
- [`docs/events.md`](events.md) — `session.park_vetoed` and the full orchestrator event-bus reference.
- [`docs/events.md`](events.md) **Liveness dead-session page** — the §6e page's evidence key, suffix and payload keys (#2153).
- `src/cw/cli/_sentinels.py:_parse_sentinel_from_transcript` — transcript sentinel reader.
- `src/cw/cli/_sentinels.py:_sentinel_frame_after` — the §6c false-park guard (negative evidence only).
- `src/cw/cli/signal_park.py` — `cw signal-park`, the §6c park-marker writer.
- `src/cw/cli/stop_hook/command.py:_page_sentinel_unroutable` — the §6d page; `src/cw/cli/stop_hook/staged_emit.py:_peek_staged_emit_result` — the lock-free staged-result peek on the `background_tasks` path.
- `src/cw/reconcile/idle/_detect.py:_staged_emit_candidate` — the §6d idle-sweep backstop producer.
- `src/cw/cli/spawn.py:_route_staged_emit_result` — `cw spawn close`'s route-before-cancel.
- `src/cw/models/park_comment_marker.py` — the marker model and its reader.
- `src/cw/reconcile/_shared/_transcripts.py:_locate_session_transcript` — transcript path resolver.
- `src/cw/reconcile/_shared/_transcripts.py:_csid_from_transcript` — claude_session_id derivation.
