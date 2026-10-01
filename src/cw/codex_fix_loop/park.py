"""Terminal exit paths for the codex fix loop: park and clean-exit builders.

Each builder finalizes the persisted snapshot its result was derived from
(#1763), reconstructs the terminal published ``Review`` via
:func:`_finalize_review`, and threads the snapshot pointer into
``friction_highlights``. ``fix_loop_escalated`` is set once the cycle count
reaches :data:`_ESCALATE_AT_CYCLE`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_fix_loop.convergence import _survivors_only_verdict
from cw.codex_fix_loop.snapshot import _finalize_snapshot, _with_snapshot_pointer
from cw.codex_review import (
    _CATEGORY_TO_REASON,
    _TRANSIENT_FAILURE_REASONS,
    CODEX_FIX_SCOPE_VIOLATION,
    make_codex_blocked,
    render_verdict_comment,
)
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult, Health, Review
    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.codex_fix_loop.fence import FenceBreach
    from cw.codex_fix_loop.snapshot import _PersistedSnapshot
    from cw.codex_review import _SensitiveHit
    from cw.executor_diagnostics import ExecutorFailureCategory
    from cw.models import TicketTask
    from cw.review_findings import AcceptedFinding, ReviewVerdict

# Cycle at/after which Health.fix_loop_escalated is set — a loop that needed
# this many passes is operator-attention-worthy even when it eventually clears.
_ESCALATE_AT_CYCLE = 3


def _finalize_review(
    *,
    cycle0_review: Review,
    final_verdict: ReviewVerdict,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    cycle_count: int,
    had_real_commit: bool,
) -> Review:
    """Reconstruct the terminal published ``Review`` from authoritative sources.

    ``should_fix`` and ``agents_run`` are taken from the final cycle's own
    correctly-derived ``Review``. ``must_fix_initial`` comes from cycle 0's
    snapshot (captured before any defer stamping, so trivially correct).
    ``deferred`` is the cross-cycle survivor count. ``fix_cycles_used`` is the
    loop's own cycle counter — set explicitly here rather than inherited from
    ``final_verdict.review`` because ``synthesize_codex_review_result`` does not
    thread the cycle index through its internal ``consolidate_verdict`` call.
    ``had_real_commit`` is the loop's OR-across-cycles real-commit tracker
    (#1723) — true iff at least one fix cycle actually committed a change.
    """
    return final_verdict.review.model_copy(
        update={
            "must_fix_initial": cycle0_review.must_fix_initial,
            "deferred": len(open_findings),
            "fix_cycles_used": cycle_count,
            "had_real_commit": had_real_commit,
        }
    )


def _apply_escalation(health: Health, cycle: int) -> Health:
    """Return *health* with ``fix_loop_escalated`` set when at/past the threshold."""
    if cycle >= _ESCALATE_AT_CYCLE:
        return health.model_copy(update={"fix_loop_escalated": True})
    return health


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
    snapshot: _PersistedSnapshot,
    reasoning_effort: str | None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Park the ticket on a failed fix invocation, persisting a diagnostics bundle.

    Reuses ``codex_review``'s category→reason map and transient-reason set so a
    timeout parks retry-eligible while a hard error parks for the operator, and
    writes the typed ``ExecutorFailure`` bundle under ``reviewer_role`` =
    ``fix-cycle-N`` (mirroring ``_persist_codex_role_diagnostics``).

    Why: unlike ``_clean_exit``/``_park_scope_violation``, this function does
    NOT stamp a finalized ``review`` onto the returned ``verdict`` (#1705) —
    it never calls ``_finalize_review`` and has no ``cycle0_review``/
    ``open_findings`` in scope to build one from. Enriching it would need a
    signature change plus updates to both call sites in
    ``_run_fix_and_commit``, which exceeds #1705's one-line-stamp scope; the
    operator explicitly deferred it as a candidate fast-follow ticket rather
    than expanding that diff (see #1705 Decisions #2).
    """
    # Deferred import: cw.codex_fix_loop.commit imports this module at load
    # time (its _run_fix_and_commit parks through the builders here), so a
    # module-level import of commit from here would be circular.
    from cw.codex_fix_loop.commit import _build_fix_codex_argv

    if verdict is not None:
        _finalize_snapshot(verdict, session_id=session_id, cycle=snapshot.cycle)
    reason = _CATEGORY_TO_REASON[category]
    failure = build_executor_failure(
        category=category,
        executor_name="codex",
        session_id=session_id,
        # The effort pin is threaded, not assumed: a diagnostic argv that
        # omits the pin that actually ran would mislead the next reader.
        argv=_build_fix_codex_argv(model=None, reasoning_effort=reasoning_effort),
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
    blocked = make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=reason,
        details=detail,
        retry_eligible=True if transient else None,
    )
    blocked = blocked.model_copy(
        update={
            "friction_highlights": _with_snapshot_pointer(
                blocked.friction_highlights, snapshot.pointer
            )
        }
    )
    return blocked, verdict


def _park_survivors(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    reason: str,
    verdict: ReviewVerdict,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    cycle0_review: Review,
    cycle_count: int,
    retry_eligible: bool | None,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
    extra_details: str | None = None,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Park a still-blocking review (cap, budget, or divergence) with survivor detail.

    Builds the terminal ``Review`` and the survivor-only verdict, renders the
    verdict comment into ``Blocker.details`` (followed by *extra_details*, when
    given), and sets ``fix_loop_escalated`` on the health block when the cycle
    count reached the escalation threshold.

    Finalizes the persisted snapshot from the ORIGINAL *verdict* argument, not
    the ``survivors`` object rebuilt below: ``_survivors_only_verdict``'s update
    dict never touches ``rejected``/``rejected_must_fix``, so the two agree on
    the field #1763 is about, and the file on disk stays the one
    ``_persist_cycle_snapshot`` wrote rather than a loop-exit reconstruction.
    """
    _finalize_snapshot(verdict, session_id=session_id, cycle=snapshot.cycle)
    review = _finalize_review(
        cycle0_review=cycle0_review,
        final_verdict=verdict,
        open_findings=open_findings,
        cycle_count=cycle_count,
        had_real_commit=had_real_commit,
    )
    survivors = _survivors_only_verdict(verdict, open_findings, review)
    # Literal True: reached only after the fix loop has actually engaged
    # (survivors are cross-cycle-tracked open findings), so the fix loop
    # was, by construction, enabled for this run.
    details = render_verdict_comment(survivors, fix_loop_enabled=True)
    if extra_details:
        details = f"{details}\n\n{extra_details}"
    blocked = make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=reason,
        details=details,
        retry_eligible=retry_eligible,
    )
    health = blocked.health.model_copy(
        update={"fix_loop_escalated": cycle_count >= _ESCALATE_AT_CYCLE}
    )
    patched = blocked.model_copy(
        update={
            "review": review,
            "health": health,
            "friction_highlights": _with_snapshot_pointer(
                blocked.friction_highlights, snapshot.pointer
            ),
        }
    )
    return patched, survivors


def _park_scope_violation(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    cycle: int,
    violations: list[_SensitiveHit],
    cycle0_review: Review,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    verdict: ReviewVerdict,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Park a fix cycle whose commit would touch a sensitive out-of-scope path.

    The gate is AND-only: ``_scope_violations`` only ever returns hits already
    computed over the out-of-scope subset, so both conditions (out of the
    cycle-0 reviewed diff's scope, and a sensitive-registry match) hold for
    every listed path — the details string says so explicitly rather than
    leaving it implicit. Follows ``_park_survivors``'s pattern verbatim:
    reconstruct the terminal ``Review`` via ``_finalize_review``, build the
    ``Blocker`` via ``make_blocked``, then patch review/health onto the
    result. ``had_real_commit`` is the pre-this-cycle OR-across-cycles
    real-commit tracker (#1723) — this cycle's own commit never landed (that
    is why it is being parked), so the caller's already-accumulated value is
    what is forwarded, not a fresh computation. Snapshot ordering (#1763)
    is owned by :func:`_park_uncommitted_cycle`.
    """
    lines = [f"- {hit.path} ({hit.category}): {hit.reason}" for hit in violations]
    details = "\n".join(
        [
            f"codex fix cycle {cycle} touched path(s) that are both out of the "
            "cycle-0 reviewed diff's scope AND match the sensitive-files "
            "registry:",
            *lines,
        ]
    )
    return _park_uncommitted_cycle(
        task=task,
        worktree=worktree,
        session_id=session_id,
        cycle=cycle,
        reason=CODEX_FIX_SCOPE_VIOLATION,
        details=details,
        recovery_hint=None,
        cycle0_review=cycle0_review,
        open_findings=open_findings,
        verdict=verdict,
        snapshot=snapshot,
        had_real_commit=had_real_commit,
    )


def _park_fence_breach(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    cycle: int,
    breach: FenceBreach,
    cycle0_review: Review,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    verdict: ReviewVerdict,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Park a fix cycle the scope fence or revert guard rejected (#2485, #2492).

    Same shape as :func:`_park_scope_violation`, plus the breach's
    ``recovery_hint`` on the ``Blocker`` so the operator is told where the
    rejected changes are and what to compare, instead of diffing the branch
    by hand (#2492).
    """
    return _park_uncommitted_cycle(
        task=task,
        worktree=worktree,
        session_id=session_id,
        cycle=cycle,
        reason=breach.reason,
        details=breach.details,
        recovery_hint=breach.recovery_hint,
        cycle0_review=cycle0_review,
        open_findings=open_findings,
        verdict=verdict,
        snapshot=snapshot,
        had_real_commit=had_real_commit,
    )


def _park_uncommitted_cycle(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    cycle: int,
    reason: str,
    details: str,
    recovery_hint: str | None,
    cycle0_review: Review,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    verdict: ReviewVerdict,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Shared body of the parks for a fix cycle whose changes were refused.

    ORDERING (#1763): the snapshot is finalized FIRST, from the verdict as
    persisted, because the rebind below replaces ``verdict.review`` with the
    loop's reconstructed cross-cycle ``Review``. Finalizing after the rebind
    would rewrite the on-disk file's ``review`` block with counts the
    intermediate persist never had.
    """
    _finalize_snapshot(verdict, session_id=session_id, cycle=snapshot.cycle)
    review = _finalize_review(
        cycle0_review=cycle0_review,
        final_verdict=verdict,
        open_findings=open_findings,
        cycle_count=cycle,
        had_real_commit=had_real_commit,
    )
    verdict = verdict.model_copy(update={"review": review})
    blocked = make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=reason,
        details=details,
        retry_eligible=None,
    )
    health = blocked.health.model_copy(
        update={"fix_loop_escalated": cycle >= _ESCALATE_AT_CYCLE}
    )
    update: dict[str, object] = {
        "review": review,
        "health": health,
        "friction_highlights": _with_snapshot_pointer(
            blocked.friction_highlights, snapshot.pointer
        ),
    }
    if recovery_hint is not None and blocked.blocker is not None:
        update["blocker"] = blocked.blocker.model_copy(
            update={"recovery_hint": recovery_hint}
        )
    return blocked.model_copy(update=update), verdict


def _clean_exit(
    result: AutoDevResult,
    verdict: ReviewVerdict,
    cycle0_review: Review,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    cycle: int,
    snapshot: _PersistedSnapshot,
    session_id: str,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict]:
    """Return the clean-exit result with the terminal review + escalation patched.

    Stamps the finalized ``review`` onto the returned *verdict* too (not just
    the returned ``AutoDevResult``) — #1705 bug #2: without this, the
    ``ReviewVerdict`` that reaches ``render_verdict_comment`` at the
    executor's Step 4b still carries the terminal ``_rereview()`` pass's own
    ``fix_cycles_used=0``, numerically indistinguishable from a genuinely
    clean first pass.

    "Clean" is a pre-existing misnomer for one branch this function also
    serves: a cycle-N rereview that mechanically-rejects a MUST_FIX with zero
    other open findings arrives here already blocked
    (``codex_must_fix_mechanically_rejected``), because ``blocking`` is False
    while ``rejected_must_fix`` is not (#1714/#1729). That is exactly the case
    #1763's terminal marker exists for, so the snapshot is finalized here —
    BEFORE the ``verdict.review`` rebind below, for the same reason spelled out
    in :func:`_park_uncommitted_cycle`.
    """
    _finalize_snapshot(verdict, session_id=session_id, cycle=snapshot.cycle)
    review = _finalize_review(
        cycle0_review=cycle0_review,
        final_verdict=verdict,
        open_findings=open_findings,
        cycle_count=cycle,
        had_real_commit=had_real_commit,
    )
    verdict = verdict.model_copy(update={"review": review})
    health = _apply_escalation(result.health, cycle)
    patched = result.model_copy(
        update={
            "review": review,
            "health": health,
            "friction_highlights": _with_snapshot_pointer(
                result.friction_highlights, snapshot.pointer
            ),
        }
    )
    return patched, verdict
