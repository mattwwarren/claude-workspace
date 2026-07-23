"""Codex fix-loop adapter for CodexExecutor's REVIEW stage (#1392).

Wraps :func:`cw.codex_review.run_review` in a bounded fix loop: after an
initial (cycle 0) review pass that surfaces blocking MUST_FIX findings, cw runs
up to :data:`_MAX_FIX_CYCLES` cycles of ``codex exec --sandbox workspace-write``
fix invocations, committing each cycle's real changes and re-running the full
per-role review pass to see which findings cleared. The loop exits clean the
moment no MUST_FIX finding remains open, or parks the ticket when the cap (or the
shared wall-clock budget) is exhausted.

This is the multi-pass counterpart to ``run_review``'s single pass (#1236) built
on the executor-neutral finding contract (#1237). ``CodexExecutor.spawn()``'s
Step 3 delegates to :func:`run_review_with_fix_loop` instead of ``run_review``.

Cross-cycle finding identity is tracked by ``review_findings._dedup_key`` so a
finding that survives every cycle (or flaps out and back) is counted exactly
once. The terminal published ``Review`` is reconstructed here rather than read
from any single ``derive_review_counts`` call: ``must_fix_initial`` is cycle 0's
pre-defer snapshot, ``deferred`` is the cross-cycle survivor count, and
``fix_cycles_used`` is the loop's own cycle counter — three values no single
formula pass over one loop-exit-state finding list can produce together.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import TYPE_CHECKING

from cw.codex_review import (
    _CATEGORY_TO_REASON,
    _MIN_ROLE_TIMEOUT_SECONDS,
    _TRANSIENT_FAILURE_REASONS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_MUST_FIX_FINDINGS,
    STAGE3_REVIEW,
    _classify_codex_failure,
    _load_ticket_context,
    _prepare_review_pass,
    render_verdict_comment,
    run_codex_roles,
    run_review,
    synthesize_codex_review_result,
)
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)
from cw.local_runner import make_blocked
from cw.review_findings import _dedup_key

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult, Health, Review
    from cw.codex_runner import CodexRunner
    from cw.executor_diagnostics import ExecutorFailureCategory
    from cw.models import TicketTask
    from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

_log = logging.getLogger(__name__)

# Maximum fix cycles attempted before parking a still-blocking review.
_MAX_FIX_CYCLES = 5
# Cycle at/after which Health.fix_loop_escalated is set — a loop that needed
# this many passes is operator-attention-worthy even when it eventually clears.
_ESCALATE_AT_CYCLE = 3
# Coarse per-fix-cycle wall-clock floor: a cycle needs at least one fix
# invocation plus one re-review role turn, so it must be able to afford two
# per-role floors. Never start a fix cycle with less remaining budget.
_FIX_CYCLE_FLOOR_SECONDS = 2 * _MIN_ROLE_TIMEOUT_SECONDS

# Matches review_findings._dedup_key's return shape (severity, file,
# line_start, line_end, evidence); None line endpoints map to -1 there.
_DedupKey = tuple[str, str, int, int, str]

_MUST_FIX = "MUST_FIX"


def _build_fix_codex_argv(*, model: str | None) -> list[str]:
    """Return the ``codex exec`` argv for a fix invocation (write-capable).

    Structurally distinct from ``codex_review._build_generic_codex_argv``: it
    hardcodes ``--sandbox workspace-write`` (the fix edits files, so read-only
    would be wrong) and omits ``--output-schema``/``-o`` entirely — a fix
    invocation mutates the worktree, it does not emit a structured document.
    """
    argv = ["codex", "exec", "--sandbox", "workspace-write"]
    if model:
        argv += ["-m", model]
    return argv


def _build_fix_prompt(
    open_findings: list[Finding],
    *,
    plan_text: str | None,
    ticket_text: str | None,
    cycle: int,
) -> str:
    """Render the fix-invocation prompt for one cycle's open MUST_FIX findings.

    Only MUST_FIX findings ever reach the fix loop's ``open_findings`` tracker,
    so no severity filtering is needed here — every rendered finding is a
    MUST_FIX one. Plan/ticket context is inlined when present (codex has no
    filesystem access to ``.cw/*``), and the prompt ends with an explicit
    minimal-fix instruction.
    """
    parts = [
        f"# Codex Fix Cycle {cycle}",
        (
            "Resolve every MUST_FIX review finding listed below by making the "
            "minimal change on the current worktree, then stop. Do not refactor "
            "unrelated code and do not create a commit — cw commits your changes."
        ),
    ]
    if ticket_text:
        parts.append(f"## Ticket Context\n{ticket_text}")
    if plan_text:
        parts.append(f"## Approved Plan\n{plan_text}")
    parts.append("## MUST_FIX Findings")
    for index, finding in enumerate(open_findings, start=1):
        loc = finding.file
        if finding.line_start is not None:
            loc = f"{loc}:{finding.line_start}"
        parts.append(
            f"### {index}. {loc}\n{finding.summary}\n\n"
            f"Suggested fix: {finding.suggested_fix}"
        )
    return "\n\n".join(parts)


def _fix_commit_summary(findings: list[Finding]) -> str:
    """Return a non-empty commit-message tail summarizing the cycle's findings."""
    count = len(findings)
    plural = "" if count == 1 else "s"
    return f"{count} MUST_FIX finding{plural}"


def _commit_fix_cycle(
    worktree: Path, cycle: int, findings: list[Finding]
) -> str | None:
    """Commit the worktree changes a fix cycle produced; return the new sha.

    A no-op fix (``git status --porcelain`` empty) is tolerated: no commit is
    created, a WARNING is logged, and ``None`` is returned — the cycle still
    counts toward the cap. Any git failure raises ``CalledProcessError``, which
    the caller treats identically to a fix-invocation failure.
    """
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=worktree, text=True
    )
    if not status.strip():
        _log.warning(
            "codex fix cycle %d produced no changes; skipping commit "
            "(cycle still counts toward the cap)",
            cycle,
        )
        return None
    subprocess.check_output(["git", "add", "-A"], cwd=worktree, text=True)
    message = f"fix(review): codex fix cycle {cycle} — {_fix_commit_summary(findings)}"
    subprocess.check_output(["git", "commit", "-m", message], cwd=worktree, text=True)
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
    ).strip()


def _track_open_findings(
    open_findings: dict[_DedupKey, AcceptedFinding],
    accepted: list[AcceptedFinding],
) -> dict[_DedupKey, AcceptedFinding]:
    """Update the cross-cycle open-MUST_FIX tracker from a re-review's accepted set.

    Keys whose finding is present in *accepted*'s MUST_FIX subset stay / get
    refreshed / get added; keys absent from that subset are dropped (the finding
    is implicitly fixed this cycle). A finding that flaps out and back reappears
    under the same dedup key and is counted once. SHOULD_FIX/NIT/PRINCIPLE
    findings never enter the tracker.
    """
    survivors = dict(open_findings)
    current = {
        _dedup_key(af.finding): af
        for af in accepted
        if af.finding.severity == _MUST_FIX
    }
    for key in list(survivors):
        if key not in current:
            del survivors[key]
    survivors.update(current)
    return survivors


def _finalize_review(
    *,
    cycle0_review: Review,
    final_verdict: ReviewVerdict,
    open_findings: dict[_DedupKey, AcceptedFinding],
    cycle_count: int,
) -> Review:
    """Reconstruct the terminal published ``Review`` from authoritative sources.

    ``should_fix`` and ``agents_run`` are taken from the final cycle's own
    correctly-derived ``Review``. ``must_fix_initial`` comes from cycle 0's
    snapshot (captured before any defer stamping, so trivially correct).
    ``deferred`` is the cross-cycle survivor count. ``fix_cycles_used`` is the
    loop's own cycle counter — set explicitly here rather than inherited from
    ``final_verdict.review`` because ``synthesize_codex_review_result`` does not
    thread the cycle index through its internal ``consolidate_verdict`` call.
    """
    return final_verdict.review.model_copy(
        update={
            "must_fix_initial": cycle0_review.must_fix_initial,
            "deferred": len(open_findings),
            "fix_cycles_used": cycle_count,
        }
    )


def _survivors_only_verdict(
    final_verdict: ReviewVerdict,
    open_findings: dict[_DedupKey, AcceptedFinding],
    review: Review,
) -> ReviewVerdict:
    """Rebuild a capped-exit verdict whose blocking state is survivor-derived.

    ``blocking`` is computed DIRECTLY from ``open_findings`` — NOT re-derived
    from ``bool(must_fix)`` over the disposition-stamped ``accepted`` list,
    which would spuriously read ``False`` once survivors are stamped
    ``disposition="deferred"`` for reporting. Each surviving MUST_FIX finding is
    stamped ``deferred`` in ``accepted``; every other accepted finding is
    unchanged. ``must_fix`` is exactly the survivor set.
    """
    survivor_keys = set(open_findings)
    accepted = [
        af.model_copy(update={"disposition": "deferred"})
        if af.finding.severity == _MUST_FIX and _dedup_key(af.finding) in survivor_keys
        else af
        for af in final_verdict.accepted
    ]
    must_fix = [af.finding for af in open_findings.values()]
    return final_verdict.model_copy(
        update={
            "blocking": bool(open_findings),
            "must_fix": must_fix,
            "accepted": accepted,
            "review": review,
        }
    )


def _apply_escalation(health: Health, cycle: int) -> Health:
    """Return *health* with ``fix_loop_escalated`` set when at/past the threshold."""
    if cycle >= _ESCALATE_AT_CYCLE:
        return health.model_copy(update={"fix_loop_escalated": True})
    return health


def _remaining_budget(deadline: float | None) -> float | None:
    """Return seconds left before *deadline*, or ``None`` for an unlimited run."""
    return None if deadline is None else deadline - time.monotonic()


def _budget_seconds(remaining: float | None) -> int | None:
    """Coerce a float remaining-budget into the int ``run_codex_roles`` expects."""
    return None if remaining is None else max(int(remaining), 0)


def _fix_timeout(remaining: float | None) -> int | None:
    """Per-fix-invocation timeout, floored at the per-role minimum."""
    return None if remaining is None else max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS)


def _park_fix_failure(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    cycle: int,
    category: ExecutorFailureCategory,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    verdict: ReviewVerdict | None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Park the ticket on a failed fix invocation, persisting a diagnostics bundle.

    Reuses ``codex_review``'s category→reason map and transient-reason set so a
    timeout parks retry-eligible while a hard error parks for the operator, and
    writes the typed ``ExecutorFailure`` bundle under ``reviewer_role`` =
    ``fix-cycle-N`` (mirroring ``_persist_codex_role_diagnostics``).
    """
    reason = _CATEGORY_TO_REASON[category]
    failure = build_executor_failure(
        category=category,
        executor_name="codex",
        session_id=session_id,
        argv=_build_fix_codex_argv(model=None),
        stdout_excerpt=stdout,
        stderr_excerpt=stderr,
        reviewer_role=f"fix-cycle-{cycle}",
        exit_code=exit_code,
    )
    persist_diagnostics_bundle(
        session_id=session_id, role_slug=f"fix-cycle-{cycle}", failure=failure
    )
    detail = append_diagnostics_pointer(
        f"codex fix cycle {cycle} failed ({reason})", session_id=session_id
    )
    transient = reason in _TRANSIENT_FAILURE_REASONS
    blocked = make_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=reason,
        details=detail,
        retry_eligible=True if transient else None,
        stage_reached=STAGE3_REVIEW,
    )
    return blocked, verdict


def _park_survivors(
    *,
    task: TicketTask,
    worktree: Path,
    reason: str,
    verdict: ReviewVerdict,
    open_findings: dict[_DedupKey, AcceptedFinding],
    cycle0_review: Review,
    cycle_count: int,
    retry_eligible: bool | None,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Park a still-blocking review (cap or budget exhausted) with survivor detail.

    Builds the terminal ``Review`` and the survivor-only verdict, renders the
    verdict comment into ``Blocker.details``, and sets ``fix_loop_escalated`` on
    the health block when the cycle count reached the escalation threshold.
    """
    review = _finalize_review(
        cycle0_review=cycle0_review,
        final_verdict=verdict,
        open_findings=open_findings,
        cycle_count=cycle_count,
    )
    survivors = _survivors_only_verdict(verdict, open_findings, review)
    blocked = make_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=reason,
        details=render_verdict_comment(survivors),
        retry_eligible=retry_eligible,
        stage_reached=STAGE3_REVIEW,
    )
    health = blocked.health.model_copy(
        update={"fix_loop_escalated": cycle_count >= _ESCALATE_AT_CYCLE}
    )
    patched = blocked.model_copy(update={"review": review, "health": health})
    return patched, survivors


def _clean_exit(
    result: AutoDevResult,
    verdict: ReviewVerdict,
    cycle0_review: Review,
    open_findings: dict[_DedupKey, AcceptedFinding],
    cycle: int,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Return the clean-exit result with the terminal review + escalation patched."""
    review = _finalize_review(
        cycle0_review=cycle0_review,
        final_verdict=verdict,
        open_findings=open_findings,
        cycle_count=cycle,
    )
    health = _apply_escalation(result.health, cycle)
    return result.model_copy(update={"review": review, "health": health}), verdict


def _run_fix_and_commit(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    open_findings: dict[_DedupKey, AcceptedFinding],
    model: str | None,
    timeout_seconds: int | None,
    session_id: str,
    cycle: int,
    plan_text: str | None,
    ticket_text: str | None,
    verdict: ReviewVerdict,
) -> tuple[AutoDevResult, ReviewVerdict | None] | None:
    """Run one cycle's fix invocation and commit; return a park tuple or ``None``.

    ``None`` means the fix invocation succeeded (its commit may have been a
    no-op) and the caller should proceed to re-review. A non-``None`` return is
    the terminal park result for a failed fix invocation or a failed commit.
    """
    findings = [af.finding for af in open_findings.values()]
    prompt = _build_fix_prompt(
        findings, plan_text=plan_text, ticket_text=ticket_text, cycle=cycle
    )
    argv = _build_fix_codex_argv(model=model)
    result = runner.run(worktree, argv, timeout_seconds, stdin=prompt)
    if result.timed_out or result.returncode != 0:
        return _park_fix_failure(
            task=task,
            worktree=worktree,
            session_id=session_id,
            cycle=cycle,
            category=_classify_codex_failure(result),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            verdict=verdict,
        )
    try:
        _commit_fix_cycle(worktree, cycle, findings)
    except subprocess.CalledProcessError as exc:
        return _park_fix_failure(
            task=task,
            worktree=worktree,
            session_id=session_id,
            cycle=cycle,
            category="runtime_error",
            stdout=exc.stdout or "",
            stderr=exc.stderr or str(exc),
            exit_code=exc.returncode,
            verdict=verdict,
        )
    return None


def _rereview(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    remaining: float | None,
    session_id: str,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Run a fresh full per-role review pass for one fix cycle."""
    prepared = _prepare_review_pass(task, worktree, default_branch)
    documents, failures = run_codex_roles(
        runner=runner,
        worktree=worktree,
        roles=prepared.roles,
        prompts_by_role=prepared.prompts_by_role,
        model=model,
        wall_clock_budget_seconds=_budget_seconds(remaining),
        session_id=session_id,
    )
    return synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=documents,
        failures=failures,
        diff=prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=session_id,
    )


def run_review_with_fix_loop(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Run the initial review pass plus a bounded MUST_FIX fix loop.

    Drop-in replacement for :func:`cw.codex_review.run_review` (identical
    signature and return shape). One shared wall-clock deadline spans the
    initial pass, every fix invocation, and every re-review. A non-blocking or
    unparseable cycle-0 verdict passes straight through with zero fix
    invocations attempted.
    """
    deadline = (
        None
        if wall_clock_budget_seconds is None
        else time.monotonic() + wall_clock_budget_seconds
    )
    result, verdict = run_review(
        runner=runner,
        task=task,
        worktree=worktree,
        default_branch=default_branch,
        model=model,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id=session_id,
    )
    if verdict is None or not verdict.blocking:
        return result, verdict

    cycle0_review = verdict.review
    plan_text, ticket_text = _load_ticket_context(worktree)
    open_findings = _track_open_findings({}, verdict.accepted)

    for cycle in range(1, _MAX_FIX_CYCLES + 1):
        remaining = _remaining_budget(deadline)
        if remaining is not None and remaining < _FIX_CYCLE_FLOOR_SECONDS:
            return _park_survivors(
                task=task,
                worktree=worktree,
                reason=CODEX_BUDGET_EXHAUSTED,
                verdict=verdict,
                open_findings=open_findings,
                cycle0_review=cycle0_review,
                cycle_count=cycle - 1,
                retry_eligible=True,
            )
        park = _run_fix_and_commit(
            runner=runner,
            task=task,
            worktree=worktree,
            open_findings=open_findings,
            model=model,
            timeout_seconds=_fix_timeout(remaining),
            session_id=session_id,
            cycle=cycle,
            plan_text=plan_text,
            ticket_text=ticket_text,
            verdict=verdict,
        )
        if park is not None:
            return park
        result, verdict = _rereview(
            runner=runner,
            task=task,
            worktree=worktree,
            default_branch=default_branch,
            model=model,
            remaining=_remaining_budget(deadline),
            session_id=session_id,
        )
        if verdict is None:
            return result, None
        open_findings = _track_open_findings(open_findings, verdict.accepted)
        if not open_findings:
            return _clean_exit(result, verdict, cycle0_review, open_findings, cycle)

    return _park_survivors(
        task=task,
        worktree=worktree,
        reason=CODEX_MUST_FIX_FINDINGS,
        verdict=verdict,
        open_findings=open_findings,
        cycle0_review=cycle0_review,
        cycle_count=_MAX_FIX_CYCLES,
        retry_eligible=None,
    )
