"""Prompt-driven codex reviewer orchestration for CodexExecutor (#1236).

Replaces the single ``codex exec review`` subprocess call with a per-reviewer-
role loop of generic ``codex exec`` calls, each fed a materialized prompt over
stdin and validated through the executor-neutral ``review_findings`` library
(#1237). This package plays the same support role for ``CodexExecutor`` that
``local_runner`` plays for ``LocalExecutor``: ``executor.py``'s Step 3 shrinks
to a thin delegation into :func:`run_review`.

Codex has no filesystem access to ``.claude/*`` the way a Claude subagent does
(snap sandbox), so every reviewer input — the authoritative agent spec, the
diff, the plan/ticket context, project rubrics, per-reviewer policy, and
sensitive-file hits — is read by ``cw`` and inlined into the prompt.

This package was split out of a single module; the public import surface
(``from cw.codex_review import X``) is preserved here via re-exports.
Submodules:

- ``_const`` — reason vocabulary, transient-failure set, and category mapping.
- ``_diff`` — unified-diff capture and parsing.
- ``_context`` — reviewer selection and prompt-context assembly (+ doc parsing),
  including ``_prepare_review_pass`` (co-located with ``_load_optional_text``).
- ``_roles`` — per-role codex execution and failure classification.
- ``_verdict`` — verdict synthesis and review-comment rendering.
- ``core`` — ``run_review`` orchestration.
"""

from __future__ import annotations

from cw.codex_review._const import (
    _CATEGORY_TO_REASON,
    _MIN_ROLE_TIMEOUT_SECONDS,
    _TRANSIENT_FAILURE_REASONS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_FIX_SCOPE_VIOLATION,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_PARTIAL,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_TIMEOUT,
    STAGE3_REVIEW,
)
from cw.codex_review._context import (
    _OUTPUT_INSTRUCTIONS,
    _REVIEWER_ROLE_AGENT_FILES,
    _SENSITIVE_HEADER,
    _build_reviewer_prompt,
    _categorize_changed_files,
    _FileCategories,
    _hit_from_entry,
    _load_agent_spec,
    _load_optional_text,
    _load_review_policy,
    _load_sensitive_hits,
    _load_ticket_context,
    _parse_review_policy,
    _parse_reviewer_document,
    _prepare_review_pass,
    _read_sensitive_manifest,
    _render_sensitive_block,
    _ReviewPassInputs,
    _select_reviewer_roles,
    _SensitiveHit,
)
from cw.codex_review._diff import (
    _DIFF_GIT_HEADER_RE,
    _HUNK_RE,
    _capture_diff,
    _parse_hunk_new_start,
    _parse_unified_diff,
)
from cw.codex_review._roles import (
    _COMMAND_NOT_FOUND_RETURNCODE,
    _build_generic_codex_argv,
    _classify_codex_failure,
    _classify_codex_output_failure,
    _codex_scratch_dir,
    _persist_codex_role_diagnostics,
    _run_codex_role,
    _slug,
    run_codex_roles,
)
from cw.codex_review._verdict import (
    _format_failures_detail,
    _render_findings,
    render_verdict_comment,
    synthesize_codex_review_result,
)
from cw.codex_review.core import run_review

__all__ = [
    "CODEX_BUDGET_EXHAUSTED",
    "CODEX_ERROR",
    "CODEX_FIX_SCOPE_VIOLATION",
    "CODEX_MUST_FIX_FINDINGS",
    "CODEX_REVIEW_PARTIAL",
    "CODEX_REVIEW_UNPARSEABLE",
    "CODEX_TIMEOUT",
    "STAGE3_REVIEW",
    "_CATEGORY_TO_REASON",
    "_COMMAND_NOT_FOUND_RETURNCODE",
    "_DIFF_GIT_HEADER_RE",
    "_HUNK_RE",
    "_MIN_ROLE_TIMEOUT_SECONDS",
    "_OUTPUT_INSTRUCTIONS",
    "_REVIEWER_ROLE_AGENT_FILES",
    "_SENSITIVE_HEADER",
    "_TRANSIENT_FAILURE_REASONS",
    "_FileCategories",
    "_ReviewPassInputs",
    "_SensitiveHit",
    "_build_generic_codex_argv",
    "_build_reviewer_prompt",
    "_capture_diff",
    "_categorize_changed_files",
    "_classify_codex_failure",
    "_classify_codex_output_failure",
    "_codex_scratch_dir",
    "_format_failures_detail",
    "_hit_from_entry",
    "_load_agent_spec",
    "_load_optional_text",
    "_load_review_policy",
    "_load_sensitive_hits",
    "_load_ticket_context",
    "_parse_hunk_new_start",
    "_parse_review_policy",
    "_parse_reviewer_document",
    "_parse_unified_diff",
    "_persist_codex_role_diagnostics",
    "_prepare_review_pass",
    "_read_sensitive_manifest",
    "_render_findings",
    "_render_sensitive_block",
    "_run_codex_role",
    "_select_reviewer_roles",
    "_slug",
    "render_verdict_comment",
    "run_codex_roles",
    "run_review",
    "synthesize_codex_review_result",
]
