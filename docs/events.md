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
