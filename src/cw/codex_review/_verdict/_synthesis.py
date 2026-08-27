"""Consolidation of reviewer documents into a typed :class:`AutoDevResult`.

Owns the disposition table — which of the blocked/empty-diff/stage_complete
outcomes a pass lands on, and in which precedence order — plus the two
suppression seams (operator voids, cross-round adjudications) applied between
consolidation and that table, and the codex-subsystem blocked-result
constructor every park here goes through.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    EMPTY_DIFF_BLOCKER_REASON,
    AutoDevResult,
    Blocker,
    Health,
    Scope,
)
from cw.codex_review._const import (
    _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_MUST_FIX_MECHANICALLY_REJECTED,
    CODEX_REVIEW_PARTIAL,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS,
    STAGE3_REVIEW,
)
from cw.codex_review._verdict._health import (
    _derive_health,
    _format_failures_detail,
    _has_transient_failure,
)
from cw.codex_review._verdict._render import render_verdict_comment
from cw.local_runner import _SCHEMA_VERSION, make_blocked, resolve_tier
from cw.review_adjudication import apply_voided_suppression
from cw.review_finding_dispositions import suppress_adjudicated_findings
from cw.review_findings import consolidate_verdict
from cw.worktree import compute_branch_diff_scope

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import StageReached
    from cw.codex_review._capability import _CodexFilesystemCapability
    from cw.models import TicketTask
    from cw.review_adjudication import VoidedFinding
    from cw.review_finding_dispositions import FindingDisposition
    from cw.review_findings import (
        AgentSpecStatus,
        CapturedDiff,
        RejectedFinding,
        ReviewerFindingsDocument,
        ReviewerRunFailure,
        ReviewerRunMetrics,
        ReviewVerdict,
    )

_log = logging.getLogger(__name__)


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


def make_codex_blocked(
    *,
    ticket_id: str,
    worktree: Path,
    reason: str,
    details: str = "",
    retry_eligible: bool | None = None,
    retry_delay_seconds: int | None = None,
    stage_reached: StageReached = STAGE3_REVIEW,
) -> AutoDevResult:
    """Codex-subsystem-owned blocked-result constructor (#1842).

    Thin wrapper around ``local_runner.make_blocked()`` that permanently
    bakes in ``next_actions=_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS`` — every
    Codex-subsystem call site uses this instead of raw ``make_blocked()``
    so a new Codex-subsystem call site inherits the correct label by
    construction. Deliberately no ``next_actions`` parameter here.
    """
    return make_blocked(
        ticket_id=ticket_id,
        worktree=worktree,
        reason=reason,
        details=details,
        retry_eligible=retry_eligible,
        retry_delay_seconds=retry_delay_seconds,
        stage_reached=stage_reached,
        next_actions=_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
    )


def _verdict_block_reason(verdict: ReviewVerdict) -> str | None:
    """The blocked reason *verdict*'s own contents demand, or ``None``.

    The three dispositions that park on what the review FOUND — as opposed to
    how the roster ran — in their load-bearing precedence order. All three
    construct a byte-identical ``make_codex_blocked`` carrying the rendered
    verdict comment, so only the reason varies and only that is returned here;
    see :func:`synthesize_codex_review_result`'s docstring for why each one
    sits where it does.

    ``None`` means the verdict itself is clean and the caller falls through to
    the roster-shaped and empty-diff branches below it.
    """
    if verdict.blocking:
        return CODEX_MUST_FIX_FINDINGS
    if verdict.rejected_must_fix:
        return CODEX_MUST_FIX_MECHANICALLY_REJECTED
    if verdict.run_failures_with_should_fix_discards:
        return CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS
    return None


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
    finding_dispositions: dict[str, FindingDisposition] | None = None,
    pre_validation_rejected: list[RejectedFinding] | None = None,
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
    - a reviewer run failed structurally while claiming a MUST_FIX or SHOULD_FIX
      finding → blocked/CODEX_REVIEWER_FAILURE_DISCARDED_FINDINGS (#2029).
      Ordered immediately after the mechanically-rejected branch and BEFORE the
      partial one, on that branch's own logic: a role that reported findings we
      then threw away unread is more specific than "the roster was incomplete",
      and only the former says something was actually lost. It sits BELOW the
      two MUST_FIX branches because both of those carry the finding's own text
      for an operator to act on, where this one carries only a count.
    - documents present but at least one selected role skipped/errored without
      producing one (a partial review) → blocked/CODEX_REVIEW_PARTIAL — a
      review that silently proceeded on an incomplete roster would be exactly
      the "spuriously clean sentinel" risk the ``agents_run`` gate exists to
      catch (Decision 7, #1236). ``retry_eligible`` is derived from
      ``failures`` the same way as the zero-documents branch above (#1836
      review finding): a capacity blip hitting one role while others still
      produced documents must not silently fall back to non-retry-eligible
      just because the roster wasn't a total wipeout.
    - documents complete and non-blocking, but the branch *measures* an empty
      diff against ``origin/<default_branch>`` (0 files / 0 lines) →
      ``empty_diff_blocked`` + ``empty_diff_no_commits`` (#1870). Ordered last
      among the non-clean branches because it is the only one that is not about
      the review at all: the reviewers did their job, there was simply nothing
      under them. An *unmeasurable* worktree (``compute_branch_diff_scope``
      returns ``None``) is explicitly NOT this case and still falls through.
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

    ``finding_dispositions`` (#1838) is the cross-round adjudication ledger,
    and is applied the same way and in the same place as ``voided_findings``
    directly above — a matching REJECTED entry is stamped
    ``disposition="rejected"`` before the disposition table is consulted. It is
    a SECOND suppression mechanism, not a replacement: a void is
    evidence-anchored and lapses when the code moves, an adjudication is
    fingerprint-keyed and does not. Applied here for the identical reason —
    both call sites reach the blocking check through this function.

    ``pre_validation_rejected`` (#2029) is the findings ``run_codex_roles``
    rescued out of their documents at parse time, threaded straight into
    :func:`consolidate_verdict`. Like ``voided_findings`` and unlike the purely
    recorded arguments above, it is NOT merely recorded: a schema-invalid
    MUST_FIX among them populates ``verdict.rejected_must_fix`` and takes the
    mechanically-rejected branch below, through #1714's existing gate.

    ``fix_loop_enabled`` (#1705) is the caller's own already-known fix-loop
    state, threaded only as far as :func:`render_verdict_comment` on the
    blocking branch — it discriminates a fix-loop-disabled single pass from a
    fix-loop-enabled pass whose cycle-0 review was already clean, a history
    ``ReviewVerdict.review`` alone cannot distinguish (R1). Like
    ``metrics_by_role``, it is unused on the zero-documents branch.
    """
    if not documents:
        result = make_codex_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_UNPARSEABLE,
            details=_format_failures_detail(failures, session_id=session_id),
            retry_eligible=_has_transient_failure(failures) or None,
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
                pre_validation_rejected=pre_validation_rejected,
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
    # #1838: the second suppression seam, applied in the same window and for
    # the same reason. Ordered AFTER the void pass deliberately — it recomputes
    # must_fix from the stamped dispositions, so it composes with whatever the
    # void pass already suppressed rather than resurrecting it.
    verdict = suppress_adjudicated_findings(
        verdict, finding_dispositions or {}, ticket_id=task.ticket_id
    )
    block_reason = _verdict_block_reason(verdict)
    if block_reason is not None:
        blocked = make_codex_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=block_reason,
            details=render_verdict_comment(verdict, fix_loop_enabled=fix_loop_enabled),
        )
        return blocked.model_copy(update={"review": verdict.review}), verdict
    if failures:
        partial = make_codex_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_REVIEW_PARTIAL,
            details=_format_failures_detail(failures, session_id=session_id),
            retry_eligible=_has_transient_failure(failures) or None,
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
    # #1870: a *measured* 0/0 is not a clean pass -- there is no diff to have
    # reviewed and nothing for FINALIZE to open a PR over. Deliberately keyed on
    # the measurement already taken above rather than a second
    # `commits_ahead_of_default` subprocess: same practical classification, no
    # new git call on the common (non-empty) path. The `measured is not None`
    # guard is load-bearing -- an *unmeasurable* worktree also reports 0/0 into
    # the scope block below, and conflating the two would park every clean
    # review whose git state could not be read (the warning above is exactly
    # that case, and it keeps its pre-#1870 stage_complete fallback).
    if (
        measured is not None
        and measured["files"] == 0
        and measured["lines_actual"] == 0
    ):
        empty = AutoDevResult(
            schema_version=_SCHEMA_VERSION,
            ticket_id=task.ticket_id,
            status="empty_diff_blocked",
            stage_reached=STAGE3_REVIEW,
            scope=Scope(
                tier=resolve_tier(task.scope_hint),
                files=0,
                lines_estimate=0,
                lines_actual=0,
                forbidden_touched=False,
            ),
            plan_source="none",
            branch=branch,
            fork_point_sha=None,
            commits=[],
            # The real verdict rides along unchanged: agents_run and the finding
            # counts describe reviewers that genuinely ran, and zeroing them
            # would misreport the review as never having happened.
            review=verdict.review,
            # Not _derive_health(documents): those documents reviewed nothing, so
            # a PROCEED derived from them would vouch for an empty branch.
            health=Health(
                lowest_agent_confidence="MEDIUM",
                any_incomplete_risk=True,
                recommendation="EXIT_FOR_HUMAN_REVIEW",
            ),
            blocker=Blocker(
                stage=STAGE3_REVIEW,
                reason=EMPTY_DIFF_BLOCKER_REASON,
                details=(
                    f"Branch {branch} measures an empty diff against "
                    f"origin/{default_branch} (0 files, 0 lines) -- "
                    "nothing to review."
                ),
            ),
            worktree_path=str(worktree),
        )
        return empty, verdict
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
