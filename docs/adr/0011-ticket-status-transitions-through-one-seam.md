# TicketTask status transitions go through a single seam

**Status:** Accepted
**Driven by:** #835 (unblocked #310)

## Decision

Every dev-queue `TicketTask` status change goes through one function —
`transition_task_status(task, new_status, ...)` in `src/cw/dev_queue.py`. Code
does **not** assign `task.status = ...` directly. The seam is the single
authority and the hook point for any logic that must fire on a transition.

## Invariant

1. No direct `TicketTask.status` assignment outside the seam body. A reviewer
   may reject any new `task.status = QueueItemStatus.*` (or
   `= _queue_status_for_salvaged(...)`) on a dev-queue task.
2. Logic that must run on a terminal/reset transition (e.g. disposition
   stamping and clearing, #310) lives **in the seam**, not replicated at call
   sites. Stamp on `COMPLETED`/`BLOCKED_ON_USER`/`FAILED`; clear on
   `PENDING`/`CANCELLED`; leave `RUNNING` untouched.
3. Companion field resets that are call-site-specific (`session_id`,
   `stage_base_ref`) stay at the call sites; the seam owns only status and the
   transition-derived fields.

## What this means for callers

- `dispatch.py`, all `reconcile/*`, `dev_queue.py`, `cli/sessions.py`,
  `doctor.py` call `transition_task_status`. Acceptance check:
  `grep -rEn "\.status = (QueueItemStatus|_queue_status_for_salvaged)" src/cw/ | grep -v tests/`
  returns only the seam body line (plus `queue.py`'s unrelated `QueueItem`).
- `Session.status` / `SessionStatus` writes are a **different** model and out
  of scope — the seam is dev-queue `TicketTask` only.

## Consequences

- Cross-cutting per-transition behavior (disposition, future audit/event hooks)
  becomes a one-site change instead of an N-site sweep. #310's disposition
  feature collapsed from a ~9-site plumbing job to a single seam extension.
- One indirection on every status change (negligible; the seam is a thin
  in-place mutation — `TicketTask` has no `validate_assignment`, so it is
  mechanically identical to the prior direct assignment).

## Alternatives considered

- **Stamp disposition at each terminal site (rejected):** the pre-seam state.
  ~40 scattered `task.status = ...` writes with no choke point — enumerating
  them by hand repeatedly missed sites (the direct cause of #310's plan
  whack-a-mole). The seam makes the set greppable and the behavior single-sourced.

## Referenced by

- #835, #310, ADR-0010
