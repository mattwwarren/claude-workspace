# A supervised dispatch loop re-execs onto new code at an idle point on SIGHUP

**Status:** Accepted
**Driven by:** #3726 (follows #2729, #2702)

## Decision

`cw dev-queue serve --supervised` runs the dispatch loop as the child of a
small parent process (`cw.dispatch_supervisor`) that owns every restart onto
newly installed code. The child learns one thing: on SIGHUP it finishes the
tick in progress and exits with `os.EX_TEMPFAIL` (75) at its next idle point.
The parent decides when to send that SIGHUP and starts a fresh child from the
code installed now. Unsupervised loops are unchanged.

## Invariant

1. A re-exec never interrupts a tick. The child's SIGHUP handler only sets a
   bool on `ReexecControl`; the loop reads it only in `idle_wait`, at the end
   of a `_run_dispatch_loop_body` iteration, after `dispatch_tick`,
   `consume_completed_sessions` and every guarded step have returned. At that
   point no `dev_queue_lock`, `sessions_lock` or `state_lock` is held
   (ADR-0019's per-thread stack is empty). `config.dispatch_loop_lock` is the
   only process-lifetime lock and stays `_UNGUARDED`.
2. The exit-code protocol: a child exits 75 only for a re-exec request (SIGHUP,
   or version drift in a supervised child). 0 is a clean stop. Any other
   non-zero exit or death by signal is not a re-exec.
3. SIGHUP is handled only under `--supervised`, and never when SIGHUP is
   inherited as `SIG_IGN` (`nohup`). This applies to both the parent and the
   child. An unsupervised loop installs no handler.
4. The supervisor never takes the dispatch-loop lock and never passes
   `--force`. It waits for a child to exit, which releases the kernel flock,
   before starting the next.
5. The parent never forces a child's exit because of elapsed time. The
   10-minute stall after a forwarded SIGHUP is a signal only
   (`dispatch.restart_stalled` plus a stderr line).

## What this means for callers

- `cw.dispatch_loop_identity.assess_dispatch_loop` marks a STALE loop
  `supervised` only when its recorded `supervisor_pid` is alive. The guard in
  `cw.cli.dev_queue._loop_warning` waits up to 90s (poll 2s) for such a loop to
  leave STALE. Afterwards `require_dispatch_loop_safe` refuses with the
  supervised refusal and `warn_if_dispatch_loop_stale` keeps warning. An
  unsupervised or dead-supervisor STALE loop takes the existing path at once.
- The supervised STALE wording is conditional ("restarts it at the next idle
  tick once the new code passes its import check; if it has not, check the
  supervisor's stderr"), so it stays true while a probe is failing, with no
  extra persisted state.

## What this means for producers

- `run_dispatch_serve` builds a `ReexecControl` only when
  `supervised_by_parent()` (`CW_DISPATCH_SUPERVISOR_PID == os.getppid()`), and
  passes `reexec=` to `run_dispatch_loop` only then.
- `loop_record_fields` adds `supervisor_pid` only for a supervised child. The
  lock-record schema stays additive and backward-readable by the oldest
  supported supervisor, because the parent's frozen code evaluates
  child-written state. When a change cannot keep that, the release notes must
  say the `--supervised` command needs a manual restart.
- The parent polls every 10s. On STALE, after the child has registered
  (`assessment.pid == child.pid`), it runs an import probe in a fresh
  interpreter, then forwards SIGHUP. Restarts are bounded four ways:
  - a 60s minimum interval after a child start;
  - one automatic restart per checkout identity (an operator SIGHUP to the
    parent overrides it);
  - a re-probe backoff of 30s doubling to 600s while the probe fails, reset by
    a new checkout identity;
  - three consecutive fast (<60s) failed starts of children launched after a
    re-exec, retried after 5s and 10s, then a loud stop
    (`reason: "supervisor_gave_up"`).
- The parent does not restart crashes: the child's in-process backoff owns
  them, and a non-zero exit or death by signal stops the parent loudly.

## Consequences

- The parent's SIGTERM path (SIGINT to the child, a 120s grace, then SIGKILL)
  is an operator-stop exception to "never mid-tick". It is an explicit
  operator stop, not an automatic restart.
- An orphaned child (its parent died) keeps its handler: SIGHUP makes it exit
  75 and nothing restarts it. This is accepted. Before this change SIGHUP
  killed it, and the next `serve --supervised` restores supervision. A later
  start of an orphan records no supervisor, because `getppid` no longer
  matches.
- Under `nohup` the parent loses `kill -HUP <parent>` as a restart trigger.
  The automatic restart still works: when its own SIGHUP is ignored, the parent
  starts each child through a three-statement trampoline that resets SIGHUP to
  `SIG_DFL` and `execv`s `python -m cw` in place (same pid). `preexec_fn` is
  not used.
- From a plain terminal with neither tmux nor `nohup`, a closed window gives
  the parent one restart cycle and then the bounded stop.
- The supervisor's own code is never re-executed; changing
  `cw.dispatch_supervisor` needs a manual restart.
- Death of the orchestrator session that started the supervisor still ends it.
- One new event type, `dispatch.restart_stalled`, non-paging like
  `codex_fix.gate_slow`.

## Alternatives considered

- **The child detects drift and exits by itself.** Rejected: a child that has
  already exited cannot pre-flight the new tree, so a half-synced install
  would take dispatch down.
- **An in-process `os.execv`.** Rejected: it replaces the process while
  codex drain threads and the flock are live, and it gives no place to
  pre-flight or bound a failing install.
- **A systemd or launchd unit.** Out of scope: not portable across the
  operator machines, and it still needs the idle-point exit.
- **Relaxing the identity guard instead.** Rejected: a stale loop still drops
  row fields it does not know (#2702).
- **An opt-in `--sighup-reexec` flag on a plain loop.** Rejected: SIGHUP keeps
  its default meaning for an unsupervised loop.
- **A sidecar probe-status file for the CLI wording.** Rejected: the
  conditional wording is true in both probe states, and all bounds stay in
  parent memory.

## Referenced by

- #3726, ADR-0019
