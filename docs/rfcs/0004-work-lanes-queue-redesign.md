# RFC 0004 — Work Lanes: Per-Focus Queues & Tunable Concurrency

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
**`client`**. There is one logical ticket lane per client, one
concurrency cap per client (`per_client_max_parallel`, default `1`), and
the "orchestration session" is an implicit `parent` session ID threaded
through the dispatch loop. This is insufficient: a client routinely has
**multiple independent streams of work** that should be prioritized,
preempted, and executed independently of one another.

This RFC promotes **focus** to a first-class partition by introducing
**lanes**: operator-declared streams of work within a client, each owning
its own queue ordering, its own concurrency budget, and its own
long-lived orchestration session. The partition key changes from
`client` to **`(client, lane)`**. Concurrency is governed by **two flat,
named, runtime-tunable knobs** — `max_parallel_clients` (how many clients
run at once) and `per_client_ceiling` (how much parallelism within a
client) — plus a per-lane `max_parallel` *shape*. A **non-destructive
scheduler** lets a newly-arrived high-priority lane claim free slots ahead
of lower-priority work *without killing in-flight sessions*.

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
   client's tickets compete in one flat priority/FIFO pool. There is **no
   global cap on how many clients dispatch at once** — every client is
   always eligible.

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

| # | Decision | Choice |
|---|---|---|
| D1 | How a lane is defined | **Operator-declared** per client (named, configured). Tickets routed to a lane at enqueue time. No inference. |
| D2 | Where the concurrency cap lives | **Per-lane budget + per-client ceiling**, under a global parallel-clients cap. |
| D3 | Orchestration lifecycle | **Long-lived per lane.** A durable context/lens that owns a lane's queue and workers and can **hand off** work to other sessions. |
| D4 | Git isolation between lanes | **None.** All workers branch off and PR into the client's default branch, exactly as today. Lanes are a *scheduling/organizational* boundary, not an integration boundary. |

D4 is load-bearing for scope: because lanes do not need their own base
branches, this RFC touches **scheduling and state**, not worktree/branch
management. `worktree.py` is unchanged.

## Core model

### The `(client, lane)` partition

```
client: lgbtqplus-map           (per_client_ceiling = 3)
├── lane: default        (max_parallel=1, priority=0)    ← steady-state
├── lane: search-rewrite (max_parallel=2, priority=10)   ← active feature
└── lane: security-hotfix(max_parallel=1, priority=100)  ← preempting batch
```

A **lane** is an operator-declared stream of work within a single client:

```python
class LaneConfig(BaseModel):
    """An operator-declared work lane within a client."""
    name: str                      # unique within the client
    max_parallel: int = 1          # this lane's own slot budget (shape)
    priority: int = 0              # higher = first dibs on the ceiling
    paused: bool = False           # operator can freeze new dispatch
    reap_policy: str = "signal_only"  # "signal_only" | "auto" — see ADR 0006
    description: str = ""
```

`reap_policy` (added per **ADR 0006**) governs whether `reconcile()` may
*automatically* reap a distressed session in this lane (`auto`) or must
only *signal* and route the task to `BLOCKED_ON_USER` for the lane's
orchestration session to authorize (`signal_only`, the default). It
resolves lane → global default → `signal_only`.

Properties:

- **Unique within a client.** `(client, name)` is the identity.
- **Owns queue ordering.** Tickets in a lane are ordered by `priority`
  then FIFO *within that lane only* — independent of other lanes.
