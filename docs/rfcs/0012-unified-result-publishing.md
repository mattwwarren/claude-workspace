# RFC 0012 — Unified result publishing: one door, per-backend harvest authorities

## Summary

Every backend today delivers its `AutoDevResult` end state through a different
mechanism: Claude daemon workers print a transcript sentinel that the Stop hook
(`cw signal-stop`) parses; `CodexExecutor` and `LocalExecutor` assign
`session.last_result` directly in-process; `reconcile/local.py` synthesizes a
result from git facts; reconcile's phantom/idle/stalled sweeps salvage-parse
transcripts; and `cw result emit` (#536) offers an authoritative manual push
that nothing automated calls. All of these converge on the same
`session.last_result` dict — but each writer carries its own copy of the
don't-clobber guard, nothing records which mechanism wrote the result, and one
write path (`persist_last_result` consuming a `stdout` event payload) has no
live producer at all.

This RFC unifies the model without changing the wire formats that work. The
invariant: **every backend has a designated harvest authority that pushes
through one validated door** — an importable `emit_result()` extracted from
`cw result emit`'s internals. Supervised-child backends (codex, aider/local)
harvest directly in their executor; the detached Claude daemon harvests via the
Stop hook; reconcile's transcript scraping remains only as explicitly labeled
salvage. The door centralizes validation, lock discipline, first-writer-wins
arbitration, and a new provenance field recording who wrote the result.
Consumers read `session.last_result` only — never transcripts.

A future backend (e.g. opencode) slots in by answering one question: is cw its
supervising parent? If yes, its executor is the harvest authority (prefer
structured output enforced at the tool level, as codex does with
`--output-schema`). If no, it needs a stop-signal harvest path like Claude's.

## Motivation

The 2026-07 codex-review incident cluster (#1390, #1391, #1392) showed how an
end state can be computed and then lost: finding text dropped from the sentinel,
gh comment posts silently discarded, tickets parking BLOCKED_ON_USER four times
with no readable cause. The root enabler is architectural: result delivery is
five mechanisms with no shared contract, so each new path re-invents (or
forgets) validation, error surfacing, and clobber protection.

Evidence of the current fragmentation (all refs at main `16e24e9`):

- `cli/sessions.py:478-491` — Stop hook skips transcript parse when
  `last_result` already carries `"status"` (#536 precedence). The guard is
  correct — and duplicated at `reconcile/phantom.py:164-172` and
  `reconcile/idle.py:441`, per-caller rather than centralized. Nothing
  structurally prevents a writer that skips the check from clobbering a
  terminal result.
- `result.py:179-233` — `cw result emit` validates before writing and is
  documented write-only, but the sequence is inlined in the Click command.
  There is no importable function; an in-process caller must shell out or
  hand-roll the ~15-line lock block.
- `executor.py:452-459, 614-621` and `reconcile/local.py:183` — three direct
  `session.last_result =` assignments, each with its own locking and no shared
  validation gate.
- `dispatch/loop.py:209-213, 230-257` — `persist_last_result` parses a
  `stdout` key from SESSION_COMPLETED payloads that **no producer sets**
  (verified across every emit site). Dead scaffolding that would double-write
  if ever activated.
- No provenance: an emitted result, a hook-harvested result, an
  executor-written result, and a salvage-scraped result are indistinguishable
  in state. Operators debugging a wedged ticket cannot tell how its
  `last_result` got there.

What already works and is deliberately kept: the sentinel-in-transcript wire
format for Claude workers (printing a sentinel is the most reliable behavior
an LLM worker can perform; #536 already demoted parsing to a harvest detail),
codex's `--output-schema` structural enforcement, aider's git-facts synthesis,
and dispatch routing Rule 6 (`routing.py:709-730`: missing/unparseable result
→ conservative BLOCKED_ON_USER).

## Design

### Epic I — The door: importable emit, provenance, write-time arbitration

Extract the validate→resolve→lock→write sequence from the `result_emit` Click
command into an importable `emit_result()` in `cw/result.py` (or a new
`cw/result_door.py` if circular imports demand it). The CLI becomes a thin
wrapper. The door owns, for every caller: Pydantic validation
(`AutoDevResult.model_validate`) strictly before state I/O; `sessions_lock()`
acquisition (callers already holding the lock use a `_locked` variant —
signal_stop writes inside its existing dual-lock block); **first-writer-wins
arbitration** — if the session already has a terminal `last_result`, the door
refuses the write, logs the collision with both sources, and returns the
existing result (centralizing the guard currently duplicated at
`phantom.py:172`, `idle.py:441`, `sessions.py:485`); and stamping a new
`Session.last_result_source` field — enum of `emit_cli`, `stop_hook_harvest`,
`executor_direct`, `git_synthesis`, `salvage_transcript`; `None` for
pre-migration state files (no state migration needed). Provenance lives on
`Session` beside `last_result`, NOT inside `AutoDevResult` — the sentinel is
the worker's contract, provenance is cw's write metadata; this avoids a
sentinel schema_version bump. The door stays write-only: it emits no events
and routes no tasks (the #536 separation — the Stop hook remains the sole
completion-event source, `_apply_sentinel_to_task` stays with its callers).

### Epic II — Writer migration, retirement, enforcement

Route every existing writer through the door, each stamping its source:
`signal_stop`'s harvest write (`stop_hook_harvest`); `CodexExecutor` and
`LocalExecutor` direct writes (`executor_direct`); `reconcile/local.py`
git-facts synthesis (`git_synthesis`); phantom/idle/stalled
`salvage_terminal_result` (`salvage_transcript`). Behavior is preserved — the
salvage paths already fire only when no terminal result exists, which is
exactly the door's arbitration rule. Then retire `persist_last_result` and the
`stdout`-payload branch of `consume_completed_sessions` (dead code with no
live producer; Rule 6's conservative fallback for absent results is
unchanged). Finally, enforce the invariant with a contract test asserting no
`last_result` assignment exists outside the door module, and document the
harvest-authority model in `docs/headless-contract.md`: read-only transcript
consumers (`dev-queue wait`, `queue_peek`, cw-followup's parser) are
explicitly blessed as display/forensic surfaces that never write state.

## Explicitly out of scope

- Workers invoking `cw result emit` themselves (LLM CLI discipline is less
  reliable than printing a sentinel; the hook harvests instead).
- Any change to the sentinel wire format, `AutoDevResult` schema fields, or
  schema_version.
- New backends (opencode etc.) — this RFC defines the slot-in rule only.
- Dispatch routing semantics (Rule 6 conservative fallback stays).
- The codex fix-loop adapter (#1392) and review-reliability fixes (#1390,
  #1391, #1061) — the "Yay Codex" wave-1/wave-2 track, orthogonal to this.

## Phasing

| Wave | Track A (door) | Track B (migration) |
|------|----------------|---------------------|
| 0 (seams, blocking) | S1 — extract `emit_result()` · S2 — provenance + arbitration | — |
| 1 | — | A1 — Stop hook · A2 — executors · A3 — reconcile/salvage |
| 2 | — | B1 — retire dead path · B2 — enforcement test + docs |

## Resolved decisions

- **D-S1 — Importable door, thin CLI.** `emit_result()` extracted from the
  `result_emit` Click command; CLI behavior byte-identical (same exit codes,
  same stderr shapes). A `_locked` variant serves callers already inside
  `sessions_lock()`.
- **D-S2 — Provenance on Session, not in the sentinel.** New optional
  `Session.last_result_source` enum field; `None` means pre-migration. No
  `AutoDevResult` schema bump, no state-file migration.
- **D-S3 — First-writer-wins at the door.** The door refuses to overwrite a
  terminal `last_result`, logs the collision (existing source + attempted
  source), and returns the existing result. Per-caller `_has_terminal_sentinel`
  pre-checks become redundant and are removed where the door now covers them.
- **D-A1 — Door writes state; callers keep events and routing.** The Stop hook
  remains the sole completion-event source; `_apply_sentinel_to_task` call
  sites are unchanged. Exception paths in executors that deliberately skip
  SESSION_COMPLETED (dispatch reverts the task) keep that behavior.
- **D-B1 — Retire, don't formalize, the stdout path.** `persist_last_result`
  and the `stdout`-payload branch are deleted; SESSION_COMPLETED payload shape
  is unchanged for all live producers. Rule 6 handles absent results as today.
- **D-B2 — Enforcement is a test, not a convention.** A unit test greps/ASTs
  `src/cw/` for `last_result` assignments outside the door module and fails on
  any new bypass.

## Tickets

### S1 — Extract importable emit_result() door from the result-emit CLI

- **Epic:** I
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** `cw result emit` inlines validate→resolve→lock→write in the Click command (`result.py:179-233`); in-process callers must shell out or hand-roll the lock block. Extract `emit_result()` (plus a `_locked` variant for callers already holding `sessions_lock()`), keeping CLI behavior byte-identical.
- **Scope:** D-S1, D-A1
- **Acceptance:**
  - `emit_result()` is importable and unit-tested; the Click command is a thin wrapper over it.
  - CLI exit codes and stderr output for valid/invalid payloads and missing-session cases are unchanged (existing CLI tests pass unmodified).
  - The door emits no events and performs no task routing.

### S2 — Provenance field and centralized first-writer-wins arbitration in the door

- **Epic:** I
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** S1
- **Context:** No field records which mechanism wrote `last_result`, and the don't-clobber guard is duplicated per-caller (`phantom.py:172`, `idle.py:441`, `sessions.py:485`). Add optional `Session.last_result_source` (enum: emit_cli, stop_hook_harvest, executor_direct, git_synthesis, salvage_transcript; None = pre-migration) and make the door refuse to overwrite a terminal result, logging the collision with both sources.
- **Scope:** D-S2, D-S3
- **Acceptance:**
  - Door writes stamp `last_result_source`; `cw result emit` stamps emit_cli.
  - A second write against a terminal `last_result` is refused, logged with existing+attempted source, and returns the existing result (unit-tested).
  - Loading a pre-migration `sessions.json` (no provenance field) round-trips cleanly with `last_result_source=None`.

### A1 — Route the Stop-hook harvest write through the door

- **Epic:** II
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S2
- **Context:** `signal_stop`'s transcript-harvest path writes `session.last_result` inside its dual-lock block (`cli/sessions.py`); route that write through the door's `_locked` variant with source stop_hook_harvest, keeping event emission, `_apply_sentinel_to_task` routing, the #536 emit-precedence check, the bg_tasks defer, and the Layer-1 budget gate exactly as they are.
- **Scope:** D-A1, D-S3
- **Acceptance:**
  - Harvested results carry `last_result_source=stop_hook_harvest`; emitted-then-stopped sessions keep their original source (door refuses the second write; hook proceeds with the existing result).
  - All existing signal-stop tests pass; no change to SESSION_COMPLETED payload shape.

### A2 — Route CodexExecutor and LocalExecutor direct writes through the door

- **Epic:** II
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S2
- **Context:** `executor.py:452-459` (LocalExecutor pre-flight/liveness failures) and `executor.py:614-621` (CodexExecutor synchronous completion) assign `last_result` directly; route both through the door with source executor_direct, preserving each path's session-status transition, event emission (or deliberate non-emission on exception paths), and lock ordering.
- **Scope:** D-A1, D-S3
- **Acceptance:**
  - Both executor writes go through the door and stamp executor_direct; direct `last_result` assignment is gone from `executor.py`.
  - Existing executor tests pass; exception paths still skip SESSION_COMPLETED so dispatch reverts the task.

### A3 — Route reconcile git-synthesis and transcript salvage through the door

- **Epic:** II
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S2
- **Context:** `reconcile/local.py:183` (git-facts synthesis) and `salvage_terminal_result` callers in phantom/idle/stalled write `last_result` from synthesis or transcript parse; route them through the door with sources git_synthesis and salvage_transcript respectively. Salvage already fires only when no terminal result exists, matching the door's arbitration; remove the now-redundant per-caller `_has_terminal_sentinel` pre-checks only where the door covers them. Bundling both sources is deliberate: all four call sites live in reconcile, share the salvage-only firing condition, and are the same one-pattern mechanical swap — unlike A1/A2's stateful hook and executor paths.
- **Scope:** D-A1, D-S3
- **Acceptance:**
  - Salvaged results are distinguishable in state (`salvage_transcript`); git-synthesized results carry git_synthesis.
  - Reconcile behavior is otherwise unchanged (existing phantom/idle/stalled/local tests pass).

### B1 — Retire persist_last_result and the stdout event-payload branch

- **Epic:** II
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** A1, A2, A3
- **Context:** `dispatch/loop.py:230-257` parses a `stdout` key that no SESSION_COMPLETED producer sets (verified across all emit sites) — dead scaffolding that would double-write outside the door if activated. Delete `persist_last_result` and the `stdout` branch in `consume_completed_sessions` (`loop.py:209-213`); Rule 6's conservative handling of absent results is unchanged.
- **Scope:** D-B1
- **Acceptance:**
  - `persist_last_result` and the stdout-payload branch are gone; no SESSION_COMPLETED payload shape change for live producers.
  - Dispatch tests covering the #694 ordering comment are updated to reflect the door-populated `last_result`; Rule 6 fallback tests pass unchanged.

### B2 — Enforcement test and harvest-authority documentation

- **Epic:** II
- **Wave:** 2
- **Sprint:** 2
- **Depends on:** B1
- **Context:** Make the invariant self-enforcing: a contract test that fails on any `last_result` assignment outside the door module, plus documentation of the harvest-authority model (per-backend authorities, the salvage label, the opencode slot-in rule) in `docs/headless-contract.md`, and explicit blessing of read-only transcript surfaces (`dev-queue wait`, `queue_peek`, cw-followup parser) as never-writing.
- **Scope:** D-B2
- **Acceptance:**
  - A unit test enumerates `last_result` assignment sites in `src/cw/` and fails if any exist outside the door module (self-documenting allowlist).
  - `docs/headless-contract.md` documents the harvest-authority model and provenance enum; `config/CONFIG_REFERENCE.md` cross-references it where backends are described.

## References

- `#536` — `cw result emit` + emit-precedence in the Stop hook (the door's seed).
- `#694` — the ordering bug that motivated pre-advance `last_result` persistence.
- `#1031` / `#1019` — staged-advance authority; sentinel refusal semantics.
- `#1390` / `#1391` / `#1392` — codex-review incident cluster (motivation).
- `RFC 0005` — executor backends (E1), local backend (F3).
- `docs/headless-contract.md` — the sentinel contract this RFC leaves unchanged.
