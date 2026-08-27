"""Reviewer role selection and prompt-context assembly for the codex-review package.

Every reviewer input — the authoritative agent spec, plan/ticket context,
project rubrics, per-reviewer policy, and sensitive-file hits — is read here by
``cw`` and inlined into each role's materialized prompt. For most inputs the
source is the worktree and nothing else: inlining from the repo is what makes a
review pass reproducible and independent of where ``.claude/*`` happens to live.

The agent specification is the one documented exception (#1773). It is still
repo-local by default and read from the worktree first, but a repo whose
``.claude/agents/<role>.md`` is missing or blank falls back to the operator's
own ``~/.claude/agents/<role>.md`` rather than silently running the reviewer
with an empty ``## Agent Specification`` section. That fallback is gateable
per-repo via ``[tool.cw.codex_review].agent_spec_global_fallback`` in
``pyproject.toml`` (default enabled), and every outcome is diagnosed rather
than swallowed: :func:`_resolve_agent_spec` logs a warning when a spec is
absent everywhere, and the per-role :class:`AgentSpecStatus` it returns is
carried onto the verdict and rendered into the posted review comment.

What is **not** unconditional is whether codex can read anything *else*. That
varies by runtime — a snap-confined install cannot reach ``bwrap`` and fails
closed, while a non-snap install on the same machine reads the worktree fine
(#1709). ``_capability`` probes which one this is, and
:func:`_select_output_instructions` picks the matching ``_OUTPUT_INSTRUCTIONS``
variant, so a capable runtime is no longer told it cannot read.

Also owns file categorization, the small-/large-scope reviewer-selection
tables, and codex output-document parsing. Consumed by ``core`` (prompt
assembly) and ``_roles`` (doc parsing).

Package split (#2043) of the historical flat module, which had grown to 1348
lines. Seven concern submodules plus this re-export shim:

- ``_util`` — the shared optional-file read primitive.
- ``_prompt_text`` — static prompt template text and variant selection.
- ``_file_selection`` — changed-file categorization and role-selection tables.
- ``_agent_spec`` — per-role agent-spec resolution and its repo gate.
- ``_sensitive_files`` — sensitive-file registry reads, matching, rendering.
- ``_repo_config`` — ``review-policy.md`` / ``CLAUDE.md`` / ruff-config reads
  and the lint-grounding block.
- ``_prompt_render`` — prompt assembly and codex output-document parsing.
- ``core`` — per-pass context loading and :func:`_prepare_review_pass`.

Names are re-exported here so ``from cw.codex_review._context import X`` keeps
working. A test that patches one of them must patch it on the submodule that
DEFINES it (``_context._agent_spec._GLOBAL_AGENTS_DIR``,
``_context.core.fetch_issue_comments``): bare-name lookups inside a submodule
read that submodule's own globals, not this shim's separate binding.
"""

from __future__ import annotations

from cw.codex_review._context._agent_spec import (
    _GLOBAL_AGENTS_DIR,
    _REVIEWER_ROLE_AGENT_FILES,
    _AgentSpecResolution,
    _load_agent_spec_fallback_gate,
    _resolve_agent_spec,
)
from cw.codex_review._context._file_selection import (
    _categorize_changed_files,
    _FileCategories,
    _select_reviewer_roles,
)
from cw.codex_review._context._prompt_render import (
    _build_reviewer_prompt,
    _parse_reviewer_document,
    _render_adjudicated_findings_block,
)
from cw.codex_review._context._prompt_text import (
    _CAPABLE_ONLY_MARKER,
    _DELTA_MODE_MARKER,
    _INLINED_ONLY_MARKER,
    _OUTPUT_INSTRUCTIONS,
    _OUTPUT_INSTRUCTIONS_CAPABLE,
    _OUTPUT_INSTRUCTIONS_INLINED_ONLY,
    _select_output_instructions,
)
from cw.codex_review._context._repo_config import (
    _load_claude_md_quality_gates,
    _load_review_policy,
    _load_ruff_lint_config,
    _parse_review_policy,
    _render_lint_grounding_block,
    _RuffLintConfig,
)
from cw.codex_review._context._sensitive_files import (
    _SENSITIVE_HEADER,
    _hit_from_entry,
    _load_sensitive_hits,
    _read_sensitive_manifest,
    _render_sensitive_block,
    _SensitiveHit,
)
from cw.codex_review._context._util import _load_optional_text
from cw.codex_review._context.core import (
    _load_finding_dispositions,
    _load_operator_comments,
    _load_pending_operator_comment_marker,
    _load_ticket_context,
    _load_voided_findings,
    _prepare_review_pass,
    _ReviewPassInputs,
)

__all__ = [
    "_CAPABLE_ONLY_MARKER",
    "_DELTA_MODE_MARKER",
    "_GLOBAL_AGENTS_DIR",
    "_INLINED_ONLY_MARKER",
    "_OUTPUT_INSTRUCTIONS",
    "_OUTPUT_INSTRUCTIONS_CAPABLE",
    "_OUTPUT_INSTRUCTIONS_INLINED_ONLY",
    "_REVIEWER_ROLE_AGENT_FILES",
    "_SENSITIVE_HEADER",
    "_AgentSpecResolution",
    "_FileCategories",
    "_ReviewPassInputs",
    "_RuffLintConfig",
    "_SensitiveHit",
    "_build_reviewer_prompt",
    "_categorize_changed_files",
    "_hit_from_entry",
    "_load_agent_spec_fallback_gate",
    "_load_claude_md_quality_gates",
    "_load_finding_dispositions",
    "_load_operator_comments",
    "_load_optional_text",
    "_load_pending_operator_comment_marker",
    "_load_review_policy",
    "_load_ruff_lint_config",
    "_load_sensitive_hits",
    "_load_ticket_context",
    "_load_voided_findings",
    "_parse_review_policy",
    "_parse_reviewer_document",
    "_prepare_review_pass",
    "_read_sensitive_manifest",
    "_render_adjudicated_findings_block",
    "_render_lint_grounding_block",
    "_render_sensitive_block",
    "_resolve_agent_spec",
    "_select_output_instructions",
    "_select_reviewer_roles",
]
