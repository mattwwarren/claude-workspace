# Event Bus

Global orchestrator event bus for cw. Skills and the daemon post events here;
the daemon and other consumers read from it using cursor-based consumption.

## Storage

- **Inbox**: `~/.local/share/cw/events/inbox.jsonl` — append-only JSONL log
- **Cursors**: `~/.local/share/cw/events/cursors/<consumer>.json` — per-consumer read position
- **Lock**: `~/.local/share/cw/events/.inbox.lock` — `fcntl` exclusive lock prevents concurrent writes

`EVENTS_DIR` is already defined in `config.py` as `STATE_DIR / "events"`.
The same directory holds `.idle` signal files used by `wrapper.py`; the new
`inbox.jsonl` coexists without conflict.

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
**Payload:** `{"ticket_id": "<str>", "client": "<str>"}`
**Semantics:** Emitted once per PENDING task when the client's local
`<default_branch>` is behind `origin/<default_branch>`. The task stays
PENDING; the slot is skipped for this tick. Operator should run
`cw dev-queue refresh-all` to fast-forward and unblock dispatch.

```json
{
  "ticket_id": "CW-42",
  "client": "my-client"
}
```

### `session.spawned`

An orchestrator-managed session was started.

```json
{
  "session_id": "<str>",
  "client": "<str>",
  "purpose": "impl|idea|debt|explore"
}
```

### `session.completed`

An orchestrator-managed session completed (normal or handoff).

```json
{
  "session_id": "<str>",
  "client": "<str>",
  "reason": "user|handoff|crashed"
}
```

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

CI checks on a monitored PR failed.

```json
{
  "pr": 42,
  "repo": "owner/repo",
  "run_id": "<str>",
  "failed_checks": ["check-name"]
}
```

### `pr.review_received`

A review was submitted on a monitored PR.

```json
{
  "pr": 42,
  "repo": "owner/repo",
  "review_id": "<int>",
  "state": "APPROVED|CHANGES_REQUESTED|COMMENTED",
  "reviewer": "github-login"
}
```

### `pr.mergeable`

A monitored PR is now mergeable (CI green, approvals met).

```json
{
  "pr": 42,
  "repo": "owner/repo"
}
```

### `pr.merged`

A monitored PR was merged.

```json
{
  "pr": 42,
  "repo": "owner/repo",
  "merge_sha": "<str>"
}
```

### `stage.entered`

The headless `/auto-dev` worker entered a new stage of its pipeline. Used
by `cw orchestrate status` / `watch` to derive a per-session `last_stage`
display. Producer contract is in
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
  "skip_reason": "freshness_gate | cap_full | spawn_error | no_pending | none"
}
```
**Semantics:** Emitted once per client per tick. `claimed` is the number of
tasks newly spawned this tick. `pending` is the pre-claim count (read before
the claim loop). `running` is the count of RUNNING tasks at tick start.
`skip_reason` follows first-match precedence: `freshness_gate` (local branch
behind origin, checked before anything else) → `cap_full` (running ≥ cap) →
`spawn_error` (exception during spawn) → `no_pending` (nothing to claim) →
`none` (at least one session spawned). `correlation_id` is `None` (per-client
aggregate, not per-ticket). Consumers MUST tolerate unknown `skip_reason`
values.

### `session.phantom_reverted`

**Emitter:** `reconcile` in `cw.reconcile` (phantom sweep)
**Payload:**
```json
{
  "session_id": "<str>",
  "ticket_id": "<str>",
  "client": "<str>",
  "worktree_dirty": false,
  "worktree_path": "<str | null>"
}
```
**Semantics:** Emitted for each DAEMON-origin session that is reaped as a
phantom (present in state but no longer backed by a live multiplexer surface)
and whose owning ticket is reverted to PENDING for retry. `worktree_dirty` is
`true` when `worktree_has_unsaved_work` reports uncommitted changes in the
session's worktree — operators should inspect before the next dispatch.
`worktree_path` is the absolute path to the worktree, or `null` when it cannot
be resolved. `correlation_id` is the `ticket_id`. NOT emitted for USER-origin
sessions (those are reaped without task revert).

### `session.salvage_skipped`

**Emitter:** `revert_stalled_headless_sessions` in `cw.reconcile`
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

## CLI

### Record an event

```bash
cw event record pr.registered --payload '{"pr": 42, "repo": "owner/repo"}'
cw event record pr.ci_failed --payload '{"pr": 42, "repo": "owner/repo", "run_id": "r1", "failed_checks": ["lint"]}' \
    --correlation-id "corr-abc"
```

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

# Machine-readable JSON output
cw event tail --json
```

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
