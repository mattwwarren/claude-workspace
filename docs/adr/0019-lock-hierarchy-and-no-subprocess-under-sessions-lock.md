# Locks form a ranked hierarchy, re-entry is an error, and nothing runs a subprocess under `sessions_lock`

**Status:** Accepted
**Driven by:** #1233 (and #1228, #485)

## Decision

cw's state-file locks form one ranked hierarchy that every acquisition
honors, enforced in code: each guarded lock context manager wraps its body
in `cw._lock_guard.lock_guard`, which keeps a per-thread stack of held locks
and checks it before any `open()` or `flock()` syscall. A same-thread
re-entry raises `CwLockReentrancyError` (always). An acquisition against the
hierarchy raises `CwLockOrderError` when `CW_LOCK_DEBUG=1` (the test suite
sets it) and logs one WARNING otherwise. A suite-wide test harness records
every lock event and every real subprocess launched under `sessions_lock`,
and fails the test on a violation.

This generalizes ADR-0005's ordering rule, "`state_lock` → `dev_queue_lock`
(never the reverse)" (`docs/adr/0005-single-state-lock.md`, invariant 2).
`state_lock` is the name ADR-0005 used for today's `sessions_lock`; this ADR
keeps that order and extends it to every other state-file lock.

## Invariant

1. **Rank table.** A thread acquires locks in non-decreasing rank:

   - `SESSIONS` (0): `sessions_lock`
   - `STATE` (1): the dev-queue lock (`dev_queue_lock` / `_lock`), the plan
     lock (`_plan_lock`), `concurrency_override_lock`, `clients_lock`,
     `dispatch_state_lock`, the focus lock
   - `LEAF` (2): the event inbox lock (`events._inbox_lock`), a session inbox
     lock (`session_inbox._inbox_lock`), a client history lock
     (`history._history_lock`)

   Two different locks of the same rank may nest (for example
   `dev_queue_lock` then `clients_lock`). Taking a lower rank while a higher
   one is held is a violation.
