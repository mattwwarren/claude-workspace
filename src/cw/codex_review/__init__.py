"""Prompt-driven codex reviewer orchestration for CodexExecutor (#1236).

Replaces the single ``codex exec review`` subprocess call with a per-reviewer-
role loop of generic ``codex exec`` calls, each fed a materialized prompt over
stdin and validated through the executor-neutral ``review_findings`` library
(#1237). This package plays the same support role for ``CodexExecutor`` that
``local_runner`` plays for ``LocalExecutor``: ``executor.py``'s Step 3 shrinks
to a thin delegation into :func:`run_review`.

Every reviewer input — the authoritative agent spec, the diff, the plan/ticket
context, project rubrics, per-reviewer policy, and sensitive-file hits — is read
by ``cw`` and inlined into the prompt, unconditionally: that is what makes a
review pass reproducible rather than dependent on where ``.claude/*`` lives.

Whether codex can read anything *beyond* the inlined material is a property of
the runtime, not a constant. A snap-confined install cannot reach ``bwrap`` and
fails closed; a non-snap install on the same machine reads the worktree fine
(#1709). ``_capability`` probes which one is live and the reviewer prompt is
selected to match, so a capable runtime is never told it has no filesystem
access.

This package was split out of a single module; the public import surface
(``from cw.codex_review import X``) is preserved here via re-exports.
Submodules:

- ``_audit_events`` — ``codex exec --json`` JSONL event-stream parsing.
- ``_capability`` — the filesystem-capability probe and its on-disk,
  fingerprint-keyed, no-TTL cache.
- ``_const`` — reason vocabulary, transient-failure set, and category mapping.
- ``_diff`` — unified-diff capture and parsing.
- ``_context`` — reviewer selection and prompt-context assembly (+ doc parsing),
  including ``_prepare_review_pass`` (co-located with ``_load_optional_text``).
- ``_roles`` — per-role codex execution and failure classification.
- ``_verdict`` — verdict synthesis and review-comment rendering.
- ``core`` — ``run_review`` orchestration.
"""

from __future__ import annotations

