# RFC 0014 — Codex Runs as a Detached Job

## Summary

ADR-0018 (`docs/adr/0018-codex-runs-as-a-detached-local-liveness-job.md`) decided that codex stops running on a daemon thread inside `cw dev-queue serve`. Instead, a stage-agnostic `cw codex run <task> --stage <s>` driver runs the whole codex job (review, fix loop, push-and-verify, verdict persistence, GitHub comment, and session completion) in a detached process launched with `start_new_session=True`. `CodexExecutor.spawn()` tracks it through the same `Session.local_liveness` handle that `LocalExecutor` and `OpencodeExecutor` already use, and `reconcile/local.py` harvests a crashed run.

This RFC is the rollout plan for that decision. The ADR records *what* was decided and why. This document breaks the work into tickets for `/sprint-buildout`. Two epics: the first builds the detached path and switches codex onto it, and the second retires the in-process model once a one-time legacy migration has covered every pre-migration session.

Prerequisite refactors are already filed and are not re-filed here: #2367 (merged: aider/opencode children launch with `start_new_session`), #2368 (`executor.py` split into a package, so `CodexExecutor` lives in its own module), and #2369 (shared fire-and-forget spawn/launch/harvest helpers plus an explicit per-executor harvest lookup). #1550 (the codex impl stage) follows this RFC as a second stage of the same driver and is also not re-filed.

## Motivation

Any restart of `cw dev-queue serve` kills in-flight codex work. The review runs on a `threading.Thread(daemon=True)` inside serve (`src/cw/codex_background.py:93`), the `codex` CLI child has no recorded PID, and results are persisted only after `run_review_with_fix_loop` returns. On shutdown, `join_outstanding_codex_threads` (`src/cw/codex_background.py:220`) gives threads a bounded drain, and anything unfinished is abandoned. `reconcile/codex_boot.py` can then only requeue or park the task, based on a psutil process-name/cwd scan it describes as a blast-radius bound, not a re-adoption.

The operator's longer-term goal is for codex to do implementation work too (#1550). A longer-running codex impl stage on the same thread model would lose more work per restart, and fix-loop commits made while serve is down would be unaccounted for. Claude workers don't have this problem because they run under `claude --bg`, outside serve, and reconcile simply polls them.

## Design

### Epic I — Detached codex job

Build the `cw codex run` driver and the codex branch of the crash-only harvest, then switch `CodexExecutor.spawn()` from a background thread to a detached `Popen` that stamps `LocalLivenessHandle`. When this epic ships, a serve restart no longer affects a running codex review. The driver completes the session itself through the existing completion door. The `StageExecutor` protocol's documented codex carve-out is removed, together with the spawn-level tests that assert thread behavior.

### Epic II — Retire the in-process model

After Epic I is released and serve is restarted onto it, run a one-time, audited legacy recovery over every pre-migration ACTIVE codex session whose `local_liveness` handle is null. Only once that pass is complete and recorded, delete the psutil boot sweep, the codex thread registry, and the shutdown drain, and drop `codex_threads_still_running` from the `DISPATCH_LOOP_EXITED` payload.

## Phasing

