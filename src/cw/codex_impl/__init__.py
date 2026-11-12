"""The codex IMPL executor (#3061, RFC 0018 C2).

One write-capable ``codex exec`` turn edits the ticket worktree and returns a
framed sentinel; it never stages, commits or pushes. cw, outside the turn, does
everything else: the file fence and growth guard, gate 1 (empty diff), gate 2
(plan scope conformance), the repo's quality gates, the one commit (carrying
the ``Auto-Dev-Stage: impl-complete`` trailer), the push and tip verification,
the cw-stamped sentinel, the stage events and the completion door.

Modules: :mod:`._argv` (the launch site), :mod:`._prompt` (the rendered prompt
and its injected sections), :mod:`._result` (the turn document, the stamper,
blocked results and the commit message), :mod:`._gates` (the guards, gates and
breach mappers), :mod:`._feedback` (the live-PR bind and the default-branch
merge) and :mod:`._run` (the ordered flow, :func:`run_impl_stage_and_complete`).
It composes the stage-neutral :mod:`cw.codex_fix_loop` primitives and calls
neither ``_run_fix_and_commit`` nor ``_commit_fix_cycle``.
"""

from __future__ import annotations

from cw.codex_impl._argv import (
    ImplArgvPolicyError,
    build_impl_argv,
    require_impl_sandbox_argv,
)
from cw.codex_impl._prompt import render_impl_prompt
from cw.codex_impl._result import (
    IMPL_BLOCKED,
    IMPL_COMPLETE_TRAILER,
    IMPL_FAILED,
    IMPL_NOT_PUSHED,
    IMPL_STAGE_COMPLETE,
    IMPL_STATUSES,
    PLAN_MISSING,
)
from cw.codex_impl._run import run_impl_stage_and_complete

__all__ = [
    "IMPL_BLOCKED",
    "IMPL_COMPLETE_TRAILER",
    "IMPL_FAILED",
    "IMPL_NOT_PUSHED",
    "IMPL_STAGE_COMPLETE",
    "IMPL_STATUSES",
    "PLAN_MISSING",
    "ImplArgvPolicyError",
    "build_impl_argv",
    "render_impl_prompt",
    "require_impl_sandbox_argv",
    "run_impl_stage_and_complete",
]
