# Reaping is gated by an authority, not automatic

**Status:** Accepted — implemented (1.1: #552/#554/#555/#556; RFC 0004 Phase 4: #594/#595/#596 — the lane's ORCHESTRATE session is the authority, consumed via `cw orchestrate run`)
**Amended by:** [ADR-0020](0020-the-operator-is-the-orchestrator-session.md) — "operator" means the orchestrator session; a human is escalated to only for product/scope forks.
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
**Amended by:** [ADR-0014](0014-timers-never-destroy-work.md) (timers never
destroy work) — narrows what may even be *proposed*: elapsed time and
transcript quietness no longer produce reap candidates at all, so this ADR's
gate now governs only evidence-driven dispositions (roster-absence phantoms).
ADR-0014 also partially overturns the "Remove reaping entirely — rejected"
alternative below: rejected-then was removal of *all* reaping; what ADR-0014
removes is specifically the timer-driven class.

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

## Amendment (#2630)

The act phase no longer removes worktrees. #1747 / ADR-0014 deleted the
reconcile-side `remove_worktree(..., force=True)` that the **Decision**
paragraph above and the "every force-remove" line under **Consequences**
describe. The acts reconcile still performs are the RUNNING→PENDING /
BLOCKED_ON_USER queue routing and the daemon surface stop. Those two
passages are left as written, as the decision's history; this amendment
supersedes their worktree clause.

- **No reconcile act removes a worktree.** A worktree is removed only at
  dispatch claim time (an unoccupied, clean stale tree, after liveness and
  dirt re-checks) or by an explicit operator command (`cw done --cleanup`,
  `cw worktree gc --apply`).
  `tests/test_reconcile_worktree_removal_guard.py` pins the reconcile side
  as a tripwire, not a proof.
- **What the cached dirty check selects.** For the phantom sweep and
  backstops the pre-lock capture (#2548) only selects the queue
  disposition, RUNNING→PENDING versus BLOCKED_ON_USER. "Destructive" here
  means on-disk work loss; the RUNNING→PENDING revert still consumes a
  capture up to 90 s old, so an in-bound clean capture of a tree dirtied
  since can route a row to PENDING (on a `reap_policy: auto` lane for the
  phantom sweep, on any lane for the backstops).
- **Not every daemon stop is gated.** Invariant 2 reads as if every
  act-phase mutation were gated by `reap_policy`. The ungated stops include the
  merged-PR, routed-sentinel and stalled foreign-result completions (queued
  on the post-lock sink)
  and the leaked-worker sweep (`sweep_leaked_daemon_workers`, called from
  `_reconcile_locked` and queued on the post-lock sink); they act on workers
  whose owning session is terminal or absent. The leaked-worker sweep can
  also stop a just-spawned worker whose session is not yet recorded (a
  pre-existing hazard, documented in `leaked_workers.py`). The mid-turn
  usage-limit act's stop is chosen only under `reap_policy: auto`.
- **Tracked, not fixed here.** `cw worktree gc --apply` force-removes after
  a classification pass with no fresh re-check (#2669); the reconcile
  dirty helper reads helper errors as clean, fail-open (#2662).

## Amendment (#2715)

Two ungated evidence acts, plus one page, release slots that nothing else
released. The authority for acting on recorded evidence is the operator's
Option B decision on #2715 (`#issuecomment-6215180981`), the round-2
resolutions (`#issuecomment-6249899648`) and the plan-approval note
(`#issuecomment-6250291426`). Neither act is gated by `reap_policy`, and
there is no kill switch, lane switch or sweep registry: Option B settled that
these acts run on every lane.

- **The shipped release (`cw.reconcile.shipped_release`).** A live ACTIVE or
  IDLE DAEMON session in the #2524 stranded population (consumed
  `cw result emit` result, roster-live, no lane-occupying row bound to it) is
  closed when its recorded result is `shipped` and that result's own PR is
  OPEN or MERGED (a lockless `gh pr list` pre-pass, at most three lookups per
  tick). The session flips COMPLETED/NORMAL with
  `reap_reason = routed_result_stranded` (the value `cw doctor --reap`
  stamps for the same class), one `session.completed` carries the additive
  `pr_url`, `merged_at` goes to the INFO log, and the daemon stop is deferred
  past `sessions_lock`. **This stops a roster-live worker**, which amends
  the "act on workers whose owning session is terminal or absent" wording
  of the #2630 amendment for this one sweep: its target session was live at
  detect time.
- **It departs from the literal Option B words.** It completes no queue row:
  the route already advanced the row, and the stalled and idle sweeps own the
  RUNNING-row case. The acceptance is the freed client-ceiling slot.
- **AC1, as relaxed.** A shipped session with a PR does not hold a slot past
  one tick once a route has recorded the result, except where a
  lane-occupying row pins it by design: a `signoff` or `hold_finalize` row
  (ADR-0001) is a human-kept gate, and such a session is left untouched. The
  tick counts from the one on which the evidence is complete.
- **It fails closed.** The worktree's `agent_spawn_stamp` must be readable,
  carry an outstanding count, be at least `fix_loop_await_deadline_minutes`
  old, and carry a `session_id` equal to the session being stopped. The
  Stop hook's snapshot writes that id: it names the context owner when the
  snapshot was written, not necessarily the session that fired the Stop. An
  absent or unreadable stamp, a stamp with another session's id or none
  (every stamp written before this change), or a young stamp means no act
  that tick. The pin is re-checked on a fresh queue read under
  `dev_queue_lock`, held across the check and the state write.
- **The age checks only delay; the evidence triggers.** The draining veto
  (`_background_work_still_draining` compares an unresolved subagent-spawn
  stamp's age with `fix_loop_await_deadline_minutes`), the added stamp-age
  and session-id checks, and the #2524 transcript bucket are each necessary
  but never sufficient: the draining veto only delays the act, and so do the
  others. Absent the recorded shipped result plus its own OPEN/MERGED PR, no
  elapsed time acts. ADR-0014 invariant 1 and ARCHITECTURE §7.13 ("thresholds
  may only debounce or delay signals and re-checks") hold.
- **The lost-session park (`cw.reconcile.lost_session_rows`).** A RUNNING,
  non-exempt, in-scope row bound to a `session_id` that resolves to no
  Session (the #2502 failed `sessions.json` write) is parked BLOCKED_ON_USER
  with disposition `running_row_session_lost`, keeping `session_id` and
  `pr_url`, with no unproductive charge. ADR-0001's "session resolves"
  invariant cannot hold for such a row (the record was never written); the
  park keeps the dangling id as evidence and deletes nothing. It can never
  produce a parked row with `session_id = None`, and the leaked-worker sweep
  stops any live unrecorded worker. The disposition escalates after 45
  minutes and is never auto-requeued; `cw dev-queue requeue` releases it.
- **Mass-park guards.** When `sessions.json` holds no session at all and
  more than three bound rows would park, nothing parks (one WARNING). Otherwise
  at most three rows park per tick, in queue order; the rest wait.
- **The sessionless page.** A RUNNING row with no `session_id` past
  `SPAWN_GRACE_SECONDS` since its claim is paged once (latched in
  `advisory_note` as `?no_session`) and never parked. The grace is a delay on
  a signal (ADR-0014); the claim-to-bind window can exceed it, so a rare false,
  non-destructive page is accepted.
- **Audit.** The park emits `task.transition` (inline, under the queue lock),
  one `session.needs_attention` and an INFO log; the release emits
  `session.completed` with `pr_url` and an INFO log. No new event type. Neither
  fires a push notification (parity with the #2524 page), but the release's
  `reap_reason` makes the queue-events poller broadcast `queue.session_reaped`
  exactly as for the doctor close.
- **Slot accounting.** The lane slot follows `OCCUPIED_LANE_STATUSES`, which
  includes BLOCKED_ON_USER: a park makes the row requeue-able and the
  operator's `requeue` frees the slot. The ceiling slot is session-status
  based: the release frees it by flipping the session; a record-missing row
  had no Session and never counted.
- **Park scope, in words.** The park covers only a bound row whose Session
  record is missing. A roster-absent live session stays with the phantom
  sweep (after its 30 s grace); a terminal session stays with the backstops
  (60 s completion grace, 30 s spawn grace, dirty-check deferral). Also
  outside both sweeps: the daemon-outage early return, backstop-exempt rows
  (`usage_limit_act`, fix-dispatch hold), live bound sessions with
  `surface_ref = None` (`compute_drift` skips them), and park overflow beyond
  the cap.
- **Accepted residuals.** A stamp written before this change carries no
  session id, so an already-stranded session holding one stays page-only
  until an operator closes it or a later Stop rewrites the stamp. Background
  work that prints nothing for longer than the deadline is indistinguishable
  from drained work. The release's three-lookup cap can be starved by three
  permanently ineligible sessions ahead of an eligible one (the #2524 page
  still fires for it).
- **Tracked, not fixed here.** The leaked-worker sweep runs before the outage
  guard and has the same blind spot on a wiped `sessions.json`; the park cap
  is per tick, not cumulative.

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
