"""The codex FINALIZE stage executor (#3062, RFC 0018 C3).

``cw codex run --stage finalize`` runs :func:`run_finalize_stage_and_complete`.
The hybrid design: one READ_ONLY ``codex exec`` turn returns a strict ``-o``
document with the judgement text only (PR title, PR body, adjudication and
operator-override section text); cw does every mechanical or ordering-critical
step in Python, outside the sandbox: the MUST_FIX override check, the
Detector Guard, Step 4.0/4.0b (re-run immediately before the arm), the Step
4a merge gate, the quality gates, the push, PR create or reuse, the
mergeability check, the arm via ``prep_pr_finalize.py`` and its verification,
the PR-link comment, the cw-stamped sentinel and the stage events.

Submodules:

- ``_argv`` — the fixed read-only argv and its policy check.
- ``_prompt`` — the rendered contract plus cw's injected sections.
- ``_result`` — the turn document, the block value and the result builders.
- ``_gates`` — the override check, the approval fence and the quality gates.
- ``_merge_gate`` — Step 4a.
- ``_pr`` — the Detector Guard, the live bind, push, create, mergeability.
- ``_arm`` — the seam check, the arm and the verification.
- ``_post`` — the PR-link comment.
- ``_run`` — the ordered flow.
"""

from cw.codex_finalize._argv import (
    FINALIZE_READ_ONLY_ARGV,
    FinalizeArgvPolicyError,
    build_finalize_argv,
    require_read_only_finalize_argv,
)
from cw.codex_finalize._result import (
    MERGE_CONFLICT_POST_PUSH,
    PR_ALREADY_TERMINAL,
    SCOPE_TIER_UNRESOLVABLE,
    FinalizeBlock,
    FinalizeTurnDocument,
)
from cw.codex_finalize._run import run_finalize_stage_and_complete

__all__ = [
    "FINALIZE_READ_ONLY_ARGV",
    "MERGE_CONFLICT_POST_PUSH",
    "PR_ALREADY_TERMINAL",
    "SCOPE_TIER_UNRESOLVABLE",
    "FinalizeArgvPolicyError",
    "FinalizeBlock",
    "FinalizeTurnDocument",
    "build_finalize_argv",
    "require_read_only_finalize_argv",
    "run_finalize_stage_and_complete",
]
