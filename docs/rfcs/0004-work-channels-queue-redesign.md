# RFC 0004 — Work Channels: Per-Focus Queues & Orchestration

| Field | Value |
|---|---|
| Status | Draft — design |
| Owner | @mattwwarren |
| Date | 2026-05-30 |
| Supersedes | none (extends the dispatch model) |
| Related | RFC 0002 (orchestrator on Agent SDK), ADR 0004 (stage events), `docs/events.md` |
| Branch | `claude/queue-system-redesign-7vG0t` |

## Summary

Today the autonomous dispatch queue partitions work by a single key:
**`client`**. There is one logical ticket channel per client, one
concurrency cap per client (`per_client_max_parallel`, default `1`), and
the "orchestration session" is an implicit `parent` session ID threaded
through the dispatch loop. This is insufficient: a client routinely has
**multiple independent streams of work** that should be prioritized,
preempted, and executed independently of one another.

This RFC promotes **focus** to a first-class partition by introducing
**work channels**: operator-declared lanes within a client, each owning
its own queue ordering, its own concurrency budget, and its own
long-lived orchestration session. The partition key changes from
`client` to **`(client, channel)`**. A per-client *ceiling* bounds the
sum of concurrent workers across all of a client's channels, and a
**non-destructive scheduler** lets a newly-arrived high-priority channel
claim free slots ahead of lower-priority work *without killing in-flight
sessions*.

## Motivation — where the limit lives today

The constraint is concrete and lives in two places:

1. **`dev_queue.py` / `DevQueueStore`** — a single global `dev_queue.json`
   holding every `TicketTask`. The only grouping field that affects
   routing is `task.client`. `TicketTask.scope_hint` and
   `DispatchPlan.grouping_hints` exist but are *not* consumed as a
   partition.
2. **`dispatch.py:dispatch_tick`** (lines ~130–276) — loops over clients,
   counts running DAEMON sessions per client, and gates new spawns on
   `config.per_client_max_parallel[client]` (default `1`). All of a
   client's tickets compete in one flat priority/FIFO pool.

Consequences the operator hits in practice:

- **No independent prioritization.** A new urgent batch and existing
  steady-state work share one ordered queue. Bumping the urgent batch
  reshuffles the single pool.
- **Head-of-line blocking.** With `per_client_max_parallel = 1`, an
  urgent ticket waits behind whatever is already running, even when the
  two streams never touch the same code.
- **No durable "focus."** The orchestration that steers a stream of work
  is just a `parent` session ID (`run_dispatch_loop(parent=…)`); there is
  no first-class entity that *owns* a stream of work as a context/lens.

> Operator framing (verbatim intent): *"A new batch of work comes in that
> needs to take priority over existing work but shouldn't block the
> current execution group. If there are lots of resources available then
> those two individual groups can also run alongside each other."*

