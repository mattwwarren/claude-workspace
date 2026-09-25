# Plan: #2286 — design(codex): detach codex reviews from serve with a durable run record (ADR)

<!-- plan-spec-reviewed: 2026-09-25 v2 -->
<!-- plan-soundness-reviewed: 2026-09-25 v1 -->

**Scope tier:** small (2 files, ~141 lines, forbidden_touched=false)

## Patterns Found

N/A — no new code abstraction proposed. This ticket produces an ADR document only; the abstractions it *describes* (a detached `cw codex run` CLI job, reuse of the existing `LocalLivenessHandle`/`Session.local_liveness` contract) are the operator's binding pre-flight resolutions (R1–R4), not something this plan invents. `src/`/`tests/` stay untouched.

## Touch-point Contract

- **Touch-point**: codex_background.py's "third bespoke background-thread" framing
  **File:line**: `src/cw/codex_background.py:1-23` (docstring)
  **Read (verbatim)**: "Threading precedent: ``cw.notify.fire_push_notification`` already fires a ``threading.Thread(daemon=True)`` for the same reason (do not make the caller wait). This is the second such occurrence in the codebase and deliberately carries no new abstraction — no thread pool, no executor service. A *third* occurrence is the trigger to stop and write an ADR for a shared background-work primitive rather than growing a third bespoke launcher."
  **Plan asserts**: This ADR IS that trigger being honored — but the resolution is not "write a shared thread primitive," it's "stop using a thread at all" (R1). The ADR states this explicitly: the third-occurrence threshold is resolved by retiring the pattern class, not extending it.
  **Match verdict**: MATCH — plan quotes the real trigger and states the real resolution.

- **Touch-point**: the actual thread spawn
  **File:line**: `src/cw/codex_background.py:93-114` (`_start_daemon_thread`), thread creation at line 110: `thread = threading.Thread(target=_wrapped, name=name, daemon=True)`
  **Plan asserts**: This whole function, plus `_default_background` (215-217) and the `_outstanding`/`_outstanding_lock` registry (89-90), is deleted as part of this ADR's implementation (a ticket in `## Tickets`, not this ticket).
  **Match verdict**: MATCH.

- **Touch-point**: `join_outstanding_codex_threads` / shutdown drain
  **File:line**: `src/cw/codex_background.py:220-246`; called from `src/cw/dispatch/loop.py:902` inside the `finally:` block at `loop.py:888-922`
  **Read (verbatim, loop.py:888-911)**: "Bounded drain of in-flight codex review threads (#1727)... `_codex_threads_still_running = join_outstanding_codex_threads()`... `"codex_threads_still_running": _codex_threads_still_running,`" on the `DISPATCH_LOOP_EXITED` payload.
  **Plan asserts**: A detached process has nothing for `serve` to join — this whole drain block and the `codex_threads_still_running` payload field go away. This is the "`DISPATCH_LOOP_EXITED` payload's thread-join semantics" the ticket calls out, and the ADR states its removal explicitly in Consequences.
  **Match verdict**: MATCH.

- **Touch-point**: `codex_runner.py` subprocess spawn
  **File:line**: `src/cw/codex_runner.py:83-91` (`RealCodexRunner.run`)
  **Read (verbatim)**: `proc = subprocess.Popen(argv, cwd=worktree, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)` — no `start_new_session`.
  **Plan asserts**: This runner is unchanged by R1 (out of scope per issue #2367's own text: "the codex runner (`src/cw/codex_runner.py:84`)... Moving codex onto the local-liveness contract is #2286's job"). R1 detaches the *whole job* (a new CLI process), not this per-role `codex exec` subprocess, which keeps running as a plain child of the detached driver.
  **Match verdict**: MATCH.

- **Touch-point**: `reconcile/codex_boot.py`'s "no PID/surface_ref" gap statement
  **File:line**: `src/cw/reconcile/codex_boot.py:62-65`
  **Read (verbatim)**: "This is a blast-radius bound, not a liveness handle: it does not make codex sessions crash-recoverable in the RFC 0005 F3 sense (no PID/surface_ref is persisted for external harvest). See the ``StageExecutor`` Protocol invariant comment in ``cw.executor`` for the accepted gap this bounds."
  **Plan asserts**: Once `LocalLivenessHandle` is recorded (R2), this gap closes — the ADR names this file's psutil-based boot sweep as **obsolete** and recommends deleting it (operator's stated recommendation, option (a)), porting its clean-requeue gate into a codex-specific branch of `reconcile/local.py`'s harvest (see next entry).
  **Match verdict**: MATCH.

