# Event Bus

Global orchestrator event bus for cw. Skills and the daemon post events here;
the daemon and other consumers read from it using cursor-based consumption.

## Storage

- **Inbox**: `~/.local/share/cw/events/inbox.jsonl` — append-only JSONL log
- **Cursors**: `~/.local/share/cw/events/cursors/<consumer>.json` — per-consumer read position
- **Lock**: `~/.local/share/cw/events/.inbox.lock` — `fcntl` exclusive lock prevents concurrent writes

`EVENTS_DIR` is already defined in `config.py` as `STATE_DIR / "events"`.
The `inbox.jsonl` file coexists with any other state in this directory without conflict.

**Not append-only-forever**: `inbox.jsonl` is unbounded by default, and
`cw event prune` (see below, #856) can truncate or rotate it — the file's
byte size is no longer guaranteed monotonic. `cw doctor` warns when the
inbox exceeds configurable size/line-count thresholds.

## Event Model

```python
class OrchestratorEvent(BaseModel):
    id: str                          # uuid4().hex[:16]
    type: OrchestratorEventType
    payload: dict[str, Any]          # event-type-specific fields
    correlation_id: str | None       # links related events
    created_at: datetime             # UTC, auto-set on creation
    consumed_at: datetime | None     # set by consumer when acknowledged
```

## Event Types

### `ticket.enqueued`

A new ticket has been added to the work queue for an orchestrator client.

```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "priority": 0
}
```

### `ticket.needs_sync`

**Emitter:** `dispatch_tick`
**Payload:** `{"ticket_id": "<str>", "client": "<str>", "lane": "<str>"}`
**Semantics:** Emitted once per PENDING task when the client's local
`<default_branch>` is behind `origin/<default_branch>`. The task stays
PENDING; the slot is skipped for this tick. Operator should run
`cw dev-queue refresh-all` to fast-forward and unblock dispatch.

```json
{
  "ticket_id": "CW-42",
  "client": "my-client",
  "lane": "default"
}
```

### `session.spawned`

A dev-queue worker session was spawned for a claimed ticket.

**Emitter:** the dispatch claim loop in `cw.dispatch`.

```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "session_id": "<str>",
  "lane": "<str>"
}
```

### `session.completed`

An orchestrator-managed session completed.

**Emitters:** the Stop-hook consumer (`cw signal-stop`, the canonical path),
the executor's synchronous completion path, and reconcile's salvage/routed
paths. Payload keys vary slightly by emitter; the Stop-hook shape is:

```json
{
  "session_id": "<str>",
  "session_name": "<str>",
  "client": "<str | null>",
  "ticket_id": "<str | null>",
  "claude_session_id": "<str | null>",
  "hook_event": "<str | null>",
  "crashed": false
}
```

Optional keys: `rescued: true` + `rescue_reason: "late_sentinel"` when a late
Stop-hook sentinel salvaged an idle-parked task (#918); `salvaged: true` +
`status: "<sentinel status>"` on reconcile's routed-sentinel backstop paths.
The dispatch loop consumes this event (consumer cursor `"dispatch"`) to
transition the matching `TicketTask` to COMPLETED.

### `pr.registered`

A pull request has been registered with the orchestrator for monitoring.

```json
{
  "pr": 42,
  "repo": "owner/repo",
  "branch": "feat/my-feature",
  "session_id": "<str>"
}
```

### `pr.ci_failed`

CI checks on a tracked PR transitioned from passing to failing.

**Emitter:** `apply_pr_state_observation` in `cw.pr_hydrate` (shared by both
the poll producer, `hydrate_pr_states`/`_persist_and_emit`, and the webhook
push producer, `observe_pushed_event` — GitHub #929/#930).
**Semantics:** Dedup'd against the task's persisted `pr_state.ci_ok` — fires
once per true->false transition, not on every still-failing observation.
`correlation_id` is the `ticket_id`.

```json
{
  "repo": "owner/repo",
  "pr_number": 42,
  "ticket_id": "CW-42",
  "client": "my-client",
  "failing_checks": ["check-name"]
}
```

### `pr.review_received`

A review was submitted on a tracked PR.

**Emitter:** `apply_pr_state_observation` in `cw.pr_hydrate` (poll + push,
same chokepoint as `pr.ci_failed` above).
**Semantics:** For `APPROVED`/`CHANGES_REQUESTED`, dedup'd against the
task's persisted `pr_state.review_decision` — fires once per value change.
`COMMENTED` reviews are a carve-out (#930): they are not a merge-gate signal,
so they never mutate `pr_state` and are NOT deduped — every `COMMENTED`
webhook delivery (including redelivered/duplicate ones) emits its own event.
`correlation_id` is the `ticket_id`.

```json
{
  "repo": "owner/repo",
  "pr_number": 42,
  "ticket_id": "CW-42",
  "client": "my-client",
  "review_decision": "APPROVED|CHANGES_REQUESTED|COMMENTED"
}
```

### `pr.mergeable`

A tracked PR entered a genuinely-mergeable `mergeStateStatus` (CI green,
approvals met) from outside that set.

**Emitter:** `apply_pr_state_observation` in `cw.pr_hydrate` (poll producer
only today — see "Push producer" below).
**Semantics:** Fires on ENTERING one of GitHub's mergeable statuses (`CLEAN`,
`UNSTABLE`, `HAS_HOOKS`) from outside the set, not on merely leaving a
blocking status into `UNKNOWN`/`DRAFT` (#929). `mergeStateStatus` is a
deliberate camelCase passthrough of the raw `gh` field name, unlike every
sibling payload key. `correlation_id` is the `ticket_id`.

```json
{
  "repo": "owner/repo",
  "pr_number": 42,
  "ticket_id": "CW-42",
  "client": "my-client",
  "mergeStateStatus": "CLEAN"
}
```

### `pr.merged`

A tracked PR was merged.

**Emitter:** `apply_pr_state_observation` in `cw.pr_hydrate` (poll + push).
**Semantics:** Fires on the first observation of a `MERGED` state (even if no
prior baseline exists, so a re-discovered merge still retires the ticket).
`correlation_id` is the `ticket_id`.

```json
{
  "repo": "owner/repo",
  "pr_number": 42,
  "ticket_id": "CW-42",
  "client": "my-client"
}
```

### Push producer: GitHub webhook -> `/pr-event` (#930)

The four `pr.*` events above are also fed by a **push** producer, not just
the serve-tick poll pass: a per-repo GitHub Actions workflow
(`.github/workflows/pr-events.yml`) posts mapped payloads to
`cw_pr_events_server`'s `POST /pr-event` endpoint via an operator-provisioned
relay tunnel (smee.io / cloudflared — see
[`dispatch-runbook.md` §10](dispatch-runbook.md#10-push-producer-webhook-relay-github-930)).
`cw.cw_pr_events_server.handle_post_pr_event` authenticates the request (HMAC
via `X-Cw-Signature` when `CW_PR_EVENTS_HMAC_SECRET` is set), then calls
`cw.pr_hydrate.observe_pushed_event`, which resolves the `(repo, pr_number)`
to a dev-queue task and routes it through the exact same
`apply_pr_state_observation` chokepoint the poll producer uses — so push and
poll share transition-dedup and both land in `dev_queue.json` (`pr_state`)
and `inbox.jsonl` (this same `pr.*` event stream). `pr.mergeable` has no
webhook trigger wired (no GitHub event carries `mergeStateStatus` directly)
— the poll producer covers it exclusively. Push is a latency optimization,
not a replacement: if the relay is down, the poll producer still covers all
four event types within `pr_hydration_interval_seconds`.

### `stage.entered`

The headless `/auto-dev` worker entered a new stage of its pipeline. Used
by `cw orchestrate status` to derive a per-session `last_stage` display.
Producer contract is in
[`headless-contract.md` §10](headless-contract.md#10-stage-event-taxonomy-producer-contract);
the invariant that `STAGE_ERRORED` does not redefine `last_stage` is
captured in [ADR-0004](adr/0004-stage-events-on-orchestrator-bus.md).

```json
{
  "session_id": "<str>",
  "ticket_id": "<str>",
  "stage": "s2_impl_started",
  "prev_stage": "s1_plan_reviewed",
  "started_at": "2026-05-23T13:01:42Z"
}
```

`stage` and `prev_stage` MUST match the closed enum in headless-contract
§10.2 (`s0_intake` … `done`).

### `stage.errored`

The headless `/auto-dev` worker hit a transient error inside a stage. The
worker may recover and continue. Visible in `cw event tail` and
`recent_events`; deliberately does NOT update `last_stage` (per ADR-0004).

```json
{
  "session_id": "<str>",
  "ticket_id": "<str>",
  "stage": "s2_impl_started",
  "error_kind": "agent_block",
  "started_at": "2026-05-23T13:04:11Z"
}
```

`error_kind` is an open enum — consumers MUST tolerate unknown values.

### `dispatch.tick`

**Emitter:** `dispatch_tick` in `cw.dispatch`
**Payload:**
```json
{
  "client": "<str>",
  "claimed": 0,
  "pending": 2,
  "running": 1,
  "cap": 3,
  "skip_reason": "availability_gate | ssh_key_gate | disk_pressure_gate | freshness_gate | usage_limited | host_capacity_gated | cap_full | lane_cap_blocked | attempt_cap_blocked | stale_pr_blocked | spawn_error | lane_circuit_paused | spawn_error_backoff | no_pending | none"
}
```
**Semantics:** Emitted once per client per tick. `claimed` is the number of
tasks newly spawned this tick. `pending` is the pre-claim count (read before
the claim loop). `running` is the count of RUNNING tasks at tick start.
`skip_reason` follows first-match precedence: `availability_gate` (fleet-wide
gh-availability preflight probe, RFC 0011 A5, checked before any per-client
gate so a real GitHub outage short-circuits every client before any pays the
freshness git-fetch cost) → `ssh_key_gate` (#927; per-client `ssh-add -l`
preflight — a session spawned without an unlocked SSH key cannot push and
would burn a slot on a guaranteed-failing session) → `disk_pressure_gate`
(client's worktree-base mount below `disk_pressure_min_free_gb` free,
checked before the freshness gate's `git pull`) → `freshness_gate` (local
branch behind origin) → `usage_limited` (API rate limit; backoff armed) →
`host_capacity_gated` (#1444; fleet-wide `host_session_budget` ceiling on
concurrently-running DAEMON sessions across the whole host, checked ahead of
the per-client cap so an operator can distinguish "this client's own cap is
full" from "the whole host is out of budget") → `cap_full` (running ≥ cap)
→ `lane_cap_blocked` (pending tasks exist but all lane slots occupied) →
`spawn_error` (exception during spawn) → `lane_circuit_paused` (per-lane
circuit breaker tripped after consecutive spawn errors, #875) →
`spawn_error_backoff` (pending tasks exist but all in exponential backoff
after spawn_error, next_eligible_at in the future) → `no_pending` (nothing
to claim) → `none` (at least one session spawned). Two values sit outside
this per-client precedence chain, emitted **per task** instead:
`attempt_cap_blocked` (payload carries `ticket_id`) when the attempt
ceiling parks a task, additionally carrying `attempt_ceiling` (int) — the
*resolved* ceiling that actually fired (#1751); read that field, not
`global_attempt_ceiling`, since the ceiling is lane-scoped and the row's
lane may have overridden the global value. `stale_pr_blocked` (#1862) when
the pre-dispatch open-PR gate parks a PLAN/IMPL-stage task whose branch
already carries an open, unmerged PR — payload carries `ticket_id`, no
`attempt_ceiling`.

Optional extra keys, grouped by which skip-reason path emits them: `lanes`
(per-lane breakdown) is present on the main claim-loop tick and every
per-client skip path (`availability_gate`, `ssh_key_gate`,
`disk_pressure_gate`, `freshness_gate`, `usage_limited`), but absent on the
two per-task ticks (`attempt_cap_blocked`, `stale_pr_blocked`).
`lane_occupants` (`dict[str, list[{"ticket_id": str, "status": str}]]`, a
top-level key — deliberately *not* nested inside `lanes`, since
`orchestrate.py`'s `_extract_lanes` hard-filters `lanes` values to
numerics and would silently strip a nested ticket-id string, #1243) and
`occupied` (int, total occupant count across lanes) are present on that
same set of ticks. `host_running` (int) and `host_budget` (int | null) are
present only on the main claim-loop tick — the one tick that can carry
`host_capacity_gated`. `disk_free_gb` / `disk_min_free_gb` (float) are
present only on `disk_pressure_gate` ticks. `freshness_detail`
(`non_main_head | main_behind_origin | main_dirty_checkout |
main_diverged_from_origin | main_detached_head`) plus `blocked_branch` are
present only on `freshness_gate` ticks.

`correlation_id` is `None` (per-client aggregate, not per-ticket).
Consumers MUST tolerate unknown `skip_reason` values.

### `gate.ssh_key_bypassed`

**Emitter:** `_emit_ssh_key_bypass` (`cw.dispatch.gating`, called from `cw.dispatch.tick`)
**Payload:**
```json
{
  "client": "<str>",
  "probe_result": false,
  "gate_enabled": false
}
```
**Semantics:** GitHub #1437. Emitted when `ssh_key_gate_enabled` is `false`
and the per-client `ssh-add -l` preflight probe (#927) reports the SSH
agent key unavailable — the operator has explicitly disabled the gate, so
the client dispatches anyway instead of being held PENDING, and this event
records that the skip was suppressed. `probe_result` is the raw probe
outcome (`false` means unavailable) and `gate_enabled` echoes the config
flag that suppressed the skip. Earlier sibling of
`gate.disk_pressure_bypassed` (#1887), which mirrors this bypass shape;
forwarded to the operator-attention channel by default (same as
`gate.auto_approved`), since an SSH-key-gate bypass is attention-worthy.

`correlation_id` is `None` (per-client, not per-ticket).

### `gate.disk_pressure_bypassed`

**Emitter:** `_emit_disk_pressure_bypass` (`cw.dispatch.gating`, called from `_apply_disk_pressure_gate`)
**Payload:**
```json
{
  "client": "<str>",
  "disk_free_gb": 1.2,
  "disk_min_free_gb": 5.0
}
```
**Semantics:** GitHub #1887 (split from #1858). Emitted when
`disk_pressure_gate_enabled` is `false` and the claim-time disk-pressure
probe (`cw.disk.check_disk_usage` on
`cw.worktree.resolve_worktree_base(client)`) reports free space below
`disk_pressure_min_free_gb` — the operator has explicitly disabled the
gate, so the client proceeds to claim this tick instead of being held
PENDING, and this event records that the skip was suppressed. Mirrors
`gate.ssh_key_bypassed` (#1437)'s bypass shape; forwarded to the
operator-attention channel by default (same as `gate.auto_approved`),
since a disk-pressure bypass is attention-worthy.

`correlation_id` is `None` (per-client, not per-ticket).

### `dispatch.loop_exited`

**Emitter:** the `cw dev-queue run` loop in `cw.dispatch` (a `finally` guard)
**Payload:**
```json
{
  "normal": true,
  "exception_type": "<str | null>"
}
```
**Semantics:** Fires whenever the dispatch loop exits, cleanly or not —
`normal` is `true` for a clean exit or Ctrl-C, `false` with
`exception_type` set otherwise. When the loop exits because the installed
cw package version drifted from the loaded one, the payload also carries
`reason: "version_drift"` plus `loaded_version`/`installed_version`.
Operator-relevant: if you see this without having stopped the loop yourself,
dispatch is down and pending tickets will not be claimed until it is
restarted.

### `dispatch.usage_limit_cleared`

**Emitter:** `run_dispatch_loop` in `cw.dispatch.loop` (via
`_handle_usage_limit_window_transition`/`_emit_usage_limit_cleared`)
**Payload:**
```json
{
  "clients_affected": ["<str>", "..."],
  "sessions_affected": 3,
  "detected_at": "<iso8601 | null>",
  "cleared_at": "<iso8601>"
}
```
**Semantics:** Fires exactly once, on the tick that observes the fleet-wide
usage-limit back-off window (`usage_limited_until`) transition from active
to lapsed — never per-client; `clients_affected` carries the full cohort in
a single event. `clients_affected` and `sessions_affected` are computed by
scanning `session.timed_out` events with `cause: "usage_limit_cutoff"`
recorded since `detected_at`. `detected_at` is the persisted arm timestamp
(the moment the window was opened, not derived from
`usage_limit_backoff_seconds`); it is `null` when that timestamp failed to
persist or predates this window's arming — the event still fires rather
than being dropped, but the cohort scan degrades to unbounded history in
that case. `cleared_at` is when the loop *noticed* the lapse, which can lag
the window's true expiry instant by up to `tick_interval_seconds` — the loop
is itself only tick-granular.

Two caveats:
- **`--once` mode never emits this event.** A single tick cannot observe a
  two-tick armed→cleared transition, and the detector is not invoked at all
  in `--once` mode.
- **In-process-local detector state.** The armed/cleared tracking lives in
  plain locals inside one `run_dispatch_loop()` call, not a persisted latch
  — this depends on the single-long-running-loop-per-fleet invariant (only
  one dispatch loop process running at a time). A second concurrent loop
  would each independently observe the same transition and double-emit.
  Tracked as a known gap by #1362 (confirmed still OPEN).

### `session.timed_out`

**Emitter:** `cw spawn close` when an operator closes a RUNNING session.
(Historically also the idle watchdog and the stalled wall-clock sweep —
those emitters were removed with the process-kill timeouts, ADR-0014; the
`cause`/`branch_state` variants below appear only in old logs.)
**Payload:**
```json
{
  "session_id": "<str>",
  "session_name": "<str>",
  "client": "<str>",
  "ticket_id": "<str | null>",
  "claude_session_id": "<str | null>",
  "elapsed_seconds": 1234.5,
  "last_assistant_message_excerpt": ""
}
```
**Semantics:** The session was stopped and its owning RUNNING task reverted
to PENDING for retry. Idle-watchdog emissions add a `cause` key
(`idle_stall_recovered` or `usage_limit_cutoff`); stalled-sweep emissions may
add `branch_state: "absent_no_merged_pr"` when the feature branch is gone
with no merged PR. This is a retry signal, not a park — parks emit
`session.needs_attention` instead.

### `session.stage_timed_out_retried` — historical (ADR-0014)

**Emitter:** none since the process-kill-timeout removal (was the stalled
sweep in `cw.reconcile.stalled`); documented for reading old logs.
**Payload:**
```json
{
  "ticket_id": "<str>",
  "session_id": "<str>",
  "stage": "harden | plan | impl | review | finalize",
  "client": "<str>",
  "elapsed_seconds": 1234.5,
  "attempts": 1
}
```
**Semantics:** Visibility-only companion to a stage-timeout revert (#724):
a stage blew its wall-clock budget and the ticket is being retried at the
same stage. Edge-triggered — fires only on the tick that first proposes the
revert, not on every re-detect (#782). Skipped for merged-PR and gh-blocked
tickets (those are not genuine timeouts). `correlation_id` is the
`ticket_id`.

### `session.reap_authorized`

**Emitter:** `cw doctor --reap` (and the automated reap consumer) in
`cw.doctor`
**Payload:**
```json
{
  "session_id": "<str>",
  "session_name": "<str>",
  "client": "<str>",
  "ticket_id": "<str | null>",
  "lane": "<str>",
  "authority": "<str>",
  "proposed_action": "<str>",
  "mutations": ["<str>", "..."]
}
```
**Semantics:** ADR-0006 audit companion to `session.reap_proposed`: records
that a proposed reap was explicitly authorized and acted on, so the
propose → authorize → act chain is fully traceable in the inbox. `mutations`
lists the destructive actions actually performed (e.g.
`blocked_task_reverted_to_pending`).

### `session.spawn_unregistered`

**Emitter:** `cw.spawn` roster-registration poll
**Payload:**
```json
{
  "surface_ref": "<str>",
  "ticket_id": "<str>",
  "reason": "unregistered_worker",
  "poll_timeout_secs": 30.0
}
```
**Semantics:** A spawned `claude --bg` worker never appeared in the daemon
roster within the poll timeout — the spawn is treated as failed (the
dispatch tick reports `spawn_error` and the task re-enters backoff).
`correlation_id` is the `ticket_id`.

### `wave.collision`

**Emitter:** `detect_wave_collisions` in `cw.collision` (run from the
dispatch loop)
**Payload:**
```json
{
  "ticket_ids": ["CW-41", "CW-42"],
  "files": ["src/cw/models.py"],
  "client": "my-client"
}
```
**Semantics:** Two RUNNING tasks for the same client have overlapping
changed-file sets in the current wave — their PRs are likely to conflict.
Emitted once per colliding pair per loop run (deduped in-memory). Purely
advisory: nothing is paused or reverted; the operator can serialize or
reorder the tickets.

### Operator-command events (`lane.*`, `ticket.*`)

CLI commands emit thin audit events; payloads carry the obvious fields:

| Event | Emitted by | Payload |
|---|---|---|
| `lane.created` | `cw lane add` | `{client, lane, max_parallel, priority}` |
| `lane.paused` | `cw lane pause`, or the per-lane circuit breaker after consecutive spawn errors (#875) | `{client, lane, source: "operator" \| "circuit_breaker"}`; circuit-breaker emissions add `consecutive_count` and `last_error` |
| `lane.resumed` | `cw lane resume` (also resets the circuit-breaker counter) | `{client, lane, source: "operator"}` |
| `ticket.enqueued` | `cw dev-queue add` | `{ticket_id, client, priority}` (see top of file) |
| `ticket.moved` | `cw dev-queue move` | `{ticket_id, client, from_lane, to_lane}` |
| `ticket.approved` | `cw dev-queue approve` | `{ticket_id, client, from_stage, to_stage}` |
| `ticket.requeued` | `cw dev-queue requeue`, `cw dev-queue drain --held` (RFC 0011 A4, #1161), and dispatch's automatic FINALIZE→IMPL regress path (#770) | `{ticket_id, client, from_stage, to_stage, reason, regressed}` |
| `ticket.unblocked` | `cw dev-queue unblock` | `{ticket_id, client}` |

See the "Known legacy gap" note under `task.deleted`: the `ticket.*` family
carries `correlation_id=None` — read `payload["ticket_id"]` to correlate.

### `focus.set`

**Emitter:** `focus_set` (`cw focus set`) in `cw.cli.focus`
**Payload:**
```json
{
  "session_id": "<str>",
  "client": "<str>",
  "lane": "<str | null>"
}
```
**Semantics:** GitHub #1644. Audit trail for `cw focus set <client>[/<lane>]`,
which points a Claude Code session at a client (and optionally a lane) for
`cw statusline render` to read. `session_id` is the resolved
`--session`/`$CLAUDE_CODE_SESSION_ID` value; `client`/`lane` are the
already-validated CLI values, never a re-read of the focus store the command
just wrote. `lane` is `null` when the operator targets a bare client with no
lane suffix. No `correlation_id` is passed (defaults to `null`) — focus is a
session-scoped operator pointer, not a ticket-scoped event. Not in
`_DEFAULT_OPERATOR_EVENT_TYPES`: a low-volume operator-command audit record,
not an attention signal.

### `focus.cleared`

**Emitter:** `focus_clear` (`cw focus clear`) in `cw.cli.focus`
**Payload:**
```json
{
  "session_id": "<str>",
  "client": "<str | null>",
  "lane": "<str | null>"
}
```
**Semantics:** GitHub #1644. Audit trail for `cw focus clear`, which drops a
session's focus entry. `client`/`lane` report what was cleared (captured via
`get_focus` before the delete) — both are `null` when the session had no
focus entry to begin with; the command is idempotent but still emits
unconditionally, mirroring `lane pause`/`resume`'s no-prior-state-check
convention. No `correlation_id` (same rationale as `focus.set`). Not in
`_DEFAULT_OPERATOR_EVENT_TYPES`.

### `session.phantom_reverted`

**Emitter:** `reconcile` in `cw.reconcile` (phantom sweep)
**Payload:**
```json
{
  "session_id": "<str>",
  "ticket_id": "<str>",
  "client": "<str>",
  "worktree_dirty": false,
  "worktree_path": "<str | null>",
  "queue_status": "pending",
  "provider_overload_detected": false
}
```
**Semantics:** Emitted for each DAEMON-origin session that is reaped as a
phantom (present in state but no longer backed by a live multiplexer surface)
and whose owning ticket is reverted to PENDING (clean worktree) or routed to
BLOCKED_ON_USER (dirty worktree, to preserve work for operator inspection).
`worktree_dirty` is `true` when `worktree_has_unsaved_work` reports uncommitted
changes in the session's worktree. `worktree_path` is the absolute path to the
worktree set on the session at time of reap (populated from `session.worktree_path`,
which is always set for DAEMON sessions; `session.branch` is always `null` for
DAEMON sessions and is NOT used for path resolution). `queue_status` is
`"pending"` when the ticket was re-queued for retry or `"blocked_on_user"` when
it was parked for operator inspection due to a dirty worktree. `provider_overload_detected`
is `true` when the session's transcript carries the provider-overload (API 529)
signature (#1923) — diagnostics-only, it carries no routing weight and never
overrides `reap_policy` or the ticket's `queue_status`. `correlation_id`
is the `ticket_id`. NOT emitted for USER-origin sessions (those are reaped
without task revert).

### `session.needs_attention`

**Emitter:** `revert_timed_out_tasks`, `revert_completed_silent_tasks`, and
the liveness sweep's distress check (`record_session_liveness_changes`) in
`cw.reconcile`; `apply_staged_decision`, `dispatch_tick` (via
`_record_client_freshness_block`) in `cw.dispatch`. (The former idle-watchdog
/ salvage / salvage-skip emitters were removed with the process-kill
timeouts, ADR-0014.)
**Payload:**
```json
{
  "session_id": "<str>",
  "session_name": "<str>",
  "client": "<str>",
  "ticket_id": "<str | null>",
  "claude_session_id": "<str | null>",
  "paused_status": "<str>",
  "breadcrumbs": "<str>",
  "crashed": false
}
```
**Semantics:** Emitted when a session requires human intervention — the
orchestrator cannot automatically retry or complete it. `paused_status` is an
open enum; consumers MUST tolerate unknown values. Known values:

- `"session_unresponsive"` — signal-only distress from the liveness sweep
  (ADR-0014): a roster-present DAEMON session crossed the top staleness
  bucket with no sentinel emitted and no pending subagent at the transcript
  tail. Edge-triggered per bucket crossing, fires a push notification, and
  mutates nothing — the session keeps running; the operator decides.
  `breadcrumbs` carries stale minutes, stage, and elapsed seconds.
- `"silently_idle"` — *historical (ADR-0014)*: the idle watchdog's park.
  No longer produced; may exist on old rows/logs.
- `"needs_salvage"` — *historical (ADR-0014)*: the git-state salvage LOW
  path's park. No longer produced; may exist on old rows/logs.
- `"dirty_worktree"` — A phantom-reaped, TIMED_OUT, or COMPLETED session's
  worktree has uncommitted changes. The owning task has been routed to
  BLOCKED_ON_USER instead of being re-dispatched to avoid clobbering in-flight
  work. `breadcrumbs` is the absolute path to the worktree. Operator should
  review, commit or discard the changes, then manually unblock the task.
- `"attempt_cap_blocked"` — the dispatch claim path refused to claim a
  PENDING row whose `unproductive_attempts` reached its attempt ceiling, and
  parked it BLOCKED_ON_USER before any spawn (#786/#1257). Session-less by
  construction: `session_id`/`session_name`/`breadcrumbs` are empty and
  `claude_session_id` is `null`, since no session was started this attempt.
  Carries one extra payload field beyond the canonical nine —
  `attempt_ceiling` (int), the *resolved* ceiling that fired (#1751). Read
  that, not `global_attempt_ceiling`: the row's lane may have overridden it,
  and a lane with `attempt_ceiling: false` never produces this park at all.
  Operator recovery is §7's "Attempt-cap reset" in
  `docs/dispatch-runbook.md`.
- `"plan_parked"` — A headless worker completed its plan stage with open
  ambiguities or unverified premises (`ambiguities_pending_resolution` or
  `premises_pending_verification` sentinel status). The task is BLOCKED_ON_USER.
  Operator should inspect the session result (`cw session result <id>`) and
  either resolve the ambiguities and re-dispatch, or close the ticket. See #923.
- `"freshness_gate_blocked"` — A client's consecutive freshness-gate-block
  latch (`ClientConcurrencyOverride.consecutive_freshness_blocks`, RFC 0007
  §W2) reached `freshness_block_attention_threshold`. Client-scoped, not
  session-scoped: `session_id`/`session_name` are empty strings, `client` is
  set, `ticket_id` is `null`. `breadcrumbs` carries the freshness_detail
  reason (e.g. `"main_behind_origin"`). Surfaces via `board.py`'s
  client-header badge only — invisible to the per-ticket row badge and to
  `cw orchestrate status` (no `ticket_id` to key off of).
- `"salvage_skip_escalated"` — *historical (ADR-0014)*: the salvage-skip
  latch escalation. No longer produced; may exist in old logs.
- `"blocked"` — fires from two sources sharing the literal: Rule 5's
  `blocked` status (a hard stage-execution blocker; `breadcrumbs` carries
  `blocker.reason`, e.g. `"plan_unreviewable"` per the #1097 incident) and
  Rule 6's unparseable/missing-sentinel fallback (`breadcrumbs` empty,
  `disposition="abandoned"`). See #1117.
- `"awaiting_operator_availability"` — Rule 5's `blocked` status carries a
  blocker reason in `OPERATOR_UNAVAILABLE_BLOCKER_REASONS`
  (`push_auth_failed`, `operator_unavailable`) — an operator/dependency
  unavailability, not a broken leg (RFC 0011 A1). Overrides the generic
  `"blocked"` paused_status for this park only; `breadcrumbs` still carries
  the specific `blocker.reason` verbatim. The task is BLOCKED_ON_USER.
  See #1155.
- `"merge_gate_blocked"` — Rule 5: the merge/CI gate rejected the PR
  (optionally `blocker.reason` in `breadcrumbs`, e.g.
  `"prior_pipeline_pr_open"` per issue #777; empty otherwise). See #1117.
- `"scope_exceeded"` — Rule 5: the session's changes exceeded the
  configured scope limit. `breadcrumbs` empty (validator forbids a
  `blocker` for this status). See #1117.
- `"forbidden_area"` — Rule 5: the session touched a forbidden-area file.
  `breadcrumbs` empty (validator forbids a `blocker` for this status).
  See #1117.
- `"merge_pending"` — Rule 3b: PR is open, awaiting CI/merge gate; not a
  failure. `breadcrumbs` empty; `pr_url` is preserved on the task
  separately. See #1117.
- `"approval_gate"` — Rule 1: a non-small-tier scope-gated approval status
  (`plan_pending_approval` or `review_pending_approval`) parked the task to
  BLOCKED_ON_USER. Deliberately distinct from `"plan_parked"`, which covers
  the unrelated v4 ambiguities/premises park. `breadcrumbs` empty. Operator
  should review the session's scope/plan and approve or redirect. See #1302.
- `"review_health_gate"` — a REVIEW-stage sentinel reported
  `health.recommendation == "EXIT_FOR_HUMAN_REVIEW"`: the review that just ran
  did not vouch for its own coverage (e.g. a reviewer document with
  `status="degraded"`). The task is BLOCKED_ON_USER with
  `disposition="review_health_gate"`, at its unchanged REVIEW stage.
  Deliberately REVIEW-scoped: `local_runner.synthesize_git_result` hardcodes
  the same recommendation on its IMPL success path as an honest "I am not a
  reviewer" default (#1580), which is not a degraded-review signal and must
  keep auto-advancing. `breadcrumbs` empty. Operator recovery is to re-run
  review — `cw dev-queue requeue` (or `cw dev-queue drain`, which selects this
  disposition); `cw dev-queue approve` deliberately fails closed here, because
  there is nothing shippable to authorize until review is re-run. See #1702.
  As of #1856, a Test-Reviewer-only `status="degraded"` document — the
  read-only-sandbox tax (Test Reviewer can never start pytest under codex
  review's read-only sandbox) — no longer triggers this park; see
  `_derive_health` in `src/cw/codex_review/_verdict.py`.
- `"codex_must_fix_mechanically_rejected"` — Rule 5: a `blocked` sentinel whose
  `blocker.reason` is `codex_must_fix_mechanically_rejected`. Review produced a
  MUST_FIX finding, but `review_findings`' validation dropped it before
  adjudication (its file/line anchor was invalid, or its evidence quote was
  absent from the diff), so it was never weighed on its merits — previously
  this fell through to a silently clean `stage_complete`. The task is
  BLOCKED_ON_USER with `disposition="codex_must_fix_mechanically_rejected"`,
  at its unchanged stage. Unlike `"review_health_gate"` above, `breadcrumbs` is
  **non-empty**: it carries the verbatim `blocker.reason`, since this park
  genuinely originates from a populated `blocker` dict. Rule 5's only
  reason-keyed disposition override — every other `blocker_reason` gets the
  generic `_hold_aware_disposition` stamp. Deliberately **not** a
  `HOLD_DISPOSITIONS` member and deliberately **not** fix-loop-eligible: a
  finding rejected because its anchor could not be trusted must never be handed
  to a fix agent. Operator recovery is to read the rejected finding on the
  posted review comment (rendered under "MUST_FIX — mechanically rejected") and
  re-run review — `cw dev-queue requeue`, or `cw dev-queue drain`, which selects
  this disposition. See #1714.
- `"finalize_regress_repeat"` — a companion signal (`src/cw/dispatch/
  regress_repeat.py`), fired ALONGSIDE whichever of the paused_status values
  above the ordinary park already emitted this pass -- never a replacement for
  it. A FINALIZE self-heal regress (#770) reverts a ticket to `Stage.IMPL`;
  if that IMPL leg produces no new commit, the round trip lands back at
  REVIEW with an unchanged branch head, and REVIEW's scope-gated gates
  re-evaluate from a blank slate, re-parking the ticket with an identical
  disposition -- burning an attempt each cycle with no operator-visible
  signal that this is a *repeat*, not a fresh park (the #1644/#1702/#1710
  incidents). This event is that signal. `breadcrumbs` is a composite
  diagnostic string, not a verbatim `blocker.reason`:
  `"attempts=<int> branch_head=<repr> pr_url=<repr> disposition=<repr>"`.
  Fires only when this pass genuinely re-parked the task (`task.status` is
  `BLOCKED_ON_USER` or `AWAITING_OPERATOR_SIGNOFF`) with the branch head
  unchanged since the regress -- never when the round trip resolved (the
  task advanced past REVIEW) or when a real commit landed. See #1717.

`correlation_id` is the `ticket_id` when resolvable, `null` otherwise.
A push notification is fired for most emissions (via `fire_push_notification`)
— **except** `"freshness_gate_blocked"` and `"salvage_skip_escalated"`, which
deliberately do not push. This mirrors the existing `gh_check_blocked`
paused_status (verified: its `_emit_phantom_terminal_events` call site,
`cw.reconcile.phantom._events`, does not call `fire_push_notification`
either).

**Operator-channel forward may be buffered (RFC 0011 A6, #1162):** the event
itself is always recorded exactly as above, on every emission — the
recording behavior on this page is unchanged. Its forward onto the
`cw-operator` SSE channel (`cw.cw_operator_events`), however, is buffered
into a single digest push instead of forwarded immediately when the event
resolves to a ticket currently parked in a hold-class disposition
(`awaiting_operator`/`finalize_gate_held` — distinct from the
`"awaiting_operator_availability"` `paused_status` value above, a different
namespace). Every other `paused_status` still forwards immediately,
unbatched. See `docs/operator-channel.md`'s "Digest coalescing" section for
the full buffer/window/flush contract.

### `session.salvage_skipped` — historical (ADR-0014)

**Emitter:** none since the process-kill-timeout removal (was the stalled
sweep's park-marker skip path); documented for reading old logs.
**Payload:**
```json
{
  "session_id": "<str>",
  "ticket_id": "<str | null>",
  "reason": "park_marker_blocks_salvage",
  "paused_status": "silently_idle"
}
```
**Semantics:** Emitted when a stalled headless session is skipped during the
salvage pass because it carries a park marker (`last_result.paused_status ==
"silently_idle"`). The session is intentionally parked BLOCKED_ON_USER and
must not be auto-retried; the operator must manually clear or close it.
`reason` is an open enum — consumers MUST tolerate unknown values.
`correlation_id` is the `ticket_id` when resolvable, `null` otherwise.

### `session.reap_proposed`

**Emitter:** `_emit_reap_proposed` in `cw.reconcile`
**Payload v3:**
```json
{
  "session_id": "<str>",
  "session_name": "<str>",
  "client": "<str>",
  "ticket_id": "<str | null>",
  "lane": "<str>",
  "proposed_action": "revert_task | crash_complete | park_blocked_on_user",
  "reason": "<ReapReason value | null>",
  "evidence": {
    "elapsed_seconds": "<float>",
    "in_roster": "<bool>",
    "transcript_age_seconds": "<float | null>",
    "transcript_mtime_age_seconds": "<float | null>"
  }
}
```
**Semantics:** Emitted by `_emit_reap_proposed` after each detect phase in
`_reconcile_locked`, before the corresponding act phase. Satisfies ADR-0006
invariant 3 (propose before act). Only emitted for `REVERT_TASK`,
`CRASH_COMPLETE`, and `PARK_BLOCKED_ON_USER` proposed actions; counter
increments, salvage completions, and skip-parked candidates do not produce
this event.

`evidence.transcript_age_seconds` is the content-aware staleness
`_liveness_veto_candidate` evaluated (last content-bearing `user`/`assistant`
entry, `_transcript_age_seconds`) — not raw file mtime.
`evidence.transcript_mtime_age_seconds` is the raw `stat().st_mtime` age,
provided separately for diagnostics; the two can diverge by orders of
magnitude when a trailing metadata-only record (`ai-title`/`mode`/
`queue-operation`/etc.) lands after the last real turn (#1427).

Dedup: `session.reap_proposed_at` is stamped on the session object at
emission time. Subsequent reconcile ticks skip sessions already stamped,
preventing duplicate events across ticks for the same session.

`correlation_id` is the `ticket_id` when resolvable, else the `session_id`.

`lane` is the owning task's lane name, or `"default"` for candidates not
associated with a task (phantom sessions without a matching queue entry).

**Consumer note:** This event is written to the orchestrator event inbox
(`events/inbox.jsonl`) as a state-snapshot delta. It does NOT appear in the
channel-server queue-events channel; the queue-events channel only carries
actionable queue mutations.

### `task.transition`

A `TicketTask` changed status. This is the push-channel signal the orchestrator
previously hand-polled `dev_queue.json` for (RFC 0008 W1, closes #978).

**Emitter:** `transition_task_status` in `cw.dev_queue` — the single
status-authority function. Because every status write routes through it, this
event fires for **all** mutation paths (dispatch claim/complete, approve,
requeue, cancel, reconcile revert/park, etc.), not just operator commands.
**Semantics:** Emitted on every *real* status change — the emit is suppressed
when `new_status == old_status` (a re-assert of the same status stays silent).
`old_status`/`new_status` are `QueueItemStatus` values. `disposition`/`pr_url`/
`blocked_reason` reflect the task state *after* the transition (stamped on
terminal moves, cleared on PENDING/CANCELLED). `blocked_reason` (GitHub #1511)
is the `blocker.reason` off a well-formed blocked/merge_gate_blocked
AutoDevResult, `None` when the task isn't blocked or was blocked with no
blocker reason. `session_id` is the value on the task at emit time (some
callers clear it immediately *after* the transition, so it is typically still
populated here). `correlation_id` is the `ticket_id`.

```json
{
  "ticket_id": "CW-42",
  "client": "my-client",
  "lane": "default",
  "stage": "finalize",
  "old_status": "running",
  "new_status": "completed",
  "disposition": "shipped",
  "session_id": "ab12cd34",
  "pr_url": "https://github.com/owner/repo/pull/42",
  "blocked_reason": null
}
```

### `task.stage_changed`

A `TicketTask` moved to a different pipeline stage (RFC 0008 W1, closes #978).

**Emitter:** `_emit_stage_change` in `cw.dev_queue` — a single shared
chokepoint called from `_advance_task_pointer` (`direction="advance"`),
`_stage_regress` (`direction="regress"`), and `_apply_requeue_stage`'s
forward/same-stage tail (`direction="advance"`). All three stage-pointer
mutation sites route through this one helper, so exactly one event fires per
real stage move.
**Semantics:** Guarded on `old_stage != new_stage` — a same-stage requeue
(e.g. `requeue --stage <current>`) stays silent. `direction` is a **closed**
enum: `"advance"` or `"regress"`, matching the producing function names.
`correlation_id` is the `ticket_id`.

**Ordering (UNSPECIFIED):** A stage advance/regress also transitions the task's
status to PENDING, so a single mutation emits **both** a `task.stage_changed`
and a `task.transition`. Their relative emission order within that mutation is
**unspecified** — consumers should rely on event *presence* plus
`correlation_id` (the shared `ticket_id`) to correlate them, never on ordering.
No order is pinned for future W3 consumers.

```json
{
  "ticket_id": "CW-42",
  "client": "my-client",
  "old_stage": "plan",
  "new_stage": "impl",
  "direction": "advance"
}
```

### `task.deleted`

A `TicketTask` row was removed from the dev queue by an operator (RFC 0008 W1,
closes #978 — a row deleted mid-run previously produced no event).

**Emitter:** `remove_ticket` (`reason="operator_remove"`) and `clear_tickets`
(`reason="operator_clear"`) in `cw.dev_queue`.
**Semantics:** One event per removed **row**, not per API call — a
`remove --all` or a `clear` that removes N tasks emits N events, each carrying
that row's own `ticket_id`/`stage`/`status_at_deletion`. `status_at_deletion`
is the `QueueItemStatus` the task held when removed. `reason` is an **open**
enum — consumers MUST tolerate unknown values (the two values above are the
only ones emitted today). `correlation_id` is the `ticket_id`.

```json
{
  "ticket_id": "CW-42",
  "client": "my-client",
  "stage": "review",
  "status_at_deletion": "blocked_on_user",
  "reason": "operator_remove"
}
```

**Known legacy gap — the `ticket.*` CLI family:** The operator-command events
`ticket.enqueued`, `ticket.moved`, `ticket.approved`, `ticket.requeued`, and
`ticket.unblocked` are emitted from the CLI layer with `correlation_id=None`
(the `ticket_id` lives only in their payloads). The three `task.*` producers
above deliberately set `correlation_id=ticket_id`; the older `ticket.*` family
was **not** retrofitted in this change to avoid touching unrelated emit sites.
Consumers that need to correlate a `ticket.*` event to a ticket must read
`payload["ticket_id"]`, not `correlation_id`.

### `session.liveness_changed`

**Emitter:** `record_session_liveness_changes` (`cw.reconcile.liveness`)
**Payload:**
```json
{
  "session_id": "<str>",
  "ticket_id": "<str | null>",
  "client": "<str | null>",
  "stage": "harden | plan | impl | review | finalize",
  "old_bucket": "live | stale_15m | stale_30m | stale_45m",
  "new_bucket": "live | stale_15m | stale_30m | stale_45m",
  "stale_minutes": "<float>"
}
```
**Semantics:** Emitted whenever a live DAEMON session's
`Session.liveness_bucket` crosses to a new value — an edge-detect latch (no
per-observation counter, unlike `idle_observation_count`): the sweep runs
every reconcile tick but only emits, and only mutates
`Session.liveness_bucket`, when the newly-classified bucket differs from
what is already persisted. Pure observation: this sweep never dispositions
a session or mutates the dev queue.

Gating mirrors the idle-watchdog sweep's 3-condition gate (DAEMON origin +
`_LIVE_STATUSES` + `surface_ref` present in the daemon's live roster) but
deliberately omits its `_has_terminal_sentinel` check — a session that
already emitted a sentinel this tick can still cross a staleness bucket
before its task routes. A session whose transcript cannot be located is
skipped for the tick (fail-open; no bucket assigned without positive
staleness evidence).

`stage` is resolved via the owning `TicketTask.stage` (looked up through
`task_by_ticket`), **not** `Session.stage` (RFC 0005 A1, dormant). `stage`
falls back to `DEFAULT_STAGE` when no owning task is found.

Classification uses floor-suppression
(`OrchestratorConfig.liveness_buckets_minutes`, default `[15, 30, 45]`
minutes, and the per-stage `liveness_first_bucket_by_stage` override): a
stage's effective floor is the entry point below which a session is always
`live`, regardless of the global thresholds. Per-stage floors may raise the
entry point above a global threshold, in which case that threshold's rung
is entirely swallowed and simply never emitted for that stage — labels
always keep their global-threshold identity (`stale_15m`/`stale_30m`/
`stale_45m` never get renamed or reassigned to a different minute value);
only the entry point moves. For example, an IMPL session with a 35-minute
floor (default global stale_30m threshold is 30) ascends straight from
`live` (<35m) to `stale_15m` (>=35m, <45m) — it never emits `stale_30m`,
since 30 < 35 is unreachable once the floor has already been crossed.
`stale_45m` needs no such guard: it is always the top rung, so crossing it
is correct regardless of where the floor sits.

The IMPL 35-minute floor (and the 15/30/45-minute global ladder) are derived
from empirical stage-timing baselines (wiki `cw-stage-timing-baselines-2026-07-05`,
n=739 legs): p95 intra-session gap ≤1m in every stage; IMPL p99 gap 31m vs
REVIEW p95 9m; real session deaths cluster ≥60m. The IMPL floor sits above its
p99 gap so normal idling doesn't cross into `stale_15m`.

`stale_minutes` mirrors the existing `elapsed_seconds` convention (float,
not integer) used elsewhere in this file, derived from
`_transcript_age_seconds` divided by 60.

`correlation_id` is the `ticket_id` when resolvable, `null` otherwise.

### `guard.busy_wait_blocked`

**Emitter:** the `cw guard-busy-wait` PreToolUse hook subprocess
(`cw.cli.guard_busy_wait`), running inside the dispatched worker — the same
emitter class as `cw signal-stop`, which already records `session.completed`
from a hook subprocess.
**Payload:**
```json
{
  "client": "<str | null>",
  "lane": "<str | null>",
  "reason": "bare_noop | bare_sleep | repeat_threshold",
  "command_hash": "<str>",
  "repeat_threshold": "<int | null>",
  "window_seconds": "<int | null>"
}
```
**Semantics:** Emitted every time the guard blocks a Bash tool call (exit 2).
`reason` names which rule tripped: `bare_noop` for a bare `true`/`:`,
`bare_sleep` for a bare `sleep N` with no follow-on work, and
`repeat_threshold` for the same command repeated past the configured count
inside the rolling window. `repeat_threshold`/`window_seconds` carry the
resolved values that tripped, and are `null` for the two stateless reasons.

`command_hash` is a truncated SHA-256 of the whitespace-normalized command,
**not** the command text — shell commands routinely embed secrets inline, and
neither the guard nor this record needs anything but equality. Two events
carrying the same hash blocked the same command.

The block itself is the enforcement; this event is the observability half. A
block that only reached the worker's own stderr would leave a false positive
indistinguishable from a call the worker never made, so the record is emitted
best-effort in its own isolated `try`/`except`: a failure to write it never
suppresses the block. Nothing consumes this event — it exists for the operator
and for `cw event tail`.

`correlation_id` is the worker's cw `session_id` from its
`.claude/cw-context.json`, or `null` when the context is unreadable.

### `concierge.recovered`

**Emitter:** `run_concierge_recoveries` (`cw.reconcile.concierge`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "recipe": "false_park_requeue | park_marker_poison_clear | cancelled_row_restore",
  "evidence": "<dict>"
}
```
**Semantics:** RFC 0008 capstone (#1015). Emitted **before** the corresponding
mutation for every recovery the mechanical recovery reactor performs — the
event is durably recorded even if the subsequent task/session write fails,
so the decision trace survives a partial write. Audit-trail only: **not**
forwarded to the operator-attention channel by default (see
`operator.escalation` below, and Q3 in the design notes) — an operator does
not need paging for a mechanical, non-destructive recovery.

Gated entirely behind `OrchestratorConfig.concierge_enabled` (default
`false`) and, per-recipe, `OrchestratorConfig.concierge_recoveries`. See
[`config/CONFIG_REFERENCE.md`](../config/CONFIG_REFERENCE.md) and
[`docs/dispatch-runbook.md`](dispatch-runbook.md)'s "Concierge & Watchdog"
section for the 3 recipes' preconditions.

`evidence` is recipe-specific (e.g. `disposition`/`attempts`/`session_id` for
`false_park_requeue`; `paused_status`/`consecutive_salvage_skips`/
`session_id` for `park_marker_poison_clear`; `worktree_path`/`default_branch`
for `cancelled_row_restore`).

`correlation_id` is the `ticket_id`.

### `concierge.recovery_backoff_armed`

**Emitter:** `_act_on_false_park_candidates` (`cw.reconcile.concierge`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "recovery_count": "<int>",
  "next_eligible_at": "<ISO 8601 timestamp>",
  "session_id": "<str | null>"
}
```
**Semantics:** GitHub #1030. Emitted from recipe 1 (`false_park_requeue`)
when the candidate's session shows the dead-on-arrival signature — its
transcript's last write lands within seconds of spawn (active lifespan
< 120s), meaning the *previous* mechanical recovery churned against the
account session limit rather than helping. **The PENDING requeue always
proceeds** on this detect — nothing about the current cycle's action is
suppressed or vetoed (contrast `session.park_vetoed` above, which
accompanies zero mutation). This event only records that
`TicketTask.false_park_recovery_count` and
`false_park_recovery_next_eligible_at` were stamped, deferring the *next*
false-park detection cycle for this ticket by an exponentially increasing
window (5 minutes initially, doubling up to a 1-hour cap). Once that window
elapses, the recipe's detect phase considers the row again as normal.

Emitted before the unconditional `concierge.recovered` event, in the same
act-phase pass, for the same candidate.

Missing evidence (no session record for the ticket, or an unlocatable
transcript) never arms this backoff — only positive evidence of an instant
death does; a row with missing evidence keeps churning under the #1015
escalation latch's existing backstop instead.

`correlation_id` is the `ticket_id`.

### `concierge.hook_context_conflict_refused`

**Emitter:** `_act_on_false_park_candidates` (`cw.reconcile.concierge`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "recipe": "false_park_requeue",
  "session_id": "<str | null>"
}
```
**Semantics:** GitHub #1674. Emitted from recipe 1 (`false_park_requeue`)
when the row's currently-resolved session is the exact session already
recorded on `TicketTask.hook_context_conflict_session_id` — the session whose
still-live `.claude/cw-context.json` made the last spawn attempt raise
`HookContextConflictError` — and that session's status is still non-terminal.
**The PENDING requeue is skipped entirely** on this detect: unlike
`concierge.recovery_backoff_armed` above (which defers only the *next* cycle
and still requeues now), respawning here cannot succeed until the session is
closed, so requeuing would burn another `attempts` increment for nothing. No
task or session field is mutated — the row keeps its disposition and stays
escalation-eligible exactly like a ceiling-refused row.

Audit-only: **not** forwarded to the operator-attention channel, matching its
`concierge.*` siblings. Deliberately **not** latched either (contrast
`operator.escalation`'s one-shot `escalation_fired_at`) — it re-fires on
every reconcile pass for as long as the row stays parked against the same
conflicting session, which costs one event line and keeps the evidence
current.

Clears when the conflicting session goes terminal — `cw spawn close
--confirmed-dead <id>` flips its status without changing its id, so the
refusal predicate goes False on the next cycle — or when a newer session
supersedes it by id. Any successful spawn also wipes the recorded id.

`correlation_id` is the `ticket_id`.

### `operator.escalation`

**Emitter:** `run_escalation_sweep` (`cw.reconcile.escalation`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "status": "blocked_on_user | awaiting_operator_signoff | failed",
  "disposition": "<str | null>",
  "lane": "<str>",
  "stage": "harden | plan | impl | review | finalize",
  "parked_at": "<ISO 8601 timestamp>",
  "elapsed_minutes": "<float>"
}
```
**Semantics:** RFC 0008 capstone (#1015). Fires exactly once per parked
episode: a task entering the escalation-eligible set gets
`TicketTask.escalation_parked_at` stamped (no event yet); once
`now - escalation_parked_at >= 45` minutes (`ESCALATION_PARK_MINUTES`, a flat
threshold — NOT per-stage, unlike the liveness sweep above), this event
fires and `escalation_fired_at` is stamped so it never re-fires for the same
episode. Both fields clear together via the `transition_task_status` seam
(`cw.dev_queue`) when the row leaves the eligible set, so a fresh parked
episode always starts the clock over.

The escalation-eligible set is a two-branch formula, not a single
disposition list: a `BLOCKED_ON_USER` row is eligible only for disposition
∈ {`ambiguities_pending_resolution`, `plan_pending_approval`,
`review_pending_approval`, `stalled_retry_cap_parked`}, while
`AWAITING_OPERATOR_SIGNOFF`/`FAILED` rows are eligible regardless of
disposition. `premises_pending_verification` is deliberately excluded.

Runs **unconditionally** every reconcile tick (not gated by
`concierge_enabled`) and is also directly callable from `cw watchdog tick`
(see `docs/dispatch-runbook.md`) — it needs no `sessions_lock`, so it fires
even when the dispatch loop itself is down.

Added to the operator-channel's default forward set (unlike
`concierge.recovered` above) — this IS the operator-facing signal.

`correlation_id` is the `ticket_id`.

### `sentinel.stage_mismatch`

**Emitter:** `_route_staged_decision` (`cw.dispatch`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "session_id": "<str | null>",
  "expected_stage": "harden | plan | impl | review | finalize",
  "sentinel_stage_reached": "<str>"
}
```
**Semantics:** GitHub #1019 (the #986 incident: a late/replayed sentinel from a
previous leg was routed against whatever stage the row currently held).
`_route_staged_decision` is the single advance authority shared by the
consume path (`apply_staged_decision`) and reconcile's emitted-sentinel
router (`_apply_sentinel_to_task`, including the #918 late-sentinel rescue
arm) — the guard runs once there rather than being duplicated in either
caller. `expected_stage` is `task.stage` at the moment of the check;
`sentinel_stage_reached` is the sentinel's raw `stage_reached` value, mapped
against `expected_stage` via `_STAGE_REACHED_TO_STAGE`. A `Stage.HARDEN` task
always mismatches by construction — HARDEN has no legitimate
`stage_reached` counterpart (RFC 0005 A1, dormant stage).

A missing or `None` `stage_reached` (e.g. a `BlockedResult`-derived payload,
which has no such field) bypasses the guard entirely and never fires this
event — Rule 1-6 routing proceeds unchanged.

On mismatch this is a true no-op: no status transition, and callers that
gate on `_route_staged_decision`'s `False` return skip `save_dev_queue`
entirely. The row stays in whatever status it holds (`RUNNING`,
`BLOCKED_ON_USER`, or `AWAITING_OPERATOR_SIGNOFF`) and remains routable by
the next legitimate sentinel or operator action.

`correlation_id` is the `ticket_id`.

### `dispatch.scope_routing_decision`

**Emitter:** `_route_scope_gated_approval`, `_route_stage_success`,
`_walk_stage_pointer_forward` (`cw.dispatch.routing`); `_approve_ticket_locked`
(`cw.dev_queue.approval`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "scope_hint": "<str | null>",
  "sentinel_tier": "<str | null>",
  "resolved_tier": "<str | null>",
  "rule": "Rule 1 | Rule 3 | stage_walk | gate_release",
  "disposition": "<str | null>"
}
```
**Semantics:** GitHub #1617. An operator/queue-set `scope_hint` of `"large"`
is meant to force an approval gate at the REVIEW->FINALIZE boundary
regardless of which sentinel status a worker emits, but nothing recorded
*why* a gate did or did not fire -- diagnosing a historical bypass required a
forensic sweep across raw dev-queue state. This event closes that gap:
emitted at every scope-gate-relevant routing decision, capturing the
sentinel's own self-reported `scope.tier`, `task.scope_hint`, the tier
`_resolve_scope_tier` actually resolved (escalate-only: either input being
`"large"` wins), which of the four call sites made the decision, and the
resulting disposition.

`rule` identifies the call site: `"Rule 1"` (`_route_scope_gated_approval`,
the scope-gated-approval statuses' pre-existing reference implementation),
`"Rule 3"` (`_route_stage_success`, the `stage_complete`/`shipped` bypass this
ticket closes), `"stage_walk"` (`_walk_stage_pointer_forward`'s REVIEW rung,
the second bypass this ticket closes -- a sentinel whose mapped stage lands
past REVIEW in one hop, e.g. the Checkpoint-3a-headless-auto-continue shape),
or `"gate_release"` (`_approve_ticket_locked`, `cw dev-queue approve` -- see
below).

For the three `cw.dispatch.routing` sites, `disposition` is `task.disposition`
read immediately after the site's mutation -- so it is the literal
sentinel-status-derived value on Rule 1's approval-gate park (e.g.
`"review_pending_approval"`), `"approval_gate"` (the `_APPROVAL_GATE_REASON`
constant) on Rule 3/stage_walk's new scope_hint-gate park,
`"finalize_gate_held"`/`"signoff_gate"` on the two pre-existing REVIEW gates,
or `null` on an ordinary unparked advance (a non-terminal
`transition_task_status` call clears disposition). For the `"gate_release"`
site, `disposition` is instead a literal naming which of
`_approve_ticket_locked`'s four branches fired --
`"finalize_held"` | `"awaiting_signoff"` | `"plan_requeued"` | `"advanced"` --
because that function's force-hold branch performs **no mutation at all**
(the row stays parked exactly as it is), so `task.disposition` would not
reflect it there.

`_approve_ticket_locked` is deliberately **excluded** from the scope_hint
park-decision gate itself (D4): it is a gate-release site, and applying the
gate there would re-park the exact ticket the operator is releasing. It is
still covered by this audit event, sourcing `sentinel_tier`/`resolved_tier`
from the owning session's `last_result` (that site has no `last_result`
parameter of its own, unlike the three `routing.py` sites).

Deliberately **not** added to `_DEFAULT_OPERATOR_EVENT_TYPES`
(`orchestrator_config.py`) -- this is an audit/diagnostic trail, not an
operator alert, and it fires on effectively every stage transition for every
ticket (Rule 1 and Rule 3 both emit unconditionally on every call, not only
when a gate fires) -- far higher volume than any currently-forwarded member.

`correlation_id` is the `ticket_id`.

### `requeue.review_delivery_degraded`

**Emitter:** `requeue_ticket` in `cw.dev_queue.requeue` (deliverability
resolved by `_review_reentry_deliverable`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "reason": "<str>",
  "backend": "<str>",
  "tracker": "<str | null>"
}
```
**Semantics:** GitHub #1730. Emitted when a requeue lands the ticket at
`Stage.REVIEW` — genuinely a review-stage re-entry of a previously-parked
ticket, the moment an operator's tracker send-back comment is supposed to
reach the reviewer — but the resolved REVIEW-stage executor backend cannot
deliver operator tracker comments: `codex` paired with any tracker other
than `github-issues`, or a backend with no comment-delivery path at all
(`claude-native` always delivers). Namespaced by its owning module
(`dev_queue/requeue.py`), same convention as `dispatch.scope_routing_decision`.
This DEGRADES rather than blocks — `requeue_ticket` proceeds with the
requeue regardless (`requeue.py`'s documented asymmetry: impl hard-exits on
a missing plan, review/finalize degrade; #1730/#1717 comment 6 rejected a
hard-fail guard here) — making this event the *only* signal that the
operator's send-back never reached the reviewer. `backend`/`tracker` are
always populated (even when deliverable, since the caller resolves them
unconditionally) so they thread verbatim into this payload without a second,
possibly-drifting resolution call. `correlation_id` is the `ticket_id`.
Unlike `dispatch.scope_routing_decision`, this **is** in
`_DEFAULT_OPERATOR_EVENT_TYPES` — an automated pipeline action proceeding
with no operator confirmation of delivery is attention-worthy, and it has no
companion "delivery succeeded" event to pair with (self-contained).

### `session.park_vetoed` — historical (ADR-0014)

**Emitter:** none since the process-kill-timeout removal (was
`_act_on_stalled_candidates` in `cw.reconcile.stalled` — the parks this
vetoed no longer exist); documented for reading old logs.
**Payload:**
```json
{
  "ticket_id": "<str | null>",
  "client": "<str | null>",
  "session_id": "<str>",
  "stage": "harden | plan | impl | review | finalize",
  "reason": "wall_clock_budget | stalled_retry_cap_parked",
  "stale_minutes": "<float>",
  "consecutive_vetoes": "<int>"
}
```
**Semantics:** GitHub #976, #1277, #1445. Emitted instead of the normal
wall-clock-budget REVERT_TASK/park when the session's freshly-classified
transcript-staleness liveness bucket (`_classify_liveness_bucket`, same
per-stage-floor ladder as `session.liveness_changed`) is still `live` at the
moment the pending park would fire — the session is demonstrably still making
progress, so the park is suppressed. `reason` names the park that was vetoed:
`wall_clock_budget` (the ordinary wall-clock revert) or
`stalled_retry_cap_parked` (the retry-cap park; the veto applies to **both**
sites since #1277). The task stays `RUNNING` and the session stays
`ACTIVE`/`IDLE` — but the session's `consecutive_park_vetoes` latch **is**
incremented (`consecutive_vetoes` in the payload is the post-increment value);
that is the only state this event mutates.

The veto is **bounded** (#1445): it is granted only while
`consecutive_park_vetoes < OrchestratorConfig.park_veto_cap` (default 2). Once
the count reaches the cap the veto stops firing and the pending park proceeds,
and — at parity across both cap-fire sites — an immediate `session.needs_attention`
is emitted (the retry-cap park via its own emission with
`paused_status=stalled_retry_cap_parked`; the wall-clock-budget SIGNAL_ONLY
reroute via a dedicated escalation loop with `paused_status=wall_clock_budget`,
non-destructive: no daemon-stop / worktree removal). The counter resets for free
per pipeline episode, since each episode constructs a brand-new `Session`.

`correlation_id` is the `ticket_id`.

### `session.sentinel_stage_mismatch_vetoed`

**Emitter:** `_act_on_phantom_candidates` in `cw.reconcile.phantom`
**Payload:**
```json
{
  "ticket_id": "<str | null>",
  "client": "<str | null>",
  "session_id": "<str>",
  "stale_minutes": "<float>",
  "new_veto_count": "<int>"
}
```
**Semantics:** GitHub #1281, #1449. Emitted instead of the phantom sweep's
already_refused → `CRASH_COMPLETE` fall-through (GitHub #1149's
`already_refused` latch: a session whose most recent tick refused a
stage-mismatched sentinel) when the session's transcript is still actively
advancing — `_transcript_age_seconds` reports a staleness below
`TRANSCRIPT_LIVENESS_WINDOW_SECONDS`. The #1281 incident: a session was
crash-completed 56 seconds before its valid `AUTO_DEV_RESULT` sentinel
landed, burning the task's final retry attempt on a session that was in fact
still making progress. The task stays `RUNNING` and the session stays
`ACTIVE`/`IDLE` — but the session's `consecutive_sentinel_mismatch_vetoes`
latch **is** incremented; that is the only state this event mutates.

Unlike `session.park_vetoed`, this veto has exactly one trigger path (the
already_refused latch), so its payload carries no `reason` field — there is
only one reason it can fire. A session whose transcript cannot be located
falls through to `CRASH_COMPLETE` unchanged (fail-toward-crash), as does a
transcript that has since gone stale beyond the liveness window.

The veto is **bounded** (#1449): it is granted only while
`consecutive_sentinel_mismatch_vetoes < OrchestratorConfig.sentinel_mismatch_veto_cap`
(default 2). Once the count reaches the cap the veto stops firing and the pending
`CRASH_COMPLETE` proceeds; under the default `SIGNAL_ONLY` policy a clean phantom
routes silently to `BLOCKED_ON_USER` **and** — at parity with the retry-cap park
— an immediate `session.needs_attention` is emitted
(`paused_status=sentinel_mismatch_veto_cap_exhausted`, carrying `new_veto_count`,
non-destructive: no daemon-stop / worktree removal). That escalation is
edge-triggered: the counter is bumped past the cap (`cap + 1`) when it fires, so
a session that stays LIVE past its cap does not re-escalate every tick. The
counter resets for free per pipeline episode, since each episode constructs a
brand-new `Session`.

`correlation_id` is the `ticket_id`.

### `session.sentinel_liveness_vetoed`

**Emitter:** `_route_blocked_result_to_task` in `cw.reconcile._shared`
(called from `_apply_sentinel_to_task`, itself invoked from the Stop hook
and reconcile's local/idle/phantom sweeps)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "session_id": "<str>",
  "transcript_age_seconds": "<float>",
  "blocker_reason": "<str>"
}
```
**Semantics:** GitHub #1406. Emitted instead of landing a RUNNING task
terminal `FAILED` when an unparseable/unrecognized-reason `BlockedResult`
(the catch-all: `status_unknown`, `multiple_result_blocks`, or any
unrecognized `blocker.reason` — not the deterministic-parse-failure or
`validation_failed` branches, which are unconditional) arrives but the
session's transcript is still actively advancing
(`0 <= transcript_age_seconds < TRANSCRIPT_LIVENESS_WINDOW_SECONDS`, 300s).
Sibling closure to #1281's `session.sentinel_stage_mismatch_vetoed` (same
incident shape, a different route to it): a malformed sentinel frame is
evidence the *frame* was broken, not that the run is over. Unlike the other
two vetoes, this one re-queues the task to PENDING and clears
`target.session_id` rather than leaving it RUNNING against the same session
— a fresh session is dispatched on retry, so there is no persisted veto
counter/cap bounding repeat vetoes against the same session.
`transcript_age_seconds` is the measured staleness at veto time;
`blocker_reason` is the sentinel's verbatim (unrecognized) `blocker.reason`.
`correlation_id` is the `ticket_id`. Not in `_DEFAULT_OPERATOR_EVENT_TYPES`.

### `gate.auto_approved`

**Emitter:** `_act_auto_approve_review` / `_act_auto_adopt_plan` (`cw.reconcile.gate_recipes`, both dispatched by `run_gate_recipes`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "lane": "<str>",
  "session_id": "<str>",
  "recipe": "auto_approve_clean_review | auto_adopt_clean_plan",
  "predicate_snapshot": "<dict>",
  "approved_at": "<ISO 8601 timestamp>"
}
```
**Semantics:** RFC 0009 P1+P2 (#1065). Emitted before the approve mutation
when a gate recipe auto-clears a clean review/plan gate with no human review —
the event is durably recorded even if the subsequent `_approve_ticket_locked`
write fails, so the decision trace survives a partial write. Unlike
`concierge.recovered` (audit-only), this **is** forwarded to the
operator-attention channel by default — an auto-approve bypassing human review
is attention-worthy.

`predicate_snapshot` is recipe-specific: for `auto_approve_clean_review` it
holds the four field values that licensed the fire (`must_fix_initial: int`,
`deferred: int`, `recommendation: str`, `forbidden_touched: bool`); for
`auto_adopt_clean_plan` it holds the two signoff marker-version strings
(`plan_spec_reviewed: str`, `plan_soundness_reviewed: str`).

`correlation_id` is the `ticket_id`.

### `gate.auto_approve_failed`

**Emitter:** `_act_auto_approve_review` / `_act_auto_adopt_plan` (`cw.reconcile.gate_recipes`, both dispatched by `run_gate_recipes`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "lane": "<str>",
  "session_id": "<str>",
  "recipe": "auto_approve_clean_review | auto_adopt_clean_plan",
  "error": "<stringified CwError>"
}
```
**Semantics:** RFC 0009 P1+P2 (#1065). Companion correction to
`gate.auto_approved`. Emitted when the act-phase `_approve_ticket_locked`
mutation raises `CwError` after `gate.auto_approved` was already recorded —
so the durable event stream carries a correction, not a standing
false-positive "approved" signal on the operator channel. Forwarded to the
operator-attention channel by default alongside `gate.auto_approved`. Stamps
`TicketTask.gate_recipe_failed_at` as a one-shot latch so a persisting
failure doesn't re-detect and re-emit both events every reconcile tick; the
latch clears itself once the condition resolves.

`correlation_id` is the `ticket_id`.

### `gate.auto_approve_held`

**Emitter:** `_act_auto_approve_review` (`cw.reconcile.gate_recipes`, dispatched by `run_gate_recipes`)
**Payload:**
```json
{
  "ticket_id": "<str>",
  "client": "<str>",
  "lane": "<str>",
  "session_id": "<str>",
  "recipe": "auto_approve_clean_review"
}
```
**Semantics:** RFC 0011 A3 (#1160). Second companion correction to
`gate.auto_approved`, alongside `gate.auto_approve_failed`. Emitted when the
act-phase `_approve_ticket_locked` call *declines* to approve — because the row
carries an armed proactive finalize hold (`--hold-finalize` /
`finalize_gate: manual`) and this caller is automatic, not the human
`cw dev-queue approve`. Distinct from `gate.auto_approve_failed`: nothing
raised and nothing is broken, the gate deliberately held, and no
`TicketTask.gate_recipe_failed_at` latch is stamped. The ticket is left exactly
as parked (`BLOCKED_ON_USER` / `finalize_gate_held`), is not reported as
approved, and gets no audit comment. Forwarded to the operator-attention
channel by default alongside `gate.auto_approved`, for the same reason: without
it, `gate.auto_approved` would stand alone on that channel as an uncorrected
"approved" signal.

Note: a *persistently* armed hold (as opposed to one armed inside the
detect→act race window) re-emits this pair on every reconcile tick — there is
deliberately no anti-noise latch, since reusing the failure latch would
conflate a deliberate hold with a broken mutation.

`correlation_id` is the `ticket_id`.

### `pr.action_taken`

**Emitter:** `_act_address_review` (`cw.reconcile.review_recipes`, dispatched by `run_review_recipes`)
**Payload:**
```json
{
  "client": "<str>",
  "lane": "<str>",
  "recipe": "address_review",
  "ticket_id": "<str>",
  "pr_url": "<str>",
  "attention_state": "changes_requested",
  "session_id": "<str | null>",
  "evidence_snapshot": { "review_decision": "<str>" }
}
```
**Semantics:** RFC 0010 P2 (#1097). Emitted before a review recipe dispatches
an `/address-review` session in response to a PR whose review came back
`changes_requested`. The event is recorded durably inside `dev_queue_lock()`
and BEFORE the `spawn_create_impl` dispatch (which runs after the lock
releases), so the decision trace survives even if the subsequent spawn fails.
The act phase performs no dev-queue mutation. Unlike `concierge.recovered`
(audit-only), this **is** forwarded to the operator-attention channel by
default — an automated PR action with no human review is attention-worthy.

`session_id` is the ORIGINATING candidate's session (which may be null); the
event fires before the new `/address-review` session exists, so it cannot carry
that session's id. `evidence_snapshot.review_decision` is the PR field that
licensed the `changes_requested` classification.

`correlation_id` is the `ticket_id`.

### `pr.action_failed`

**Emitter:** `_act_address_review` (`cw.reconcile.review_recipes`, dispatched by `run_review_recipes`)
**Payload:** same keys as `pr.action_taken`, plus:
```json
{
  "error": "<stringified failure reason>"
}
```
**Semantics:** RFC 0010 P2 (#1097). Companion correction to `pr.action_taken`.
Emitted in two cases: (1) the `spawn_create_impl` dispatch raises `CwError`
after `pr.action_taken` was already recorded; or (2) a precondition anomaly
blocks the action before dispatch — an unparseable/missing `pr_url`, an
unresolvable client, or a missing/absent worktree (in the anomaly case no
`pr.action_taken` precedes it). Either way the durable event stream carries a
correction, not just a non-durable log line. Forwarded to the
operator-attention channel by default alongside `pr.action_taken`. Unlike the
gate-recipe failure path, this emits no latch and performs no mutation.

`correlation_id` is the `ticket_id`.

### `review.finding_voided`

**Emitter:** `apply_voided_suppression` (`cw.review_adjudication`), reached
from `synthesize_codex_review_result` (`cw.codex_review._verdict`) on the codex
backend and from `cw review check-voided` (`cw.cli.review`) on the
Claude-native backend.
**Payload:**
```json
{
  "file": "<str>",
  "severity": "MUST_FIX | SHOULD_FIX | DEBT | NIT | PRINCIPLE",
  "summary": "<str>",
  "operator_comment_id": "<str>",
  "voided_at": "<str>",
  "original_rationale": "<str>"
}
```
**Semantics:** GitHub #1814. One event per re-derived review finding that was
suppressed because its content fingerprint matched a `VoidedFinding` the
operator had already settled — the finding is stamped
`disposition="rejected"` and leaves `must_fix`/`blocking` without any reviewer
or coordinating session adjudicating it in that pass.

That is the whole reason this event is mandatory rather than optional. Every
other way a finding stops blocking leaves a decision record somewhere in the
same pass (an `Adjudication` entry, a fix commit, a `RejectedFinding`);
suppression's record was written on an earlier pass, possibly by a different
backend, so without this event a finding simply vanishes from one review to
the next with nothing local explaining why. `apply_voided_suppression` emits
it inline for that reason — suppressing and recording the suppression are not
separable steps (see [ADR-0015](adr/0015-voided-finding-suppression-is-content-anchored.md)).

`operator_comment_id` is the `"<author>@<created_at>"` composite the
coordinating session recorded when it minted the void; `voided_at` and
`original_rationale` come from the same record, so an operator reading this
event can find the settling comment without re-fetching the whole thread.

Deliberately **not** added to `_DEFAULT_OPERATOR_EVENT_TYPES`
(`orchestrator_config.py`): a suppression firing is the *expected* outcome of
an operator decision they already made, so forwarding it would page them about
their own instruction being honored. It is an audit trail, consulted when a
finding's disappearance needs explaining.

`correlation_id` is the `ticket_id`.

### `review.treadmill_detected`

**Emitter:** `_emit_treadmill_diagnostic` (`cw.codex_fix_loop_convergence`),
reached from `_track_open_findings` on every in-loop fix cycle.
**Payload:**
```json
{
  "file": "<str>",
  "severity": "MUST_FIX",
  "summary": "<str>",
  "fingerprint": ["<file>", "<normalized summary>"],
  "previous_reviewed_sha": "<str>"
}
```
**Semantics:** GitHub #1837. One event per newly-appearing MUST_FIX finding the
fix loop's admission gate refused: the finding is on code the latest fix cycle
did not touch, and it carried neither a `transitive_impact_evidence` quote
found verbatim in the delta nor a `release_critical_exception` whose evidence
is present in the current worktree. The finding is promoted into
`ReviewVerdict.debt` instead of joining the open-findings set.

Same rationale as `review.finding_voided` above: this is the one place a
MUST_FIX stops blocking without any reviewer or coordinating session
adjudicating it in that pass, so the refusal needs a durable record of its
own. `fingerprint` is `review_debt.fingerprint_v1`'s `(file, normalized
summary)` pair — `null` only for a `file="N/A"` finding, which cannot be
fingerprinted at all. `previous_reviewed_sha` is the head the refused cycle's
delta was taken from, so an operator can reconstruct exactly what the gate
compared against.

Deliberately **not** added to `_DEFAULT_OPERATOR_EVENT_TYPES`
(`orchestrator_config.py`), for the same reason: a gate refusal is the
expected steady-state outcome on any branch with pre-existing debt, and the
debt itself is already surfaced on the posted review comment.

`correlation_id` is the `ticket_id`.

### `review.finding_disposition_suppressed`

**Emitter:** `suppress_adjudicated_findings`
(`cw.review_finding_dispositions`), reached from
`synthesize_codex_review_result` (`cw.codex_review._verdict`) — so both
`run_review` and the fix loop's per-cycle `_rereview` go through it.
**Payload:**
```json
{
  "file": "<str>",
  "summary": "<str>",
  "outcome": "REJECTED",
  "rationale": "<str>",
  "recorded_at": "<str>"
}
```
**Semantics:** GitHub #1838. One event per re-derived review finding suppressed
because its `review_debt.fingerprint_v1` identity matched a `REJECTED` entry in
the ticket's cross-round adjudication ledger
(`TicketTask.finding_dispositions`, schema v31). The finding is stamped
`disposition="rejected"` and leaves `must_fix`/`blocking`.

Mandatory for the same reason as `review.finding_voided` above, and NOT a reuse
of it: the two suppressions have different identities (fingerprint-keyed vs.
evidence-anchored), different lifetimes (an adjudication does not lapse when
the code moves; a void does), and different payloads — one event type would
produce an audit trail that cannot say which mechanism fired. `outcome` is
always `"REJECTED"`: an `ACCEPTED` ledger entry reaches the reviewer prompt but
never the mechanical gate, so it emits nothing.

Because the suppression does not expire, it is also surfaced on the posted
review comment: the stamped `AcceptedFinding.disposition_detail` names the
file, the `recorded_at` date, the operator's original rationale, and the
re-adjudicate-if-the-code-changed caveat, which
`_disposition_annotation`/`_render_findings` (`cw.codex_review._verdict`)
already render inline. This event is the durable half of that record; the
comment is the human-visible half.

Deliberately **not** added to `_DEFAULT_OPERATOR_EVENT_TYPES`
(`orchestrator_config.py`), matching both siblings above: a suppression is the
expected steady-state outcome once an operator has settled a finding, and it is
already visible on the review comment.

`correlation_id` is the `ticket_id`.

### `watched_pr.collision`

**Emitter:** `register_or_adopt_watched_pr` (`cw.dev_queue.crud`).
**Payload:**
```json
{
  "client": "<str>",
  "repo": "<str>",
  "pr_number": "<int>",
  "colliding_client": "<str>",
  "colliding_source": "<str>"
}
```
**Semantics:** GitHub #1927. A `stale_dispatch` park's `WatchedPr` registration
found an active watch for the same `(repo, pr_number)` already owned by a
DIFFERENT, non-`None` client. `register_watched_pr`'s existing
`(repo, pr_number, active)` dedup has no `client` dimension, so a bare `False`
return from it is indistinguishable from "already handled" — for this
producer specifically, it can mean a second client's park can never be
attributed a merged-PR fact and will hold its lane slot `BLOCKED_ON_USER`
indefinitely. `register_or_adopt_watched_pr` refuses that silent outcome:
when the existing watch is client-less (the common case — a pre-existing
webhook/cli watch), it adopts it in place instead (no event, `client` is
simply set); this event fires only for the genuine collision, where the
existing watch already belongs to someone else.

`correlation_id` is `client`.

Deliberately **not** added to `_DEFAULT_OPERATOR_EVENT_TYPES`
(`orchestrator_config.py`): the condition requires two dev-queue clients
mapped to the same repo colliding on the same PR number, which the codebase's
`(client, repo)` injectivity premise (#1269) treats as configuration drift
rather than a steady-state outcome. The event exists as the durable,
queryable record (`cw event tail`/`cw event wait`); opting it into the
default push-notification forward-set is a separate policy call left for a
follow-up if this proves to fire in practice.

### Operator-attention channel (RFC 0008 W3, #1002)

A server-side filter (`cw.cw_operator_events`) forwards a declarative subset
of this bus — `task.transition` (only for terminal/attention-worthy
`new_status` values: `blocked_on_user`, `awaiting_operator_signoff`,
`completed`, `failed`, `cancelled` by default), `task.deleted`,
`session.needs_attention`, all seven `pr.*` types (the five PR-lifecycle
events plus `pr.action_taken`/`pr.action_failed`), `session.liveness_changed`
(only at `new_bucket >= stale_30m`), `operator.escalation`,
`gate.auto_approved`, `gate.auto_approve_failed`, and
`gate.auto_approve_held` — onto a distinct
`cw-operator` SSE topic on the existing `cw_queue_events_server`, consumed
with cursor name `"operator-channel-bridge"`. `concierge.recovered`,
`concierge.recovery_backoff_armed` and
`concierge.hook_context_conflict_refused` are deliberately excluded
(audit-only).
See [`docs/operator-channel.md`](operator-channel.md) for the filter
reference, subscription instructions, and degradation contract.

## CLI

### Record an event

```bash
cw event record pr.registered --payload '{"pr": 42, "repo": "owner/repo"}'
cw event record pr.ci_failed --payload '{"pr": 42, "repo": "owner/repo", "run_id": "r1", "failed_checks": ["lint"]}' \
    --correlation-id "corr-abc"
```

`cw event record` accepts only the producer-facing subset of event types:
`ticket.enqueued`, `session.spawned`, `session.completed`,
`session.timed_out`, `stage.entered`, `stage.errored`, `pr.registered`,
`pr.ci_failed`, `pr.review_received`, `pr.mergeable`, `pr.merged`. The
orchestrator-internal types (`task.*`, `dispatch.*`, reconcile events, etc.)
are emitted only from inside cw and cannot be recorded from the CLI.

### Tail events

```bash
# All events
cw event tail

# From a consumer cursor (advances cursor after reading)
cw event tail --since daemon

# Since an ISO timestamp
cw event tail --since 2025-01-01T00:00:00Z

# Filter by type (repeatable)
cw event tail --type pr.ci_failed --type pr.review_received

# Filter by payload.client (repeatable; comma-separated also accepted)
cw event tail --client my-client

# Collapse repeated terminal re-fires (timed_out, reap_proposed,
# needs_attention, stage_timed_out_retried) for the same session
cw event tail --dedup-terminal

# Collapse consecutive same-type repeats into `TYPE xN over Mm` summary lines
cw event tail --collapse-repeats

# Return only the most recent N matching events (filter-then-limit)
cw event tail --limit 20
cw event tail -n 20

# Machine-readable JSON output
cw event tail --json
```

One-shot `--since <consumer>` with an unknown consumer initializes the cursor
at the end of the inbox (no history replay) instead of replaying everything.

`--limit`/`-n` bounds the already-filtered set to the most recent N events; it
is not supported with `--follow`, which streams unboundedly. The default
(non-`--json`) output format is compact: nested dict/list-of-dict payload
fields — e.g. `dispatch.tick`'s `lanes` and `lane_occupants` — are omitted,
while scalar fields and scalar-lists are kept regardless of length (the
filter is shape-based, not size-based). `--json` is unaffected and always
emits the full event, nested fields included.

`--collapse-repeats` merges consecutive events of the same type sharing an
identical compact payload into a single `TYPE xN over Mm` summary line; a
run interrupted by an unrelated event re-opens rather than merging across
the gap. It is not supported with `--follow` (see below) — collapsing
requires buffering a run until it closes, which would delay events past
the immediate-flush contract. It composes with `--dedup-terminal` and
`--limit`, applied last in the pipeline (filter → limit → dedup-terminal →
collapse-repeats). `--json` is unaffected: passing `--collapse-repeats`
with `--json` is a no-op, one JSON line per original event.

### Follow mode

Add `--follow` (or `-f`) to stream new events in real time instead of exiting
after the first read.  Output is line-buffered — each event flushes immediately,
so piping to `jq --unbuffered` or `grep` works without stalling.

```bash
# Stream all new events (blocks until SIGINT)
cw event tail --follow

# Combine with --since, --type, --json
cw event tail --since 2026-07-15T00:00:00Z --type session.completed --json --follow
cw event tail --since daemon --type pr.mergeable --follow
```

Behaviour notes:
- Polls the inbox every 50 ms; events are visible within 100 ms of being written.
- `--since <consumer>` in follow mode loads the consumer's saved cursor as the
  starting position but does **not** advance it — cursor advance is one-shot only.
- If the consumer cursor is not found (unknown consumer or rotated inbox), a
  warning is printed to stderr and replay starts from the beginning of the inbox.
- Exits with code 130 on SIGINT (`Ctrl-C`), 0 on broken pipe.
- Out of scope: persistent cursor advance on `--follow`; `--since now` semantics
  (`now` is treated as a consumer name — use an ISO timestamp instead).

### Wait for an event

`cw event wait` blocks until a matching event arrives — the scripting
primitive for "tell me when ticket X does anything":

```bash
# Block until any event correlates to ticket CW-42 (correlation_id match)
cw event wait --ticket CW-42

# Wait for a specific session to complete, with a 10-minute ceiling
cw event wait --session ab12cd34 --type session.completed --timeout 600

# Wait for the next attention-worthy signal on a client
cw event wait --client my-client --type session.needs_attention,session.timed_out

# Stream every match instead of exiting on the first
cw event wait --ticket CW-42 --follow
```

Behaviour notes:
- Reads from the **beginning** of the inbox, so events recorded before the
  command started also match — safe to start after the fact.
- Outputs one JSON line per match. Exits 0 on match (or `--follow`
  exhaustion); non-zero on timeout (default ceiling 3600 s).
- `--ticket` matches the event's `correlation_id`; `--session` and `--client`
  match the payload's `session_id` / `client` fields. `--type` is repeatable
  and accepts comma-separated values.

### Prune events

`events/inbox.jsonl` grows unbounded by default. `cw event prune` truncates
it by age or by count (issue #856):

```bash
# Keep only the newest 500 events; archive the rest (default)
cw event prune --keep 500

# Archive everything created before a cutoff timestamp
cw event prune --before 2026-01-01T00:00:00Z

# Hard-drop instead of archiving
cw event prune --keep 500 --delete

# Machine-readable output
cw event prune --keep 500 --json
```

Behaviour notes:
- `--before` and `--keep` are mutually exclusive; exactly one is required.
- By default (no `--delete`), pruned events are appended to
  `events/inbox.<YYYY-MM-DD>.jsonl` (plain JSONL, no compression) before being
  dropped from `inbox.jsonl`. Repeated prunes on the same day append to the
  same archive file.
- `--delete` discards pruned events outright — nothing is written.
- The read, rewrite, and archive-append happen under a single acquisition of
  the inbox lock, so a concurrent `record_event`/`read_events` call sees
  either the pre-prune or post-prune inbox, never a partial state.
- No audit event is emitted for a prune (avoids a self-deadlock: the inbox
  lock is not reentrant).
- `--json` emits exactly this schema:

  ```json
  {
    "archived_count": 3,
    "deleted_count": 0,
    "archive_path": "/home/user/.local/share/cw/events/inbox.2026-07-07.jsonl",
    "kept_count": 2
  }
  ```

  `archive_path` is `null` when nothing was archived (empty inbox, or
  `--delete` was passed).

## Python API

```python
from cw.events import record_event, read_events, advance_cursor
from cw.models import OrchestratorEventType

# Record
event = record_event(
    OrchestratorEventType.PR_REGISTERED,
    {"pr": 42, "repo": "owner/repo"},
    correlation_id="corr-123",
)

# Read all events newer than the daemon's cursor
events = read_events(consumer="daemon")

# Advance the cursor after processing
if events:
    advance_cursor("daemon", events[-1].id)
```

## Naming note

The existing `cw.history.EventType` covers session lifecycle events
(SESSION_STARTED, SESSION_BACKGROUNDED, etc.) and is stored per-client in
`~/.local/share/cw/history/<client>.jsonl`.

The new `OrchestratorEventType` (in `cw.models`) covers orchestrator-level
signals (PR lifecycle, ticket queue, cross-session coordination) and is stored
in the shared `inbox.jsonl` above.

## Out of scope (downstream)

Emitter wiring inside `/auto-dev` and `/review-monitor` skills (in the
`global-claude` repo) is handled separately — those skills will call
`cw event record` as part of their workflows once upgraded.
