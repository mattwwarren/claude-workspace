"""The two plan review stations and sign-off marker stamping (#3060 R1).

After the PLAN turn and gate set B, cw runs the Plan Reviewer and then the
Plan Soundness Reviewer as per-role, read-only ``codex exec`` turns through the
review roster's own machinery (:func:`cw.codex_review.run_codex_roles`, called
with no wall-clock budget, so no timeout, ADR-0014). Each station's
instructions are the repo's agent spec for its role, resolved repo-first and
then from the operator's global agents directory, exactly as review roles are.

A station verdict is **clean** when it is exactly one document with
``status == "ok"``, no ``MUST_FIX`` finding and no rejected item; ``degraded``
is not clean. cw stamps each sign-off marker only from its station's clean
verdict; the model is never asked to stamp one. On a completing exit the
stations are binding (a non-clean verdict blocks, and the soundness station
does not run after a blocking Plan Reviewer); on a park exit they are
advisory, and their findings ride in the park comment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from cw.codex_plan._result import (
    PLAN_SOUNDNESS_MARKER_VERSION,
    PLAN_SPEC_MARKER_VERSION,
)
from cw.codex_review import (
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    PLAN_STATION_AGENT_FILES,
    _load_agent_spec_fallback_gate,
    _resolve_agent_spec,
    run_codex_roles,
)
from cw.dev_queue.lifecycle import (
    _PLAN_SOUNDNESS_MARKER,
    _PLAN_SPEC_MARKER,
)
from cw.models.enums import SandboxMode

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from cw.codex_runner import CodexRunner
    from cw.review_findings import (
        Finding,
        RejectedFinding,
        ReviewerFindingsDocument,
        ReviewerRunFailure,
    )

# The role names and their agent specs have one source in the shared mapping.
# Station order follows mapping insertion order: soundness runs only after a
# clean Plan Reviewer.
PLAN_STATIONS: Final[tuple[str, ...]] = tuple(PLAN_STATION_AGENT_FILES)
PLAN_REVIEWER: Final = PLAN_STATIONS[0]
PLAN_SOUNDNESS_REVIEWER: Final = PLAN_STATIONS[1]

#: The blocked reason a non-clean station verdict parks with.
STATION_BLOCK_REASONS: Final[dict[str, str]] = {
    PLAN_REVIEWER: "plan_unreviewable",
    PLAN_SOUNDNESS_REVIEWER: "plan_unsound",
}

#: The only retry-eligible station failure: a provider capacity blip.
_RETRYABLE_FAILURES: Final = frozenset({CODEX_MODEL_CAPACITY})

_MUST_FIX: Final = "MUST_FIX"

#: The fixed output instruction every station prompt ends with.
STATION_OUTPUT_INSTRUCTIONS: Final = (
    "## Output\n\n"
    "You review the plan above; you do not edit it and you write no files. "
    "Return one JSON document matching the output schema: reviewer_role (your "
    "role name), status (ok, degraded or failed), detail (what you checked; "
    "required when there are no findings, and the reason when degraded or "
    "failed) and findings. Each finding has severity (MUST_FIX, SHOULD_FIX, "
    "DEBT, NIT or PRINCIPLE), file (the plan path .cw/plan.md, or the source "
    "path the finding is about), line_start and line_end (null when not "
    "anchored to a line), summary, consequence, suggested_fix, evidence and "
    "confidence (HIGH, MEDIUM or LOW). Use MUST_FIX only for a finding that "
    "must block the plan. Report an uncodified risk as SHOULD_FIX with a "
    "[RISK] prefix on its summary.\n"
)


@dataclass(frozen=True)
class StationBlock:
    """Why a binding station refused the plan."""

    reason: str
    details: str
    retry_eligible: bool


@dataclass(frozen=True)
class StationsOutcome:
    """The stations' result: a block, or clean, plus every finding line seen."""

    block: StationBlock | None
    findings: tuple[str, ...] = field(default=())

    @property
    def clean(self) -> bool:
        """True when every station that ran was clean."""
        return self.block is None


def build_station_prompt(role: str, spec: str, plan_text: str) -> str:
    """The station's prompt: its agent spec, the plan, then the output rules."""
    spec_section = spec.strip() or "(no agent specification was found)"
    return (
        f"You are the {role}, reviewing an implementation plan before any code "
        "is written. This is one read-only turn.\n\n"
        f"## Agent Specification\n\n{spec_section}\n\n"
        f"## Plan Under Review\n\n{plan_text.rstrip()}\n\n"
        f"{STATION_OUTPUT_INSTRUCTIONS}"
    )


