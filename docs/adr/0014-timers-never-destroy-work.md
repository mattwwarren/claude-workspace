# Timers never destroy work

**Status:** Accepted — implemented (process-kill-timeout removal)
**Driven by:** operator-reported work and telemetry loss from timer-driven
kills; the long tail of patches trying to make time-based reaping safe
(#215, #265, #314, #326, #340, #384, #543, #544, #545, #756, #918, #976,
#1020, #1054, #1061, #1277, #1445, #1471) — each a better guess at *when* a
timer may kill, none of which fixed the structural problem that elapsed time
is not evidence of death.
**Builds on:** [ADR-0006](0006-reaping-is-gated-by-an-authority.md) (reaping
is gated by an authority — this ADR narrows *what may even be proposed*),
[ADR-0003](0003-stop-hook-canonical-completion-signal.md).

## Decision

No timer may destroy work. Elapsed wall-clock time and transcript quietness
are **signals for the operator**, never triggers for a disposition. Every
code path where a timeout expiry killed a process, stamped `TIMED_OUT`,
reverted RUNNING→PENDING, parked `BLOCKED_ON_USER`, stopped a daemon
surface, or removed a worktree is removed:

- the **stalled sweep**'s wall-clock budgets (`headless_timeout_by_*`),
  retry-cap park, finalize-blocked park, and liveness-veto machinery;
- the **idle watchdog** (`idle_watchdog_*`, confirm-before-reap counter,
  git-salvage, silently-idle park) and its post-lock salvage passes;
- the **Stop hook**'s budget-expiry `TIMED_OUT` transition — a sentinel-less
  headless Stop now defers unconditionally;
- the **codex executor**'s wall-clock kill deadline — executors are spawned
  with `wall_clock_budget_seconds=None` and `proc.kill()`-on-timer is
  unreachable;
- the config/CLI surface for all of the above (`--timeout`, the budget and
  cap keys — stripped with a warning from existing configs).

## What remains (and why it is allowed to act)

Dispositions survive only when driven by **evidence**, not clocks:

- **Phantom sweep** — the surface is absent from the daemon roster: the
  process is already gone; nothing is killed. Still gated by `reap_policy`
  (ADR-0006, default `signal_only`) with the mass-reap outage guard and
  spawn grace.
- **Foreign-result completion / emitted-sentinel routing / local harvest** —
  a terminal result or sentinel exists, or the local PID has exited:
  constructive completion of finished work.
- **Operator commands** (`cw spawn close`, `cw doctor --reap`, ticket
  delete) — a human is the authority.

## The signal that replaces the timers

The operator still needs to know when a worker looks wrong. That class of
signal is preserved and made multi-dimensional instead of one-dimensional
(bare elapsed time):

- the liveness-bucket ladder (RFC 0008 W2) latches transcript-staleness
  transitions and emits `session.liveness_changed`;
- a crossing into the **top bucket** by a session that is still in the
  daemon roster, has emitted **no sentinel**, and is **not awaiting a
  subagent** emits one edge-triggered `SESSION_NEEDS_ATTENTION`
  (`paused_status=session_unresponsive`) plus a push notification —
  and mutates nothing;
- `cw queue-peek` remains the rich advisory surface (age, idle gap,
  sentinel, PR state) recommending WAIT/PEEK/STOP to the human;
- the escalation latch, dispatch-loop dead-man's switch, and
  `cw dev-queue wait`'s ATTENTION exit codes are unchanged.

## Invariant

1. No code path may compare elapsed time or transcript age against a
   threshold and, on exceedance, mutate session status, the dev queue, the
   daemon roster, or a worktree. Thresholds may only debounce or delay
   *signals* and *re-checks*.
2. A destructive act requires evidence (roster absence, recorded terminal
   result, dead PID) or an explicit operator command — and remains subject
   to ADR-0006's `reap_policy` gate where applicable.
3. New health heuristics land as signals (events / notifications /
   advisories) first; promoting one to an automatic disposition requires a
   new ADR superseding this one.

## Consequences

- A genuinely hung worker occupies its lane slot until the operator acts on
  the distress signal. This is the accepted trade: an occupied slot is
  recoverable; killed in-flight work and lost telemetry are not.
- `SessionStatus.TIMED_OUT`, `ReapReason.WALL_CLOCK_BUDGET`/`IDLE_STALL`/
  `STALLED_RETRY_CAP_PARKED`, and related enum values remain for
  deserializing historical state/events but are no longer produced.
- The `tasks.py` terminal backstops still heal legacy `TIMED_OUT` rows
  (including `complete_timed_out_merged_tasks`).

## Alternatives considered

- **Keep timers but route everything through `signal_only`.** Rejected: the
  retry-cap park, finalize-blocked park, and Stop-hook budget path were
  structurally exempt from the ADR-0006 gate, and even the gated paths
  yanked tasks out of RUNNING — state churn that cost work and telemetry
  without killing the process.
- **Better thresholds / more veto heuristics.** Rejected: seventeen tickets
  of threshold tuning preceded this ADR; one-dimensional elapsed time keeps
  misclassifying healthy long runs.
