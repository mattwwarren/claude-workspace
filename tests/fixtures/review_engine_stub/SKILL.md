---
name: repo-review
description: Stub repo review engine for cw's hermetic and live tests (#3216).
---

# Stub repo review engine

A two-role engine used by cw's tests. On a claude-native lane this skill would
run its own roles and write the summary named in the `cw-engine:` args line.

On a codex lane cw reads `cw-engine.json` instead:

- one `codex exec` per role in `roles`, each prompted with that role's
  `agent_spec` file;
- then one read-only aggregation turn prompted with `aggregation_prompt` and
  the role documents.

The roles are deliberately small so a live probe stays cheap:

- `security` looks for injection, secrets and unsafe file handling.
- `quality` looks for correctness bugs and dead code.

Neither role writes to the worktree.
