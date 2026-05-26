# Stop hook is the canonical worker-completion signal

**Status:** Accepted
**Driven by:** #147 (inject Stop hook for direct completion signal),
#165 (origin-aware Stop hook + safe `settings.local.json` write),
#176 (Layer 1 transcript backstop + JSONL sentinel detector),
#151 (defer when `background_tasks` pending),
#184 (`cw signal-stop` CLI surface),
#225 (last-result capture for headless DAEMON sessions).
**Builds on:** [ADR-0000](0000-native-supervisor-migration.md) — `claude --bg`
is the daemon-spawn primitive; the wrapper subprocess is no longer in the
process chain for DAEMON-origin sessions.

## Decision

A daemon-spawned worker signals completion by invoking `cw signal-stop` from
a Stop hook that cw injects into the worktree before spawn. The hook is the
canonical signal; the `wrapper.py` sentinel-buffer parse is the legacy
interactive-pane fallback; `reconcile.py`'s crashed-pane sweep is the
catch-all backstop. These three layers MUST be considered in this priority
order — new completion behavior belongs in the Stop hook path.

## Invariant

For every DAEMON-origin Session created by `cw.spawn.spawn_create_impl`:

1. `spawn._write_hook_context` MUST land both files under `<worktree>/.claude/`
   before the daemon is asked to spawn the worker:
   - `settings.local.json` configuring a Stop hook → `cw signal-stop`.
   - `cw-context.json` carrying `session_id`, `session_name`, `client`,
     `purpose`, `ticket_id`, and `headless`.
2. `cw signal-stop` MUST be idempotent. A session already in `COMPLETED`,
   `IDLE`, or `TIMED_OUT` is a no-op.
3. `cw signal-stop` MUST defer (silent return, no state I/O) when the hook
   payload's `background_tasks` list is non-empty. Completing here would
   orphan an in-flight `run_in_background: true` subagent; the Stop hook
   fires again on the next turn boundary once the background work drains.
4. When `cw-context.json.headless == true` AND `origin == DAEMON`, the Stop
   hook MUST NOT transition to `COMPLETED` unless the `<<<AUTO_DEV_RESULT`
   sentinel is present in Claude's session transcript. Under wall-clock
   budget → defer. Over budget → transition to `TIMED_OUT` (loud,
   retry-eligible), revert the owning `TicketTask` from RUNNING → PENDING,
   and `claude stop` the daemon session.
5. For `origin == USER` sessions, the Stop hook transitions `ACTIVE → IDLE`
   only — it MUST NOT emit `SESSION_COMPLETED` (no `TicketTask` to retire)
   and MUST NOT call `native_daemon.stop`. The `_write_hook_context` path
   refuses to clobber a pre-existing `settings.local.json` (raises
   `HookContextConflictError`); USER worktrees may carry user-managed
   settings.

## What this means for callers

- **`cw.spawn`** is the only place that writes `cw-context.json` and
  `settings.local.json` for DAEMON sessions. Any future spawn entry point
  (USER-origin `claude --bg`, future supervisor flows) MUST route through
  `_write_hook_context` with the correct `origin`. Direct
  `settings.local.json` writes outside spawn are forbidden — they break the
  USER-origin safety guarantee.
- **`cw.cli.signal_stop`** is the single entry point for the hook. The
  layered model (idempotency → `background_tasks` defer → USER carve-out →
  headless transcript backstop → COMPLETED transition) is encoded here in
  this order. New completion semantics belong in this function, not in
  callers reading `last_result`.
- **`cw.reconcile`** MUST NOT reap sessions whose status is already
  `TIMED_OUT` (carve-out introduced by #176 Layer 1) and MUST honor parked
  statuses (per [ADR-0001](0001-parked-tasks-pin-their-session.md)). The
  reconciler stays the backstop for genuinely crashed processes; it does
  not race the Stop hook on healthy exits.
- **`cw.wrapper.signal_completed`** remains the buffer-parse fallback for
  interactive panes that still run under `cw run-claude`. DAEMON-origin
  spawns bypass the wrapper entirely (ADR-0000) — wrapper parse never
  fires for them. New work targeting DAEMON sessions MUST NOT depend on
  wrapper-side capture.

## What this means for producers

- The `/auto-dev` skill MUST emit its `<<<AUTO_DEV_RESULT…>>>` sentinel
  before its final agent turn ends. The Stop hook's Layer-1 transcript
  walk (`_parse_sentinel_from_transcript`) reads from
  `~/.claude/projects/<encoded-cwd>/<claude-session-id>.jsonl`; the
  sentinel must be in that JSONL before the turn ends or the budget gate
  trips.
- Any skill that schedules `run_in_background: true` subagents MUST allow
  the parent's turn to end normally — do not `claude stop` from within the
  subagent. The Stop hook's `background_tasks` defer relies on the natural
  turn cycle to fire again once the subagent's result arrives as the
  parent's next turn.

## Consequences

- `<worktree>/.claude/cw-context.json` becomes a per-worktree identity
  artifact that the worker reads from its own filesystem. Worktrees are no
  longer disposable mid-session — clobbering or relocating one breaks the
  Stop hook for that session. Accepted: the worktree was already the
  source of truth post-ADR-0000; the artifact just makes the dependency
  explicit.
- `signal_stop` in `cli.py` is ~210 lines including comments. Each
  deferral / branch covers an independent failure mode (subagent orphan,
  USER-origin noise, headless silent-success) documented inline. Folding
  the branches would couple invariants whose failure modes are unrelated.
- A user who deletes `<worktree>/.claude/settings.local.json` after spawn
  disables the hook for that session. cw treats this as user error:
  reconcile will eventually mark the session CRASHED and revert its
  `TicketTask` to PENDING. No silent recovery, but also no silent wedge.
- For headless sessions, capture of the parsed sentinel into
  `Session.last_result` moved from `wrapper.signal_completed` into
  `signal_stop` itself (#225). Consumers reading `last_result`
  (`consume_completed_sessions`, `/cw-followup`) work unchanged; only the
  capture site changed.

## Alternatives considered

- **Continue parsing the wrapper buffer for daemon sessions.** Rejected.
  `claude --bg` runs the worker under the per-user supervisor (ADR-0000),
  so there is no `cw run-claude` in the process chain — the buffer does
  not exist. An out-of-process tail of supervisor stdout was prototyped
  (issue #133) and rejected as fragile under ANSI/streaming edge cases
  later confirmed by #203.
- **Poll `claude agents --json` for completion status.** Rejected as the
  canonical signal: it reports `idle`/`completed`/`failed`, which is
  *liveness*, not *outcome*. It cannot tell `shipped` from `no_op` from
  `blocked` — those live in the sentinel. Used inside reconcile as the
  liveness backstop, but not the primary signal.
- **Pass identity via env vars to the worker instead of `cw-context.json`.**
  Rejected. `claude --bg` does not propagate the caller's environment to
  the supervisor-owned process (issue #133); a filesystem artifact is the
  only reliable channel.
- **Fold the three deferral guards into one.** Rejected. Idempotency,
  `background_tasks`, and headless-budget catch independent failure modes.
  Folding `background_tasks` into the headless backstop, for example,
  would orphan subagents in non-headless DAEMON sessions.

## Referenced by

- #147, #151, #165, #176, #184, #225, ADR-0002
