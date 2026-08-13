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
    _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
    _TRANSIENT_FAILURE_REASONS,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_MUST_FIX_MECHANICALLY_REJECTED,
    CODEX_REVIEW_PARTIAL,
    CODEX_REVIEW_UNPARSEABLE,
    STAGE3_REVIEW,
)
from cw.executor_diagnostics import append_diagnostics_pointer
from cw.local_runner import _SCHEMA_VERSION, make_blocked, resolve_tier
from cw.review_adjudication import apply_voided_suppression
from cw.review_findings import consolidate_verdict
from cw.worktree import compute_branch_diff_scope

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import Review
    from cw.codex_review._capability import _CodexFilesystemCapability
    from cw.models import TicketTask
    from cw.review_adjudication import VoidedFinding
    from cw.review_findings import (
        AcceptedFinding,
        AgentSpecStatus,
        CapturedDiff,
        ReviewerFindingsDocument,
        ReviewerRunFailure,
        ReviewerRunMetrics,
        ReviewerRunRecord,
        ReviewVerdict,
        Severity,
    )

_log = logging.getLogger(__name__)


# Confidence values other than HIGH render an inline annotation on their
# finding line so a reader can weight it — confidence is display-only and
# must never gate/filter/reorder findings (R0, #1555). HIGH is the common
# case and stays unmarked to keep the common path uncluttered.
_CONFIDENCE_ANNOTATION = " _({confidence} confidence)_"

