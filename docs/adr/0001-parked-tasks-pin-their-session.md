# Parked tasks pin their Session

**Status:** Accepted
**Driven by:** #58
**Builds on:** [ADR-0000](0000-native-supervisor-migration.md) — assumes
`claude --bg <id>` is the resume primitive.

## Decision

When a `TicketTask` is parked (in `PAUSED_NEEDS_HUMAN` or `REQUEUED_DEFERRED`),
its underlying `Session` and worktree MUST remain intact and resumable. No
sweep — reconcile, doctor, or future cleanup — may garbage-collect them.

## Invariant

When `TicketTask.status ∈ {PAUSED_NEEDS_HUMAN, REQUEUED_DEFERRED}`:

1. `TicketTask.session_id` resolves to a real `Session` in `sessions.json`.
2. That `Session` has `claude_session_id` set (so `claude --bg <id>` resumes).
3. That `Session`'s `worktree_path` exists on disk and remains a valid git
   worktree.
4. `Session.last_result` is populated with the parsed `AutoDevResult` that
   drove the parked state.

## What this means for callers

- **`reconcile.py`** must not reap Sessions whose `id` is referenced by a
  parked `TicketTask`. Today reconcile reaps any Session whose multiplexer
  surface is dead; this carve-out lands with #58.
- **`cw doctor`** may warn about long-parked items but MUST NOT delete them.
- **Any future cleanup sweep** must check the queue before touching a Session.
- **Resume implementations** (see #59) read context from `Session.last_result`
  and the worktree; they depend on this invariant.

## What this means for producers

- The `/auto-dev` skill must emit its `<<<AUTO_DEV_RESULT` sentinel block
  before the worker exits or its worktree is torn down. Today's contract:
  §3 of [`docs/headless-contract.md`](../headless-contract.md).
- `merge_gate_blocked` workers do not create a branch (per §3.3 pre-branch
  statuses). The worktree is still preserved; deferred-retry reuses it.

## Consequences

- Disk usage grows monotonically with parked items until an operator runs
  `cw dev-queue purge` or `cw dev-queue retry`. Accepted: losing diagnostic
  or in-progress state is worse than the disk cost.
- Reconcile must learn one new exception (skip-if-parked). Covered by tests
  in #58.

## Alternatives considered

- **Auto-reap parked items after N days.** Rejected. Operators cannot
  reliably distinguish "still working on approving" from "abandoned"; reaping
  in-flight work silently is a worse failure mode than disk growth.
- **Inline the invariant as a comment on `QueueItemStatus`.** Rejected.
  Multiple modules (`reconcile`, `doctor`, #59, #127) need to cite the same
  rule; a comment on one enum value doesn't carry.

## Referenced by

- #58, #59, #126, #127