def _format_finding(role: str, finding: Finding) -> str:
    return (
        f"- {role} [{finding.severity}] {finding.summary} — "
        f"{finding.consequence} (fix: {finding.suggested_fix})"
    )


def _failure_block(failure: ReviewerRunFailure) -> StationBlock:
    detail = f" ({failure.detail})" if failure.detail else ""
    details = f"{failure.role} station failed: {failure.reason}{detail}"
    if failure.reason in _RETRYABLE_FAILURES:
        return StationBlock(CODEX_MODEL_CAPACITY, details, retry_eligible=True)
    return StationBlock(CODEX_ERROR, details, retry_eligible=False)


def _classify(
    role: str,
    documents: list[ReviewerFindingsDocument],
    failures: list[ReviewerRunFailure],
    rejected: list[RejectedFinding],
) -> tuple[StationBlock | None, list[str]]:
    """One station's verdict: a block (or ``None`` when clean) and its findings."""
    if failures or len(documents) != 1:
        if failures:
            block = _failure_block(failures[0])
        else:
            details = f"{role} station returned {len(documents)} documents"
            block = StationBlock(CODEX_ERROR, details, retry_eligible=False)
        return block, []
    document = documents[0]
    reason = STATION_BLOCK_REASONS[role]
    if document.reviewer_role != role:
        details = f"{role} station returned reviewer_role {document.reviewer_role!r}"
        return StationBlock(reason, details, retry_eligible=False), []
    lines = [_format_finding(role, finding) for finding in document.findings]
    if document.status != "ok":
        details = f"{role} returned status {document.status}: {document.detail}"
        return StationBlock(reason, details, retry_eligible=False), lines
    if rejected:
        details = f"{role} returned {len(rejected)} finding(s) that failed validation"
        return StationBlock(reason, details, retry_eligible=False), lines
    blocking = [
        _format_finding(role, finding)
        for finding in document.findings
        if finding.severity == _MUST_FIX
    ]
    if blocking:
        details = f"{role} MUST_FIX findings:\n" + "\n".join(blocking)
        return StationBlock(reason, details, retry_eligible=False), lines
    return None, lines


def run_plan_stations(
    *,
    runner: CodexRunner,
    worktree: Path,
    plan_text: str,
    model: str | None,
    reasoning_effort: str | None,
    session_id: str,
    binding: bool,
) -> StationsOutcome:
    """Run both stations over *plan_text*; never mark a plan reviewed otherwise.

    *binding* (a completing exit): the first non-clean station returns its
    block and later stations do not run. Advisory (a park exit): every station
    runs and only its findings are kept; the outcome never blocks.
    """
    fallback = _load_agent_spec_fallback_gate(worktree)
    findings: list[str] = []
    for role in PLAN_STATIONS:
        spec = _resolve_agent_spec(
            worktree,
            role,
            global_fallback_enabled=fallback,
            agent_files=PLAN_STATION_AGENT_FILES,
        ).text
        documents, failures, _metrics, rejected = run_codex_roles(
            runner=runner,
            worktree=worktree,
            roles=[role],
            prompts_by_role={role: build_station_prompt(role, spec, plan_text)},
            model=model,
            reasoning_effort=reasoning_effort,
            wall_clock_budget_seconds=None,
            session_id=session_id,
            reviewer_sandbox_mode=SandboxMode.READ_ONLY,
        )
        block, lines = _classify(role, documents, failures, rejected)
        findings.extend(lines)
        if block is not None and binding:
            return StationsOutcome(block=block, findings=tuple(findings))
    return StationsOutcome(block=None, findings=tuple(findings))


def stamp_markers(plan_text: str, *, today: date) -> str:
    """Prepend both sign-off markers, each on its own line, to *plan_text*."""
    stamp = today.isoformat()
    return (
        f"{_PLAN_SPEC_MARKER}: {stamp} {PLAN_SPEC_MARKER_VERSION} -->\n"
        f"{_PLAN_SOUNDNESS_MARKER}: {stamp} "
        f"{PLAN_SOUNDNESS_MARKER_VERSION} -->\n\n"
        f"{plan_text.lstrip()}"
    )