That sentence is the spec for the scheduler (see
[Scheduling](#scheduling-the-crux)).

## Design decisions (locked)

These were decided up front and constrain everything below:

| # | Decision | Choice |
|---|---|---|
| D1 | How a channel is defined | **Operator-declared lanes** per client (named, configured). Tickets routed to a channel at enqueue time. No inference. |
| D2 | Where the concurrency cap lives | **Per-channel budget + per-client ceiling.** Each channel has `max_parallel`; the client ceiling caps the *sum*. |
| D3 | Orchestration lifecycle | **Long-lived per channel.** A durable context/lens that owns a channel's queue and workers and can **hand off** work to other sessions. |
| D4 | Git isolation between channels | **None.** All workers branch off and PR into the client's default branch, exactly as today. Channels are a *scheduling/organizational* boundary, not an integration boundary. |

D4 is load-bearing for scope: because channels do not need their own base
branches, this RFC touches **scheduling and state**, not worktree/branch
management. `worktree.py` is unchanged.

## Core model

### The `(client, channel)` partition

```
client: lgbtqplus-map
├── channel: default      (max_parallel=1, priority=0)   ← steady-state
├── channel: search-rewrite (max_parallel=2, priority=10) ← active feature
└── channel: security-hotfix (max_parallel=1, priority=100, paused=false)
                                                          ← preempting batch
client ceiling: 3 concurrent workers
```

A **work channel** is an operator-declared lane within a single client:

```python
class ChannelConfig(BaseModel):
    """An operator-declared work lane within a client."""
    name: str                      # unique within the client
    max_parallel: int = 1          # this channel's own slot budget
    priority: int = 0              # higher = first dibs on the ceiling
    paused: bool = False           # operator can freeze new dispatch
    description: str = ""
```

Properties:

- **Unique within a client.** `(client, name)` is the identity.
- **Owns queue ordering.** Tickets in a channel are ordered by
  `priority` then FIFO *within that channel only* — independent of other
  channels.
- **Owns a concurrency budget** (`max_parallel`).
- **Bound to one long-lived orchestration session** (see
  [Orchestration sessions](#orchestration-sessions-first-class)).

### Where channels are declared

Channel *definitions* live in **`clients.yaml`** (the client owns its
lanes); the per-client *ceiling* stays in **`orchestrator.yaml`** where
concurrency policy already lives:

```yaml
# clients.yaml
clients:
  lgbtqplus-map:
    repo_path: ~/code/lgbtqplus-map
    branch: main
    channels:
      - name: default
        max_parallel: 1
        priority: 0
      - name: search-rewrite
        max_parallel: 2
        priority: 10
```

```yaml
# orchestrator.yaml
# per_client_max_parallel keeps its name but its MEANING shifts from
# "the cap" to "the ceiling" (sum across channels). Backward compatible:
# a client with a single implicit `default` channel behaves identically.
per_client_max_parallel:
  lgbtqplus-map: 3
default_max_parallel: 1   # default ceiling for unlisted clients
```

## Scheduling (the crux)

The scheduler runs each dispatch tick, **per client**, and answers: given
the ceiling and the per-channel budgets, which channels get the free
slots this tick?

### Invariants

1. **Non-destructive preemption.** Priority only governs *allocation of
   free slots*. It **never** terminates an in-flight worker. A high-prio
   channel that arrives mid-flight gets the *next* freed slots, not a
   forced eviction. (This is the "shouldn't block the current execution
   group" requirement.)
2. **Concurrent when resources allow.** If `ceiling ≥ Σ channel.max_parallel`,
   every channel runs at its own cap simultaneously — zero contention.
3. **Priority under contention.** If demand exceeds the ceiling, higher-
   priority channels are filled first; leftover slots cascade down.
4. **Saturation cap.** No channel ever exceeds its own `max_parallel`,
   regardless of how much ceiling is free.

### Algorithm (per client, per tick)

```
running[ch]   = count of ACTIVE/IDLE DAEMON sessions in channel ch
pending[ch]   = count of PENDING tasks in channel ch (skip if ch.paused)
available     = client_ceiling - Σ running[ch]        # free slots
channels_sorted = channels ordered by (priority desc, fairness tiebreak)

for ch in channels_sorted:
    if available <= 0: break
    want  = min(ch.max_parallel - running[ch], pending[ch])
    grant = min(want, available)
    spawn grant workers from ch's queue (priority then FIFO)
    available -= grant
```

**Fairness tiebreak** (channels of equal priority): least-recently-
dispatched first (round-robin), to prevent starvation among equals.
See [Open questions](#open-questions) for strict-priority vs weighted
alternatives.

### Worked example

Ceiling = 3. Channels: `default`(cap1,p0), `search`(cap2,p10),
`hotfix`(cap1,p100). All have pending work, nothing running yet.

- `available = 3`
- `hotfix` (p100): want=min(1,…)=1, grant=1 → available=2
- `search` (p10): want=min(2,…)=2, grant=2 → available=0
- `default` (p0): available=0 → granted 0 this tick

Result: hotfix + search run immediately and concurrently; default waits.
When `search` finishes a ticket, the freed slot is re-evaluated next tick
and (since hotfix is satisfied at its cap) flows to the next channel by
priority. **No running session was killed** to make room for hotfix —
hotfix simply took the free slot first.

If the ceiling were 4 instead of 3, all three channels would run at once
(1+2+1 = 4 ≤ 4) — invariant 2.

## State & schema changes

### `TicketTask` gains a `channel`

```python
class TicketTask(BaseModel):
    ticket_id: str
    client: str
    channel: str = "default"   # NEW — routing lane within the client
    priority: int = 0          # now scoped to the channel's queue
    ...
```

Single global `dev_queue.json` is **retained** (Option A). Adding a field
is far less disruptive than per-channel files: the existing file lock
still gives atomic cross-channel operations (reprioritize, move ticket
between channels) under one lock, and the dedup/stale-event machinery in
`dispatch.py` keeps working with a one-field grouping change.

- **Dedup stays `(client, ticket_id)`.** A ticket belongs to exactly one
  channel. Re-routing is an explicit `cw dev-queue move` operation, not a
  second insert.

### Dev-queue schema v3 + migration

Bump `DEV_QUEUE_SCHEMA_VERSION = 2 → 3`. In `migrate_dev_queue`:

```python
def _fill_channel_default(task_raw: dict) -> None:
    if "channel" not in task_raw:
        task_raw["channel"] = "default"
```

Every legacy task is stamped `channel="default"`. Combined with the
implicit-default-channel rule below, **existing deployments upgrade with
zero behavior change.**

### `ChannelRegistry` (config) + `ClientConfig.channels`

```python
class ClientConfig(BaseModel):
    ...
    channels: list[ChannelConfig] = Field(default_factory=list)

    @property
    def effective_channels(self) -> list[ChannelConfig]:
        """Declared channels, or a synthesized `default` lane.

        When no channels are declared, return a single `default` channel
        whose max_parallel mirrors the client's pre-RFC cap so behavior
        is identical to the one-lane world.
        """
        if self.channels:
            return self.channels
        return [ChannelConfig(name="default")]
```

## Orchestration sessions (first-class)

D3 makes an orchestration session a **durable context bound to a
channel** — a lens the operator (or a steering Claude) drives work
through, and which can hand off to other sessions via the existing
`/handoff` pipeline.

### Minimal changes

- **`Session.channel: str | None`** — set on the orchestration session
  and propagated to its workers. `None` for legacy/unscoped sessions.
- **New `SessionPurpose.ORCHESTRATE`** — distinguishes the steering
  session from `IMPL/IDEA/DEBT` workers. Orchestration sessions are
  `origin=USER` (interactive) by default; their dispatched workers stay
  `origin=DAEMON`.
- **Binding.** A channel maps to at most one live orchestration session.
  Resolve via state: the ACTIVE/IDLE session with
  `purpose=ORCHESTRATE`, matching `client`, matching `channel`.
- **Linkage is unchanged.** Workers carry
  `parent_session_id = orchestrator.id` and the orchestrator's
  `worker_session_ids` is appended, reusing the existing
  `orchestrator_workers` / `orchestrator_parent` plumbing in
  `orchestrate.py`. The "parent" threaded through `run_dispatch_loop`
  becomes "the channel's orchestration session."

### Lifecycle

```
cw orchestrate start <client> --channel search-rewrite
  → spawns (or rebinds) the long-lived ORCHESTRATE session for the lane
  → that session enqueues / reprioritizes / dispatches within its channel
  → workers spawn under the per-channel budget + client ceiling
  → session persists across dispatch waves; survives worker churn
  → can /handoff to a fresh session without losing the channel binding
```

The dispatcher (daemon or `cw dev-queue run`) remains the thing that
*actually spawns* workers; the orchestration session is the **owner and
driver** of a channel's intent, not necessarily the process running the
tick loop.

## Dispatcher changes (`dispatch.py`)

- `_claim_next_pending(client, *, channel, priority_ticket_ids)` gains a
  `channel` filter; ordering becomes priority-then-FIFO *within the
  channel*.
- `dispatch_tick` restructures its per-client body into the
  [allocation algorithm](#algorithm-per-client-per-tick): compute
  `running[ch]`/`pending[ch]`, derive `available` from the ceiling, walk
  channels by priority, spawn grants. The freshness gate, spawn
  error-revert (issue #149), and `session_id` stamping (issue #97) are
  preserved per spawn.
- Spawned workers get `channel` stamped on both the `Session` and carried
  in `SESSION_SPAWNED` payloads.

## Events (`events.py`, `models.py`)

- Add `"channel"` to the payloads of `TICKET_ENQUEUED`,
  `SESSION_SPAWNED`, `SESSION_COMPLETED`, `TICKET_NEEDS_SYNC`.
- New optional event types (telemetry / dashboard only):
  `CHANNEL_CREATED`, `CHANNEL_PAUSED`, `CHANNEL_RESUMED`. Not required for
  correctness; they make the channel lifecycle auditable like stage
  events (ADR 0004).
- `consume_completed_sessions` / `_apply_events_to_store` need no logic
  change — they already match on `ticket_id` + `session_id`; `channel`
  rides along on the task.

## Reconciler interaction (`reconcile.py`)

- RUNNING→PENDING reverts preserve `channel` automatically (it lives on
  the task). No change to the revert path.
- The **transient-outage guard** stays *per client* (mass-reap
  suppression is a client-level safety net; channels do not subdivide
  it).
- Phantom detection is by session, unaffected by channel.

## CLI surface

```
# Channel administration (writes clients.yaml)
cw channel add <client> <name> [--max-parallel N] [--priority P] [--description ...]
cw channel ls  <client>
cw channel rm  <client> <name>
cw channel pause   <client> <name>
cw channel resume  <client> <name>

# Routing at enqueue (defaults to `default` channel)
cw dev-queue add <ticket> [--client C] --channel <name>
cw dev-queue move <ticket> --client C --to <channel>     # re-route
cw dev-queue ls  [--client C] [--channel <name>]

# Orchestration
cw orchestrate start  <client> --channel <name>          # spawn/bind lens
cw orchestrate status [--client C] [--channel <name>]    # grouped by channel
```

- `cw orchestrate status` and `cw dashboard` group running sessions and
  pending tickets under `(client → channel)` and show, per channel:
  `running/cap`, `pending`, `priority`, `paused`, bound orchestrator id.

## Backward compatibility

This is the make-or-break property. The upgrade is **silent and
behavior-preserving**:

| Surface | Pre-RFC | Post-RFC with no channels declared |
|---|---|---|
| `TicketTask` | no `channel` | migrated to `channel="default"` |
| Client with no `channels:` block | one flat pool | one synthesized `default` channel |
| `per_client_max_parallel[c] = N` | cap of N | ceiling N over a single `default` channel ⇒ cap N (identical) |
| Dispatch ordering | priority then FIFO | same, within `default` |
| Orchestration | implicit `parent` ID | implicit `parent` ID (ORCHESTRATE purpose opt-in) |

No operator action is required to keep current behavior. Channels are
strictly additive.

## Phased rollout

- **Phase 1 — Data model + migration.** Add `TicketTask.channel`,
  `ChannelConfig`, `ClientConfig.channels`, `effective_channels`, schema
  v3 migration. No behavior change (everything is `default`). Tests:
  migration round-trip, single-default equivalence.
- **Phase 2 — Scheduler.** Rework `dispatch_tick` to the per-client
  allocation algorithm with the ceiling. Channel-aware
  `_claim_next_pending`. Tests: the worked example, non-destructive
  preemption, saturation cap, concurrent-when-roomy, starvation/fairness.
- **Phase 3 — CLI + routing.** `cw channel *`, `--channel` on
  `dev-queue add`, `dev-queue move`, channel-grouped `status`.
- **Phase 4 — First-class orchestration.** `SessionPurpose.ORCHESTRATE`,
  `Session.channel`, `cw orchestrate start --channel`, channel binding
  resolution, dashboard grouping. Channel lifecycle events.

Each phase is independently shippable; Phase 1 alone is safe to merge
because it changes no behavior.

## Naming — collision with MCP "channels"

`channel` is **already used** in this codebase for MCP push transport
(`cw_pr_events_channel.py`, `cw_queue_events_channel.py`, RFC 0002's
`--channels` flag). To avoid ambiguity:

- The new concept is **"work channel"** in prose and docs.
- In code, the field/CLI term is `channel` *within the dev-queue /
  dispatch domain* (`TicketTask.channel`, `cw channel …`,
  `--channel`). MCP transport channels are always referenced by their
  server names (`cw-pr-events`, `cw-queue-events`), never bare
  "channel", so the domains don't overlap at the symbol level.
- **Alternative** if the collision proves confusing in review: rename the
  concept to **"lane"** (`TicketTask.lane`, `cw lane …`). Cheap to do
  before Phase 1 lands; expensive after. Flagged as an open question.

## Open questions

1. **Fairness policy.** Round-robin tiebreak among equal-priority
   channels (proposed) vs weighted fair-share (each channel gets a slot
   proportion) vs strict priority with explicit starvation alarms. Which
   matches operator intent when many channels contend for a small
   ceiling?
2. **Naming: `channel` vs `lane`.** Resolve before Phase 1 to avoid a
   rename migration. (See above.)
3. **Ceiling source of truth.** Keep reusing `per_client_max_parallel`
   (meaning shifts to "ceiling"), or introduce an explicit
   `per_client_ceiling` and deprecate the old key over a release?
4. **Channel auto-provisioning.** Should `cw dev-queue add --channel X`
   auto-create channel `X` with defaults if undeclared, or hard-fail and
   force `cw channel add` first? (D1 says operator-declared → lean
   hard-fail, but auto-create-with-warning may be friendlier.)
5. **Orchestration ↔ dispatcher relationship.** Does each channel's
   ORCHESTRATE session run its *own* tick loop scoped to its channel, or
   does one global dispatcher serve all channels and the ORCHESTRATE
   session merely owns intent? (Proposed: one global dispatcher; the
   session owns intent — simpler, single ceiling enforcement point.)
6. **Legacy `queue.py` (`QueueStore`).** The older per-client
   inter-session message queue is untouched here. Does it also want a
   channel dimension, or does it remain a separate concern? (Proposed:
   out of scope.)
7. **Cross-channel ticket moves mid-flight.** If a ticket is RUNNING when
   `dev-queue move` is issued, do we forbid the move, or allow re-tagging
   the channel without disturbing the running session?

## References

- `src/cw/dispatch.py` — `dispatch_tick`, `_claim_next_pending`,
  `run_dispatch_loop` (the per-client cap to be generalized)
- `src/cw/dev_queue.py` — `DevQueueStore`, `add_ticket`, schema migration
- `src/cw/models.py` — `TicketTask`, `OrchestratorConfig`,
  `ClientConfig`, `SessionPurpose`, `Session`
- `src/cw/orchestrate.py` — `orchestrator_workers`, `orchestrator_parent`
  (parent/worker linkage reused for channel orchestration)
- `src/cw/reconcile.py` — RUNNING→PENDING revert + transient-outage guard
- RFC 0002 — Agent SDK orchestrator + MCP channels (naming collision)
- ADR 0004 — stage events on the orchestrator bus (event-shape precedent)
- `docs/events.md` — orchestrator event bus
- `ROADMAP.md` v4 (Autonomous Delegation), v6 (JARVIS)
