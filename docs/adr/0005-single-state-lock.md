# All `sessions.json` mutations go through a single state lock

**Status:** Accepted — implemented (single state lock + `mutate_state`, #387/#563; released v1.1.0)
**Driven by:** RFC 0004 (work lanes — adds concurrent state writers); reconcile.py:19-25 race note (pre-existing, previously deferred)

## Decision

Every mutation of `sessions.json` (`CwState`) MUST go through a
`mutate_state()` helper that holds an exclusive file lock
(`STATE_DIR/.state.lock`, `fcntl.flock`) across the entire
`load_state → mutate → save_state` cycle. Bare `load_state(); …;
save_state()` sequences are no longer permitted outside that helper. This
brings `sessions.json` to parity with `dev_queue.json` and the event
inbox, which already lock their read-modify-write cycles.

## Problem this closes

`save_state` is atomic at the file level (`atomic_write_text` — temp +
rename), so there are no torn files. But there is no lock spanning the
read and the write, and there are **21 `load → mutate → save` call sites
across 9 modules** (`cli`, `wrapper`, `spawn`, `dispatch`, `orchestrate`,
`reconcile`, `doctor`, `session`, `config`). Concurrent writers therefore
lose updates (last-writer-wins). The two writers that are *designed* to
run simultaneously:

- **The worker Stop-hook** — `wrapper.signal_completed / signal_idle /
  signal_needs_attention`, executing in the worker's own process when it
  finishes.
- **The orchestrator loop** — `reconcile()` (every tick) and
  `spawn_create_impl()` (every dispatch), executing in the dispatcher
  process.

Interleaving (both load snapshot `S`, both save):

```
signal_completed:  load(S) ─ mark worker COMPLETED ──────────── save(S')
reconcile/spawn:   load(S) ─ revert phantom / append session ── save(S'')   ⟵ wins, clobbers S'
```

Observed-by-inspection failure modes, all silent:

1. A completion write is lost → the ticket stays `RUNNING` forever → the
   concurrency slot leaks and is never reclaimed.
2. A freshly-spawned session write is lost → orphaned worker, no
   parent/worker linkage, invisible to `cw orchestrate workers`.
3. A `TIMED_OUT` revert is clobbered → a stalled session is never retried.

The `reconcile.py:19-25` race note assumed "concurrent writers are rare in
the single-user model." That assumption does not hold: the Stop-hook and
the loop are concurrent by design, and RFC 0004 multiplies the writers
(one long-lived orchestration session per lane).

## Invariant

1. All `CwState` mutations MUST be performed inside `mutate_state(fn)`,
   which acquires `state_lock()` (exclusive `flock` on
   `STATE_DIR/.state.lock`), calls `load_state()`, applies `fn(state)`,
   and `save_state(state)` — all under the held lock.
2. `state_lock()` MUST be the *only* lock ordered around `sessions.json`.
   When a code path also holds `dev_queue_lock()`, the acquisition order
   MUST be **`state_lock` → `dev_queue_lock`** (never the reverse) to
   avoid deadlock. (Today `wrapper.signal_completed` and
   `dispatch.dispatch_tick` both touch state and the dev-queue; they must
   adopt this order.)
3. Read-only callers MAY continue to use bare `load_state()` without the
   lock — a stale read is acceptable; a lost write is not.
4. The lock is advisory and process-local to the host; it protects
   against the in-process and cross-process writers cw itself spawns, not
   against arbitrary external editors of the file.

## What this means for callers

- **`wrapper.signal_*`** — the highest-risk writers (out-of-process,
  fired by the worker Stop-hook). Migrate first.
- **`dispatch.dispatch_tick` / `reconcile`** — wrap their state mutations;
  honor the `state_lock → dev_queue_lock` order where both are held.
- **`spawn.spawn_create_impl`** — the append-new-session write.
- **`cli`, `session`, `doctor`, `orchestrate`** — lifecycle and retirement
  writes; lower contention but must use the helper for uniformity.
- **Read-only snapshots** (`orchestrate.orchestrator_status`, TUI) — no
  change; they only read.

## Consequences

- Adds lock-acquisition latency to every state mutation. Negligible
  versus the subprocess spawns and git operations these paths already do.
- A crashed holder releases the `flock` on fd close (OS-level), so a dead
  dispatcher cannot wedge the lock — consistent with the existing
  `dev_queue_lock` behavior.
- The 21 call sites migrate incrementally; until all are converted, the
  guarantee is partial. The migration order above front-loads the
  highest-contention pair (Stop-hook vs loop), which removes the dominant
  race even before full coverage.
- `mutate_state(fn)` forces the mutation into a callback, a small
  ergonomic cost at call sites that previously inlined the edit. Worth it
  for a single audited choke point.

## Alternatives considered

- **Leave it (status quo).** Rejected. The "rare concurrent writer"
  premise is false for the Stop-hook/loop pair, and RFC 0004 makes it
  worse, not better.
- **Per-session lock files instead of one state lock.** Rejected. The
  whole file is rewritten on every save (single JSON document), so
  per-session granularity buys nothing — any two writers still serialize
  on the one file.
- **Move sessions to a per-session-file directory** (one JSON per session,
  no global rewrite) to enable true per-session locking. A real option
  but a much larger migration (schema, reconcile, status snapshot all
  assume one document). Deferred; the single state lock is the
  proportional fix for the race today.
- **Compare-and-swap on a version field** (optimistic concurrency: bump a
  `revision`, retry on mismatch). Rejected for now — retries reintroduce
  the read-modify-write loop the lock removes, and the contention is low
  enough that a plain mutex is simpler and sufficient.

## Referenced by

- RFC 0004 (State integrity section)
- #387 (T1 — foundation + dominant-race writers), #388 (T2 — sweep)
- Plan: `docs/superpowers/plans/2026-05-30-single-state-lock.md`
