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
3. Bumps the schema version to 5.

### Worktree paths

**Worktrees stay exactly where they are.** cw still creates them via
`worktree.py`; no paths are moved, renamed, or recreated by this upgrade.
If you used a custom `worktree_base` in 0.x your worktrees remain under that
path.

## What `cw doctor` now checks

`cw doctor` verifies that every session carrying a `worktree_path` still has
that path on disk. Sessions where `worktree_path` is `None` are skipped
silently.

Example output when worktrees are present and healthy:

```
  [OK] worktree/summary — 3 sessions checked, 0 missing worktrees
```

Example output when a worktree was deleted manually:

```
  [WARN] worktree/a1b2c3d4 — path does not exist: /home/user/ws/.worktrees/client/feature
  [OK]   worktree/summary — 2 sessions checked, 1 missing worktrees
```

The check is **read-only and warn-only**: cw never moves or recreates a
missing worktree. A missing worktree is informational — you may have
intentionally deleted a finished branch. No manual action is required unless
you want to resume that session, in which case create a new worktree and
update the session's `worktree_path` by editing
`~/.local/share/cw/sessions.json` directly.

## What you need to do

Nothing. Worktrees stay where they are. The `surface_ref` migration runs
automatically on first `load_state()` call after upgrading to 1.0.
