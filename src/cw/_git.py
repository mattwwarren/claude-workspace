"""Shared git-subprocess primitives.

Keep this module free of imports from other ``cw.*`` modules. It is a leaf by
construction so that even the import-disciplined modules — notably
:mod:`cw.review_finding_dispositions`, which may import nothing from ``cw`` at
module scope — can reach it from a function body without risking a cycle.

``cw._git`` is the shared util ``native_daemon._spawn_clean_env``'s docstring
named as the eventual home for the triplicated ``_git_clean_env`` helper
(``spawn.py``, ``native_daemon.py``, ``worktree_gc.py``). Those three copies
are deliberately left in place here: consolidating them is a separate change
with its own blast radius. What lives here is the copy every NEW git
subprocess call should use.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def git_clean_env() -> dict[str, str]:
    """Return ``os.environ`` with every ``GIT_*`` variable stripped.

    ``cw`` can run inside a git hook, where ``GIT_DIR``, ``GIT_WORK_TREE`` and
    ``GIT_INDEX_FILE`` point at the hook's repository rather than the path the
    command was handed (#766). A ``git`` subprocess that inherits them reads a
    DIFFERENT repository than its ``cwd``/``-C`` argument names — and does so
    silently, returning a plausible sha or diff for the wrong tree. Stripping
    them makes the path the caller passed the only thing that decides which
    repository answers.

    Mirrors ``spawn._git_clean_env`` and ``worktree_gc._git_clean_env``; see
    the module docstring for why those copies still exist.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def capture_head_sha(
    worktree: Path,
    *,
    ref: str = "HEAD",
    strict: bool = True,
    timeout: float | None = None,
) -> str:
    """Return the sha *ref* (default ``HEAD``) resolves to in *worktree*.

    *ref* is any revision ``git rev-parse`` accepts; the codex boot reaper
    passes ``origin/<branch>`` to compare a worktree's HEAD against its local
    tracking ref (#2285).

    *timeout* bounds the ``git`` call in seconds (``None``, the default, is
    unbounded). A timeout is one more way for ``git`` to fail, so it follows
    *strict* exactly as the other failures below do: ``strict=True`` raises
    ``subprocess.TimeoutExpired``, ``strict=False`` returns ``""``. The boot
    reaper passes one because it runs before the dispatch loop's first tick,
    where a hung ``git`` would keep dispatch from starting at all.

    One implementation for two deliberately different callers (#2232). The
    review pass's diff capture must FAIL LOUDLY when it cannot resolve the
    commit it is about to review — a blank sha there would silently record a
    disposition against nothing. ``cw review dispositions`` must NOT: its job
    is to show the operator what the ledger holds, and an unreadable
    repository must not cost them the listing. That difference is this
    parameter rather than two copies of the subprocess call.

    ``strict=True`` (the default) propagates whatever ``git`` failed with — a
    ``CalledProcessError`` for a non-zero exit, an ``OSError`` for a missing
    binary or a vanished directory. ``strict=False`` returns ``""`` for both,
    which :func:`cw.review_finding_dispositions.disposition_drifted` reads as
    "no question to answer" so the caller's staleness column degrades to
    unknown rather than to a confident "not stale".
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=strict,
            env=git_clean_env(),
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        if strict:
            raise
        return ""
    if completed.returncode != 0:
        # Only reachable with strict=False; check=True already raised above.
        return ""
    return completed.stdout.strip()
