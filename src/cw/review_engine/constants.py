"""Constants and typed vocabularies for the repo review engine (RFC 0017 I1).

Leaf module: stdlib only at runtime, so every other ``cw.review_engine``
module and ``cw.review_adjudication`` can import it without a cycle.
"""

from __future__ import annotations

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
}

#: Causes ``engine_unresolvable`` may carry.
UNRESOLVABLE_CAUSES: Final = frozenset(
    {
        "skill_not_found",
        "skill_unreadable",
        "materialize_failed",
        "default_branch_unresolved",
    }
)