- **Touch-point**: `codex_boot.py`'s clean-requeue gate (the part that survives)
  **File:line**: `src/cw/reconcile/codex_boot.py:407-426` (`_gate_clean_requeue`), `376-404` (`_resolve_orphan_action`)
  **Read (verbatim, 417-420)**: "With no writer left, requeue only a provably clean orphan under ``auto``." — checks `reap_policy is ReapPolicy.AUTO`, `_resolve_codex_fix_loop_enabled` is False, worktree porcelain-clean except the verdict file, HEAD unmoved from `task.stage_base_ref`.
  **Plan asserts**: This four-part gate is the "harvest semantics differ from aider/opencode" answer the ticket demands: reconcile/local.py's dead-PID sweep must NOT run aider's git-fact synthesis (`synthesize_git_result`) for a codex-origin candidate. It gets its own crash-only-fallback branch that runs this exact gate — requeue if clean, else park (never a synthesized "completion").
  **Match verdict**: MATCH.

- **Touch-point**: the psutil process scan itself
  **File:line**: `src/cw/reconcile/codex_boot.py:302-356` (`_codex_processes_in`, `_live_writer_park`)
  **Read (verbatim, 302-313)**: "A cwd scan is the only signal available: no PID is persisted for the codex child... Never raises, and fails closed... Only ``[]`` means no writer."
  **Plan asserts**: This entire ambiguity class (scan-inconclusive parks) disappears once liveness is PID+start-time based (`_local_process_alive`, deterministic, no cwd-scan false positives/negatives). The ADR states this as a genuine simplification, not just a relocation.
  **Match verdict**: MATCH.

- **Touch-point**: `PidSnapshot` shape reference in the ticket
  **File:line**: `src/cw/models/session.py:27-41`
  **Read (verbatim)**: `class LocalLivenessHandle(BaseModel): ... pid: int; start_time_ns: int` — frozen, "Process-liveness handle for a LocalExecutor aider subprocess (RFC 0005 F3)."
  **Plan asserts**: The ticket's own body and pre-flight comment both name this "PidSnapshot" — **no such class exists**. The real name is `LocalLivenessHandle`. The ADR uses the real name throughout and the plan flags this naming mismatch (see Discoveries in the Friction Report) rather than perpetuating it.
  **Match verdict**: MISMATCH (ticket text vs. code) — resolved by using the actual identifier.

- **Touch-point**: `Session.local_liveness` field
  **File:line**: `src/cw/models/session.py:129-134`
  **Read (verbatim)**: "RFC 0005 F3 — process-liveness handle for a fire-and-forget LocalExecutor aider subprocess. Set when LocalExecutor.spawn() launches aider and leaves the session ACTIVE; reconcile/local harvest reads it to detect the dead process... None for every non-LOCAL session (surface_ref-backed sessions use daemon-roster liveness)."
  **Plan asserts**: This comment's "None for every non-LOCAL session" becomes stale once codex also populates it — the ADR names this comment as one that must be updated (added to the "documentation that becomes false" list; not literally in the ticket's enumerated list but caught by reading the field).
  **Match verdict**: MATCH, with an additional finding beyond the ticket's named list.

- **Touch-point**: `LocalExecutor` (aider) spawn — the pattern to mirror
  **File:line**: `src/cw/executor.py:425-593`; launch call at line 511 (`proc = self._runner.launch(worktree, argv, env)`); liveness stamp at 512-523
  **Read (verbatim, class docstring 428-434)**: "spawn() is non-blocking on the launch path: after synchronous pre-flight checks, it launches aider via ``AiderRunner.launch`` (Popen, no wait), records a ``Session.local_liveness`` handle (PID + /proc start-time), leaves the session ACTIVE, and returns the sid immediately."
  **Plan asserts**: This is the exact shape `CodexExecutor.spawn()` adopts (R2) — pre-flight synchronous, launch fire-and-forget, stamp `LocalLivenessHandle`, return sid. The ADR's "What this means for producers" section states this mirroring explicitly, line-for-line against this docstring.
  **Match verdict**: MATCH.

