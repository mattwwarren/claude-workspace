# Event channel wire contract

The `cw-pr-events`, `cw-queue-events` and `cw-operator` channels deliver live
notifications over SSE and replay missed ones from a per-channel log. This page
pins what a consumer sees on the wire and what it may rely on.

## Envelope

Every item is the on-disk record the event bus wrote for that event:
`{notification_type, message, title, offset}`. The same dict is the live
notification's `params.data`, and it is the replayed item too. Live and replay
are byte-identical for the same offset.

- `message` is a channel-specific JSON string. Parse it to get the payload.
- `offset` is an integer assigned by the server under the log's `file_lock`.
  It is the only source of ordering and identity; consumers must not infer it
  from timestamps, contents, or a local counter.

Observed live notification (values redacted, shape kept):

```json wire-example
{
  "jsonrpc": "2.0",
  "method": "notifications/message",
  "params": {
    "level": "info",
    "logger": "cw-pr-events",
    "data": {
      "notification_type": "pr",
      "message": "<redacted JSON string>",
      "title": "<redacted>",
      "offset": 0
    }
  }
}
```

## Identity

An item's identity is `(params.logger, params.data.offset)`. Each channel has its
own log and its own offset space: `cw-pr-events`, `cw-queue-events` and
`cw-operator` never share offsets.

## Cursor and acknowledgement

- A cursor is the **next offset to read**. Replay on subscribe selects records
  with `offset >= cursor`.
- A consumer that has durably accepted offset `N` acknowledges `N + 1`:
  `POST /ack` (PR and queue channels) or `POST /operator/ack` (operator channel),
  body `{"client_id": "<id>", "offset": N + 1}`.
- The ack endpoints store the value as given. They do not check it against the
  log, and they do not make the cursor monotonic.
- Sending a notification is not evidence that the consumer completed anything.
  Only the consumer's own durable acceptance justifies an ack.

## Delivery guarantees

- At-least-once. After a crash between durable acceptance and the ack, the
  replayed item is identical to the live one it replaces. Dedupe on identity.
- Live delivery to each subscriber is in offset order. Fan-out happens inside
  the same critical section that assigns the offset.

## Limits

- Deleting a channel's log restarts its offsets at `0`. Identity is only
  guaranteed within one log generation.
- Offset uniqueness is guaranteed per single writer process. The lock is
  thread-scoped, not process-scoped. Two processes appending to one log (for
  example a lingering server during a crash-restart overlap) can reuse an offset
  (see `_last_offset_from_reverse_scan` in `src/cw/event_bus.py`). Dedupe on
  `(logger, offset)` could then drop a distinct event.
- Retention and log rotation are out of scope (#1991). Replay reads the whole
  log per subscribe (#1990).
