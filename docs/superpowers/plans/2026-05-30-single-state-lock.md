# Single State Lock — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Driven by:** ADR 0005 (`docs/adr/0005-single-state-lock.md`), RFC 0004 State-integrity finding **S1**. Tracked by **#387 (T1, foundation — Tasks 1-3)** and **#388 (T2, sweep — Task 4)**.

**Goal:** Make every mutation of `sessions.json` (`CwState`) pass through a single `mutate_state()` helper that holds an exclusive file lock across the whole `load_state → mutate → save_state` cycle, closing the last-writer-wins race between the worker Stop-hook (`wrapper.signal_*`, out-of-process) and the orchestrator loop (`reconcile` / `spawn`, in the dispatcher process).

**Architecture:** Add a `state_lock()` context manager and a `mutate_state(fn)` helper to `src/cw/config.py`, mirroring the existing `_queue_lock` (`queue.py:32`), `dev_queue` lock (`dev_queue.py:40`), and `events.py:42` flock patterns. `state_lock()` acquires an exclusive `fcntl.flock` on a new `STATE_LOCK = STATE_DIR / ".state.lock"` path constant. `mutate_state(fn)` opens the lock, calls `load_state()`, applies `fn(state)` (which mutates in place or returns a new state), calls `save_state(state)`, and releases — all inside the held lock. `save_state` already does an atomic temp+rename write (`config.py:287`), so no torn files exist today; the lock closes the read-modify-write *interleaving*, not file tearing. Read-only callers keep using bare `load_state()` — a stale read is acceptable, a lost write is not.

**Lock ordering (deadlock guard):** Some paths hold both the state lock and the dev-queue lock (`wrapper.signal_completed`, `dispatch.dispatch_tick`). The required acquisition order is **`state_lock()` → `dev_queue_lock()`**, never the reverse. Encode this as a docstring invariant on `state_lock()` and verify it in review for every call site that touches both.

**Tech Stack:** Python 3.12+, Pydantic, Click, pytest, uv, `fcntl` advisory locks. Follows existing cw patterns: file-locked state access, `tmp_config_dir` autouse fixture for path isolation.

---

## File Structure

**Files to modify:**
- `src/cw/config.py` — add `STATE_LOCK` constant (near `DEV_QUEUE_LOCK`, line ~60), a `state_lock()` accessor (mirroring `dev_queue_lock()` at line ~123), a `state_lock()` context manager, and a `mutate_state(fn)` helper. Keep `load_state` / `save_state` unchanged so read-only callers are untouched.
- `src/cw/wrapper.py` — migrate `signal_needs_attention` (line ~180/218), `signal_idle` (~329/338), `signal_completed` (~380/406) to `mutate_state`. **Highest priority** — these are the out-of-process Stop-hook writers.
- `src/cw/dispatch.py` — migrate state mutations in `dispatch_tick`; where it also takes the dev-queue lock, enforce `state_lock → dev_queue_lock` order.
- `src/cw/reconcile.py` — migrate the phantom-revert / TIMED_OUT mutation; this is the loop-side writer that races the Stop-hook. Remove/replace the stale "concurrent writers are rare" note (lines ~19-25).
- `src/cw/spawn.py` — migrate the append-new-session write in `spawn_create_impl`.

**Files to modify (T2 — mechanical sweep, lower contention):**
- `src/cw/cli.py`, `src/cw/session.py`, `src/cw/doctor.py`, `src/cw/orchestrate.py` — migrate remaining lifecycle/retirement writers to `mutate_state` for uniformity. No new races closed here; this is so the "all mutations go through one choke point" invariant is literally true and greppable.

**Tests to modify/create:**
- `tests/test_config.py` — `state_lock` acquire/release; `mutate_state` applies fn and persists; lock is released on exception (does not wedge).
- `tests/test_wrapper.py` — `signal_*` still produce the same state; a concurrent-writer test (two `mutate_state` calls serialize, both updates survive).
- `tests/test_dispatch.py`, `tests/test_reconcile.py` — existing behavior unchanged; add a lock-ordering regression note where both locks are held.

**Responsibility boundaries:**
- `cw.config` owns: the lock primitive + `mutate_state` choke point. It knows *how* to serialize state writes.
- Every other module owns: *what* mutation to apply (the `fn`). None of them open the lock directly.

---

## Task 1: Add `state_lock()` + `mutate_state()` to config.py

**Why first:** every other task depends on this primitive. Pin it down before migrating any writer.

**Files:** Modify `src/cw/config.py`; Test `tests/test_config.py`.