- **Owns a concurrency budget** (`max_parallel`).
- **Bound to one long-lived orchestration session** (see
  [Orchestration sessions](#orchestration-sessions-first-class)).

### Where lanes are declared

Lane *definitions* live in **`clients.yaml`** (the client owns its lanes);
*concurrency budgets* live in **`orchestrator.yaml`** where concurrency
policy already lives:

```yaml
# clients.yaml — the SHAPE (what lanes exist, their relative priority)
clients:
  lgbtqplus-map:
    repo_path: ~/code/lgbtqplus-map
    branch: main
    lanes:
      - name: default
        max_parallel: 1
        priority: 0
      - name: search-rewrite
        max_parallel: 2
        priority: 10
```

## Concurrency knobs & runtime tuning

This is the part designed for a future observability/auto-tuning
platform. There are exactly **two tunable knobs** (flat, named — no nested
"parallelism of parallelism"), plus the per-lane shape:

| Knob | Field | Scope | Tunable at runtime? | Default = today's behavior |
|---|---|---|---|---|
| **A — parallel clients** | `max_parallel_clients` | global | ✅ | `null` → unbounded (all clients eligible, as now) |
| **B — parallel within a client** | `per_client_ceiling` (`{client: int}` + `default_ceiling`) | per client | ✅ | mirror today's `per_client_max_parallel` |
| shape (not a tuning knob) | `lane.max_parallel` | per lane | operator-declared | — |

The platform tunes **budgets** (A, B); the operator declares **shape**
(lanes). Knob A does **not exist today** — it is introduced here with a
sentinel default (`null` = unbounded) so adding it changes nothing until a
value is set.

### Setting the knobs up *today*, before the tuner exists

The requirement: a future platform must be able to **discover, read,
write, and apply** these values. Four properties deliver that — and three
of the four can land now, decoupled from building the global scheduler:

```yaml
# orchestrator.yaml — the BUDGETS (the tunable surface)
concurrency:
  max_parallel_clients: null          # knob A — null = unbounded (today)
  default_ceiling: 1                  # knob B default
  per_client_ceiling:                 # knob B per-client overrides
    lgbtqplus-map: 3
```

**1. Sentinel defaults = current behavior.** Define both knobs now.
`max_parallel_clients: null` and `per_client_ceiling` mirroring the old
cap mean an upgrade is a no-op. The platform can *write* a value today; it
simply has no effect until the scheduler tier that honors knob A ships.
Knob existence is decoupled from knob enforcement.

**2. Layered resolution** — so the tuner never clobbers operator config:

```
1. runtime override store   (concurrency_overrides.json — the platform owns this)   ← highest
2. env var                  (CW_MAX_PARALLEL_CLIENTS, CW_CLIENT_CEILING__<client>)
3. orchestrator.yaml        (operator-declared)
4. built-in default         (max_parallel_clients=null; default_ceiling=1)
```

The override store is a separate lock-protected JSON file the platform
writes to (same file-lock pattern as `dev_queue.json`). Clearing a key
reverts to declared config, so a bad auto-tune is one delete away from
"back to what the human set." The operator's YAML stays pristine.

**3. Hot-reload (the one behavioral code change).** Today
`run_dispatch_loop` calls `load_orchestrator_config()` **once**, *before*
`while True` (`dispatch.py:461`) — so a config write requires a restart.
Move the resolved-config read to the **top of each tick**. A platform
write then takes effect within one `tick_interval_seconds`, live.

**4. A stable read/write interface.** `cw config concurrency get|set`
operating over the resolved chain (writing to the override store), so the
platform talks to an API, not a YAML parser:

```
cw config concurrency get                         # resolved values + source layer
cw config concurrency set max_parallel_clients 4  # writes override store
cw config concurrency set per_client_ceiling lgbtqplus-map 5
cw config concurrency clear max_parallel_clients  # revert to declared
```

### The observability counterpart

A tuner needs the **read-side** to know which way to turn each knob.
Each tick, emit current knob values *and* utilization:

- via `cw orchestrate status --json`: `max_parallel_clients`,
  `active_clients`, and per client `{ceiling, running}` + per lane
  `{running, cap, pending, priority, paused}`.
- via a `CONCURRENCY_SAMPLE` orchestrator event (same bus as stage events,
  ADR 0004) so samples are durable/auditable.

Knobs (write-side) + samples (read-side) together form the control surface
a platform needs; neither alone is sufficient.

## Scheduling (the crux)

The scheduler runs each dispatch tick. **Tier 1 (global):** select up to
`max_parallel_clients` clients that have eligible work. **Tier 2 (per
selected client):** allocate that client's ceiling across its lanes.

### Invariants

1. **Non-destructive preemption.** Priority only governs *allocation of
   free slots*. It **never** terminates an in-flight worker. A high-prio
   lane that arrives mid-flight gets the *next* freed slots, not a forced
   eviction. (This is the "shouldn't block the current execution group"
   requirement.)
2. **Concurrent when resources allow.** If `ceiling ≥ Σ lane.max_parallel`,
   every lane runs at its own cap simultaneously — zero contention.
3. **Priority under contention.** If demand exceeds the ceiling, higher-
   priority lanes are filled first; leftover slots cascade down.
4. **Saturation cap.** No lane ever exceeds its own `max_parallel`,
   regardless of how much ceiling is free.

### Algorithm

```
# Tier 1 — global
candidate_clients = clients with ≥1 eligible (PENDING, unpaused) ticket
selected = first max_parallel_clients of candidate_clients
           (ordered by fairness: least-recently-dispatched first;
            null max_parallel_clients ⇒ select all)

# Tier 2 — per selected client
for client in selected:
    running[lane] = ACTIVE/IDLE DAEMON sessions in that lane
    pending[lane] = PENDING tasks in lane (skip if lane.paused)
    available     = per_client_ceiling[client] - Σ running[lane]
    for lane in lanes ordered by (priority desc, fairness tiebreak):
        if available <= 0: break
        want  = min(lane.max_parallel - running[lane], pending[lane])
        grant = min(want, available)
        spawn grant workers from lane's queue (priority then FIFO)
        available -= grant
```

**Fairness tiebreak** (equal priority): least-recently-dispatched first
(round-robin), to prevent starvation. See
[Open questions](#open-questions) for weighted alternatives.

### Worked example (Tier 2)

Ceiling = 3. Lanes: `default`(cap1,p0), `search`(cap2,p10),
`hotfix`(cap1,p100). All have pending work, nothing running yet.

- `available = 3`
- `hotfix` (p100): want=1, grant=1 → available=2
- `search` (p10): want=2, grant=2 → available=0
- `default` (p0): available=0 → granted 0 this tick

hotfix + search run immediately and concurrently; default waits. When
`search` finishes a ticket, the freed slot is re-evaluated next tick and
flows to the next lane by priority. **No running session was killed** to
make room for hotfix — hotfix simply took the free slot first. With
ceiling 4, all three run at once (1+2+1 ≤ 4) — invariant 2.

## State & schema changes

### `TicketTask` gains a `lane`

```python
class TicketTask(BaseModel):
    ticket_id: str
    client: str
    lane: str = "default"      # NEW — routing lane within the client
    priority: int = 0          # now scoped to the lane's queue
    ...
```

Single global `dev_queue.json` is **retained** (Option A). Adding a field
is far less disruptive than per-lane files: the existing file lock still
gives atomic cross-lane operations (reprioritize, move ticket between
lanes) under one lock, and the dedup/stale-event machinery in
`dispatch.py` keeps working with a one-field grouping change.

- **Dedup stays `(client, ticket_id)`.** A ticket belongs to exactly one
  lane. Re-routing is an explicit `cw dev-queue move` operation.

### Dev-queue schema v3 + migration

Bump `DEV_QUEUE_SCHEMA_VERSION = 2 → 3`. In `migrate_dev_queue`:

```python
def _fill_lane_default(task_raw: dict) -> None:
    if "lane" not in task_raw:
        task_raw["lane"] = "default"
```

Every legacy task is stamped `lane="default"`. Combined with the
implicit-default-lane rule below, **existing deployments upgrade with zero
behavior change.**

### `ClientConfig.lanes`

```python
class ClientConfig(BaseModel):
    ...
    lanes: list[LaneConfig] = Field(default_factory=list)

    @property
    def effective_lanes(self) -> list[LaneConfig]:
        """Declared lanes, or a synthesized `default` lane.

        When no lanes are declared, return a single `default` lane so
        behavior is identical to the one-lane world.
        """
        return self.lanes or [LaneConfig(name="default")]
```

## Orchestration sessions (first-class)

D3 makes an orchestration session a **durable context bound to a lane** —
a lens the operator (or a steering Claude) drives work through, and which
can hand off to other sessions via the existing `/handoff` pipeline.

### Minimal changes

- **`Session.lane: str | None`** — set on the orchestration session and
  propagated to its workers. `None` for legacy/unscoped sessions.
- **New `SessionPurpose.ORCHESTRATE`** — distinguishes the steering
  session from `IMPL/IDEA/DEBT` workers. Orchestration sessions are
  `origin=USER` (interactive) by default; their dispatched workers stay
  `origin=DAEMON`.
- **Binding.** A lane maps to at most one live orchestration session:
  the ACTIVE/IDLE session with `purpose=ORCHESTRATE` matching
  `(client, lane)`.
- **Linkage is unchanged.** Workers carry
  `parent_session_id = orchestrator.id` and the orchestrator's
  `worker_session_ids` is appended, reusing the existing
  `orchestrator_workers` / `orchestrator_parent` plumbing in
  `orchestrate.py`. The "parent" threaded through `run_dispatch_loop`
  becomes "the lane's orchestration session."

### Lifecycle

```
cw orchestrate start <client> --lane search-rewrite
  → spawns (or rebinds) the long-lived ORCHESTRATE session for the lane
  → that session enqueues / reprioritizes / dispatches within its lane
  → workers spawn under the per-lane budget + client ceiling + global cap
  → session persists across dispatch waves; survives worker churn
  → can /handoff to a fresh session without losing the lane binding
```

The dispatcher (daemon or `cw dev-queue run`) remains the thing that
*actually spawns* workers; the orchestration session is the **owner and
driver** of a lane's intent.

## Dispatcher changes (`dispatch.py`)

- Resolve concurrency config **at the top of each tick** (hot-reload, see
  knobs section), not once before the loop.
- Add the **Tier-1 client selection** gated on `max_parallel_clients`.
- `_claim_next_pending(client, *, lane, priority_ticket_ids)` gains a
  `lane` filter; ordering becomes priority-then-FIFO *within the lane*.
- Restructure the per-client body into the Tier-2 allocation algorithm.
  The freshness gate, spawn error-revert (issue #149), and `session_id`
  stamping (issue #97) are preserved per spawn.
- Spawned workers get `lane` stamped on the `Session` and carried in
  `SESSION_SPAWNED` payloads.

## Events (`events.py`, `models.py`)

- Add `"lane"` to the payloads of `TICKET_ENQUEUED`, `SESSION_SPAWNED`,
  `SESSION_COMPLETED`, `TICKET_NEEDS_SYNC`.
- New event types: `LANE_CREATED`, `LANE_PAUSED`, `LANE_RESUMED`
  (lifecycle audit) and `CONCURRENCY_SAMPLE` (tuning telemetry).
- `consume_completed_sessions` / `_apply_events_to_store` need no logic
  change — they match on `ticket_id` + `session_id`; `lane` rides along.

## Reconciler interaction (`reconcile.py`)

- RUNNING→PENDING reverts preserve `lane` automatically (it lives on the
  task) — the lane-preservation *mechanics* are unchanged.
- **Whether the revert fires automatically is gated by `lane.reap_policy`
  (ADR 0006).** Under the default `signal_only`, `reconcile()` detects the
  phantom/budget/idle condition, emits `SESSION_REAP_PROPOSED`, and routes
  the task to `BLOCKED_ON_USER` instead of reverting — the lane's
  ORCHESTRATE session is the reap authority. Under `auto`, the revert fires
  as today. The Tier-2 allocator counts a `signal_only`-blocked session as
  **occupying a slot**, so a stalled lane is not over-spawned.
- The **transient-outage guard** stays *per client* (mass-reap
  suppression is a client-level safety net; lanes do not subdivide it).
- Phantom detection is by session, unaffected by lane.

## CLI surface

```
# Lane administration (writes clients.yaml)
cw lane add <client> <name> [--max-parallel N] [--priority P] [--description ...]
cw lane ls  <client>
cw lane rm  <client> <name>
cw lane pause   <client> <name>
cw lane resume  <client> <name>

# Concurrency knobs (writes the override store; resolved chain)
cw config concurrency get
cw config concurrency set max_parallel_clients <N|null>
cw config concurrency set per_client_ceiling <client> <N>
cw config concurrency clear <key> [<client>]

# Routing at enqueue (defaults to `default` lane)
cw dev-queue add <ticket> [--client C] --lane <name>
cw dev-queue move <ticket> --client C --to <lane>
cw dev-queue ls  [--client C] [--lane <name>]

# Orchestration
cw orchestrate start  <client> --lane <name>
cw orchestrate status [--client C] [--lane <name>] [--json]
```

`cw orchestrate status` / `cw dashboard` group by `(client → lane)` and
show per lane: `running/cap`, `pending`, `priority`, `paused`, bound
orchestrator id; and per client: `running/ceiling`; and globally:
`active_clients/max_parallel_clients`.

## Backward compatibility

The upgrade is **silent and behavior-preserving**:

| Surface | Pre-RFC | Post-RFC with nothing declared |
|---|---|---|
| `TicketTask` | no `lane` | migrated to `lane="default"` |
| Client with no `lanes:` block | one flat pool | one synthesized `default` lane |
| `per_client_max_parallel[c] = N` | cap of N | `per_client_ceiling` N over a single `default` lane ⇒ cap N (identical) |
| `max_parallel_clients` | (didn't exist) | `null` ⇒ unbounded ⇒ all clients eligible (identical) |
| Dispatch ordering | priority then FIFO | same, within `default` |

No operator action is required to keep current behavior. Lanes and the
global knob are strictly additive.

> **Migration of `per_client_max_parallel`.** The old key is read as the
> seed for `per_client_ceiling` for one release (a `model_validator`
> lifts it, mirroring the existing `default` → `default_max_parallel`
> migration in `OrchestratorConfig`), then deprecated with a warning.

## State integrity (tick correctness)

A static audit of the tick loops (`dispatch.run_dispatch_loop`,
`daemon.run_watcher_tick`) surfaced state-loss conditions that this
redesign **worsens** — every lane adds a concurrent state writer — and so
must be fixed as part of it, not after. They are catalogued here; the two
load-bearing ones (the state lock and hot-reload) are pulled into Phase 0
because the lane scheduler is neither correct nor tunable without them.

| # | Condition | Symptom | Severity | Fix |
|---|---|---|---|---|
| S1 | `sessions.json` has no lock around `load → mutate → save` (21 call sites, 9 modules). The worker Stop-hook (`wrapper.signal_*`) races the loop's `reconcile`/`spawn`. | Lost completion ⇒ ticket stuck `RUNNING`, slot leaks; lost spawn ⇒ orphan session; clobbered `TIMED_OUT` revert ⇒ no retry. **Worsens linearly with lane count.** | **P0** | `state_lock()` + `mutate_state(fn)` — see **ADR 0005**. |
| S2 | Config read **once before** `while True` in both loops (`dispatch.py:461`, `daemon.py:358`). | `tick_interval`, caps, and every RFC-0004 knob are frozen for process life — the tunable knobs are inert. | **P1** | Re-resolve config at the top of each tick. |
| S3 | `run_watcher_tick` guards only `retire_merged_prs`; `watch_prs_for_client` is unguarded. | One malformed monitor file or IO hiccup raises out of `while True` ⇒ **daemon silently exits**, all PR watching stops. | **P1** | Per-client try/except + whole-tick guard, mirroring `dispatch_tick`. |
| S4 | `ThrottleStore` (and per-client `WatcherSnapshot`) do unlocked `load → mutate → save`. | Lost throttle updates ⇒ **the duplicate PR dispatch the throttle exists to prevent.** | **P1** | File lock around the throttle/snapshot mutate. |
| S5 | Events emitted as the PR loop walks, but the snapshot is saved only after the client loop returns (`watch_prs_for_client`). | Crash between emit and save ⇒ next tick **re-emits** `PR_REVIEW_RECEIVED`/`PR_CI_FAILED` (the channel consumers aren't cursor-deduped). | **P2** | Persist snapshot per-PR, or make the channel consumer idempotent. |
| S6 | Event inbox (`events.py`): (a) cursor-id absent from inbox ⇒ `read_events` returns `[]` **forever** (silent wedge); (b) full file re-read+parsed every call every tick (unbounded O(n)); (c) reads are unlocked ⇒ a torn trailing line crashes the consuming tick. | Silent consumer stall; per-tick cost grows without bound; crash on concurrent append. | **P2** | Cursor-not-found falls back + logs; cursor-aware compaction; read under `_inbox_lock` or tolerate a partial final line. |

The state lock (S1) is recorded as its own cross-cutting invariant in
**ADR 0005** because it outlives this RFC: it governs *all* `CwState`
mutation, not just lane dispatch.

## Phased rollout

- **Phase 0 — State integrity + knob surface (no scheduler change).**
  Land the **state lock + `mutate_state()`** (S1, ADR 0005), **per-tick
  config hot-reload** (S2) in both loops, and the **per-client watcher
  guard** (S3). Then the `concurrency:` block, layered resolution,
  override store, `cw config concurrency`, and `CONCURRENCY_SAMPLE`
  telemetry. Also land the **throttle/snapshot lock** (S4) since it is the
  same trivial lock class as S3. All behavior-preserving:
  `max_parallel_clients=null` keeps dispatch identical, and the
  lock/guard/reload only change *correctness under concurrency*, not the
  happy path. *This is the foundation both the lane scheduler and a future
  tuning platform stand on.* (S5 snapshot-atomicity and S6 inbox edges are
  not on the lane critical path — track as a follow-up cleanup.)
- **Phase 1 — Data model + migration.** `TicketTask.lane`, `LaneConfig`,
  `ClientConfig.lanes`, `effective_lanes`, schema v3 migration. No
  behavior change (everything is `default`).
- **Phase 2 — Scheduler.** Tier-1 client selection + Tier-2 lane
  allocation in `dispatch_tick`. Tests: the worked example, non-
  destructive preemption, saturation cap, concurrent-when-roomy,
  starvation/fairness, `max_parallel_clients` enforcement.
- **Phase 3 — CLI + routing.** `cw lane *`, `--lane` on `dev-queue add`,
  `dev-queue move`, lane-grouped `status`.
- **Phase 4 — First-class orchestration.** `SessionPurpose.ORCHESTRATE`,
  `Session.lane`, `cw orchestrate start --lane`, binding resolution,
  dashboard grouping, lane lifecycle events.

Each phase is independently shippable. Phase 0 and Phase 1 change no
behavior and can merge first.

## Resolved decisions

- **Naming.** `lane` (not "channel") — avoids collision with MCP push
  channels (`cw_pr_events_channel.py`, RFC 0002 `--channels`). MCP
  transport channels are always referenced by server name
  (`cw-pr-events`), so the domains never overlap at the symbol level.
- **Ceiling source of truth.** Introduce explicit, flat
  `max_parallel_clients` + `per_client_ceiling` rather than overloading
  `per_client_max_parallel`. Flat named knobs beat a reinterpreted key
  for a tuning platform. The old key is migrated for one release then
  deprecated.

## Open questions

1. **Fairness policy.** Round-robin tiebreak among equal-priority lanes
   (proposed) vs weighted fair-share vs strict priority with starvation
   alarms — at both Tier 1 (clients) and Tier 2 (lanes).
2. **Lane auto-provisioning.** Should `cw dev-queue add --lane X`
   auto-create lane `X` with defaults if undeclared, or hard-fail and
   force `cw lane add` first? (D1 leans hard-fail; auto-create-with-
   warning may be friendlier.)
3. **Orchestration ↔ dispatcher relationship.** Does each lane's
   ORCHESTRATE session run its own tick loop scoped to its lane, or does
   one global dispatcher serve all lanes and the session owns intent?
   (Proposed: one global dispatcher — single enforcement point for the
   global knob.)
4. **Legacy `queue.py` (`QueueStore`).** The older per-client inter-
   session message queue is untouched here. Does it also want a lane
   dimension? (Proposed: out of scope.)
5. **Cross-lane ticket moves mid-flight.** If a ticket is RUNNING when
   `dev-queue move` is issued, forbid the move or re-tag the lane without
   disturbing the running session?
6. **Auto-tuner authority bounds.** Should the override store enforce
   min/max guardrails (e.g. `max_parallel_clients ≤ 8`) so a runaway
   tuner can't oversubscribe the host?
7. **Per-lane reap-policy granularity.** `reap_policy` is per lane (ADR
   0006). Is a per-*ticket* override ever warranted (e.g. a known-flaky
   ticket forced to `auto`), or does lane-level granularity suffice?
   (Proposed: lane-level only; revisit if a real case appears.)
8. **Client as a non-filesystem grouping.** D4 keeps `client` = a single
   workspace on disk. A SaaS service with many integration channels may
   want lanes that resolve to *different* workspaces under one logical
   client — i.e. `client` as a pure grouping and the workspace bound per
   lane. Deliberately **deferred**: decide before Phase 3 hardens the
   `(client, lane)` → one-workspace assumption. Not in scope for the
   initial lanes work.

## References

- `src/cw/dispatch.py` — `dispatch_tick`, `_claim_next_pending`,
  `run_dispatch_loop` (config loaded once at line ~461 — hot-reload target)
- `src/cw/dev_queue.py` — `DevQueueStore`, `add_ticket`, schema migration
- `src/cw/config.py` — `load_orchestrator_config` (line ~293)
- `src/cw/models.py` — `TicketTask`, `OrchestratorConfig`,
  `ClientConfig`, `SessionPurpose`, `Session`
- `src/cw/orchestrate.py` — `orchestrator_workers`, `orchestrator_parent`
- `src/cw/reconcile.py` — RUNNING→PENDING revert + transient-outage guard
- RFC 0002 — Agent SDK orchestrator + MCP channels (naming collision)
- ADR 0004 — stage events on the orchestrator bus (event-shape precedent)
- ADR 0005 — single state lock (S1; the cross-cutting state-integrity invariant)
- `src/cw/daemon.py` — `run_watcher_tick` (S2/S3/S4/S5), `src/cw/events.py` (S6)
- `docs/events.md` — orchestrator event bus
- `ROADMAP.md` v4 (Autonomous Delegation), v6 (JARVIS)
