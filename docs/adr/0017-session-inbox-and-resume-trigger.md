# A session's inbound channel is a durable mailbox plus a paused-only resume trigger

**Status:** Accepted
**Driven by:** #2212 (and #1889, whose spike finding forces the shape)

## Decision

An operator can answer a paused cw session with `cw session send`. The message
is appended to a per-session, append-only mailbox that is durable independent
of `sessions.json`, and a separate adapter then attempts to wake the session by
respawning it with `--resume`. The two halves are independent: queuing succeeds
or fails on its own, and delivery is best-effort on top of it.

The resume trigger is restricted, deliberately and as an architecture decision,
to sessions that are **already paused** — the owning `TicketTask` is
`BLOCKED_ON_USER`, or the `Session` is `SessionStatus.IDLE`. Delivering to a
genuinely live, mid-task session is **not implemented** and is deferred to
**#2255**.

## Invariant

1. **The mailbox is append-only.** Consumption advances a cursor file; the
   `inbox.jsonl` itself is never rewritten, truncated, or compacted by a
   consumer. Anything that "removes" a message by editing the inbox breaks
   replay idempotence.
2. **Delivery is at-least-once.** A cursor that no longer appears in the inbox
   replays every message from the start, exactly as `events.read_events` does.
   Every consumer must be idempotent.
3. **Queuing and delivery are separate outcomes.** A command or caller must not
   report a queued-but-undelivered message as a failure, and must not treat a
   delivery success as evidence the message was queued.
4. **The resume trigger touches no harness client until the gate passes.** A
   session that is neither `BLOCKED_ON_USER` nor `IDLE` must produce zero
   calls into `NativeDaemonClient` — no `spawn_bg`, no `stop`, not even
   `list_live_session_short_ids`.
5. **The resume trigger never calls `daemon.stop()`.** See Consequences.
6. **The mailbox is not session state.** It has its own lock and its own
   files, and `cw session send`'s append path mutates no `Session` field, so
   it never contends with the ADR-0005 state lock.

## What this means for callers

- `src/cw/cli/session_send.py` — appends first, triggers second, and exits 0
  whenever the append succeeded. Exit 1 is reserved for the cases where
  nothing was queued (unknown session, terminal session, bad flags).
- `src/cw/queue_peek.py` — reports a `BLOCKED_ON_USER` row as
  `AWAITING_OPERATOR` rather than scoring it on the age/idle ladder. A parked
  row is awaiting a human, not wedged, and it is never a "suggested stop."
- Any future consumer that drains a session's mailbox must call
  `advance_cursor` after acting, and must tolerate seeing a message twice.

## What this means for producers

- `cw.session_inbox.append_message` is the only writer. A session id becomes a
  directory name, so it is validated at that boundary rather than trusted.
- Anything adding a second `ResumeTriggerAdapter` implementation must keep the
  gate check first, before its own harness client is touched.

## Consequences

- **This is a respawn, not an injection, and the naming says so.** #1889's
  spike concluded there is no code path that injects a message into a running
  `claude --bg` process, and `NativeDaemonClient` exposes no such method. The
  trigger therefore composes exactly what `resume_session`'s dead-surface
  branch already does. The cost is honesty about what the feature is; the
  benefit is that it reuses a proven path rather than inventing a shaky one.
- **No `daemon.stop()` call.** `grep -n "\.stop(" src/cw/session.py` returns
  nothing: the branch being reused has never needed a stop, for any session it
  has ever respawned. The gate is what makes that safe — a paused session is
  by definition not concurrently mid-task. A gate-eligible session that
  nevertheless still holds a live roster entry respawns without a preceding
  stop, which is pre-existing accepted behavior on that branch, not a new risk.
- **The live case stays broken until #2255.** Pre-#2255, a `RUNNING`/`ACTIVE`
  session's message queues and is reported as not yet delivered. This is the
  common outcome, which is exactly why it is a warning and exit 0 rather than
  an error.
- **No busy/idle signal is invented.** `SessionStatus` (`ACTIVE`, `IDLE`,
  `BACKGROUNDED`, `COMPLETED`, `TIMED_OUT`) carries nothing that distinguishes
  "mid-tool-call" from "paused, safe to interrupt," and neither does any
  derived signal in `src/cw/session.py`. Designing that signal is #2255's
  first problem, and this ADR is the context it starts from.
- **One production harness, one adapter.** The `ResumeTriggerAdapter` Protocol
  boundary exists with a single real implementation. codex is wired in this
  repo only as a one-shot review subprocess, not a resumable worker session, so
  a second real adapter would mean inventing worker-spawn machinery that does
  not exist. The boundary makes that a follow-up rather than a rewrite.
- **Disk cost is negligible and unbounded in principle.** The mailbox is never
  pruned. A session's inbox holds only operator-typed messages, so growth is
  human-rate; if that ever stops being true it needs the retention treatment
  `cw event prune` already gives the global bus.

## Alternatives considered

- **Parameterize `cw.events` by session.** Rejected: that inbox is the
  fleet-wide orchestrator bus, one file and one lock for every session, whose
  producers correlate through `ticket_id`. A session-partitioned mailbox routed
  through it would serialize unrelated sessions' appends behind one lock and
  force every reader to filter a fleet-wide stream — the opposite of a mailbox
  that is greppable per session in a post-mortem.
- **A second worker-side marker for "I am awaiting an operator."** Rejected as
  duplication: `cw signal-park` plus `_route_stopped_without_sentinel`'s
  BLOCKED_ON_USER routing already *is* that signal. #2212 makes the existing
  signal visible in `cw queue peek`, where it was not, rather than minting a
  parallel one.
- **Auto-unpark the dev-queue row on send.** Rejected: the acceptance text
  describes the session resuming, not dev-queue disposition automation, and
  transitioning a row's status on an unrelated trigger would invite a fresh
  ambiguity of the same flavor #2212 exists to remove.
- **Fail the command when the message cannot be delivered.** Rejected: see
  Invariant 3. Pre-#2255 that would make the common case look like an error.

## Referenced by

- #2212, #2255, #1889, ADR-0005, ADR-0011
