# Migrating from cw 0.x to 1.0

## What changes

### Session backend

cw 1.0 ships the **tmux backend as the default on Linux** and keeps cmux on macOS.
The `surface_ref` field on sessions (used by 0.x to track multiplexer pane
handles) remains in the state schema for compatibility. Clearing stale
`surface_ref` entries and the `claude_session_id` → `state.json.resumeSessionId`
mapping are handled by the Phase F migration in issue #119 and are **not** part
of this upgrade step.

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

Nothing. For worktrees: they stay where they are. For `surface_ref` and
`claude_session_id` migration: that work belongs to issue #119 (Phase F) and
will be handled in a separate upgrade step.
