#!/usr/bin/env python3
"""Guard script: abort if the current git repo is the operator's main checkout.

Usage (from within an impl agent's working directory):
    python .claude/scripts/check_not_main_checkout.py

Context: cw dispatch workers run in isolated worktrees.  The cw-context.json
written by ``cw`` at spawn time carries ``workspace_path`` — the operator's
main checkout, which is FORBIDDEN for any git mutation from a worker.

This script reads .claude/cw-context.json (relative to the script's parent's
parent, i.e. the worktree root's .claude/), resolves both the forbidden
workspace_path and the current git repo root, and exits non-zero if they
match.  It is a no-op when:

- .claude/cw-context.json cannot be found (interactive / non-dispatch use)
- workspace_path is null in the context (USER-origin sessions)
- The current directory is not inside any git repository

The script is intentionally a single-file, stdlib-only guard with no
dependencies.

Exit codes:
    0  — safe to proceed (not the main checkout, or no dispatch context found)
    1  — BLOCKED: current git repo root matches the forbidden workspace_path
    2  — usage / unexpected error (does NOT block — treats as safe to avoid
         false-positive blockage on misconfiguration)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _find_context_file() -> Path | None:
    """Search upward from cwd for .claude/cw-context.json."""
    current = Path.cwd().resolve()
    for candidate in [current, *current.parents]:
        ctx = candidate / ".claude" / "cw-context.json"
        if ctx.exists():
            return ctx
    return None


def _git_toplevel(path: Path) -> str | None:
    """Return the git repository root for *path*, or None if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def main() -> int:
    """Run the guard.  Return 0 (safe), 1 (blocked), or 2 (error/no-op)."""
    ctx_path = _find_context_file()
    if ctx_path is None:
        # No dispatch context — interactive use, no constraint.
        return 0

    try:
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"check_not_main_checkout: could not read {ctx_path}: {exc}",
            file=sys.stderr,
        )
        # Treat as safe — don't block on a misconfigured context file.
        return 2

    forbidden_raw: object = ctx.get("workspace_path")
    if not forbidden_raw:
        # Null or missing workspace_path — no constraint (USER-origin session or
        # a context written by an older cw that pre-dates #766).
        return 0

    forbidden = str(Path(str(forbidden_raw)).resolve())

    current_toplevel = _git_toplevel(Path.cwd())
    if current_toplevel is None:
        # Not in a git repo — nothing to protect.
        return 0

    current_resolved = str(Path(current_toplevel).resolve())
    if current_resolved == forbidden:
        worktree = ctx.get("worktree_path", "<unknown>")
        print(
            f"BLOCKED (#766): git mutation rejected — current repo root\n"
            f"  {current_resolved}\n"
            f"matches the operator main checkout (workspace_path in cw-context.json).\n"
            f"Worker must only mutate its own worktree:\n"
            f"  {worktree}\n"
            f"Context file: {ctx_path}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    code = main()
    sys.exit(code)