| Wave | Epic I | Epic II |
|------|--------|---------|
| 0 | S1 — `cw codex run` driver | |
| 1 | A1 — codex harvest branch (after #2369) | |
| 2 | A2 — `CodexExecutor.spawn()` rewire | |
| — | *release + serve restart* | |
| 3 | | B1 — legacy migration gate |
| 4 | | B2 — retire boot sweep + thread registry |

## Resolved decisions

- **D-1 — Process boundary, not a deferred import.** `src/cw/codex_driver.py` owns the `cw codex run` entry point and is invoked only as a subprocess of `CodexExecutor.spawn()`. `cw.executor` never imports it, which structurally removes the `cw.executor` ↔ `cw.codex_background` import cycle.
- **D-2 — The driver reuses the existing job logic.** The driver calls the existing review-and-complete path (`_run_codex_review_and_complete` and `run_review_with_fix_loop`) rather than reimplementing it. Everything it depends on is file- or lock-based (`sessions_lock`, `dev_queue_lock`, `load_effective_config`), so it runs unchanged in a fresh process. The thread plumbing stays in place, unused on the new path, until B2.
- **D-3 — The `impl` stage is reserved.** `cw codex run` accepts `--stage review`. `--stage impl` exits non-zero with a clear "not implemented (#1550)" error until #1550 lands. The driver's inputs, result location, and completion semantics are defined stage-agnostically so that #1550 is an addition, not a redesign.
- **D-4 — Crash-only harvest with an audited gate.** A dead codex PID with no recorded completion routes to a codex branch of the per-executor harvest lookup (from #2369). It ports `codex_boot.py`'s four-part clean-requeue gate (`_gate_clean_requeue` / `_resolve_orphan_action`): fix loop off, worktree clean apart from the verdict files, HEAD unmoved from `task.stage_base_ref`, and `reap_policy is ReapPolicy.AUTO`. If the gate passes, the task is requeued; otherwise it's parked. Before either transition, a durable recovery audit event is committed, carrying the session and task ids, client, prior and resulting state, PID/start-time evidence, every gate check, a timestamp, and the executor identity. If the event can't be committed, no transition occurs. Codex candidates never reach aider's `synthesize_git_result` or opencode's log parse.
- **D-5 — Docs and tests change in the ticket that makes them false.** The `StageExecutor` protocol's codex carve-out comment and the spawn-level thread assertions in `tests/test_codex_executor.py` change in A2, because A2 is what invalidates them. `tests/test_codex_background.py` and the thread registry change in B2, which deletes the registry. (ADR-0018 lists these as separate tickets 5 and 6. They're folded here because each ticket must pass all gates on its own.)
- **D-6 — The sprint boundary is a deployment.** Epic II's legacy pass must run on a serve that already has Epic I's harvest path, so Sprint 2 starts only after Epic I is released and serve is restarted onto it.
- **D-7 — The legacy pass is one-time and recorded.** B1 processes every pre-migration ACTIVE codex session with a null `local_liveness` handle, using the D-4 gate and audit event. It records scanned/requeued/parked/failed counts and a durable completion marker, and publishes an operator runbook for reversing an incorrect transition. B2 refuses to start (it is dispatched only by the operator) until the marker exists and no failures are unresolved.
- **D-8 — Fix-loop configuration is honored as-is.** The driver resolves `codex_fix_loop_enabled` through the existing lane/client/global precedence. This RFC doesn't turn the fix loop on for any lane.

## Tickets

### S1 — `cw codex run` CLI entry point and driver module

- **Epic:** I
- **Wave:** 0
- **Sprint:** 1
- **Depends on:** none
- **Context:** Add `src/cw/codex_driver.py` and a `cw codex run <task> --stage <review|impl>` command that loads the task and session, runs the existing codex review-and-complete path end to end in the current process (review, fix loop, push-and-verify, verdict persistence, GitHub comment, and session completion through the existing door), and exits; `--stage impl` exits non-zero naming #1550; nothing in `cw.executor` imports the new module.
- **Scope:** D-1, D-2, D-3, D-8
- **Acceptance:**
  - `cw codex run --help` documents both stages; `cw codex run <t> --stage impl` exits non-zero with an error naming #1550.
  - A test runs the driver against a fake codex runner and asserts the session completes through the existing completion door with the same result payload the thread path produces today.
  - grep finds no import of `cw.codex_driver` anywhere under `src/cw/executor*`.
  - All local quality gates pass; patch coverage is at least 90%.

### A1 — Codex branch of the crash-only harvest with an audited clean-requeue gate

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** none
- **Context:** Requires #2369 merged (it introduces the per-executor harvest lookup); register a codex entry that, for a dead codex PID with no recorded completion, evaluates the four-part clean-requeue gate ported from `reconcile/codex_boot.py` (`_gate_clean_requeue` / `_resolve_orphan_action`), commits a durable recovery audit event, and then requeues or parks, never running `synthesize_git_result` or the opencode log parse.
- **Scope:** D-4
- **Acceptance:**
  - A dead-PID codex candidate with every gate check passing is requeued, and one failing any check is parked; tests cover each of the four checks failing on its own.
  - The audit event carries session id, task id, client, prior and resulting state, PID/start-time evidence, per-check results, timestamp, and executor identity; a test asserts no transition happens when the event write fails.
  - A test asserts a codex candidate never reaches `synthesize_git_result` or the opencode sentinel parse.
  - All local quality gates pass; patch coverage is at least 90%.

### A2 — Switch `CodexExecutor.spawn()` to a detached `cw codex run` launch

- **Epic:** I
- **Wave:** 2
- **Sprint:** 1
- **Depends on:** S1, A1
- **Context:** Requires #2368 and #2369 merged; replace the background-thread hand-off with the shared fire-and-forget spawn helper launching `cw codex run` via `Popen(..., start_new_session=True)` and stamping `LocalLivenessHandle` exactly as the aider and opencode executors do, keep pre-flight failures synchronous, rewrite the `StageExecutor` protocol comment that calls codex an accepted exception, and update the spawn-level thread assertions in `tests/test_codex_executor.py` (`test_spawn_returns_before_background_work_completes`, `_sync_codex_executor`).
- **Scope:** D-1, D-5
- **Acceptance:**
  - `CodexExecutor.spawn()` returns after `Popen` with `start_new_session=True` and a `LocalLivenessHandle` stamped on the session; a test asserts both.
  - Pre-flight failures (codex binary missing, capability probe failing) still complete the session synchronously with a blocked result, exactly as before.
  - The `StageExecutor` protocol comment no longer describes codex as an exception to the liveness invariant.
  - A codex review survives a simulated serve restart in an integration-style test: the driver process keeps running and completes the session.
  - All local quality gates pass; patch coverage is at least 90%.

### B1 — One-time legacy recovery and migration gate

- **Epic:** II
- **Wave:** 3
- **Sprint:** 2
- **Depends on:** A2
- **Context:** Runs only after Epic I is released and serve is restarted onto it; add a one-time recovery pass (a `cw doctor` or `cw codex` subcommand) that processes every ACTIVE codex session with a null `local_liveness` handle through the A1 gate and audit event, records scanned/requeued/parked/failed counts plus a durable completion marker, and ships an operator runbook for reversing an incorrect transition.
- **Scope:** D-4, D-6, D-7
- **Acceptance:**
  - The pass is idempotent: a second run after the marker exists does nothing and reports that.
  - The recorded counts and marker persist across a restart, and the runbook documents the reversal command for a wrongly requeued or parked session.
  - Tests cover a mixed population (clean, dirty worktree, moved HEAD, already-handled) and assert the counts.
  - All local quality gates pass; patch coverage is at least 90%.

### B2 — Retire the boot sweep, thread registry, and shutdown drain

- **Epic:** II
- **Wave:** 4
- **Sprint:** 2
- **Depends on:** B1
- **Context:** Only after B1's completion marker exists with no unresolved failures; delete `reconcile/codex_boot.py`'s psutil process-table sweep, the codex thread registry and `join_outstanding_codex_threads` in `src/cw/codex_background.py`, the shutdown drain call in `src/cw/dispatch/loop.py`, and the `codex_threads_still_running` field of the `DISPATCH_LOOP_EXITED` payload, and rewrite or delete `tests/test_codex_background.py` accordingly.
- **Scope:** D-5, D-7
- **Acceptance:**
  - grep finds no `psutil` process scan in `reconcile/codex_boot.py` (or the module is gone) and no `join_outstanding_codex_threads` or `codex_threads_still_running` anywhere in `src/` or `tests/`.
  - `DISPATCH_LOOP_EXITED` payload tests reflect the removed field, and the CHANGELOG names the removal.
  - All local quality gates pass; total coverage stays at or above 88%.

## References

- `docs/adr/0018-codex-runs-as-a-detached-local-liveness-job.md` — the decision this RFC implements.
- `src/cw/codex_background.py:93` — `_start_daemon_thread`, the in-serve thread that A2 replaces.
- `src/cw/codex_background.py:215` — `_default_background`, the spawn hand-off A2 replaces.
- `src/cw/codex_background.py:220` — `join_outstanding_codex_threads`, removed in B2.
- `src/cw/dispatch/loop.py:900` — the shutdown drain and the `codex_threads_still_running` payload field, removed in B2.
- `src/cw/reconcile/codex_boot.py:374` — `_resolve_orphan_action`, ported into A1's gate.
- `src/cw/reconcile/codex_boot.py:405` — `_gate_clean_requeue`, ported into A1's gate.
- `src/cw/reconcile/local.py:72` — `_detect_local_harvest_candidates`, the backend-agnostic dead-PID detection A1 builds on.
- `src/cw/reconcile/local.py:113` — `_synthesize_harvest_sentinel`, replaced by #2369's per-executor lookup, which A1 extends.
- `src/cw/executor.py:253` — the `StageExecutor` protocol whose codex carve-out comment A2 rewrites (moves into `executor/core.py` with #2368).
- `tests/test_codex_executor.py:72` — `_sync_codex_executor`, the thread-model helper A2 rewrites.
- `tests/test_codex_executor.py:846` — `test_spawn_returns_before_background_work_completes`, rewritten in A2.

## Issues

Milestone: [v1.59.0 — Codex Runs as a Detached Job](https://github.com/mattwwarren/claude-workspace/milestone/16)

Epics: I #2384 · II #2385

Issues: S1 #2386 · A1 #2387 · A2 #2388 · B1 #2389 · B2 #2390

Pulled in: #2355 (inherited fix-loop race), #1868 (resolved by Epic I). Prerequisites: #2367, #2368, #2369. Follow-on: #1550.
