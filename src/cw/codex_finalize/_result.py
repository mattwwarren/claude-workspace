"""The FINALIZE turn's document, cw's blocks, and the cw-built results (#3062).

cw, never the model, emits the FINALIZE sentinel (RFC 0018 C3 R10): the turn
returns a strict four-field document (PR title, PR body, adjudication section
text and operator-override section text) and nothing else. Every result is
built here from cw's own measurements: ``schema_version``, ``ticket_id``,
``stage_reached``, ``worktree_path``, ``branch``, ``fork_point_sha``,
``commits`` (``local_runner.git_facts``; ``stamp_stage_facts`` is not used),
``pr`` (cw's verified :class:`~cw.gh.PrHead`), ``review`` (zeros, as the doc
templates carry), ``scope.tier`` (``resolve_review_scope_tier``) and
``plan_source`` (``resolve_plan_source``).

The three reasons this executor adds to the vocabulary are owned here and
restated as literals in ``cw.auto_dev_result.schema._vocab``; a lockstep test
in ``tests/test_auto_dev_result.py`` pins the two copies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from cw.auto_dev_result import AutoDevResult
from cw.auto_dev_result.parse import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_VALIDATION_FAILED,
)
from cw.codex_review import (
    STAGE4A_MERGE_GATE,
    STAGE5_POST_CREATE,
    make_codex_blocked,
)
from cw.dev_queue.gates import FINALIZE_PARK_STAGE_REACHED
from cw.unavailability import classify_unavailability as _classify

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import PlanSource, ScopeTier, StageReached
    from cw.local_runner import GitFacts

#: New reasons owned by the codex FINALIZE executor (registered in ``_vocab``).
PR_ALREADY_TERMINAL: Final = "pr_already_terminal"
MERGE_CONFLICT_POST_PUSH: Final = "merge_conflict_post_push"
SCOPE_TIER_UNRESOLVABLE: Final = "scope_tier_unresolvable"

#: Existing reasons the FINALIZE stage reuses.
AGENT_BLOCK: Final = "agent_block"
AUTOMERGE_NOT_ARMED: Final = "automerge_not_armed"
OPERATOR_UNAVAILABLE: Final = "operator_unavailable"
PRIOR_PIPELINE_PR_OPEN: Final = "prior_pipeline_pr_open"
PUSH_AUTH_FAILED: Final = "push_auth_failed"
IMPL_NOT_PUSHED: Final = "impl_not_pushed"
CODEX_MUST_FIX_FINDINGS: Final = "codex_must_fix_findings"

#: The ``blocker.stage`` of a must-fix override refusal (the doc's label).
STAGE4_MUST_FIX_OVERRIDE: Final = "stage4_must_fix_override"

#: ``next_actions`` of the gate parks (the doc's templates).
REQUEUE_STAGE_REVIEW: Final = "requeue_stage_review"
REQUEUE_STAGE_FINALIZE: Final = "requeue_stage_finalize"
WAIT_FOR_CI: Final = "wait_for_ci"

#: The PR title's maximum length (``/prep-pr``'s title rule).
PR_TITLE_MAX_CHARS: Final = 72

_SCHEMA_VERSION: Final = 4
_NUMERIC_TICKET: Final = re.compile(r"[0-9]+")
_ZERO_REVIEW: Final[dict[str, int]] = {
    "must_fix_initial": 0,
    "should_fix": 0,
    "fix_cycles_used": 0,
}
_PROCEED_HEALTH: Final[dict[str, object]] = {
    "lowest_agent_confidence": "HIGH",
    "any_incomplete_risk": False,
    "shortcuts": [],
    "recommendation": "PROCEED",
    "downgrade_applied": False,
    "fix_loop_escalated": False,
}


class FinalizeTurnDocument(BaseModel):
    """The FINALIZE turn's ``-o`` document: the judgement text only."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    pr_title: str
    pr_body: str
    adjudication_section: str
    override_section: str


@dataclass(frozen=True)
class FinalizeBlock:
    """A cw-decided FINALIZE block: reason, details and where it happened.

    *stage* is the wire ``stage_reached``; *blocker_stage* overrides
    ``blocker.stage`` (the override refusal's ``stage4_must_fix_override``).
    *pr_info* is injected into the dumped result for ``automerge_not_armed``
    (``AutoDevResult`` has no ``pr_info`` field; the dispositions reader does).
    """

    reason: str
    details: str
    retry_eligible: bool = False
    stage: StageReached = STAGE4A_MERGE_GATE
    blocker_stage: str | None = None
    pr_info: dict[str, object] | None = None


@dataclass(frozen=True)
class FinalizeFacts:
    """cw's stamped facts, shared by every result builder."""

    ticket_id: str
    worktree: Path
    git: GitFacts
    tier: ScopeTier
    plan_source: PlanSource
    friction: list[str] = field(default_factory=list)


def parse_finalize_turn(content: str | None) -> FinalizeTurnDocument | FinalizeBlock:
    """Parse the ``-o`` *content* strictly.

    A missing, empty, non-document or extra-key document is
    ``no_result_emitted``; an empty ``pr_title`` is ``validation_failed``.
    """
    if content is None or not content.strip():
        return FinalizeBlock(
            BLOCKER_REASON_NO_RESULT_EMITTED, "the FINALIZE turn wrote no -o document"
        )
    try:
        document = FinalizeTurnDocument.model_validate_json(content)
    except ValidationError as exc:
        return FinalizeBlock(
            BLOCKER_REASON_NO_RESULT_EMITTED,
            f"the -o document is not the four-field FINALIZE object: {exc}",
        )
    if not document.pr_title.strip():
        return FinalizeBlock(
            BLOCKER_REASON_VALIDATION_FAILED, "the turn returned an empty pr_title"
        )
    return document


