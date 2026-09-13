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
    - operator.escalation
    - gate.auto_approved
    - gate.auto_approve_failed
    - pr.action_taken
    - pr.action_failed
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
- Every other admitted type (`task.deleted`, `session.needs_attention`, the
  five PR-lifecycle types `pr.registered`/`pr.ci_failed`/`pr.review_received`/
  `pr.mergeable`/`pr.merged`, `operator.escalation`, `gate.auto_approved`,
  `gate.auto_approve_failed`, `pr.action_taken`, `pr.action_failed`) forwards
  unconditionally once present in `event_types` — there is no sub-condition
  for them.

`operator.escalation` (RFC 0008 capstone, #1015) was added to the default
forward set: it is the durable-escalation-latch's operator-facing signal,
firing once per parked episode past a 45-minute threshold (see
`docs/events.md`). Its sibling event, `concierge.recovered` (the mechanical
recovery reactor's audit trail), is deliberately **excluded** from the
default set — it records a non-destructive, already-resolved recovery the
operator does not need paging for.

Four autonomous-action signals were later added to the default set:
`gate.auto_approved` / `gate.auto_approve_failed` (RFC 0009 P1+P2, #1065 — a
gate recipe approving a plan/review with no human in the loop, and its
failed-mutation companion so a lone "approved" never stands uncorrected) and
`pr.action_taken` / `pr.action_failed` (RFC 0010 P2, #1097 — a review recipe
dispatching an `/address-review` action, with the same failed-companion
rationale). Each is operator-attention-worthy precisely because no human was
in the loop when it fired.

**Fail-loud validation:** unlike `reap_policy` (which silently coerces an
invalid value to its safe default per ADR-0006), an invalid
`operator_channel_forward` value — an unknown event type, an unknown
`QueueItemStatus`, or an unknown `LivenessBucket` — raises a
`pydantic.ValidationError` that crashes `cw queue-channel serve` at startup.
This mirrors `default_signoff`'s asymmetry with `reap_policy`
(config/CONFIG_REFERENCE.md): under-forwarding here is a silent
operator-facing regression, so a config typo must fail loudly rather than
quietly dropping events an operator is relying on.

## Digest coalescing (RFC 0011 A6, #1162)

A `session.needs_attention` event whose ticket is currently parked in a
*hold* class (`TicketTask.disposition` in `HOLD_DISPOSITIONS` --
`awaiting_operator` or `finalize_gate_held`) is **buffered** instead of
forwarded immediately: "we could not reach the operator or a dependency"
parks pile up quietly overnight rather than paging once per park. Every
other admitted event -- a genuine `blocked`/broken park (any other
disposition), a ticketless fleet-wide event (e.g. `gh_availability_outage`,
no owning ticket), or a ticket the bridge can't resolve to a known row --
still forwards immediately, unbatched, exactly as before. Classification
joins the event's `ticket_id`/`client` payload fields against the live
dev-queue store; it does **not** match `payload["paused_status"]` against
`HOLD_DISPOSITIONS` -- those are two disjoint string namespaces (see
`AWAITING_OPERATOR_DISPOSITION`'s docstring in
`cw.dev_queue.lifecycle`), so the classification always resolves the actual
`TicketTask.disposition`.

The buffer is durable, not in-memory: the first held event for a ticket
stamps `TicketTask.attention_digest_buffered_at` (idempotently -- a later
re-fire of the same episode does not reset it), which survives a daemon
restart and is cleared unconditionally by `transition_task_status` on any
subsequent status transition. A ticket resolved between buffering and flush
therefore drops out of the digest automatically -- the flush always
re-derives live held state from the dev-queue store, never replays a
buffered event.

The buffer flushes to a single digest SSE push once two gates both pass:

- **Delivery window** -- a local-timezone daily window
  (`attention_digest_window_tz` / `_start_hour` / `_end_hour`, default
  `America/New_York` 08:00-20:00), resolved via `zoneinfo` so it tracks DST
  correctly. Outside the window, held parks keep buffering indefinitely --
  nothing flushes overnight.
- **Idle-drain floor** (`attention_digest_idle_floor_seconds`, default 60) --
  inside the window, a flush additionally waits until this many seconds
  have elapsed since the **most recently buffered arrival** in the current
  batch, not the oldest. A fresh held park arriving mid-wait pushes the
  flush back out, giving it a chance to land in the same digest instead of
  triggering a second push moments later.

When the window opens after a quiet overnight buildup, every currently
buffered held ticket flushes together in one digest -- there is no
additional per-ticket wait once the window is open and the idle floor has
already elapsed for all of them.

The digest notification shares the same three-key outer envelope as every
other `cw-operator` push (`notification_type`, `message`, `title`); only the
inner `message` differs:

```json
{
  "event": "session.needs_attention.digest",
  "digest": true,
  "count": 2,
  "created_at": "2026-07-30T12:00:00+00:00",
  "entries": [
    {"ticket_id": "GEN-123", "client": "acme", "breadcrumbs": "operator unavailable"},
    {"ticket_id": "GEN-456", "client": "acme", "breadcrumbs": null}
  ]
}
```

`entries` carries exactly `ticket_id` / `client` / `breadcrumbs` per held
ticket -- `breadcrumbs` is `TicketTask.blocked_reason` verbatim (`null` when
the park carried no reason). `"event"` is a literal string, not a registered
`OrchestratorEventType` -- the digest is never persisted as an
`OrchestratorEvent` (it has no single owning ticket to attribute one to);
it exists only as this one ephemeral SSE push. `count` is uncapped: a large
overnight batch flushes in full, every ticket listed.

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
against each admitted event's `client` payload field). The
`CW_OPERATOR_EVENTS_CLIENT_ID` env var is an equivalent alternative
(`--client-id` wins when both are set). Omitting both relays every admitted
event across all clients. The same value doubles as the proxy's durable
replay-cursor identity on the server; when neither is set, the hostname is
used for cursor tracking, so reconnects still resume where they left off.

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

## `cw pr-channel proxy` repo scoping (#2146)

The `cw-pr-events` proxy is a sibling of this channel with a different
scoping axis, documented here because operators wire both from the same
`.mcp.json`.

One `cw pr-channel serve` process serves every client, so a proxy started
with `--client-id <client>` used to still forward every repo's PR events
into that client's session. It now forwards only events whose `repo` matches
the repo `<client>` resolves to — `clients.yaml` entry → workspace path →
`origin` remote → `owner/repo` slug, the same composition
`reconcile/stale_dispatch_watch.py` already uses. The compare is
case-insensitive.

Scoping is by **repo, not ticket**: a PR event carries `repo`/`pr_number`
and no lane or ticket identity (`PREventRequest` has no client field), and
adding one would be a server-side schema change. The repo a client works in
is the coarsest identity already present on every event.

```json
{
  "mcpServers": {
    "cw-pr-events": {
      "command": "cw",
      "args": ["pr-channel", "proxy", "--client-id", "<client-name>"]
    }
  }
}
```

### Fail-closed on resolution failure

Repo resolution is a cross-tenant isolation boundary, so it **fails closed**
(ARCHITECTURE.md §7 principle 12). If `--client-id` was given and the repo
cannot be resolved — the client is missing from a populated `clients.yaml`
("dangling client"), or its workspace has no parseable GitHub `origin`
remote — the proxy forwards **nothing** for that client rather than falling
open to every repo. An operator sees both of:

- a `logger.error` on the proxy naming the client and which resolution step
  failed, and
- one `notifications/message` at level `error` on the subscribed session,
  carrying `event: repo_resolution_failed` and the client name.

The fix is to repair `clients.yaml` or the workspace remote. `--all-repos`
is the only supported path to an intentionally unfiltered stream; it skips
repo resolution entirely and therefore never trips the fail-closed path.
Omitting `--client-id` (and `CW_PR_EVENTS_CLIENT_ID`) is also unfiltered —
no filtering was requested, so there is nothing to fail.

## Related docs

- [`docs/events.md`](events.md) — full `OrchestratorEventType` catalogue and
  the underlying event bus this channel filters.
- [`docs/dispatch-runbook.md`](dispatch-runbook.md) §10 — the `cw-pr-events`
  push producer, whose push/poll degradation framing this channel's
  degradation contract mirrors, and the server-side webhook relay setup
  behind the repo-scoped proxy documented above.
- `config/CONFIG_REFERENCE.md` — `operator_channel_forward` field reference
  alongside the rest of `orchestrator.yaml`.
