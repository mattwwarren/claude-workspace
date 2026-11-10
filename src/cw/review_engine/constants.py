"""Constants and typed vocabularies for the repo review engine (RFC 0017 I1).

Leaf module: stdlib only at runtime, so every other ``cw.review_engine``
module and ``cw.review_adjudication`` can import it without a cycle.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Final

#: Workspace role the engine round records under. Its workspace slug is
#: ``repo-review-engine``.
ENGINE_ROLE: Final = "Repo Review Engine"

ENGINE_DEFAULT_CONSEQUENCE: Final = (
    "The repo review engine reported this finding without a stated consequence."
)
ENGINE_DEFAULT_SUGGESTED_FIX: Final = (
    "See the repo review engine's evidence; no suggested fix was stated."
)

#: Engine state lives under the worktree's ``.cw`` directory, never in git.
ENGINE_STATE_RELATIVE_DIR: Final = Path(".cw/review-engine")
ENGINE_SUMMARY_FILENAME: Final = "summary.json"
ENGINE_SKILL_SUBDIR: Final = "skill"

#: Prefix of the one-line args block ``materialize`` prints for the engine.
ENGINE_ARGS_PREFIX: Final = "cw-engine:"

#: Read-only preamble the engine skill receives verbatim. The prose in
#: ``auto-dev-review.md`` Step 3a.5 quotes it, and a test pins the two equal.
ENGINE_PREAMBLE: Final = (
    "This is a gate step. The worktree is read-only: do not edit, stage, "
    "commit, push or post anything. Write only the summary file named in the "
    "args line. Do not ask questions; report what you could not verify as "
    "coverage PARTIAL or FAILED."
)

#: Read-only preamble each codex engine role receives (RFC 0018 A4, #3216). It
#: is the first text under the role prompt's ``## Agent Specification``
#: heading. ``docs/headless-contract.md`` section 12.10 quotes it verbatim.
ENGINE_ROLE_PREAMBLE: Final = (
    "This is a gate step. The worktree is read-only: do not edit, stage, "
    "commit, push or post anything, and do not write any file. Do not ask "
    "questions. Emit only this role's findings JSON document, following the "
    "output schema in this prompt. Report anything you could not verify as "
    "status degraded, with the reason in detail."
)

#: Read-only preamble the codex aggregation turn receives first on stdin. cw
#: writes the summary file itself and fills ``schema_version``, ``head_sha``
#: and ``counts``. Section 12.10 quotes it verbatim.
ENGINE_AGGREGATOR_PREAMBLE: Final = (
    "This is a gate step. The worktree is read-only: do not edit, stage, "
    "commit, push or post anything, and do not write any file; cw writes the "
    "summary. Emit exactly one JSON object matching the output schema, with "
    "only verdict, coverage, risk, risk_notes, findings, acceptance and "
    "gate_signals; cw fills schema_version, head_sha and counts. The verdict "
    "is FAIL if and only if at least one finding is MUST_FIX or SHOULD_FIX. "
    "Never report coverage COMPLETE when a role document below is failed or "
    "missing."
)

#: The manifest a codex-capable engine commits beside its ``SKILL.md``.
ENGINE_MANIFEST_FILENAME: Final = "cw-engine.json"

#: Round ids name a directory under ``.cw/review-engine``; the same shape as
#: ``cw.review_workspace._ID_RE``.
ROUND_ID_RE: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

#: Caps on the materialized skill directory.
MAX_SKILL_FILES: Final = 512
MAX_SKILL_BYTES: Final = 8 * 1024 * 1024
#: Bound on symlink hops while resolving the skill in the default-branch tree.
MAX_SYMLINK_HOPS: Final = 8

#: Exit code for a park (preflight) and for a failed round that was recorded
#: (materialize, record). Exit 1 is a fault, exit 2 stays click's usage error.
EXIT_PARK: Final = 3
EXIT_ROUND_FAILED: Final = 3


class EngineRoundFailure(StrEnum):
    """Why an engine round failed. Each value is a ``reviewer_failed`` reason."""

    ENGINE_SUMMARY_INVALID = "engine_summary_invalid"
    ENGINE_COVERAGE_INCOMPLETE = "engine_coverage_incomplete"
    ENGINE_HEAD_MISMATCH = "engine_head_mismatch"
    ENGINE_NO_SUMMARY = "engine_no_summary"
    ENGINE_WORKTREE_MUTATED = "engine_worktree_mutated"
    ENGINE_UNRESOLVABLE = "engine_unresolvable"
    #: Codex lane only (#3216): a role turn failed, so the aggregator is skipped.
    ENGINE_ROLE_FAILED = "engine_role_failed"
    #: Codex lane only (#3216): the aggregation turn produced no valid output.
    ENGINE_AGGREGATION_FAILED = "engine_aggregation_failed"


class EngineRouteRefusalCode(StrEnum):
    """Why ``engine_route`` refused an adjudication row."""

    ENGINE_REJECT_FORBIDDEN = "engine_reject_forbidden"
    ENGINE_NON_DEFERRABLE = "engine_non_deferrable"
    ENGINE_MUST_FIX_DEFER_REQUIRES_OPERATOR = "engine_must_fix_defer_requires_operator"


#: One friction template per failure. None echoes finding text.
FRICTION: Final[dict[EngineRoundFailure, str]] = {
    EngineRoundFailure.ENGINE_SUMMARY_INVALID: (
        "engine_summary_invalid: {reason} at {field_path}"
    ),
    EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE: (
        "engine_coverage_incomplete: coverage={coverage}"
    ),
    EngineRoundFailure.ENGINE_HEAD_MISMATCH: (
        "engine_head_mismatch: summary={summary} expected={expected} head={head}"
    ),
    EngineRoundFailure.ENGINE_NO_SUMMARY: "engine_no_summary: {path}",
    EngineRoundFailure.ENGINE_WORKTREE_MUTATED: (
        "engine_worktree_mutated: git status or git diff HEAD --stat non-empty "
        "after the engine run"
    ),
    EngineRoundFailure.ENGINE_UNRESOLVABLE: "engine_unresolvable: {skill} ({cause})",
    EngineRoundFailure.ENGINE_ROLE_FAILED: "engine_role_failed: {role} ({reason})",
    EngineRoundFailure.ENGINE_AGGREGATION_FAILED: (
        "engine_aggregation_failed: {reason}"
    ),
}

#: ``{reason}`` values ``engine_role_failed`` carries: a ``ReviewerRunFailure``
#: reason, or the two engine-round-only causes.
ROLE_REASON_CODEX_TIMEOUT: Final = "codex_timeout"
ROLE_REASON_CODEX_ERROR: Final = "codex_error"
ROLE_REASON_CODEX_UNPARSEABLE: Final = "codex_review_unparseable"
ROLE_REASON_BUDGET_EXHAUSTED: Final = "budget_exhausted"
ROLE_REASON_FAILED_DOCUMENT: Final = "failed_document"
ROLE_REASON_UNREVIEWED_SHARD: Final = "unreviewed_shard"
ROLE_FAILED_REASONS: Final = frozenset(
    {
        ROLE_REASON_CODEX_TIMEOUT,
        ROLE_REASON_CODEX_ERROR,
        ROLE_REASON_CODEX_UNPARSEABLE,
        ROLE_REASON_BUDGET_EXHAUSTED,
        ROLE_REASON_FAILED_DOCUMENT,
        ROLE_REASON_UNREVIEWED_SHARD,
    }
)

#: ``{reason}`` values ``engine_aggregation_failed`` carries.
AGGREGATION_REASON_CODEX_TIMEOUT: Final = "codex_timeout"
AGGREGATION_REASON_CODEX_ERROR: Final = "codex_error"
AGGREGATION_REASON_CODEX_MODEL_CAPACITY: Final = "codex_model_capacity"
AGGREGATION_REASON_MISSING_OUTPUT: Final = "missing_output"
AGGREGATION_REASON_EMPTY_OUTPUT: Final = "empty_output"
AGGREGATION_REASON_INVALID_JSON: Final = "invalid_json"
AGGREGATION_REASON_SCHEMA_MISMATCH: Final = "schema_mismatch"
AGGREGATION_FAILED_REASONS: Final = frozenset(
    {
        AGGREGATION_REASON_CODEX_TIMEOUT,
        AGGREGATION_REASON_CODEX_ERROR,
        AGGREGATION_REASON_CODEX_MODEL_CAPACITY,
        AGGREGATION_REASON_MISSING_OUTPUT,
        AGGREGATION_REASON_EMPTY_OUTPUT,
        AGGREGATION_REASON_INVALID_JSON,
        AGGREGATION_REASON_SCHEMA_MISMATCH,
    }
)


def role_slug(role: str) -> str:
    """Filesystem-safe slug for a reviewer or engine role name."""
    return re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")


# Engine-resolution causes shared by the default-branch reader and codex pass.
CAUSE_MATERIALIZE_FAILED: Final = "materialize_failed"
CAUSE_DEFAULT_BRANCH_UNRESOLVED: Final = "default_branch_unresolved"


#: Causes ``engine_unresolvable`` may carry.
UNRESOLVABLE_CAUSES: Final = frozenset(
    {
        "skill_not_found",
        "skill_unreadable",
        CAUSE_MATERIALIZE_FAILED,
        CAUSE_DEFAULT_BRANCH_UNRESOLVED,
    }
)


#: Causes the ``review_engine_unsupported_backend`` refusal carries (#3216).
CAUSE_NO_CODEX_MANIFEST: Final = "no_codex_manifest"
CAUSE_CODEX_NOT_DECLARED: Final = "codex_not_declared"
CAUSE_MANIFEST_INVALID: Final = "manifest_invalid"
CAUSE_BACKEND_UNSUPPORTED: Final = "backend_unsupported"
ENGINE_REFUSAL_CAUSES: Final = frozenset(
    {
        CAUSE_NO_CODEX_MANIFEST,
        CAUSE_CODEX_NOT_DECLARED,
        CAUSE_MANIFEST_INVALID,
        CAUSE_BACKEND_UNSUPPORTED,
    }
)

_REFUSAL_REMEDY: Final[dict[str, str]] = {
    CAUSE_NO_CODEX_MANIFEST: (
        "add cw-engine.json beside SKILL.md on the default branch, or point "
        "review.engine at a skill that has one"
    ),
    CAUSE_CODEX_NOT_DECLARED: 'list "codex" in the manifest\'s backends',
    CAUSE_MANIFEST_INVALID: (
        "repair cw-engine.json (see `cw review engine validate --kind manifest`)"
    ),
    CAUSE_BACKEND_UNSUPPORTED: (
        "run review on a claude-native or codex lane, or remove review.engine"
    ),
}


def engine_refusal_details(backend: str, cause: str, detail: str = "") -> str:
    """The ``blocker.details`` of a ``review_engine_unsupported_backend`` refusal.

    Shared by the opencode/local pre-flight guard and the codex startup check.
    Lives in this leaf module because ``cw.executor`` imports ``cw.codex_review``,
    so the codex lane cannot import the executor-side guard.
    """
    parts = [f"review engine refused on {backend} lane: cause={cause}"]
    if detail:
        parts.append(detail)
    parts.append(_REFUSAL_REMEDY[cause])
    parts.append("then cw dev-queue requeue")
    return "; ".join(parts)
