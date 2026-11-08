"""Preflight: does this branch touch the engine skill? (RFC 0017 R7).

Reads git objects only. It creates no files and reads no summary. The guard
set comes from the default-branch copy of the skill, so a branch that edits
the skill cannot disarm its own guard. The run/park result is typed and
``extra="forbid"``: the prose maps exit 0 to run and exit 3 to park.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from cw._git import git_output
from cw.dev_queue.review_engine_allow import review_engine_head_allowed
from cw.exceptions import CwError
from cw.review_convergence import resolve_default_branch_ref
from cw.review_engine.skill import guard_patterns, resolve_skill

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_engine.context import EngineContext

REASON_NO_ENGINE_PATHS = "no_engine_paths_touched"
REASON_ENGINE_PATHS = "engine_paths_touched"
REASON_ALLOWED_HEAD = "allowed_head"


class PreflightResult(BaseModel):
    """The preflight verdict printed as JSON. ``extra="forbid"`` pins its shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["run", "park"]
    reason: str
    touched: list[str]


def _git(worktree: Path, argv: list[str]) -> str:
    """``git -C worktree argv`` as text; a failing git is a :class:`CwError`."""
    try:
        return git_output(["-C", str(worktree), *argv])
    except subprocess.CalledProcessError as exc:
        msg = f"git {' '.join(argv)} failed (exit {exc.returncode})"
        raise CwError(msg) from exc


def _changed_paths(worktree: Path, diff_base: str) -> list[str]:
    """Changed paths of ``diff_base...HEAD``, renames split so both sides count."""
    raw = _git(
        worktree,
        ["diff", "--name-only", "--no-renames", "-z", f"{diff_base}...HEAD"],
    )
    return [path for path in raw.split("\0") if path]


def _allowed_head(context: EngineContext, head: str) -> bool:
    """Whether the row names *head* in its operator allows (fails closed)."""
    if context.ticket_id is None or context.client is None:
        return False
    return review_engine_head_allowed(context.ticket_id, context.client, head)


def run_preflight(
    worktree: Path, *, diff_base: str, context: EngineContext
) -> PreflightResult:
    """Run or park the engine round for *worktree* against *diff_base*."""
    ref = resolve_default_branch_ref(worktree, context.default_branch)
    if ref is None:
        msg = f"default branch {context.default_branch!r} does not resolve"
        raise CwError(msg)
    skill = resolve_skill(worktree, ref.commit, context.skill)
    guard = guard_patterns(skill, context.skill, context.guarded_paths)
    touched = guard.touched(_changed_paths(worktree, diff_base))
    if not touched:
        return PreflightResult(
            decision="run", reason=REASON_NO_ENGINE_PATHS, touched=[]
        )
    head = _git(worktree, ["rev-parse", "HEAD"]).strip()
    if _allowed_head(context, head):
        return PreflightResult(
            decision="run", reason=REASON_ALLOWED_HEAD, touched=touched
        )
    return PreflightResult(decision="park", reason=REASON_ENGINE_PATHS, touched=touched)