- **Touch-point**: `OpencodeExecutor` spawn — the second precedent
  **File:line**: `src/cw/executor.py:674-817`; launch call at line 748; liveness stamp 749-760
  **Plan asserts**: Same shape as LocalExecutor, confirming R2's "exactly as `LocalExecutor`/`OpencodeExecutor` do" is accurate for both existing executors, not just one.
  **Match verdict**: MATCH.

- **Touch-point**: `start_new_session=True` claim in R4 / #2367
  **File:line**: `src/cw/local_runner.py:164-187` (`RealAiderRunner.launch`); `src/cw/opencode_runner.py:126-150` (`RealOpencodeRunner.launch`)
  **Read (verbatim, local_runner.py Popen call)**: `subprocess.Popen(argv, env=env, cwd=worktree, stdout=log_file, stderr=subprocess.STDOUT)` — **no `start_new_session` argument**. Identical shape in `opencode_runner.py`.
  **Plan asserts**: `grep -rn "start_new_session" src/` across the whole repo returns **zero hits**. GitHub issue #2367 ("fix(executor): launch aider/opencode children with start_new_session...") is confirmed **OPEN**, not merged — its own body states "`grep -rn start_new_session src/cw/` has no hits on main (v1.57.0)." R4's premise ("#2367 does this for aider and opencode... The codex driver adopts it from the start") is therefore a **forward dependency, not yet landed** in this branch. The ADR states this as a stated prerequisite (referenced in `## Tickets` and `Referenced by`), not as already-true code.
  **Match verdict**: MISMATCH between ticket narrative ("#2367 does this") and current repo state (#2367 still open) — resolved by stating the dependency explicitly rather than asserting it's done.

- **Touch-point**: `reconcile/local.py` dead-PID sweep
  **File:line**: `src/cw/reconcile/local.py:60-69` (`_local_process_alive`), `72-110` (`_detect_local_harvest_candidates`), `113-146` (`_synthesize_harvest_sentinel`), `149-259` (`_act_on_local_harvest_candidates`)
  **Read (verbatim, 113-125)**: "If ``.cw/opencode.log`` exists, the session was spawned by OpencodeExecutor — parse the JSONL log for the sentinel. Otherwise, fall back to git-fact synthesis (aider path)."
  **Plan asserts**: This binary branch (opencode-log-exists vs. git-fallback) is exactly where a third, codex-shaped branch would currently fall through incorrectly into git-synthesis if codex naively joined this contract. The ADR states the fix is #2369's "explicit per-executor harvest lookup," not a third `if` bolted onto this function — directly answering the "harvest semantics differ" required decision.
  **Match verdict**: MATCH — plan does not claim this function already handles codex; it names the extraction ticket that must add the branch correctly.

- **Touch-point**: `StageExecutor` Protocol docstring's "accepted, documented exception"
  **File:line**: `src/cw/executor.py:252-276`
  **Read (verbatim, 268-276)**: "CodexExecutor is an accepted, documented exception (#1727): it no longer blocks the caller (it hands the review to a ``cw.codex_background`` daemon thread and returns) but still carries no liveness handle, so its session is not crash-recoverable via harvest... The blast radius is bounded, not closed, from two sides: ``run_dispatch_loop``'s shutdown path bounded-joins outstanding codex threads before DISPATCH_LOOP_EXITED... and ``cw.reconcile.codex_boot`` flags any codex session still ACTIVE at the next boot."
  **Plan asserts**: This entire paragraph becomes false once CodexExecutor carries `Session.local_liveness` like Local/Opencode. The ADR states it must be rewritten to remove CodexExecutor from the "exception" list entirely — it becomes a plain conformer to the invariant's first sentence, not a carve-out.
  **Match verdict**: MATCH.

- **Touch-point**: `cw.codex_background`'s lazy import of `cw.executor` (the import cycle)
  **File:line**: `src/cw/codex_background.py:682-690` (inside `_run_codex_review_and_complete`), `822-824` (except branch)
  **Read (verbatim, 682-690)**: "Function-level import breaks the cw.executor <-> cw.codex_background cycle: executor.py imports this module at its top, and the name below is defined *after* executor.py's own import block, so a module-level import here would hit a partially initialized module."
  **Plan asserts**: `executor.py:13-18` confirms the forward direction — `from cw.codex_background import (_complete_session_as_unexpected_error, _default_background, _run_codex_review_and_complete, _stamp_session_id_on_running_task)` at module top. The ADR resolves this per the ticket's explicit-decision requirement: the new driver module (recommendation: a new `src/cw/codex_driver.py`, sibling to `codex_background.py`/`codex_runner.py`) is invoked only as a **subprocess** (`cw codex run ...`) from `CodexExecutor.spawn()` — `executor.py` never imports the driver module at all, only shells out to it. The cycle is eliminated structurally by the process boundary, not deferred by a function-level import.
  **Match verdict**: MATCH — plan resolves the ticket's open decision with a concrete, code-grounded answer.

- **Touch-point**: `max_parallel=1 lanes` concurrency claim
  **File:line**: `src/cw/executor.py:701` (OpencodeExecutor docstring)
  **Read (verbatim)**: "opencode has no ``--output-schema`` (probe-confirmed, #1669 R3)... Appropriate only for max_parallel=1 lanes (mirrors CodexExecutor)."
  **Plan asserts**: `grep -rn "max_parallel" src/cw/` (full results reviewed) finds **no code that enforces "=1" specifically for codex or opencode** — every `max_parallel` hit is the general, backend-agnostic `LaneConfig.max_parallel` / dispatch-admission machinery (`dispatch/lanes.py:738`, `dispatch/tick.py`, `models/orchestrator_config.py`). This is advisory prose in one docstring, not an enforced code path. The ADR states this plainly: detaching the whole codex job changes nothing about concurrency admission, because nothing codex-specific exists to change — lane occupancy (ADR-0006, `running + blocked + signoff`) is already backend-agnostic.
  **Match verdict**: MATCH — resolved with evidence (grep), not guessed.

- **Touch-point**: `tests/test_codex_background.py` — thread-model tests
  **File:line**: whole file; specifically the `_outstanding`/`join_outstanding_codex_threads` tests (lines 82-248 range) and `test_module_docstring_names_the_threading_precedent` (line 248)
  **Plan asserts**: This file tests a mechanism (`_start_daemon_thread`, `_outstanding` registry, `join_outstanding_codex_threads`, the "second/third occurrence" docstring text) that a detached-process model deletes wholesale. The ADR's `## Tickets` names the ticket that rewrites/retires this file.
  **Match verdict**: MATCH.

- **Touch-point**: `tests/test_codex_executor.py` thread-registry assertions
  **File:line**: `test_spawn_returns_before_background_work_completes` (lines 846-901), which asserts `len(codex_background._outstanding) == 1` while a review blocks, and calls `join_outstanding_codex_threads`; also the `_sync_codex_executor` helper (lines 75-84) that injects `background=lambda fn: fn()` as the test seam for the (soon-gone) threading handoff
  **Plan asserts**: Both are pinned to the in-process thread model and must be rewritten for a Popen-based detached launch (assert on `Session.local_liveness` / a mocked launcher, not on a thread registry). Named as the same implementation ticket as `test_codex_background.py`'s rewrite, or a paired one — the ADR's `## Tickets` states this.
  **Match verdict**: MATCH.

## Pre-flight Resolution Conformance

- R1: Detach the whole codex job via a new `cw codex run <task> --stage <review|impl>` CLI entry point; serve no longer hosts a codex thread — the ADR's Decision section states this as the chosen design, names the new driver module (`src/cw/codex_driver.py`), and names every file the change touches (in `## Tickets`) [SATISFIED]
- R2: Reuse the existing local-liveness contract exactly as `LocalExecutor`/`OpencodeExecutor` do (`LocalLivenessHandle` on `Session.local_liveness`, `reconcile/local.py` dead-PID detection); no new contract, no dev-queue schema bump — the ADR's Invariant and "What this means for producers" sections state this reuse explicitly and quote the mirrored docstrings [SATISFIED]
- R3: Stage-agnostic driver contract from the start — Phase 1 (this ADR) covers REVIEW only, but the contract (CLI shape, liveness handle, completion-via-door semantics) is written generally enough for #1550 (IMPL) to slot in as a second `--stage` value with no redesign — the Decision and `## Tickets` both name #1550 as the stage-2 consumer of the same contract [SATISFIED]
- R4: Every local-liveness executor launches with `start_new_session=True` from the start, same as #2367 for aider/opencode — the ADR states this as a stated dependency (Referenced by: #2367), correctly notes #2367 is currently OPEN/unmerged (verified via `gh issue view` and a repo-wide grep with zero hits), and the codex driver's own launch call adopts the flag directly rather than waiting for a shim [SATISFIED]

## Phase 1 (tests)

N/A — docs-only ticket, no `src/` or `tests/` changes permitted (per the binding deliverable constraint). No test phase.

## Phase 2 (implementation)

Two file operations, both edits/creates under `docs/adr/`.

**1. Create `docs/adr/0018-codex-runs-as-a-detached-local-liveness-job.md`**, following `docs/adr/template.md`'s exact section order, content per section:

- **Title**: `Codex runs as a detached, local-liveness-tracked job` (short, declarative, present tense per template)
- **Status**: `Accepted` — Driven by: `#2286`. Rationale for Accepted-not-Proposed: every design fork the template's "Status" implies discussion over is already operator-resolved (R1-R4, binding); comparable precedent is ADR-0013 ("Accepted") which also documents a decision ahead of full rollout. (ADR-0007/0008 are the counter-examples — genuinely still open questions — which this is not.)
- **Decision**: the two-sentence summary drafted above — CodexExecutor becomes a third fire-and-forget `LocalLivenessHandle`-based launcher (mirroring Local/Opencode) instead of an in-process thread; the detached process runs the entire job (review, fix loop, push-and-verify, verdict persistence, GitHub comment, door completion) and is the normal completion path, with reconcile/local's dead-PID detection as a crash-only fallback.
- **Invariant**: the 5-item numbered list drafted above (non-blocking spawn; every codex job carries a liveness handle; the detached driver is the sole normal completion path; crash-fallback harvest requeues only under the ported 4-part clean gate else parks; the process-table boot sweep is retired).
- **What this means for callers**: `reconcile/local.py`'s harvest dispatch gains an explicit per-executor branch (built by #2369) so a dead codex PID with no recorded completion routes to the ported clean-requeue gate, never to aider's git-fact synthesis or opencode's log-parse.
- **What this means for producers**: `CodexExecutor.spawn()` launches `cw codex run` via `Popen(..., start_new_session=True)` instead of `_default_background`; stamps `LocalLivenessHandle` exactly where Local/Opencode do; the new `src/cw/codex_driver.py` module owns the CLI entry point and is invoked only as a subprocess by `spawn()` — never imported by `cw.executor` — which is how the `cw.executor`↔`cw.codex_background` import cycle is eliminated (structurally, by process boundary) rather than deferred.
- **Consequences**: loses `join_outstanding_codex_threads`'s bounded shutdown drain and the `DISPATCH_LOOP_EXITED.codex_threads_still_running` field (nothing left for `serve` to join); loses the psutil cwd-scan's "scan inconclusive" ambiguity class entirely (PID+start-time is deterministic — a real simplification, stated as a benefit); gains a new CLI surface and subprocess-launch failure modes that pre-flight (endpoint/binary missing) must still complete synchronously exactly as Local/Opencode's pre-flight does today; `reconcile/local.py` needs a third harvest branch, so its dispatch can no longer be a bare two-way `if opencode_log.exists()`.
- **Alternatives considered**: detaching only the `codex` subprocess (the ticket's original, non-operator-resolved proposal) — rejected because it leaves fix-loop commits made after `serve` exits unaccounted for; whereas detaching the whole job means the same process doing the committing is the one still running and still liveness-tracked.
- **Referenced by**: `#2286, #1550, #2367, #2368, #2369, #2370`
- **`## Tickets`** (new section this ticket adds, after Referenced by): the 7-item dispatchable breakdown drafted above — (1) `cw codex run` CLI entry point + driver module, (2) `CodexExecutor.spawn()` rewire to Popen+liveness-stamp — **sequencing note (Plan Soundness Reviewer finding): must be marked blocked-by ticket (3) below at dispatch time**, since shipping (2) before (3)/#2369 lands would let a dead codex PID fall through `reconcile/local.py`'s existing binary check into aider's git-fact synthesis (a wrong completion for a codex session) — (3) `reconcile/local.py` per-executor harvest branch for codex's clean-requeue-or-park fallback (depends on #2369), (4) delete `codex_boot.py`'s psutil sweep + `codex_background`'s thread registry + shutdown drain, update `DISPATCH_LOOP_EXITED` payload, (5) rewrite the `StageExecutor` Protocol docstring, (6) rewrite `tests/test_codex_background.py` and the thread-registry assertions in `tests/test_codex_executor.py`, (7) extend the driver to `--stage impl` for #1550.

**2. Edit `docs/adr/README.md`** — insert one row into the Index table, in number order, immediately after the `[0017]` row (line 62) and before the closing "ADR-0000 is the foundational record" paragraph:

```
| [0018](0018-codex-runs-as-a-detached-local-liveness-job.md) | Codex runs as a detached, local-liveness-tracked job, not an in-process thread | Accepted |
```

No other file changes. `ARCHITECTURE.md`'s own ADR reference table (lines 358-370) is a candidate for the same row, but touching it is out of scope for this ticket's binding deliverable list (ADR file + README.md index only) — flagged as a Discovery below for a follow-up, not done here.

## Files Modified

- `docs/adr/0018-codex-runs-as-a-detached-local-liveness-job.md` (new, ~140 lines)
- `docs/adr/README.md` (~1 line)

## Ambiguities

NO_AMBIGUITIES

(Every fork the ticket flagged as "the ADR must decide explicitly" — boot-sweep fate, harvest semantics, stale docs, import cycle, concurrency/max_parallel, which ticket rewrites the pinned tests — was resolved above with code-grounded evidence, not left open. The one genuine judgment call, the ADR's `Status:` value, was resolved with cited precedent (ADR-0013) rather than parked.)

## Pre-flight verification

`docs/adr/0018-*.md` does not exist (`ls docs/adr/` shows `0000`–`0017` plus `README.md`/`template.md` only); `docs/adr/README.md`'s index stops at `[0017]`. Not already satisfied — proceeding with the normal plan above.

## Review notes (Plan Quality Review, Stage 1)

- **Plan Reviewer**: initial pass found one MUST_FIX (Touch-point Contract entry for `tests/test_codex_executor.py` cited a fabricated helper name `_inline_codex_executor`; real name is `_sync_codex_executor`, verified at line 72). Fixed in one revision cycle; re-review returned NO_ISSUES.
- **Plan Soundness Reviewer**: NO_ISSUES against ARCHITECTURE.md §7/§8 Tier 1 and Tier 2 Risk Radar shapes. Flagged one non-blocking sequencing discovery (folded into `## Tickets` item (2) above as a dependency note) — not a §7/§8 violation or Risk Radar shape, so it did not gate this plan.

## Friction Report
- **Level**: INFO
- **Scope**: 2 files changed (1 new, 1 edited), ~140 lines
- **Assumptions**: (1) ADR Status = `Accepted` rather than `Proposed`, reasoned from ADR-0013's precedent (decision settled, rollout pending) since every fork is operator pre-resolved. (2) New driver module path recommended as `src/cw/codex_driver.py` — a naming choice, not specified by the ticket; any implementation ticket is free to rename it without touching this ADR's substance.
- **Deviations**: NONE
- **Discoveries**: (1) The ticket/pre-flight comment refers to "`PidSnapshot`" at `models/session.py:31-40` — no such class exists; the real type is `LocalLivenessHandle` (models/session.py:27-41). Used the real name throughout. (2) Issue #2367 (start_new_session for aider/opencode), which R4 treats as landed prep work ("#2367 does this for aider and opencode"), is confirmed still **OPEN** — `grep -rn start_new_session src/` returns zero hits repo-wide, and #2367's own body confirms this on main v1.57.0. The ADR states this as a stated dependency rather than an accomplished fact. (3) `Session.local_liveness`'s field comment ("None for every non-LOCAL session") at `models/session.py:133` becomes stale once codex populates it too — added to the ADR's "documentation that becomes false" list, beyond the ticket's own enumerated set. (4) No code anywhere enforces "max_parallel=1" for codex or opencode specifically — it's advisory docstring prose only (`executor.py:701`); real admission control is the backend-agnostic lane-occupancy machinery (ADR-0006). (5) `ARCHITECTURE.md`'s own ADR reference table is a natural place for a matching 0018 row but is outside this ticket's binding deliverable list — left untouched, noted for a follow-up. (6) Plan Soundness Reviewer flagged that `## Tickets` item (2) should be marked blocked-by item (3)/#2369 at dispatch time — folded into the plan above.
- **Risks**: NONE — docs-only, no shared code touched, no interface changes in this ticket. The ADR itself, once merged, becomes the binding contract for #2286's implementation tickets, which is the intended risk transfer (design risk resolved here, before code is written).

## Health Check
- **Context usage**: MEDIUM
- **On-spec confidence**: HIGH
- **Shortcuts taken under pressure**: NONE
- **Could work be incomplete?**: NO
- **Recommendation**: PROCEED


<!-- cw-agent-authored -->
