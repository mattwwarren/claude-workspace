# Reaping is gated by an authority, not automatic

**Status:** Accepted — implemented (1.1: #552/#554/#555/#556; RFC 0004 Phase 4: #594/#595/#596 — the lane's ORCHESTRATE session is the authority, consumed via `cw orchestrate run`)
**Driven by:** #542 (session reaped mid-wait → task PENDING, wait rides to
124 instead of ATTENTION), #402 (isolation breach), #438 (`doctor --reap`
unguarded write); operator distrust of automatic reaping (milestone 1.1).
**Builds on:** [ADR-0005](0005-single-state-lock.md) (single state lock —
closes the *lost-write* race; this ADR closes the *unwanted-action* class),
[RFC 0004](../rfcs/0004-work-lanes-queue-redesign.md) (lanes + first-class
orchestration sessions — supplies the reap *authority*).
**Amends:** RFC 0004 §"Reconciler interaction" (which states "no change to
the revert path"). This ADR changes that default; see Reconciliation below.
**Implemented by:** #552 (detect/act split), #554 (`reap_policy` gate),
#555 (`SESSION_REAP_PROPOSED` + authorization), #556 (doc inversion).

## Decision

`reconcile()` is split into a **detect** phase (classify phantoms,
budget-exceeded, silently-idle) and an **act** phase (revert `TicketTask`
RUNNING→PENDING, stop the daemon surface, force-remove the worktree). The
act phase is **gated by a `reap_policy`** that defaults to `signal_only`:
detection emits a distress event and routes the owning task to
`BLOCKED_ON_USER`, but performs no destructive mutation until an
**authority** authorizes it. Per RFC 0004, that authority is the **lane's
long-lived orchestration session** (`SessionPurpose.ORCHESTRATE`); absent
one, an explicit operator command. Automatic reaping becomes opt-in
(`reap_policy: auto`), not the default.

This is the complement to ADR-0005, not an overlap. ADR-0005 makes
concurrent writes *not clobber each other* (S1). This ADR governs whether
a *correct, serialized* write should fire at all. A session can be reaped
losslessly (ADR-0005) and still be reaped *wrongly* — that is the gap #542
and #402 sit in.

## Invariant

1. The detect phase MUST NOT mutate session *status*, the dev queue, the
   daemon roster, or worktrees. Detect classifies and returns candidates
   (with evidence); it performs no writes. (Observation bookkeeping per
   #545 and `claude_session_id` backfill stay in detect.) The
   `BLOCKED_ON_USER` routing of a distressed task is an act-phase write —
   the non-destructive one permitted under `signal_only`.
2. Every act-phase mutation runs only when `reap_policy` authorizes it for
   that session's lane, OR via explicit operator command
   (`cw doctor --reap <session>` / `cw reconcile --apply`). All such
   mutations go through ADR-0005's single serialized write path: code not
   holding `sessions_lock` calls `mutate_state()`; code already inside the
   reconcile lock window mutates in place and `save_state`s under the held
   lock (`mutate_state` is non-reentrant — per-open-fd flock; see the
   #387/#563 deadlock). Either way, this ADR introduces no second write
   path.
3. The detect phase MUST classify (and, once #555 lands, emit
   `SESSION_REAP_PROPOSED` carrying `lane` per RFC 0004,
   `proposed_action`, `reason`, evidence) for every would-be reap BEFORE
   any act. A reap with no preceding proposal is a bug.
4. `signal_only` is the default. Absent/unknown config → `signal_only`
   (fail-safe: never auto-destroy on a malformed config).
5. `reap_policy` resolves per lane (lane `LaneConfig` override → global
   default → `signal_only`), reusing RFC 0004's lane-config layering.
   Until lanes land (RFC 0004 waves), it is a single global field.
6. The ADR-0005 transient-outage guard and spawn-grace stay in detect.

## What this means for callers

- **`dispatch_tick`** calls detect every tick (unchanged cadence) and acts
  only on lanes whose `reap_policy` is `auto`. Under `signal_only` a
  distressed session keeps its slot until an authority resolves it —
  callers MUST count `BLOCKED_ON_USER`/`REAP_PROPOSED` sessions as
  occupying capacity (so RFC 0004's Tier-2 allocator does not over-spawn
  into a stalled lane) and MUST NOT read a below-cap lane as a stuck
  dispatcher.
- **The lane's ORCHESTRATE session** (RFC 0004) consumes
  `SESSION_REAP_PROPOSED` from the event bus and authorizes or salvages
  per lane. Authorizing a reap in lane X never reaps a session in lane Y.
- **`cw status`/`list`/`start`** trigger detection only; never reap under
  the default policy. They surface `REAP_PROPOSED` distinctly.
- **#542 specifically:** a sentinel-aware `wait` must observe
  `REAP_PROPOSED`/`BLOCKED_ON_USER` and resolve to ATTENTION rather than
  riding the wall-clock to 124 — the gated path makes that state
  observable instead of a silent task→PENDING flip.

## What this means for producers

- `reconcile.py` is the sole producer of `SESSION_REAP_PROPOSED`; the three
  sweeps feed one detect→propose path and one policy-gated act path, so a
  future fourth sweep inherits the gate.
- The dirty-worktree guard (#404/#425) is the in-tree prototype: it already
  refuses-to-remove, routes to `BLOCKED_ON_USER`, and emits
  `needs_attention`. This ADR generalizes that branch to be the default
  for *all* destructive paths.

## Reconciliation with RFC 0004

RFC 0004 §"Reconciler interaction" assumed the revert path is unchanged
("RUNNING→PENDING reverts preserve lane … no change"). That holds for the
*lane-preservation* mechanics, but this ADR changes *when* the revert
fires: under `signal_only` it does not fire automatically. RFC 0004 should
absorb a per-lane `reap_policy: signal_only | auto` field on `LaneConfig`
and note that the Tier-2 allocator treats a `signal_only`-blocked session
as occupying a slot. Tracked as the RFC-0004 amendment in the 1.1
reconciliation plan.

## Consequences

- Under the default, a genuinely-dead session no longer self-heals; its
  slot stays occupied until an authority acts. Unattended `auto` lanes
  (overnight debt) opt back into self-healing per lane.
- The destructive surface becomes auditable in one gated choke point —
  every force-remove and queue revert is enumerable, closing the #402/#438
  class where a reap fired from an unexpected path.
- Reap regression tests bifurcate: detect-phase asserts classification +
  no mutation; act-phase asserts mutation only under `auto` or explicit
  authorization.

## Alternatives considered

- **Rely on ADR-0005 alone.** Rejected. The state lock stops *lost* writes
  but not *unwanted* ones — #542 is a correctly-serialized reap that
  shouldn't have happened. Locking does not address authority.
- **More liveness heuristics** (#340/#384/#543/#544/#545). Each is a better
  guess at *when to auto-act*; the operator distrust is structural — the
  decision belongs to the lane authority, not a heuristic.
- **Remove reaping entirely.** Rejected. `auto` lanes want self-healing;
  this is policy, not a global switch.

## Referenced by

- ADR-0005, RFC 0004
- #542, #402, #438
- #552, #554, #555, #556
