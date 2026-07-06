# Operator-Attention Channel (RFC 0008 W3, #1002)

A server-side filter over the orchestrator event bus (`cw.events`,
`~/.local/share/cw/events/inbox.jsonl`) that forwards only the subset an
operator actually needs to act on, onto a distinct `cw-operator` SSE topic
mounted on the **existing** queue-events server (`cw_queue_events_server`).
No new server, no new port — `cw-operator` shares the same host/port as
`cw-queue-events` (default `127.0.0.1:8789`).

## Why a filtered channel

The orchestrator bus carries every lifecycle signal (`dispatch.tick`,
`stage.entered`, `session.spawned`, ...) — most of it is noise for a human
operator watching a session. `cw_operator_events` narrows that firehose down
to the handful of event types that mean "an operator should look at this,"
so an agent subscribed to `<channel source="cw-operator">` sees a low-volume,
high-signal stream instead of hand-filtering the raw bus.

## Filter semantics

Declared via `OrchestratorConfig.operator_channel_forward`
(`~/.claude-workspace/orchestrator.yaml`) as an `OperatorChannelForward`
submodel with three fields:

```yaml
operator_channel_forward:
  event_types:
    - task.transition
    - task.deleted
    - session.needs_attention
    - pr.registered
    - pr.ci_failed
    - pr.review_received
    - pr.mergeable
    - pr.merged
    - session.liveness_changed
  task_transition_statuses:
    - blocked_on_user
    - awaiting_operator_signoff
    - completed
    - failed
    - cancelled
  liveness_min_bucket: stale_30m
```

The values above are the **defaults** — this block does not need to appear
in `orchestrator.yaml` unless you want to override it.

- **`event_types`** — the admitted `OrchestratorEventType` set. An event
  whose type is absent from this set is dropped outright, regardless of the
  two sub-condition fields below.
- **`task_transition_statuses`** — for `task.transition` events only: the
  event is admitted iff its `new_status` payload field is in this set. The
  default set is exactly the terminal/attention-worthy statuses — a
  transition to `running` or `pending` never forwards, even though
  `task.transition` itself is in `event_types`.
- **`liveness_min_bucket`** — for `session.liveness_changed` events only: the
  event is admitted iff its `new_bucket` payload field is at or above this
  rung on the closed `live < stale_15m < stale_30m < stale_45m` ladder. The
  default (`stale_30m`) skips the earliest, most speculative staleness
  signal (`stale_15m`) and only surfaces once a session has been quiet for a
  more concerning window.
- Every other admitted type (`task.deleted`, `session.needs_attention`, all
  five `pr.*` types) forwards unconditionally once present in `event_types` —
  there is no sub-condition for them.

**Fail-loud validation:** unlike `reap_policy` (which silently coerces an
invalid value to its safe default per ADR-0006), an invalid
`operator_channel_forward` value — an unknown event type, an unknown
`QueueItemStatus`, or an unknown `LivenessBucket` — raises a
`pydantic.ValidationError` that crashes `cw queue-channel serve` at startup.
This mirrors `default_signoff`'s asymmetry with `reap_policy`
(config/CONFIG_REFERENCE.md): under-forwarding here is a silent
operator-facing regression, so a config typo must fail loudly rather than
quietly dropping events an operator is relying on.

## Cadence

The bridge (`cw.cw_operator_events.poll_and_forward_operator_channel`) runs
on the same 2-second poller thread `cw_queue_events_server` already uses for
`queue.*` deltas (`_poller_tick`), immediately after that tick's queue.*
broadcasts. Events are visible to a `cw-operator` subscriber within ~2
seconds of landing in the orchestrator inbox — an acceptable latency bound
for attention events, which are inherently lower-frequency and less
time-critical than the queue channel's own per-tick deltas.

The bridge call is isolated in its own `try/except`, outside the queue
poller's `_file_lock`, so a bug in the bridge can never block or delay the
`queue.*` broadcasts running in the same tick. A bridge failure logs
`"operator-bridge error"` (distinct from the poller's own `"poller error"`)
and the next tick simply retries — no crash-loop, no lost `queue.*` delivery.

## How to subscribe

Add the `cw-operator` MCP server to your `.mcp.json` (see
`config/cw-operator-events.mcp.json.example`):

```json
{
  "mcpServers": {
    "cw-operator": {
      "command": "cw",
      "args": ["operator-channel", "proxy", "--client-id", "<client-name>"],
      "env": {
        "CW_OPERATOR_EVENTS_BASE_URL": "http://127.0.0.1:8789"
      }
    }
  }
}
```

This is **manual wiring only** — `cw init` does not auto-wire `cw-operator`
into `.mcp.json` the way it does for `cw-queue-events`/`cw-pr-events` today.
Skill/runbook adoption of this channel is deferred to a follow-up ticket
(#1003).

There is no `operator-channel serve` subcommand — the channel rides the
existing `cw queue-channel serve` process (`cw_queue_events_server.make_app()`
mounts the three `cw-operator` routes alongside its own three). Start (or
keep running) `cw queue-channel serve` as usual; `cw-operator` becomes
available on the same host/port automatically.

### `--client-id` scoping

Like `cw-queue-events`, the `cw-operator` proxy accepts an optional
`--client-id` to scope the subscription to one client's events (matched
against each admitted event's `client` payload field). Omitting it relays
every admitted event across all clients.

**Known gap:** `pr.registered`'s payload has no `client` key (a pre-existing
producer gap; fixing the producer is out of scope for this channel — see
GitHub #1002). The `cw-operator` proxy special-cases `pr.registered` to
always relay it regardless of `--client-id`, since a client-scoped operator
silently missing PR registrations is worse than rare cross-client noise on
this one low-volume event type. This is the same trade-off, applied
one level downstream, that the filter engine itself does not need to make
(the filter's `event_types`/`task_transition_statuses`/`liveness_min_bucket`
fields have no notion of `client` at all — client scoping is a proxy-side
concern only).

## Degradation contract

If `cw queue-channel serve` is not running, or `cw-operator` is not wired
into `.mcp.json`: **no events are silently lost from the orchestrator's own
record.** The underlying orchestrator inbox (`inbox.jsonl`) is unaffected —
it is written by `cw.events.record_event` regardless of whether any bridge
or subscriber is running. `cw event tail --type task.transition --type
session.needs_attention ...` (see `docs/events.md`) remains the documented
polling fallback for any operator without a live `cw-operator` subscription.
This mirrors the push/poll coexistence framing in `dispatch-runbook.md` §10:
the operator channel is a low-latency convenience on top of the durable
inbox, not the inbox's only means of consumption.

The queue-events server itself follows its own established degradation
story (documented in `dispatch-runbook.md`): if `cw queue-channel serve`
crashes and restarts, the bridge's own cursor
(`~/.local/share/cw/events/cursors/operator-channel-bridge.json`) picks up
exactly where it left off — no replay, no gap, since the orchestrator inbox
cursor semantics (`cw.events.read_events`) are already at-least-once and
idempotent for this consumer.

## Related docs

- [`docs/events.md`](events.md) — full `OrchestratorEventType` catalogue and
  the underlying event bus this channel filters.
- [`docs/dispatch-runbook.md`](dispatch-runbook.md) §10 — the `cw-pr-events`
  push producer, whose push/poll degradation framing this channel's
  degradation contract mirrors.
- `config/CONFIG_REFERENCE.md` — `operator_channel_forward` field reference
  alongside the rest of `orchestrator.yaml`.
