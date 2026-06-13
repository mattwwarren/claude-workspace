# Reconcile cadence and ownership: on-demand, ticker, or daemon primary runner

**Status:** Proposed
**Driven by:** #639 (stale session state between cw commands — hours-long gaps with long-running DAEMON workers)
**Builds on:** [ADR-0005](0005-single-state-lock.md) (single state lock), [ADR-0006](0006-reaping-is-gated-by-an-authority.md) (reaping is gated by an authority)

## Decision

Deferred — this ADR documents the options and recommendation for the decision owners to ratify. The recommended path is **Option B (opt-in background ticker)** as the next step, with an explicit upgrade path to Option C if the ticker proves reliable over multiple sprints.

The narrow per-symptom fixes (#635 spawn-return csid backfill, #637 world-state check before revert) are **independent of this decision and ship first**.

## Context

`reconcile()` runs from exactly four call sites today:

- `session.py:84` — `cw start` / `cw resume` (before state load)
- `dispatch.py:236` — each `dispatch_tick` call
- `doctor.py:275` — `cw doctor`
- `cli.py:996` — `cw status` / `cw list`

There is no background process. Session state is stale for the entire interval between operator commands. With long-running parallel DAEMON workers, that interval is hours.

On 2026-06-13 a single 12:02→19:56 UTC operator-command gap produced three distinct failure classes:

- **#635** — `claude_session_id` never backfilled while a worker was active (~1h blind spot). The spawn-tick's `claude agents --json` didn't list the milliseconds-old session; no later tick retried. Liveness detection (csid→transcript→silence) was impossible.
- **#637** — a worker completed at 18:09 but its dev-queue task still read `running`; the next operator `dispatch_tick` (~1h later) reverted and **re-dispatched the already-shipped ticket**.
- **Stuck-`running` tasks** (#578-class) that required manual resolution (`cw done` / `cancel`) — all rooted in the same staleness.

The operator's strategic note (from #639): "No background daemons run today — the on-demand model is deliberate. **If** a ticker proves reliable, it could become the primary runner." That architectural question — cadence and ownership of reconcile — is what this ADR addresses.

## Decision Drivers

1. **Bounded staleness** — a gap of hours between reconcile passes is the proximate cause of #635, #637, and the #578 class. The goal is to shrink the staleness window to a knowable upper bound.
2. **No new failure modes without vetting** — the project has no background processes today. Introducing one adds lifecycle/install/failure concerns that must be contained.
3. **Preserve ADR-0005 and ADR-0006 invariants** — any ticker must go through `sessions_lock` (ADR-0005) and must not short-circuit the reap-policy gate (ADR-0006).
4. **Upgrade path** — start small, promote if proven. A ticker that proves reliable earns the right to become the primary runner (Option C).

## Considered Options

### Option A: On-demand only (status quo)

`reconcile()` continues to run only at the four existing call sites. Staleness mitigation is point-wise: fix the acute symptoms (#635, #637) at their specific failure sites; accept that state may lag for hours between commands.

**Pros:**
- No new process or failure mode.
- Simplest — zero infrastructure change.
- The #635 and #637 fixes address the immediate incidents without touching cadence.

**Cons:**
- The staleness hazard recurs at every new site where the gap matters. #635/#637 are point fixes, not a structural answer.
- Liveness detection (`claude_session_id`→transcript→silence) is inherently unreliable when reconcile never runs during the worker's lifetime.
- Operator must run `cw status` / `cw doctor` manually to flush stale state; easy to forget during an automated overnight run.
- Future features that depend on accurate real-time session state (e.g., cost tracking, alerting) are unimplementable under this model.

### Option B: Background ticker (recommended)

A periodic reconcile loop runs on a configurable interval (e.g., 60s default). Implementation options, in ascending complexity:

- **B1 — cron / launchd job** calling `cw doctor` (or a new `cw reconcile` subcommand) on a schedule. No persistent process; the OS scheduler owns liveness.
- **B2 — `dispatch_tick` background thread** inside `cw orchestrate run` — the long-lived orchestrator session already exists when a queue is active; add a ticker thread that calls `reconcile()` between ticks.
- **B3 — standalone `cw ticker` daemon** launched at login (launchd plist / systemd unit). Persistent; full lifecycle management.

Regardless of implementation variant, the ticker calls the existing `reconcile()` unchanged — it goes through `sessions_lock`, respects `reap_policy`, and emits events normally.

**Pros:**
- Bounds staleness to the ticker interval (60s or configurable).
- Reuses existing `reconcile()` + `sessions_lock` + `reap_policy` machinery unchanged.
- B1 (cron) has no new persistent process — failure is silent omission, not crash.
- B2 piggybacks on an already-running process; no new install story.
- Directly solves the #635/#637 class structurally: a csid backfill or completed-task detection that runs every 60s closes the multi-hour gap.

**Cons:**
- Concurrent `reconcile()` calls are possible (ticker fires while operator also runs `cw status`). The existing `sessions_lock` serializes them correctly, but contention under `claude agents --json` latency could cause one caller to block noticeably.
- Cadence choice is underdetermined: 60s is a guess. Too short drives `claude agents --json` subprocess overhead; too long still leaves meaningful gaps.
- B1 (cron): install story requires a per-machine setup step; easy to omit.
- B3 (standalone daemon): adds daemon lifecycle — start/stop/restart, crash detection, port/socket or file-based IPC. Significant new failure surface.
- Does not give a single authoritative view of session state — the ticker and on-demand callers both mutate state; consistency is guaranteed by the lock, not by ownership.

### Option C: Daemon primary runner

Promote a reliable background ticker to *the* authoritative source of reconcile and dispatch. `cw` commands become thin clients that query a long-lived daemon; the daemon owns the reconcile/dispatch loop, the event bus, and state mutations. On-demand calls become requests to the daemon, not direct invocations.

**Pros:**
- Single authority: no contention between on-demand and background callers; the daemon serializes everything.
- Real-time state: staleness bounded by the daemon's poll interval.
- Enables features that require continuous visibility (cost tracking, real-time alerting, live progress UI).
- Naturally resolves the "who owns the event bus" question (RFC 0004 §orchestration).

**Cons:**
- Largest architectural shift in the project's history. Fundamentally changes the "cw is a CLI that reads/writes files" model established in ADR-0000.
- Daemon lifecycle: install (launchd plist / systemd unit), auto-restart on crash, version migration when the daemon protocol changes, handling of the daemon being absent.
- IPC protocol needed between `cw` CLI and daemon (Unix socket, HTTP, or file-based signaling). Adds a new failure class: `cw` hangs waiting for a dead daemon.
- Testing complexity: integration tests must manage daemon lifecycle; unit tests lose the simplicity of calling `reconcile()` directly.
- Blast radius: every caller of `load_state()`, `save_state()`, `reconcile()`, and `dispatch_tick()` is affected. This is essentially a rewrite of the execution model.

## Decision Outcome

### Recommendation: Option B2 as the next step

Start with **B2** — add a periodic `reconcile()` call inside the `cw orchestrate run` tick loop (or an analogous long-lived command path), configurable via a `reconcile_interval_seconds` field in `clients.yaml` or a global config key, defaulting to 60s. This:

- Delivers bounded staleness with no new install story (the orchestrator session already runs when a queue is active).
- Reuses all existing machinery unchanged.
- Is reversible: if B2 proves unreliable, disable the thread; if it proves reliable, it earns promotion to B3 or C.

If the operator is not running an orchestrator session (pure on-demand use), **B1 (cron)** is the fallback — a `cw reconcile` subcommand (thin wrapper around `reconcile()`) with a documented launchd/cron snippet in `docs/INSTALL.md`. This handles the overnight-daemon-worker scenario that drove #635/#637.

Option C is the right long-term destination **if and only if** B proves reliable across multiple sprint cycles without introducing new incident classes. The decision to promote should be driven by evidence (ticker uptime, contention incidents, operator trust), not by architectural ambition alone.

### Open questions before implementation

1. **Lock contention under `claude agents --json` latency.** `reconcile()` holds `sessions_lock` while calling the `claude agents` subprocess. A 60s ticker + a slow `claude agents` response could block on-demand callers (e.g., `cw status`) for several seconds. Measure p99 latency of `claude agents --json` before choosing the ticker interval.
2. **Interval tuning.** 60s is a guess. The right interval is `max_acceptable_staleness / 2`. For the #635/#637 class, 60s is sufficient. If cost per `claude agents --json` call is high, consider 120s or a backoff strategy.
3. **Ticker failure detection.** If the ticker thread crashes silently (B2), staleness returns to status-quo without warning. The ticker must emit a log event (or a new `RECONCILE_TICKER_FAILED` orchestrator event) on repeated failure so an operator can detect drift.
4. **Interaction with `dispatch_tick`.** `dispatch_tick` already calls `reconcile()` at the start of each tick. A separate ticker thread running at 60s intervals means `reconcile()` may be called twice in quick succession when a dispatch tick fires. The lock serializes this correctly, but double-reaping proposals within a short window should be tested.
5. **Install story for B1 (cron fallback).** A launchd plist and a `cron` snippet both need to be documented and tested on a clean machine. Consider making `cw install-ticker` a subcommand that writes the appropriate file for the detected OS.

## What this does NOT change

- The `reconcile()` function signature, detect/act split, and `reap_policy` gate (ADR-0006) are **unchanged**. A ticker is a new call site, not a new code path.
- The single state lock (ADR-0005) serializes all concurrent callers; no new locking logic is needed.
- The point fixes for #635 (backfill-at-spawn) and #637 (world-state check before revert) are **independent** of this decision. They ship in 1.1.2 regardless of which option is chosen here.

## Consequences

- **If B is adopted:** `cw` gains a new configuration key and (for B2) a background thread within the orchestrator process. Operators who don't run `cw orchestrate` get bounded staleness only if they configure B1. Contention under slow `claude agents --json` is a new latency risk; mitigated by interval choice.
- **If A is maintained:** The #635/#637 class of failures recurs at any new code site where the staleness gap matters. Acceptable for now given the point fixes; unacceptable if the worker fleet grows or overnight runs become standard.
- **If C is adopted (future):** All of the above complexity lands at once. Do not jump to C without a proven B running stably for several sprints.

## Alternatives considered

- **Reconcile-on-wait** (Option 2 from #639) — have `cw dev-queue wait` / monitor poll path call `reconcile()` so state stays fresh while something is watching. Rejected as primary: partial coverage (only fresh while a watcher is running), and adds latency to the watch loop without solving the unattended overnight scenario.
- **Increase on-demand call sites** — add `reconcile()` to more CLI subcommands. Rejected: does not help when no command is run for hours; adds overhead to every interactive command.

## Referenced by

- #639 (this ticket)
- #635 (claude_session_id backfill — independent fix, same root cause)
- #637 (world-state check before revert — independent fix, same root cause)
- #578 (stuck-running tasks class)
- ADR-0005 (single state lock — unchanged by this ADR)
- ADR-0006 (reaping is gated by an authority — unchanged by this ADR)