from cw.codex_review._audit_events import (
    _EXPECTED_REVIEWER_ITEM_TYPES,
    _extract_terminal_error_message,
    _parse_codex_audit_events,
)
from cw.codex_review._capability import (
    _CodexFilesystemCapability,
    _CodexFingerprint,
    _probe_filesystem_capability,
    _reset_filesystem_capability_cache,
)
from cw.codex_review._const import (
    _CATEGORY_TO_REASON,
    _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
    _COMMAND_NOT_FOUND_RETURNCODE,
    _MIN_ROLE_TIMEOUT_SECONDS,
    _TRANSIENT_FAILURE_REASONS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_FIX_SCOPE_VIOLATION,
    CODEX_MODEL_CAPACITY,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_MUST_FIX_MECHANICALLY_REJECTED,
    CODEX_REVIEW_PARTIAL,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_TIMEOUT,
    STAGE3_REVIEW,
)
from cw.codex_review._context import (
    _GLOBAL_AGENTS_DIR,
    _OUTPUT_INSTRUCTIONS,
    _OUTPUT_INSTRUCTIONS_CAPABLE,
    _OUTPUT_INSTRUCTIONS_INLINED_ONLY,
    _REVIEWER_ROLE_AGENT_FILES,
    _SENSITIVE_HEADER,
    _AgentSpecResolution,
    _build_reviewer_prompt,
    _categorize_changed_files,
    _FileCategories,
    _hit_from_entry,
    _load_agent_spec_fallback_gate,
    _load_claude_md_quality_gates,
    _load_optional_text,
    _load_review_policy,
    _load_ruff_lint_config,
    _load_sensitive_hits,
    _load_ticket_context,
    _parse_review_policy,
    _parse_reviewer_document,
    _prepare_review_pass,
    _read_sensitive_manifest,
    _render_lint_grounding_block,
    _render_sensitive_block,
    _resolve_agent_spec,
    _ReviewPassInputs,
    _RuffLintConfig,
    _select_output_instructions,
    _select_reviewer_roles,
    _SensitiveHit,
)
from cw.codex_review._diff import (
    _DIFF_GIT_HEADER_RE,
    _HUNK_RE,
    _capture_delta_diff,
    _capture_diff,
    _capture_head_sha,
    _parse_hunk_new_start,
    _parse_unified_diff,
)
from cw.codex_review._roles import (
    _AUDIT_ARGV_FLAGS,
    _FLAG_REJECTION_MARKERS,
    _TERMINAL_EVENTS,
    _build_generic_codex_argv,
    _classify_codex_failure,
    _classify_codex_output_failure,
    _codex_scratch_dir,
    _is_audit_flag_rejection,
    _is_model_capacity_error,
    _persist_codex_role_diagnostics,
    _run_codex_role,
    _slug,
    run_codex_roles,
)
from cw.codex_review._verdict import (
    _CONFIDENCE_ANNOTATION,
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
    "CODEX_MODEL_CAPACITY",
    "CODEX_MUST_FIX_FINDINGS",
    "CODEX_MUST_FIX_MECHANICALLY_REJECTED",
    "CODEX_REVIEW_PARTIAL",
    "CODEX_REVIEW_UNPARSEABLE",
    "CODEX_TIMEOUT",
    "STAGE3_REVIEW",
    "_AUDIT_ARGV_FLAGS",
    "_CATEGORY_TO_REASON",
    "_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS",
    "_COMMAND_NOT_FOUND_RETURNCODE",
    "_CONFIDENCE_ANNOTATION",
    "_DIFF_GIT_HEADER_RE",
    "_EXPECTED_REVIEWER_ITEM_TYPES",
    "_FLAG_REJECTION_MARKERS",
    "_GLOBAL_AGENTS_DIR",
    "_HUNK_RE",
    "_MIN_ROLE_TIMEOUT_SECONDS",
    "_OUTPUT_INSTRUCTIONS",
    "_OUTPUT_INSTRUCTIONS_CAPABLE",
    "_OUTPUT_INSTRUCTIONS_INLINED_ONLY",
    "_REVIEWER_ROLE_AGENT_FILES",
    "_SENSITIVE_HEADER",
    "_TERMINAL_EVENTS",
    "_TRANSIENT_FAILURE_REASONS",
    "_AgentSpecResolution",
    "_CodexFilesystemCapability",
    "_CodexFingerprint",
    "_FileCategories",
    "_ReviewPassInputs",
    "_RuffLintConfig",
    "_SensitiveHit",
    "_build_generic_codex_argv",
    "_build_reviewer_prompt",
    "_capture_delta_diff",
    "_capture_diff",
    "_capture_head_sha",
    "_categorize_changed_files",
    "_classify_codex_failure",
    "_classify_codex_output_failure",
    "_codex_scratch_dir",
    "_extract_terminal_error_message",
    "_format_failures_detail",
    "_hit_from_entry",
    "_is_audit_flag_rejection",
    "_is_model_capacity_error",
    "_load_agent_spec_fallback_gate",
    "_load_claude_md_quality_gates",
    "_load_optional_text",
    "_load_review_policy",
    "_load_ruff_lint_config",
    "_load_sensitive_hits",
    "_load_ticket_context",
    "_parse_codex_audit_events",
    "_parse_hunk_new_start",
    "_parse_review_policy",
    "_parse_reviewer_document",
    "_parse_unified_diff",
    "_persist_codex_role_diagnostics",
    "_prepare_review_pass",
    "_probe_filesystem_capability",
    "_read_sensitive_manifest",
    "_render_findings",
    "_render_lint_grounding_block",
    "_render_sensitive_block",
    "_reset_filesystem_capability_cache",
    "_resolve_agent_spec",
    "_run_codex_role",
    "_select_output_instructions",
    "_select_reviewer_roles",
    "_slug",
    "render_verdict_comment",
    "run_codex_roles",
    "run_review",
    "synthesize_codex_review_result",
]
