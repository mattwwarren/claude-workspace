"""Gate set B: cw's cross-checks of a PLAN turn's plan text and sentinel (#3060 R6).

Each gate is a pure function returning a :class:`GateFailure` or ``None``. The
model classifies the scope tier; cw only cross-checks it (exactly one
``**Scope tier:**`` line, and the sentinel's tier must match it). No tier is
ever computed here from a file or line count. TMPDIR scrubbing does not apply:
these are in-process Python checks with no spawned child to scrub.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.auto_dev_result import SCOPE_TIER_LARGE
from cw.codex_plan._result import (
    PLAN_APPROVAL_NEXT_ACTIONS,
    PLAN_PENDING_APPROVAL,
    PLAN_STAGE_COMPLETE,
)
from cw.plan_files import (
    _SCOPE_TIER_MARKER,
    _strip_fenced_lines,
    parse_plan_files_modified,
    parse_plan_scope_tier,
)

if TYPE_CHECKING:
    from cw.auto_dev_result import AutoDevResult
    from cw.models import TicketTask

#: The plan text cw would write is missing or empty.
CODEX_PLAN_ARTIFACT_MISSING: Final = "codex_plan_artifact_missing"
#: A gate-set-B structural check refused the plan; ``details`` names the gate.
CODEX_PLAN_GATE_FAILED: Final = "codex_plan_gate_failed"

#: The ``next_actions`` a large plan parked for row approval carries.
APPROVAL_NEXT_ACTIONS: Final = PLAN_APPROVAL_NEXT_ACTIONS


@dataclass(frozen=True)
class GateFailure:
    """One refused gate: the blocked reason and the details naming the gate."""

    reason: str
    details: str


@dataclass(frozen=True)
class ApprovalCheck:
    """The result after the large-tier approval gate, and any re-park note."""

    result: AutoDevResult
    mismatch: tuple[str, str] | None = None


def check_artifact(plan_text: str) -> GateFailure | None:
    """The plan text must be non-empty."""
    if plan_text.strip():
        return None
    return GateFailure(
        CODEX_PLAN_ARTIFACT_MISSING, "the PLAN turn returned an empty plan_markdown"
    )


def check_plan_files(plan_text: str) -> GateFailure | None:
    """The plan must carry a ``## Files Modified`` section with at least one path."""
    if parse_plan_files_modified(plan_text):
        return None
    return GateFailure(
        CODEX_PLAN_GATE_FAILED,
        "gate files_modified: no ## Files Modified section with a file path",
    )


def _tier_line_count(plan_text: str) -> int:
    """How many fence-aware ``**Scope tier:**`` lines the plan carries."""
    return sum(
        1
        for line in _strip_fenced_lines(plan_text.splitlines())
        if _SCOPE_TIER_MARKER.match(line) is not None
    )


def check_scope_tier(plan_text: str, sentinel_tier: str | None) -> GateFailure | None:
    """Exactly one tier line, and the sentinel's ``scope.tier`` must equal it."""
    count = _tier_line_count(plan_text)
    if count != 1:
        return GateFailure(
            CODEX_PLAN_GATE_FAILED,
            f"gate scope_tier_line: expected exactly one **Scope tier:** line, "
            f"found {count}",
        )
    plan_tier = parse_plan_scope_tier(plan_text)
    if plan_tier != sentinel_tier:
        return GateFailure(
            CODEX_PLAN_GATE_FAILED,
            f"gate scope_tier_match: the plan says {plan_tier!r} but the "
            f"sentinel says {sentinel_tier!r}",
        )
    return None


def has_approval_evidence(task: TicketTask) -> bool:
    """True when the row carries a row-path approval (``cw dev-queue approve``)."""
    return task.plan_approved_at is not None and bool(task.plan_approved_fingerprint)


def check_large_tier_approval(
    result: AutoDevResult, task: TicketTask, plan_fingerprint: str
) -> ApprovalCheck:
    """A large ``stage_complete`` needs approval evidence bound to this plan.

    With no evidence, the result becomes ``plan_pending_approval``. With
    evidence whose fingerprint differs from *plan_fingerprint*, it re-parks
    the same way and carries ``(approved, current)`` so the park comment can
    quote both. Only exact equality is evidence in C1 (#3465 covers the
    operator-authority delta and the comment path).
    """
    if result.status != PLAN_STAGE_COMPLETE or result.scope.tier != SCOPE_TIER_LARGE:
        return ApprovalCheck(result)
    parked = result.model_copy(
        update={
            "status": PLAN_PENDING_APPROVAL,
            "next_actions": list(APPROVAL_NEXT_ACTIONS),
        }
    )
    if not has_approval_evidence(task):
        return ApprovalCheck(parked)
    approved = task.plan_approved_fingerprint or ""
    if approved == plan_fingerprint:
        return ApprovalCheck(result)
    friction = (
        f"plan_approval_fingerprint_mismatch: approved={approved} "
        f"current={plan_fingerprint}"
    )
    parked = parked.model_copy(
        update={"friction_highlights": [*parked.friction_highlights, friction]}
    )
    return ApprovalCheck(parked, mismatch=(approved, plan_fingerprint))
