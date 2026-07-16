# Migrating from cw 0.x to 1.0

## What changes

### Session backend

The **multiplexer layer (cmux/tmux) is removed entirely in cw 1.0**. Sessions
are no longer launched inside tmux panes or cmux workspaces. Instead, cw
spawns workers directly via `claude --bg` and tracks their liveness through
`claude agents --json` / the daemon roster at
`~/.claude/daemon/roster.json`.

The `surface_ref` field on sessions remains in the state schema for
compatibility. On first load after upgrading, cw automatically:

1. Backs up your pre-migration `sessions.json` to
   `.sessions.json.0.x-backup` in the same directory.
2. Clears any legacy multiplexer pane reference (e.g. `ws:0.1`,
   `tmux-pane-3`) from `surface_ref`, replacing it with `null`. Valid
   8-char hex daemon session ids are preserved unchanged.
3. Bumps the schema version to the current value (v14 as of cw 1.20.0).

### Schema version chain

The legacy-`surface_ref` clearing above is the **v5** step (#119/#521) — the
only step in the chain that rewrites existing data. Every schema version since
is purely additive: new fields default safely, so old state files load without
user action. Highlights:

- **v6** (#545): added `Session.idle_observation_count` — the count of
  consecutive idle observations before a confirm-before-reap decision is made
  (defaults to `0`).
- **v7** (#380): added `Session.reap_reason` — the `ReapReason` enum value
  recorded when a session is reaped by the reconciler (defaults to `None`).
- **v8–v14**: further additive fields (`reap_proposed_at`, `lane`, `stage`,
  `local_liveness`, `consecutive_salvage_skips`, `liveness_bucket`) plus a
  v14 normalization of stale `local_liveness` handles (#921) — all applied
  automatically on load.

### `cw daemon`

`cw daemon` is a **deprecated shim** — it emits a deprecation notice and
exits. The PR-dispatch/watch role it once held has been replaced by the
cw-pr-events channel-based orchestrator. Use `cw orchestrator-start` instead.

### Worktree paths

**Worktrees stay exactly where they are.** cw still creates them via
`worktree.py`; no paths are moved, renamed, or recreated by this upgrade.
If you used a custom `worktree_base` in 0.x your worktrees remain under that
path.

## New in 1.0

Brief summaries below; the linked docs have the authoritative detail.

- **`cw schema`** — inspect Pydantic model schemas for `AutoDevResult`,
  `TicketTask`, and `Session` directly from the CLI (`cw schema list`,
  `cw schema show <name>`).
- **`cw result validate`** (#482) — pre-emit gate: validates a candidate
  `AutoDevResult` JSON object against the authoritative schema before the
  worker emits the sentinel block. See `cw result validate --help`.
- **Sentinel-aware `cw dev-queue wait`** (#535) — `wait` now detects
  `AUTO_DEV_RESULT` sentinels in the transcript directly rather than relying
  solely on task-status polling, eliminating false-timeout (exit 124) for
  long-running healthy workers whose reconcile cycle hasn't fired yet.
- **`queue.session_reaped` bus events + `ReapReason` taxonomy** (#380) — the
  reconciler emits a structured `queue.session_reaped` event for every reap
  decision, carrying a `ReapReason` value (as of 1.20.0:
  `phantom_surface`, `idle_stall`, `usage_limit_cutoff`, `retry_cap_parked`,
  `stalled_retry_cap_parked`, `wall_clock_budget`, `completed_backstop`,
  `salvage_completed`, `salvage_parked`, `finalize_blocked`,
  `terminal_sibling`). See [`docs/headless-contract.md`](headless-contract.md)
  for the full event schema.
- **Confirm-before-reap** (#545) — the idle watchdog waits for
  `idle_confirm_observations` (default: 2) consecutive idle observations before
  triggering an idle-stall reap, reducing false-positive reaps on workers that
  are legitimately between tool calls.
- **Widened subagent-liveness window** (#544) —
  `SUBAGENT_LIVENESS_WINDOW_SECONDS` raised 900 s → 1800 s so a single long
  quiet tool call or in-flight subagent is not reaped as idle; the
  transcript-mtime window and elapsed budgets are unchanged.
- **Operator docs** (#538/#539) — `docs/dispatch-runbook.md` covers the
  end-to-end `cw dev-queue` dispatch procedure;
  `docs/session-disposition.md` explains how to read a session's outcome from
  the transcript sentinel. See those files for the full reference.

## What you need to do

Nothing. Worktrees stay where they are. The `surface_ref` migration runs
automatically on first `load_state()` call after upgrading to 1.0. The v6–v14
schema additions are purely additive and require no manual action.