2. **Re-entry is an error, never a wait.** Every guarded lock is a
   per-open-fd `fcntl.flock`, which is not reentrant: a second acquisition
   by the same thread opens a fresh fd and blocks forever (#1228). The guard
   raises `CwLockReentrancyError` instead, before any syscall. Another thread
   waiting on the same lock is not a re-entry and still blocks. Held state is
   per thread (not per process, not per `ContextVar`): cw runs poller,
   notifier and anyio worker threads, and anyio copies the context into its
   workers.
3. **No subprocess under `sessions_lock`.** `sessions_lock` serializes every
   `cw` process on the host, so an external call under it (git, gh, `claude`)
   stalls them all for its duration (#485 liveness; the tree's "#485
   SHOULD_FIX 4" comments). Do external work before the lock or after it.
   The remaining exceptions are tracked, each by a ticket:

   | Site | Ticket |
   |---|---|
   | `reconcile.gate_recipes`: `fetch_approved_plan_comment` runs `gh` per candidate | #2545 |
   | `reconcile.concierge` recipe 3 git call; `reconcile.main_drift` checks | #2546 |
   | `reconcile.phantom._detect` and `reconcile.tasks`: phantom dirty-check git | #2548 |
   | `reconcile.usage_limit_mid_turn`: in-lock surface stop | #2549 |
   | `reconcile._shared`: `claude agents --json` | #2550 |
   | `session`: `done --cleanup` removes the worktree in-lock | #2557 |
   | `reconcile.codex_boot` clean check (`git status`) via `codex_reparks` and `reconcile.local` | #2563 |
   | `pr_hydrate` repo-slug check (`git remote get-url`) in the review-recipe act phase | #2564 |
   | `local_runner.synthesize_git_result` in the local harvest | #2565 |
   | `cli.stop_hook.locked` headless scope verification (`git merge-base`, `git diff --numstat`) | #2566 |

   #2563 to #2566 were found by the #1233 suite probe. #2551 (post-lock drain
   concurrency) is related but is not an in-lock subprocess.
4. **One inbox-emit rule.** Emitting an event inline while holding
   `dev_queue_lock` (`record_event` takes the event inbox lock inside it) is
   allowed: that is STATE then LEAF. The inbox lock is a strict leaf: nothing
   is acquired while it is held, including another leaf. Emitting after the
   queue lock is released is a permitted choice (some reconcile passes collect
   events under the lock and emit them afterwards), not a requirement and not
   a deadlock fix. This replaces the three contradicting descriptions that
   lived in code comments (#765 "risks deadlock", "never nests", and
   "deadlock-safe").
5. **Guard behavior.** Re-entry always raises. A rank or leaf violation
   raises only under `CW_LOCK_DEBUG=1` and in the test suite; in production
   it logs one WARNING per violating acquisition on logger `cw._lock_guard`
   and proceeds. The WARNING is the data source for the follow-up below.
6. **Allowlist policy.** The harness's
   `SUBPROCESS_UNDER_SESSIONS_ALLOWLIST` (`tests/_lock_invariants.py`) maps a
   module to the ticket that removes its exception. An entry needs a ticket
   in its value and its comment (a hygiene test enforces both), is added only
   for a site the suite actually hits, and is deleted when its ticket closes.

## What this means for callers

- Never call a function that takes a lock you already hold. Work under the
  held lock instead (for `sessions_lock`: mutate the loaded state and call
  `save_state`, not `mutate_state`).
- Take `sessions_lock` first, then STATE locks, then at most one leaf, and
  acquire nothing while a leaf is held.
- Do subprocess and network work before taking `sessions_lock` or after it
  releases. A new in-lock site needs a ticket and an allowlist entry, or the
  suite fails.
- A test that violates the discipline on purpose carries
  `@pytest.mark.lock_violations_expected("reentry" | "order" | "subprocess")`.
  A lock still held at teardown always fails.

## What this means for producers

- A new state-file lock context manager wraps its body in `lock_guard(name,
  path, rank)`. `tests/test_lock_guard.py` scans `src/cw` with `ast` and
  fails when a context manager that takes `fcntl.flock(..., LOCK_EX ...)`,
  `try_flock_until` or `acquire_sessions_flock` is neither guarded nor listed
  as unguarded with a reason.
- Unguarded by design (each listed in that test's `_UNGUARDED` map):
  - `config.dispatch_loop_lock`: process-lifetime singleton taken with
    `LOCK_NB`; it fails fast with `DispatchLoopLockedError` and cannot hang.
  - `_hook_context._context_lock`: per-worktree, bounded `try_flock_until`,
    fails open.
  - `doctor.routed_result_wedge._audit_outbox_lock`: deliberately reentrant
    through a contextvar depth permit.
  - `codex_legacy_recovery.codex_legacy_marker_lock`: a single-use marker
    lock.
  - `session_resume_trigger._resume_trigger_lock`: per-session and held
    across a spawn that takes `sessions_lock`, so conceptually it ranks below
    `SESSIONS`. Folding it into the table needs a rank below 0 and is a
    follow-up.

## Consequences

- A same-thread re-entry that used to hang a `cw` process forever now raises
  a `CwError`. Existing handlers on the historical re-entry paths already
  catch `CwError`, so a regression of the #1228 shape surfaces as a logged
  failure. The harness records it even when a handler swallows it.
- A production rank violation is not fatal yet; it only logs. **Follow-up:**
  promote the order check to always-on after a clean full-suite run and a
  soak period with no WARNING from `cw._lock_guard`.
- Equal-rank nesting is allowed, so two STATE locks taken in opposite orders
  by two processes could still deadlock undetected. Today the only STATE to
  STATE nesting is `dev_queue_lock` then `clients_lock`. If peer nestings
  grow, rank the STATE locks individually.
- The harness sees only real subprocess execs. A test that mocks
  `subprocess.run` or `Popen` bypasses it (no false positive, but that path
  is unchecked), and a subprocess launched by a helper thread while the
  parent thread holds the lock is invisible because held state is per
  thread. Static guards (`tests/test_reconcile_stop_call_sites_guard.py`,
  the `bounded=True` allowlist in `tests/test_config.py`) cover some of that
  gap.
- Cost: one thread-local list operation and an environment read per
  acquisition in production; in the suite, one mutex-guarded append per lock
  event and a stack walk per `Popen` made under `sessions_lock`.

## Alternatives considered

- **A module-level held set** (the ticket's first sketch, premised on cw
  being single-threaded per process). Rejected: cw runs threads, and a
  thread waiting on another thread's lock would be reported as a re-entry.
- **A `ContextVar`.** Rejected: anyio copies the context into worker threads,
  so a worker would inherit its caller's held locks.
- **Raise on rank violations in production now.** Rejected for this change:
  the rank table is new and only proven against the suite. The WARNING gives
  the evidence needed to promote it.
- **Make the locks reentrant.** Rejected: a reentrant `sessions_lock` would
  let a nested `mutate_state` save over the outer caller's in-memory state,
  the lost-write bug ADR-0005 exists to prevent.

## Referenced by

- #1233, #1228, #1229, #485, #765, #2545-#2551, #2557, #2563-#2566, ADR-0005