- [ ] **Step 1:** Add `STATE_LOCK = STATE_DIR / ".state.lock"` near `DEV_QUEUE_LOCK` (config.py:60) and a `state_lock()` accessor mirroring `dev_queue_lock()` (config.py:123).
- [ ] **Step 2:** Write failing tests in `tests/test_config.py`: (a) `mutate_state` applies the callback and the change is visible via a fresh `load_state`; (b) an exception inside the callback releases the lock (a subsequent `mutate_state` succeeds, does not block).
- [ ] **Step 3:** Implement `state_lock()` as a `@contextmanager` doing `fcntl.flock(fd, LOCK_EX)` / `LOCK_UN` on `STATE_LOCK`, copying the `queue.py:32-41` shape. Add the lock-ordering invariant to its docstring: *"If you also hold the dev-queue lock, acquire state_lock FIRST."*
- [ ] **Step 4:** Implement `mutate_state(fn: Callable[[CwState], None]) -> CwState` that opens `state_lock()`, `load_state()`, `fn(state)`, `save_state(state)`, returns `state`.
- [ ] **Step 5:** `ruff check` + `mypy` + the new tests pass.

## Task 2: Migrate `wrapper.signal_*` (the dominant race)

**Why second:** these are the out-of-process Stop-hook writers — the side of the race cw cannot otherwise control. Migrating them + the loop writers (Task 3) closes the dominant race even before the full sweep.

**Files:** Modify `src/cw/wrapper.py`; Test `tests/test_wrapper.py`.

- [ ] **Step 1:** Add a concurrent-writer regression test: simulate two interleaved mutations (one marking a session COMPLETED, one appending/ reverting another) and assert both survive.
- [ ] **Step 2:** Rewrite `signal_needs_attention`, `signal_idle`, `signal_completed` to express their edit as a `fn(state)` passed to `mutate_state`, replacing the bare `load_state()`/`save_state()` pair.
- [ ] **Step 3:** Where a `signal_*` also touches the dev-queue, confirm `state_lock` is taken before `dev_queue_lock`.
- [ ] **Step 4:** Full `tests/test_wrapper.py` green.

## Task 3: Migrate the loop writers — dispatch, reconcile, spawn

**Files:** Modify `src/cw/dispatch.py`, `src/cw/reconcile.py`, `src/cw/spawn.py`; Tests `tests/test_dispatch.py`, `tests/test_reconcile.py`, `tests/test_spawn.py`.

- [ ] **Step 1:** `reconcile.py` — wrap the phantom-revert / TIMED_OUT mutation in `mutate_state`; delete the stale "concurrent writers are rare" comment (lines ~19-25) and reference ADR 0005 instead.
- [ ] **Step 2:** `dispatch.py` — wrap `dispatch_tick` state mutations; audit every path that also takes `dev_queue_lock` for correct ordering.
- [ ] **Step 3:** `spawn.py` — wrap the append-new-session write.
- [ ] **Step 4:** All three modules' test suites green; existing behavior unchanged.

**↑ Tasks 1–3 are issue #387 (T1) and fully close the dominant race. Stop here for the T1 PR.**

## Task 4: Mechanical sweep — remaining lifecycle writers (issue #388 / T2)

**Files:** `src/cw/cli.py`, `src/cw/session.py`, `src/cw/doctor.py`, `src/cw/orchestrate.py` + their tests.

- [ ] **Step 1:** For each remaining `load_state(); …; save_state()` site, convert to `mutate_state`. These are lower-contention but must use the helper so the invariant is greppable.
- [ ] **Step 2:** Add a grep-guard test or a CI note: any new `save_state(` outside `config.py` is a review flag. (Optional — discuss with maintainer before adding lint.)
- [ ] **Step 3:** Full suite green; `grep "save_state(" src/cw/` shows mutations only inside `config.py` + `mutate_state` callers.

---

## Risks & callouts

- **Partial coverage window:** until Task 4 lands, the guarantee is partial — but Tasks 1–3 close the *dominant* (Stop-hook vs loop) race, which is the whole motivation. Acceptable to ship T1 and T2 separately.
- **Deadlock:** the single ordering rule (`state_lock → dev_queue_lock`) is the only thing standing between this and a lock-order-inversion deadlock. Every reviewer of a dual-lock path must check it.
- **Do NOT** introduce per-session lock files or a `revision`/CAS scheme — both were considered and rejected in ADR 0005. Stay with the single mutex.
- **Read paths untouched:** do not add the lock to `orchestrate.orchestrator_status` or the TUI snapshot — they only read, and a stale read is fine.

## Referenced by

- ADR 0005, RFC 0004 (State integrity, S1), issues #387 + #388.
