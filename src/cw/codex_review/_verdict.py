"""Verdict synthesis and review-comment rendering for the codex-review package.

Consolidates the per-role reviewer documents into a single verdict, maps it to
a typed :class:`AutoDevResult` (blocked/CODEX_* on empty, MUST_FIX, or partial
rosters; stage_complete otherwise), and renders the consolidated verdict into a
GitHub-issue-comment markdown body. Consumed by ``core`` (result synthesis).
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from cw.auto_dev_result import AutoDevResult, Health, Scope
from cw.codex_review._const import (
    _TRANSIENT_FAILURE_REASONS,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_PARTIAL,
    CODEX_REVIEW_UNPARSEABLE,
    STAGE3_REVIEW,
)
from cw.executor_diagnostics import append_diagnostics_pointer
from cw.local_runner import _SCHEMA_VERSION, make_blocked, resolve_tier
from cw.review_findings import consolidate_verdict
from cw.worktree import compute_branch_diff_scope

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_review._capability import _CodexFilesystemCapability
    from cw.models import TicketTask
    from cw.review_findings import (
        CapturedDiff,
        ReviewerFindingsDocument,
        ReviewerRunFailure,
        ReviewerRunMetrics,
        ReviewVerdict,
        Severity,
    )

_log = logging.getLogger(__name__)


# Confidence values other than HIGH render an inline annotation on their
# finding line so a reader can weight it — confidence is display-only and
# must never gate/filter/reorder findings (R0, #1555). HIGH is the common
# case and stays unmarked to keep the common path uncluttered.
_CONFIDENCE_ANNOTATION = " _({confidence} confidence)_"


def _format_failures_detail(
    failures: list[ReviewerRunFailure], *, session_id: str
) -> str:
    """Render *failures* as a short ``role (reason)`` summary for ``details``.

    Appends a pointer to the on-disk diagnostics bundle so an operator reading
    the blocked sentinel knows where the per-role failure artifacts landed.
    """
    summary = "; ".join(f"{f.role} ({f.reason})" for f in failures)
    return append_diagnostics_pointer(summary, session_id=session_id)


def _derive_health(documents: list[ReviewerFindingsDocument]) -> Health:
    """Derive the clean-review ``Health`` signal from reviewer document status.

    Reached only after the caller has already established there is no
    MUST_FIX finding and no :class:`ReviewerRunFailure` — i.e. "clean" here
    means "nothing wrong was found," not "full coverage was achieved."
    ``failures`` is deliberately not a parameter: every call site reaches
    this helper only after ``if failures: ...`` has already returned, so
    ``failures == []`` is already an established invariant here.

    Any document whose ``status`` is not ``"ok"`` — a ``degraded`` role that
    could not complete a required check, or a self-reported ``failed``
    document that still parsed — means that role's coverage was reduced even
    though it produced neither a MUST_FIX finding nor a run failure. Reporting
    that as full HIGH-confidence PROCEED would be exactly the "spuriously
    clean sentinel" risk the surrounding disposition logic exists to catch.
    """
    if any(doc.status != "ok" for doc in documents):
        return Health(
            lowest_agent_confidence="MEDIUM",
            any_incomplete_risk=True,
            recommendation="EXIT_FOR_HUMAN_REVIEW",
        )
    return Health(
        lowest_agent_confidence="HIGH",
        any_incomplete_risk=False,
        recommendation="PROCEED",
    )


def _with_capability(
    verdict: ReviewVerdict, capability: _CodexFilesystemCapability | None
) -> ReviewVerdict:
    """Return *verdict* with the probed capability mode recorded on it (#1709).

    ``consolidate_verdict`` is executor-neutral and knows nothing about codex
    sandboxes, so the two fields are stamped on afterwards rather than threaded
    through it. ``None`` in, unchanged verdict out — a caller that never probed
    must not be reported as having run in some mode.
    """
    if capability is None:
        return verdict
    return verdict.model_copy(
        update={
            "capability_mode": "capable" if capability.capable else "degraded",
            "capability_reason": capability.reason,
        }
    )


def synthesize_codex_review_result(
    *,
    task: TicketTask,
    worktree: Path,
    documents: list[ReviewerFindingsDocument],
    failures: list[ReviewerRunFailure],
    diff: CapturedDiff,
    reviewed_sha: str,
    session_id: str,
    default_branch: str,
    metrics_by_role: dict[str, ReviewerRunMetrics] | None = None,
    capability: _CodexFilesystemCapability | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Map consolidated review documents to a typed AutoDevResult.

    Disposition:
    - zero documents (all roles failed/skipped) → blocked/CODEX_REVIEW_UNPARSEABLE,
      with ``failures`` folded into ``details`` and ``retry_eligible=True`` when
      at least one failure is transient (``codex_timeout``/``budget_exhausted``)
      (MUST_FIX 2, #1236).
    - consolidated verdict is blocking            → blocked/CODEX_MUST_FIX_FINDINGS
    - documents present but at least one selected role skipped/errored without
      producing one (a partial review) → blocked/CODEX_REVIEW_PARTIAL — a
      review that silently proceeded on an incomplete roster would be exactly
      the "spuriously clean sentinel" risk the ``agents_run`` gate exists to
      catch (Decision 7, #1236).
    - otherwise (documents complete, no MUST_FIX)  → stage_complete

    Returns ``(result, verdict)``; ``verdict`` is ``None`` only on the zero-
    documents path (nothing to render into a review comment).

    ``metrics_by_role`` (#1710) is per-role codex audit telemetry, threaded
    into :func:`consolidate_verdict` so it lands on ``verdict.agents_run``. It
    is deliberately unused on the zero-documents branch: no ``ReviewVerdict``
    is built there, so the telemetry has nowhere to attach. Nothing in the
    disposition table or :func:`_derive_health` reads it (R2).

    ``capability`` (#1709) is the probed filesystem-capability verdict the
    reviewer prompts were built against. Same shape and same non-influence as
    ``metrics_by_role``: recorded onto the ``ReviewVerdict``, never read by the
    disposition table or :func:`_derive_health`. It is optional so callers with
    no such concept (and the direct-synthesis tests) record nothing rather than
    a mode nobody probed.
    """
    if not documents:
        transient = any(f.reason in _TRANSIENT_FAILURE_REASONS for f in failures)
        result = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_UNPARSEABLE,
            details=_format_failures_detail(failures, session_id=session_id),
            retry_eligible=True if transient else None,
            stage_reached=STAGE3_REVIEW,
        )
        return result, None
    verdict = _with_capability(
        consolidate_verdict(
            documents,
            diff,
            reviewed_sha,
            worktree=worktree,
            failed_reviewers=failures,
            metrics_by_role=metrics_by_role,
        ),
        capability,
    )
    if verdict.blocking:
        blocked = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_MUST_FIX_FINDINGS,
            details=render_verdict_comment(verdict),
            stage_reached=STAGE3_REVIEW,
        )
        return blocked.model_copy(update={"review": verdict.review}), verdict
    if failures:
        partial = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_PARTIAL,
            details=_format_failures_detail(failures, session_id=session_id),
            stage_reached=STAGE3_REVIEW,
        )
        return partial.model_copy(update={"review": verdict.review}), verdict
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree, text=True
    ).strip()
    # Why: files/lines_actual were hardcoded 0/0 — a code-level placeholder that
    # reported "this review covered nothing" for every clean pass (#1487). Measure
    # them instead. compute_branch_diff_scope is called directly rather than
    # reconcile_result_scope because there is no self-report here to reconcile
    # against, so the mismatch-warning layer would fire on every clean review.
    # None (unverifiable git state) keeps today's 0/0 behavior.
    measured = compute_branch_diff_scope(worktree, default_branch)
    if measured is None:
        _log.warning(
            "scope_verification_unavailable: ticket=%s worktree=%s could not be "
            "measured against origin/%s; reporting files=0 lines_actual=0",
            task.ticket_id,
            worktree,
            default_branch,
        )
    result = AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=task.ticket_id,
        status="stage_complete",
        stage_reached=STAGE3_REVIEW,
        scope=Scope(
            tier=resolve_tier(task.scope_hint),
            files=measured["files"] if measured is not None else 0,
            # lines_estimate stays 0: a plan/scope_hint line-count mapping is a
            # follow-on, same as synthesize_git_result's own precedent.
            lines_estimate=0,
            lines_actual=measured["lines_actual"] if measured is not None else 0,
            forbidden_touched=False,
        ),
        plan_source="none",
        branch=branch,
        fork_point_sha=None,
        commits=[],
        review=verdict.review,
        health=_derive_health(documents),
        worktree_path=str(worktree),
    )
    return result, verdict


