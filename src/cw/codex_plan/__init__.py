"""The codex PLAN executor (#3060, RFC 0018 C1).

One read-only ``codex exec`` turn writes nothing and returns the plan text and
a framed sentinel; cw, outside the sandbox, does everything else: gate set B,
the two plan review stations, the sign-off markers, ``.cw/plan-draft.md`` and
``.cw/plan.md``, the cw-stamped sentinel, the stage events, the completion
door and the tracker post.

Modules: :mod:`._argv` (the launch site), :mod:`._prompt` (the command-doc
source, the prefetch and the prompt), :mod:`._result` (the turn document, the
stamper and the artifacts), :mod:`._gates` (gate set B), :mod:`._stations`
(the review stations and markers), :mod:`._post` (the plan and park comment)
and :mod:`._run` (the ordered flow, :func:`run_plan_stage_and_complete`).
"""

from __future__ import annotations

from cw.codex_plan._argv import (
    PLAN_READ_ONLY_ARGV,
    PLAN_TRACKER_READ_TOOLS,
    PlanArgvPolicyError,
    build_plan_argv,
    require_read_only_argv,
)
from cw.codex_plan._gates import CODEX_PLAN_ARTIFACT_MISSING, CODEX_PLAN_GATE_FAILED
from cw.codex_plan._prompt import command_docs_root, render_plan_prompt
from cw.codex_plan._result import ResumeDraft, read_resume_draft
from cw.codex_plan._run import run_plan_stage_and_complete

__all__ = [
    "CODEX_PLAN_ARTIFACT_MISSING",
    "CODEX_PLAN_GATE_FAILED",
    "PLAN_READ_ONLY_ARGV",
    "PLAN_TRACKER_READ_TOOLS",
    "PlanArgvPolicyError",
    "ResumeDraft",
    "build_plan_argv",
    "command_docs_root",
    "read_resume_draft",
    "render_plan_prompt",
    "require_read_only_argv",
    "run_plan_stage_and_complete",
]