def compose_pr_title(title: str, ticket_id: str) -> str:
    """The PR title: one line, at most 72 characters, ``(#<id>)`` for GitHub ids.

    The suffix is appended only for a numeric (GitHub issue) id and only when
    the model did not already end the title with it; the title text is
    truncated so the whole title fits.
    """
    line = " ".join(title.split())
    suffix = ""
    if _NUMERIC_TICKET.fullmatch(ticket_id):
        suffix = f" (#{ticket_id})"
        if line.endswith(suffix.strip()):
            line = line[: -len(suffix.strip())].rstrip()
    room = PR_TITLE_MAX_CHARS - len(suffix)
    return line[:room].rstrip() + suffix


def _scope(facts: FinalizeFacts, tier: ScopeTier | None = None) -> dict[str, Any]:
    git = facts.git
    return {
        "tier": tier or facts.tier,
        "files": git["files"],
        "lines_estimate": git["lines_actual"],
        "lines_actual": git["lines_actual"],
        "forbidden_touched": False,
    }


def _base(facts: FinalizeFacts, *, status: str, stage: StageReached) -> dict[str, Any]:
    git = facts.git
    return {
        "schema_version": _SCHEMA_VERSION,
        "ticket_id": facts.ticket_id,
        "status": status,
        "stage_reached": stage,
        "scope": _scope(facts),
        "plan_source": facts.plan_source,
        "branch": git["branch"] or None,
        "worktree_path": str(facts.worktree),
        "fork_point_sha": git["fork_point"] or None,
        "commits": list(git["commits"]),
        "pr": None,
        "review": dict(_ZERO_REVIEW),
        "health": dict(_PROCEED_HEALTH),
        "friction_highlights": list(facts.friction),
        "blocker": None,
        "next_actions": [],
    }


def finalize_blocked(
    block: FinalizeBlock,
    *,
    ticket_id: str,
    worktree: Path,
    facts: FinalizeFacts | None,
) -> dict[str, Any]:
    """A blocked FINALIZE result as the dumped payload the door persists.

    Built on ``make_codex_blocked`` (the finalize ``next_actions`` label by
    stage); with *facts* the git facts, tier and friction are stamped over.
    """
    result = make_codex_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason=block.reason,
        details=block.details,
        retry_eligible=block.retry_eligible,
        stage_reached=block.stage,
    )
    payload = result.model_dump(mode="json")
    if facts is not None:
        git = facts.git
        payload.update(
            {
                "scope": _scope(facts),
                "plan_source": facts.plan_source,
                "branch": git["branch"] or None,
                "fork_point_sha": git["fork_point"] or None,
                "commits": list(git["commits"]),
                "friction_highlights": list(facts.friction),
            }
        )
    if block.blocker_stage is not None:
        payload["blocker"]["stage"] = block.blocker_stage
    payload = AutoDevResult.model_validate(payload).model_dump(mode="json")
    if block.pr_info is not None:
        payload["pr_info"] = dict(block.pr_info)
    return payload


def finalize_approval_park(
    facts: FinalizeFacts,
    *,
    tier: ScopeTier,
    friction: list[str],
    next_action: str = REQUEUE_STAGE_REVIEW,
) -> dict[str, Any]:
    """The Step 4.0 / 4.0b ``review_pending_approval`` park.

    ``stage4a_merge_gate``, ``pr`` and ``blocker`` null; *friction* replaces
    the friction wholesale (the doc's rule for both parks).
    """
    payload = _base(
        facts, status="review_pending_approval", stage=FINALIZE_PARK_STAGE_REACHED
    )
    payload["scope"] = _scope(facts, tier)
    payload["friction_highlights"] = list(friction)
    payload["next_actions"] = [next_action]
    return AutoDevResult.model_validate(payload).model_dump(mode="json")


def finalize_merge_gate_blocked(
    facts: FinalizeFacts, *, details: str
) -> dict[str, Any]:
    """The Step 4a ``merge_gate_blocked`` result (``prior_pipeline_pr_open``)."""
    payload = _base(facts, status="merge_gate_blocked", stage=STAGE4A_MERGE_GATE)
    payload["blocker"] = {
        "stage": STAGE4A_MERGE_GATE,
        "reason": PRIOR_PIPELINE_PR_OPEN,
        "details": details,
        "retry_eligible": True,
    }
    return AutoDevResult.model_validate(payload).model_dump(mode="json")


def finalize_shipped(
    facts: FinalizeFacts,
    *,
    pr_number: int,
    pr_url: str,
    auto_merge: bool,
    base: str,
) -> dict[str, Any]:
    """The ``shipped`` result: a verified ``pr`` and ``wait_for_ci``."""
    payload = _base(facts, status="shipped", stage=STAGE5_POST_CREATE)
    payload["pr"] = {
        "number": pr_number,
        "url": pr_url,
        "auto_merge": auto_merge,
        "base": base,
    }
    payload["next_actions"] = [WAIT_FOR_CI]
    return AutoDevResult.model_validate(payload).model_dump(mode="json")


def classify_unavailability(stderr: str) -> str | None:
    """``push_auth_failed`` when *stderr* carries an unavailability signature.

    The signatures are the shared table in :mod:`cw.unavailability` (the
    appendix's mirrored list); ``None`` means no signature matched and the
    caller keeps its own reason (``impl_not_pushed`` for a push).
    """
    if _classify(stderr) is None:
        return None
    return PUSH_AUTH_FAILED
