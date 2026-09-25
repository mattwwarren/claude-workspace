# Codex runs as a detached, local-liveness-tracked job

**Status:** Accepted
**Driven by:** #2286

## Decision

`CodexExecutor` becomes a third fire-and-forget `LocalLivenessHandle`-based
launcher (mirroring `LocalExecutor`/`OpencodeExecutor` in `src/cw/executor.py`)
instead of an in-process daemon thread (the current `cw.codex_background`
model). A new stage-agnostic CLI entry point, `cw codex run <task> --stage
<review|impl>`, runs the entire job — review, fix loop, push-and-verify,
verdict persistence, GitHub comment, and session completion — in a detached
process (`start_new_session=True`). `serve` no longer hosts a codex thread.
The detached process is the normal completion path; `reconcile/local.py`'s
dead-PID detection is a crash-only fallback.

## Invariant

1. Every codex job is launched as a non-blocking, detached subprocess
   (`Popen(..., start_new_session=True)`) — `CodexExecutor.spawn()` never
   blocks on review/fix-loop completion.
2. Every codex job carries a `Session.local_liveness` (`LocalLivenessHandle`:
   PID + `/proc` start-time) handle, stamped exactly as
   `LocalExecutor`/`OpencodeExecutor` do.
3. The detached driver process itself is the sole *normal* completion path —
   it completes the session via the existing door/completion contract before
   it exits.
4. Crash-only fallback: if `reconcile/local.py`'s dead-PID sweep finds a
   codex session with a dead PID and no recorded completion, it requeues the
   task ONLY if a four-part clean-requeue gate passes (fix loop off,
   worktree clean apart from verdict files, HEAD unmoved from
   `task.stage_base_ref`, `reap_policy is ReapPolicy.AUTO`) — mirroring
   `codex_boot.py`'s existing `_gate_clean_requeue`/`_resolve_orphan_action`
   logic — else it parks the task. Before either transition, it must write a
   durable, operator-visible recovery audit event containing the session and
   task IDs, tenant, previous and resulting state, PID/start-time liveness
   evidence, each gate check, timestamp, and executor/job identity. If that
   event cannot be committed, neither transition occurs. The recorded prior
   state and evidence must support an operator-visible recovery command or
   runbook for reversing an incorrect transition. It must NOT run aider's
   git-fact synthesis (`synthesize_git_result`) for a codex-origin candidate.
5. `reconcile/codex_boot.py`'s psutil-based process-table boot sweep is
   retired only after a one-time legacy recovery has covered every pre-
   migration ACTIVE Codex session with a null `local_liveness` handle,
   requeuing only under the same audited clean gate and otherwise parking it.
   This legacy pass is the final use of the cwd scan; normal recovery then
   uses deterministic PID+start-time liveness and has no cwd-scan false
   positive/negative ambiguity.

## What this means for callers

- `reconcile/local.py`'s harvest dispatch gains an explicit per-executor
  branch (built by #2369's shared harvest-lookup extraction) so a dead codex
  PID with no recorded completion routes to the ported clean-requeue gate,
  never to aider's git-fact synthesis or opencode's log-parse.

## What this means for producers

- `CodexExecutor.spawn()` launches `cw codex run` via
  `Popen(..., start_new_session=True)` instead of `_default_background`;
  stamps `LocalLivenessHandle` exactly where `LocalExecutor`/
  `OpencodeExecutor` do (`src/cw/executor.py`).
- The new `src/cw/codex_driver.py` module owns the CLI entry point and is
  invoked only as a subprocess by `spawn()` — never imported by
  `cw.executor` — which is how the `cw.executor` <-> `cw.codex_background`
  import cycle is eliminated structurally (by process boundary), not
  deferred by a function-level import.

## Consequences

- Loses `join_outstanding_codex_threads`'s bounded shutdown drain and the
  `DISPATCH_LOOP_EXITED.codex_threads_still_running` payload field (nothing
  left for `serve` to join).
- Loses the psutil cwd-scan's "scan inconclusive" ambiguity class entirely
  (PID+start-time is deterministic — a real simplification).
- Gains a new CLI surface and subprocess-launch failure modes that
  pre-flight (endpoint/binary missing) must still complete synchronously
  exactly as Local/Opencode's pre-flight does today.
- `reconcile/local.py` needs a third harvest branch, so its dispatch can no
  longer be a bare two-way `if opencode_log.exists()`.

## Alternatives considered

- **Detaching only the `codex` subprocess** (the ticket's original,
  non-operator-resolved proposal) — rejected because it leaves fix-loop
  commits made after `serve` exits unaccounted for; whereas detaching the
  whole job means the same process doing the committing is the one still
  running and still liveness-tracked.

## Referenced by

- #2286, #1550, #2367, #2368, #2369, #2370

## Tickets

1. `cw codex run` CLI entry point + driver module
   (`src/cw/codex_driver.py`).
2. `CodexExecutor.spawn()` rewire to `Popen` + liveness-stamp. **Blocked by
   ticket 3 / #2369** — shipping this before the harvest branch lands would
   let a dead codex PID fall through `reconcile/local.py`'s existing binary
   check into aider's git-fact synthesis (a wrong completion for a codex
   session).
3. `reconcile/local.py` per-executor harvest branch for codex's
   clean-requeue-or-park fallback (depends on #2369).
4. Delete `codex_boot.py`'s psutil sweep + `codex_background`'s thread
   registry + shutdown drain only after ticket 8's legacy migration; update
   `DISPATCH_LOOP_EXITED` payload to drop `codex_threads_still_running`.
5. Rewrite the `StageExecutor` Protocol docstring in `src/cw/executor.py`
   (currently calls `CodexExecutor` "an accepted, documented exception" —
   that carve-out goes away).
6. Rewrite `tests/test_codex_background.py` and the thread-registry
   assertions in `tests/test_codex_executor.py`
   (`test_spawn_returns_before_background_work_completes`, the
   `_sync_codex_executor` helper) for the Popen-based model.
7. Extend the driver to `--stage impl` for #1550.
8. Run the migration/rollout gate before ticket 4: deploy the audited
   per-executor harvest path first, run one legacy boot recovery over all
   null-handle ACTIVE Codex sessions, and record scanned/requeued/parked/
   failed counts plus a durable completion marker. Do not remove the boot
   sweep until coverage is complete, failures are operator-resolved, and no
   unprocessed legacy sessions remain; publish the audit-reversal command or
   runbook with the migration.