# A non-"fixed" disposition means the finding is no longer blocking — voided
# by an operator ("rejected", #1814), deferred, or never decided ("dropped",
# #1805). Before this annotation existed, `_render_findings` filtered on
# severity alone and discarded `disposition` before the loop body ran, so a
# suppressed MUST_FIX rendered byte-identically to a live one and the posted
# comment lied about its own contents. Display-only, exactly like
# _CONFIDENCE_ANNOTATION above: nothing is filtered, reordered, or split into
# a second heading.
_DISPOSITION_ANNOTATION = " _(suppressed — {disposition}{detail})_"


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

    See :func:`_render_degraded_roles_note` (#1775) for where a degraded
    role's stated reason (``ReviewerRunRecord.detail``) surfaces on the
    rendered comment — this function only derives the health signal, it does
    not render anything.
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


def _with_agent_spec_status(
    verdict: ReviewVerdict, agent_spec_status: list[AgentSpecStatus] | None
) -> ReviewVerdict:
    """Return *verdict* with the per-role agent-spec resolution stamped on it.

    Same shape and same reason as :func:`_with_capability` (#1773):
    ``consolidate_verdict`` is executor-neutral and knows nothing about where
    ``.claude/agents/`` lives, so the record is stamped on afterwards. ``None``
    in, unchanged verdict out — a caller that resolved no specs must not be
    reported as having found none.
    """
    if agent_spec_status is None:
        return verdict
    return verdict.model_copy(update={"agent_spec_status": agent_spec_status})


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
    fix_loop_enabled: bool,
    metrics_by_role: dict[str, ReviewerRunMetrics] | None = None,
    capability: _CodexFilesystemCapability | None = None,
    agent_spec_status: list[AgentSpecStatus] | None = None,
    voided_findings: list[VoidedFinding] | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Map consolidated review documents to a typed AutoDevResult.

    Disposition:
    - zero documents (all roles failed/skipped) → blocked/CODEX_REVIEW_UNPARSEABLE,
      with ``failures`` folded into ``details`` and ``retry_eligible=True`` when
      at least one failure is transient (``codex_timeout``/``budget_exhausted``/
      ``codex_model_capacity``) (MUST_FIX 2, #1236; capacity added by #1836).
    - consolidated verdict is blocking            → blocked/CODEX_MUST_FIX_FINDINGS
    - verdict carries a mechanically-rejected MUST_FIX (validation dropped it
      before adjudication) → blocked/CODEX_MUST_FIX_MECHANICALLY_REJECTED
      (#1714). Ordered AFTER the blocking check (a surviving MUST_FIX is the
      stronger, actionable signal and keeps its own reason) and BEFORE the
      partial check (a MUST_FIX thrown away unread is more specific than "the
      roster was incomplete"). ``verdict.blocking`` stays False on this path by
      design — see ``_const.CODEX_MUST_FIX_MECHANICALLY_REJECTED`` for why the
      fix loop must not be entered.
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

    ``agent_spec_status`` (#1773) is the per-role agent-spec resolution record
    the reviewer prompts were built from. Same shape and same non-influence as
    ``capability``: recorded onto the ``ReviewVerdict`` (and from there onto
    the rendered comment), never read by the disposition table or
    :func:`_derive_health` — a reviewer that ran unspecified still produced a
    document, and its degradation is reported, not adjudicated here.

    ``voided_findings`` (#1814) is the operator-settled REJECT record fetched
    off the ticket thread. Unlike every optional argument above it is NOT
    merely recorded: a matching re-derived finding is stamped
    ``disposition="rejected"`` and drops out of ``must_fix``/``blocking``
    before the disposition table below is consulted. It has to be applied here
    rather than at either call site because both of them — ``core.run_review``
    and the fix loop's ``_rereview`` — reach the blocking check through this
    function, and a suppression applied at only one would let the same voided
    finding re-park the run from the other.

    ``fix_loop_enabled`` (#1705) is the caller's own already-known fix-loop
    state, threaded only as far as :func:`render_verdict_comment` on the
    blocking branch — it discriminates a fix-loop-disabled single pass from a
    fix-loop-enabled pass whose cycle-0 review was already clean, a history
    ``ReviewVerdict.review`` alone cannot distinguish (R1). Like
    ``metrics_by_role``, it is unused on the zero-documents branch.
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
            next_actions=_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
        )
        return result, None
    verdict = _with_agent_spec_status(
        _with_capability(
            consolidate_verdict(
                documents,
                diff,
                reviewed_sha,
                worktree=worktree,
                failed_reviewers=failures,
                metrics_by_role=metrics_by_role,
            ),
            capability,
        ),
        agent_spec_status,
    )
    # #1814: applied between consolidation and the disposition table, so every
    # branch below (blocking, mechanically-rejected, partial, clean) sees the
    # suppressed state. The returned adjudications are the Claude path's to
    # record; this path has no ADJUDICATIONS array and needs none — the same
    # outcome is already stamped on `verdict.accepted`.
    verdict, _voided_adjudications = apply_voided_suppression(
        verdict, voided_findings or [], ticket_id=task.ticket_id
    )
    if verdict.blocking:
        blocked = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_MUST_FIX_FINDINGS,
            details=render_verdict_comment(verdict, fix_loop_enabled=fix_loop_enabled),
            stage_reached=STAGE3_REVIEW,
            next_actions=_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
        )
        return blocked.model_copy(update={"review": verdict.review}), verdict
    if verdict.rejected_must_fix:
        dropped = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_MUST_FIX_MECHANICALLY_REJECTED,
            details=render_verdict_comment(verdict, fix_loop_enabled=fix_loop_enabled),
            stage_reached=STAGE3_REVIEW,
            next_actions=_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
        )
        return dropped.model_copy(update={"review": verdict.review}), verdict
    if failures:
        partial = make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_PARTIAL,
            details=_format_failures_detail(failures, session_id=session_id),
            stage_reached=STAGE3_REVIEW,
            next_actions=_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
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


def _disposition_annotation(accepted: AcceptedFinding) -> str:
    """Annotate a finding whose disposition says it is no longer blocking.

    ``""`` for the ``"fixed"`` default (the common case stays uncluttered,
    same convention as HIGH confidence). ``disposition_detail`` is appended
    when the producer recorded one — it carries the *why* (which operator
    comment voided it, which adjudication deferred it), which is the whole
    point of surfacing this on the posted comment rather than only in the
    persisted verdict artifact.
    """
    if accepted.disposition == "fixed":
        return ""
    detail = f": {accepted.disposition_detail}" if accepted.disposition_detail else ""
    return _DISPOSITION_ANNOTATION.format(
        disposition=accepted.disposition, detail=detail
    )


def _render_findings(
    verdict: ReviewVerdict, severity: Severity, heading: str
) -> list[str]:
    # Iterates the AcceptedFinding, not just `.finding`, so `disposition` is
    # still in scope in the loop body (#1814/A1).
    accepted = [af for af in verdict.accepted if af.finding.severity == severity]
    if not accepted:
        return []
    lines = [f"### {heading}", ""]
    for af in accepted:
        finding = af.finding
        loc = finding.file
        if finding.line_start is not None:
            loc = f"{loc}:{finding.line_start}"
        annotation = (
            ""
            if finding.confidence == "HIGH"
            else _CONFIDENCE_ANNOTATION.format(confidence=finding.confidence)
        )
        suppression = _disposition_annotation(af)
        lines.append(f"- **{loc}**{annotation}{suppression} — {finding.summary}")
    lines.append("")
    return lines


def _render_clean_headline(review: Review, *, fix_loop_enabled: bool) -> str:
    """Render the non-blocking headline, distinguishing three histories (#1705).

    ``Review.fix_cycles_used``/``must_fix_initial``/``deferred`` alone cannot
    tell a fix-loop-disabled single pass apart from a fix-loop-enabled pass
    whose cycle-0 review was already clean — both produce
    ``fix_cycles_used == 0``. ``fix_loop_enabled`` (caller-known, threaded in
    via ``synthesize_codex_review_result``) is the discriminator (R1).

    Within the ``fix_cycles_used > 0`` (genuine fix-loop engagement) branch,
    ``Review.had_real_commit`` (#1723) further discriminates a converged loop
    that actually committed a change from one that converged purely because
    every fix cycle was a tolerated no-op — the latter renders an UNVERIFIED
    headline rather than claiming findings were resolved.
    """
    if review.fix_cycles_used > 0:
        resolved = review.must_fix_initial - review.deferred
        if review.had_real_commit is False:
            return (
                f"**UNVERIFIED** — the fix loop converged without changing "
                f"any file: {resolved} of {review.must_fix_initial} "
                f"originally-found MUST_FIX finding(s) show as resolved "
                f"across {review.fix_cycles_used} fix cycle(s), but no fix "
                "cycle actually committed a change. Treat this as unverified "
                "rather than genuinely fixed."
            )
        return (
            f"**Non-blocking** — {resolved} of {review.must_fix_initial} "
            f"originally-found MUST_FIX finding(s) resolved across "
            f"{review.fix_cycles_used} fix cycle(s); none remain open."
        )
    if fix_loop_enabled:
        return (
            "**Non-blocking** — no MUST_FIX findings. The fix loop was "
            "available for this run; none were needed."
        )
    return (
        "**Non-blocking** — no MUST_FIX findings. Single-pass review "
        "(fix loop disabled for this lane)."
    )


def _render_history_note(review: Review, *, fix_loop_enabled: bool) -> list[str]:
    """Render the blocking-branch history note (R1's blocking-branch half).

    Mirrors ``_render_clean_headline``'s discrimination for the still-blocking
    case: a fix-loop-disabled block must state its own single-pass state
    rather than silently looking like a fix loop that made no progress.
    """
    if not fix_loop_enabled:
        return ["_Single-pass review — fix loop disabled for this lane._", ""]
    if review.fix_cycles_used > 0:
        resolved = review.must_fix_initial - review.deferred
        return [
            f"_{resolved} of {review.must_fix_initial} originally-found "
            f"MUST_FIX finding(s) resolved across {review.fix_cycles_used} "
            f"fix cycle(s); {review.deferred} still open._",
            "",
        ]
    return []


def _render_failed_roles_note(verdict: ReviewVerdict) -> list[str]:
    """Render a "PARTIAL COVERAGE" note naming any role that failed to run.

    Reads ``verdict.agents_run`` (#1710's ``ReviewerRunRecord`` list) directly
    — no new plumbing needed. Surfaces reviewer-run failure onto the posted
    GitHub comment; previously only reached ``Blocker.details`` internally via
    ``_format_failures_detail`` on the zero-documents path.
    """
    failed_roles = [r.reviewer_role for r in verdict.agents_run if r.status == "failed"]
    if not failed_roles:
        return []
    roles = ", ".join(failed_roles)
    plural = "" if len(failed_roles) == 1 else "s"
    return [
        f"**PARTIAL COVERAGE** — {len(failed_roles)} role{plural} failed to run: "
        f"{roles}.",
        "",
    ]


def _degraded_role_label(record: ReviewerRunRecord) -> str:
    """Name one degraded role, with its stated reason if it gave one (#1775).

    ``record.detail`` is copied verbatim from the source
    ``ReviewerFindingsDocument`` by :func:`consolidate_verdict`, so a blank
    value here means the reviewer genuinely gave no reason -- not that the
    plumbing dropped it.
    """
    if record.detail:
        return f"{record.reviewer_role}: degraded — {record.detail}"
    return f"{record.reviewer_role}: degraded (no reason given)"


def _render_degraded_roles_note(verdict: ReviewVerdict) -> list[str]:
    """Render a "DEGRADED COVERAGE" note naming any role that ran degraded.

    Sibling of :func:`_render_failed_roles_note`: reads ``verdict.agents_run``
    directly, same empty-list-returns-``[]`` shape. A "failed" role (never
    produced a document) and a "degraded" role (produced a document but
    could not complete a required check) are distinct facts, so this note is
    additive to -- not a replacement for -- the partial-coverage note (#1775).
    """
    degraded = [r for r in verdict.agents_run if r.status == "degraded"]
    if not degraded:
        return []
    labels = ", ".join(_degraded_role_label(r) for r in degraded)
    plural = "" if len(degraded) == 1 else "s"
    return [
        f"**DEGRADED COVERAGE** — {len(degraded)} role{plural} ran degraded: {labels}.",
        "",
    ]


def _render_capability_note(verdict: ReviewVerdict) -> list[str]:
    """Render the probed filesystem-capability mode the review ran under.

    Deferred from #1709 pending #1705's rewrite of this function (#1725).
    ``capability_mode`` is ``None`` for any run that never probed (e.g. the
    LocalExecutor path, or a test verdict built without capability wiring) --
    that must render nothing, not "unknown", per #1709/#1725: an unprobed run
    and a probed-but-unclassifiable run are different facts, and only the
    probe (``_classify_capability_failure``) is allowed to say "unknown".
    """
    if verdict.capability_mode is None:
        return []
    if verdict.capability_mode == "capable":
        return ["_Reviewed with repo filesystem access (capable)._", ""]
    reason_suffix = (
        f" (reason: {verdict.capability_reason})" if verdict.capability_reason else ""
    )
    return [
        "_Reviewed in degraded mode — inlined-diff-only, no repo filesystem "
        f"access{reason_suffix}._",
        "",
    ]


def _agent_spec_label(status: AgentSpecStatus) -> str:
    """Name why *status*'s role ran without a loaded specification (#1773).

    ``empty_repo_file`` is checked first and independently of ``source``: once
    the repo-tracked file was found blank AND nothing usable replaced it, that
    is the actionable fact for whoever reads the comment, whichever source was
    consulted last.
    """
    if status.empty_repo_file:
        return "present but empty, no usable fallback"
    if status.source == "global":
        return "global spec found but empty"
    return "absent"


def _render_agent_spec_note(verdict: ReviewVerdict) -> list[str]:
    """Render the per-role agent-spec resolution summary (#1773).

    An empty ``agent_spec_status`` renders nothing: a verdict from a path that
    never resolved specs (the LocalExecutor path, a directly-synthesized test
    verdict) has no claim to make either way — same convention as
    ``_render_capability_note``'s unprobed case.

    A role counts as unspecified iff its final ``empty`` is True, whatever
    ``source`` says, which yields exactly one of three headlines. The
    recovered-empty-repo-file addendum is then appended independently of which
    headline won, so a truncated repo-tracked file still gets reported in a
    pass where some *other* role was also unspecified.
    """
    statuses = verdict.agent_spec_status
    if not statuses:
        return []
    unspecified = [s for s in statuses if s.empty]
    total = len(statuses)
    if not unspecified:
        line = f"_Agent specs loaded for all {total} reviewer role(s)._"
    elif len(unspecified) == total:
        line = (
            "**ALL AGENT SPECS UNSPECIFIED** — no reviewer role in this pass "
            "had a loaded agent specification (repo or global); every "
            "prompt's `## Agent Specification` section was empty."
        )
    else:
        named = ", ".join(f"{s.role} ({_agent_spec_label(s)})" for s in unspecified)
        line = (
            f"**AGENT SPEC(S) UNSPECIFIED** — {len(unspecified)} of {total} "
            f"role(s) ran without a loaded specification: {named}."
        )
    # A still-unspecified role already carries "(present but empty, no usable
    # fallback)" above, so only genuinely recovered ones get the addendum.
    for s in statuses:
        if s.empty_repo_file and not s.empty:
            line += (
                f" **NOTE:** {s.role}'s repo-tracked spec was present but "
                "empty — recovered via the global fallback; the repo-tracked "
                "file may be truncated or need attention."
            )
    return [line, ""]


def _render_rejected_must_fix(verdict: ReviewVerdict) -> list[str]:
    """Render the MUST_FIX findings validation dropped before adjudication.

    ``_render_findings`` iterates ``verdict.accepted`` only, so before #1714 a
    mechanically-rejected MUST_FIX was invisible on the posted comment even
    when it was the reason the pipeline blocked — the reader saw a park with no
    findings behind it. Rendered unconditionally (mirroring
    ``_render_failed_roles_note``'s empty-list-returns-``[]`` shape) so the
    mixed case, where an accepted MUST_FIX also blocks, still surfaces both.

    ``RejectedFinding.raw`` is the pre-validation ``Finding.model_dump()``, so
    it carries ``Finding``'s field names — but read via ``.get()`` because a
    rejected payload is by definition one that failed validation.

    ``rf.detail`` (#1792), when non-blank (populated for the
    ``evidence_not_in_diff`` reason specifically — see
    ``_evidence_window_discrepancy_detail``), renders as an indented
    follow-up line so the diagnosable discrepancy (declared vs. evidence
    line counts) reaches the operator reading the posted comment, not just
    the persisted verdict artifact.
    """
    if not verdict.rejected_must_fix:
        return []
    lines = ["### MUST_FIX — mechanically rejected (not adjudicated)", ""]
    for rf in verdict.rejected_must_fix:
        loc = str(rf.raw.get("file", "<unknown file>"))
        line_start = rf.raw.get("line_start")
        if line_start is not None:
            loc = f"{loc}:{line_start}"
        summary = str(rf.raw.get("summary", "<no summary>"))
        lines.append(f"- **{loc}** — {summary} (rejected: {rf.reason})")
        if rf.detail:
            lines.append(f"  - {rf.detail}")
    lines.append("")
    return lines


def render_verdict_comment(verdict: ReviewVerdict, *, fix_loop_enabled: bool) -> str:
    """Render a consolidated verdict into a GitHub-issue-comment markdown body.

    ``fix_loop_enabled`` is the caller's own already-known fix-loop state for
    this run — required (not optional) so no call site can silently fall back
    to a wrong default (#1705). It discriminates fix-loop-disabled from
    fix-loop-enabled-but-unneeded histories that would otherwise render
    identically from ``verdict.review`` alone.

    The headline is three-way as of #1714: blocking, mechanically-rejected-
    MUST_FIX, or clean. The rejected-MUST_FIX *section* is rendered
    unconditionally regardless of which headline won, so the mixed case
    (something blocking AND something dropped) reports both.
    """
    lines = ["## Codex Review Verdict", ""]
    if verdict.blocking:
        lines.append(
            f"**BLOCKING** — {len(verdict.must_fix)} MUST_FIX finding(s) must be "
            "addressed before this branch can proceed."
        )
        lines.extend(
            _render_history_note(verdict.review, fix_loop_enabled=fix_loop_enabled)
        )
    elif verdict.rejected_must_fix:
        # #1714: never render the clean headline here. Nothing survived to
        # block on, but a MUST_FIX was dropped unread -- "Non-blocking, no
        # MUST_FIX findings" would be the exact false all-clear this branch
        # exists to prevent.
        lines.append(
            f"**MUST_FIX REJECTED — OPERATOR REVIEW REQUIRED** — "
            f"{len(verdict.rejected_must_fix)} MUST_FIX finding(s) were "
            "mechanically rejected before adjudication (dropped, not evaluated "
            "on their merits) and require operator review before this branch "
            "can proceed."
        )
    else:
        lines.append(
            _render_clean_headline(verdict.review, fix_loop_enabled=fix_loop_enabled)
        )
    lines.append("")
    lines.extend(_render_failed_roles_note(verdict))
    lines.extend(_render_degraded_roles_note(verdict))
    lines.extend(_render_capability_note(verdict))
    lines.extend(_render_agent_spec_note(verdict))
    lines.extend(_render_rejected_must_fix(verdict))
    lines.extend(_render_findings(verdict, "MUST_FIX", "MUST_FIX"))
    lines.extend(_render_findings(verdict, "SHOULD_FIX", "SHOULD_FIX"))
    return "\n".join(lines).rstrip() + "\n"