def _render_findings(
    verdict: ReviewVerdict, severity: Severity, heading: str
) -> list[str]:
    findings = [
        af.finding for af in verdict.accepted if af.finding.severity == severity
    ]
    if not findings:
        return []
    lines = [f"### {heading}", ""]
    for finding in findings:
        loc = finding.file
        if finding.line_start is not None:
            loc = f"{loc}:{finding.line_start}"
        annotation = (
            ""
            if finding.confidence == "HIGH"
            else _CONFIDENCE_ANNOTATION.format(confidence=finding.confidence)
        )
        lines.append(f"- **{loc}**{annotation} — {finding.summary}")
    lines.append("")
    return lines


def render_verdict_comment(verdict: ReviewVerdict) -> str:
    """Render a consolidated verdict into a GitHub-issue-comment markdown body."""
    lines = ["## Codex Review Verdict", ""]
    if verdict.blocking:
        lines.append(
            f"**BLOCKING** — {len(verdict.must_fix)} MUST_FIX finding(s) must be "
            "addressed before this branch can proceed."
        )
    else:
        lines.append("**Non-blocking** — no MUST_FIX findings.")
    lines.append("")
    lines.extend(_render_findings(verdict, "MUST_FIX", "MUST_FIX"))
    lines.extend(_render_findings(verdict, "SHOULD_FIX", "SHOULD_FIX"))
    return "\n".join(lines).rstrip() + "\n"
