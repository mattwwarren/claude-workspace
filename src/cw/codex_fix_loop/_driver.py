"""Codex fix-loop driver for CodexExecutor's REVIEW stage (#1392).

Wraps :func:`cw.codex_review.run_review` in a bounded fix loop: after an
initial (cycle 0) review pass that surfaces blocking MUST_FIX findings, cw runs
up to :data:`_MAX_FIX_CYCLES` cycles of ``codex exec --sandbox workspace-write``
fix invocations, committing each cycle's real changes and re-running the full
per-role review pass to see which findings cleared. The loop exits clean the
moment no MUST_FIX finding remains open, or parks the ticket when the cap (or the
shared wall-clock budget) is exhausted — or earlier, when the divergence guard in
:mod:`cw.codex_fix_loop.divergence` sees the loop growing the diff without
resolving any originally-found MUST_FIX (#2394).

An optional cycle observer (#2816, :mod:`cw.codex_fix_loop.rounds`) sees every
dirty pass — cycle 0, and each re-review that still has open MUST_FIX findings
— and may stop the loop; the clean-mode codex lane records each such pass as a
convergence-ledger round through it. Its stop is checked after the convergence
check and before the divergence check, so the divergence guard (and the growth
budgets) stay as additional guards beside the ledger's detectors. Its
settled-finding suppression is forwarded to every review pass of the loop.
Without an observer (blocking mode) the loop is unchanged.

This is the multi-pass counterpart to ``run_review``'s single pass (#1236) built
on the executor-neutral finding contract (#1237). ``CodexExecutor.spawn()``'s
Step 3 delegates to :func:`run_review_with_fix_loop` instead of ``run_review``.

Every cycle's full ``ReviewVerdict`` (findings intact) is persisted under the
diagnostics bundle dir as it completes, and a pointer naming that cycle's
specific snapshot FILE is threaded into ``friction_highlights`` on every exit
path, so whichever cycle's verdict actually produced the terminal disposition —
not just cycle 0's — stays discoverable from the sentinel (#1485, #1739, #1763).

The pointer is an out-of-band signal, so the snapshots also carry an in-band
one: each per-cycle persist stamps ``is_terminal_snapshot=False`` (a fix-loop
persist is never final at the moment it is written), and each true exit path
re-writes exactly the one file its returned ``Blocker.details`` was rendered
from with ``is_terminal_snapshot=True`` (#1763). An operator reading a snapshot
straight off disk can then tell whether its ``rejected_must_fix`` is the set the
reported blocker cites, instead of assuming cycle 0's file is authoritative and
reading a legitimately-empty one (#1729). Two exit paths deliberately finalize
nothing: a mechanically-rejected cycle-0 verdict never enters the loop (no
snapshot is ever written), and an unparseable rereview's park details come from
``_format_failures_detail`` rather than any persisted verdict.

Cross-cycle finding identity is tracked by ``review_debt.fingerprint_v1``
(#1837) so a finding that survives every cycle — or flaps out and back, or
gets re-raised a few lines further down after a fix moved it — is counted
exactly once. From cycle 1 onward the re-review covers only the delta since
the previous reviewed head, and the admission gate in
:mod:`cw.codex_fix_loop.convergence` decides which newly-appearing MUST_FIX
findings this cycle actually caused; the rest are recorded in the verdict's
debt ledger rather than restarting the loop.

The terminal published ``Review`` is reconstructed here rather than read
from any single ``derive_review_counts`` call: ``must_fix_initial`` is cycle 0's
pre-defer snapshot, ``deferred`` is the cross-cycle survivor count, and
``fix_cycles_used`` is the loop's own cycle counter — three values no single
formula pass over one loop-exit-state finding list can produce together.

The operator's binding constraints (#2633) are loaded once per run, before
cycle 1, from the ticket's newest marker-bearing resolutions comment by the
operator login (:mod:`cw.codex_fix_loop.constraints`); a comment posted while
the loop runs is not seen until the next run.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_fix_loop.commit import _run_fix_and_commit
from cw.codex_fix_loop.constraints import (
    constraints_from_comment,
    select_constraint_comment,
)
from cw.codex_fix_loop.convergence import _track_open_findings
from cw.codex_fix_loop.divergence import (
    _DIVERGENCE_STALL_CYCLES,
    emit_divergence_event,
    initial_divergence_state,
    is_diverging,
    loop_generated_findings,
    net_lines_for_commit,
    pre_loop_diff_lines,
    record_divergence_cycle,
    render_divergence_report,
)
from cw.codex_fix_loop.fence import fix_scope_allowlist
from cw.codex_fix_loop.growth import (
    DEFAULT_CUMULATIVE_GROWTH_BUDGET_LINES,
    DEFAULT_GROWTH_BUDGET_LINES,
    start_loop_budget,
)
from cw.codex_fix_loop.park import _clean_exit, _park_survivors
from cw.codex_fix_loop.rounds import CycleRound
from cw.codex_fix_loop.seed import seed_verdict
from cw.codex_fix_loop.snapshot import _persist_cycle_snapshot, _with_snapshot_pointer
from cw.codex_review import (
    _MIN_ROLE_TIMEOUT_SECONDS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_MUST_FIX_FINDINGS,
    FIX_LOOP_DIVERGING,
    ReviewInputTooLargeError,
    _capture_diff,
    _fetch_ticket_comments,
    _load_ticket_context,
    _prepare_review_pass,
    make_codex_blocked,
    run_codex_roles,
    run_review,
    synthesize_codex_review_result,
)
from cw.local_runner import resolve_tier
from cw.review_debt import dedupe_debt

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path
    from typing import TypeGuard

    from cw.auto_dev_result import AutoDevResult, Review, ScopeTier
    from cw.codex_fix_loop.constraints import OperatorConstraints
    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.codex_fix_loop.divergence import DivergenceState
    from cw.codex_fix_loop.growth import RunGrowth
    from cw.codex_fix_loop.rounds import CycleObserver
    from cw.codex_fix_loop.snapshot import _PersistedSnapshot
    from cw.codex_review._context import _ReviewPassInputs
    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask
    from cw.review_findings import (
        AcceptedFinding,
        DebtRecord,
        Finding,
        ReviewVerdict,
    )

# Maximum fix cycles attempted before parking a still-blocking review.
_MAX_FIX_CYCLES = 5
# Coarse per-fix-cycle wall-clock floor: a cycle needs at least one fix
# invocation plus one re-review role turn, so it must be able to afford two
# per-role floors. Never start a fix cycle with less remaining budget. Public
# (#2807): the confirm-feedback re-entry refuses under the same floor.
FIX_CYCLE_FLOOR_SECONDS = 2 * _MIN_ROLE_TIMEOUT_SECONDS

_MUST_FIX = "MUST_FIX"

_log = logging.getLogger(__name__)


def _remaining_budget(deadline: float | None) -> float | None:
    """Return seconds left before *deadline*, or ``None`` for an unlimited run."""
    return None if deadline is None else deadline - time.monotonic()


def _budget_seconds(remaining: float | None) -> int | None:
    """Coerce a float remaining-budget into the int ``run_codex_roles`` expects."""
    return None if remaining is None else max(int(remaining), 0)


def _fix_timeout(remaining: float | None) -> int | None:
    """Per-fix-invocation timeout, floored at the per-role minimum."""
    return None if remaining is None else max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS)


def _rereview(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    reasoning_effort: str | None,
    remaining: float | None,
    session_id: str,
    previous_reviewed_sha: str,
    prior_open_findings: list[Finding],
    claim_tier_enabled: bool = False,
    disposition_drift_check_enabled: bool = True,
    settled_suppression: Callable[[ReviewVerdict], ReviewVerdict] | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None, _ReviewPassInputs]:
    """Run a per-role review pass over the delta since the last cycle (#1837).

    Reviews ``previous_reviewed_sha..HEAD``, not the whole branch: a full
    rescan surfaces fresh findings on code no fix cycle touched, which is the
    treadmill this ticket exists to stop. ``prior_open_findings`` is inlined
    so the reviewers still see what remains unresolved from earlier cycles.

    Returns the prepared inputs alongside the usual pair — the caller needs
    this cycle's ``delta_diff``/``delta_changed_files`` to run the admission
    gate, and they are captured here.

    ``claim_tier_enabled`` (#2210) is forwarded untouched to
    ``synthesize_codex_review_result``; this function makes no decision with
    it. Cycles 1+ reach the ledger backstop only through here, so a gate
    threaded into cycle 0 alone would arm half the loop.
    ``disposition_drift_check_enabled`` (#2232) rides the same hop on the same
    terms and for the same reason, as does ``settled_suppression`` (#2816,
    the clean-mode ledger's seam; ``None`` in blocking mode).
    """
    prepared = _prepare_review_pass(
        task,
        worktree,
        default_branch,
        runner=runner,
        session_id=session_id,
        delta_from_sha=previous_reviewed_sha,
        prior_open_findings=prior_open_findings,
    )
    documents, failures, metrics_by_role, pre_validation_rejected = run_codex_roles(
        runner=runner,
        worktree=worktree,
        roles=prepared.roles,
        prompts_by_role=prepared.prompts_by_role,
        model=model,
        reasoning_effort=reasoning_effort,
        wall_clock_budget_seconds=_budget_seconds(remaining),
        session_id=session_id,
    )
    result, verdict = synthesize_codex_review_result(
        task=task,
        worktree=worktree,
        documents=documents,
        failures=failures,
        diff=prepared.delta_diff if prepared.delta_diff is not None else prepared.diff,
        reviewed_sha=prepared.reviewed_sha,
        session_id=session_id,
        default_branch=default_branch,
        # Literal True: _rereview is only ever called from inside the
        # already-entered fix loop (run_review_with_fix_loop's for-loop).
        fix_loop_enabled=True,
        metrics_by_role=metrics_by_role,
        capability=prepared.capability,
        agent_spec_status=prepared.agent_spec_status,
        # #1814: re-fetched and re-applied every cycle, not carried over from
        # cycle 0 — an operator can void a finding mid-loop, and a fix cycle
        # can rewrite the code out from under a void's content anchor.
        voided_findings=prepared.voided_findings,
        # #1838: the ledger `_prepare_review_pass` merged for THIS cycle — the
        # running task's durable record plus any marker the operator posted
        # mid-loop. Re-merged every cycle for the same reason the voids are
        # re-fetched: an operator can settle a finding while the loop runs.
        finding_dispositions=prepared.finding_dispositions,
        # #2210 round 3: marker records refused at parse time; not in the
        # ledger above, so they ride beside it to reach the verdict.
        refused_dispositions=prepared.refused_dispositions,
        # #2210: the lane-resolved claim-tier gate, forwarded unchanged.
        claim_tier_enabled=claim_tier_enabled,
        # #2232: the lane-resolved drift-check gate, likewise unchanged.
        disposition_drift_check_enabled=disposition_drift_check_enabled,
        # #2029: this cycle's own parse-time rescues. Per-cycle, not carried
        # over — each re-review re-runs the roles and re-parses their output.
        pre_validation_rejected=pre_validation_rejected,
        settled_suppression=settled_suppression,
    )
    if verdict is not None:
        verdict = verdict.model_copy(
            update={"previous_reviewed_sha": previous_reviewed_sha}
        )
    return result, verdict, prepared


def _stamp_debt(
    verdict: ReviewVerdict, debt_ledger: dict[tuple[str, str], DebtRecord]
) -> ReviewVerdict:
    """Stamp the run's accumulated debt onto *verdict*.

    Applied after every tracker update so whichever exit path returns this
    verdict carries the ledger as of that moment — the loop has several exits
    and threading a stamp onto each one separately is how one gets missed.
    """
    return verdict.model_copy(update={"debt": dedupe_debt(list(debt_ledger.values()))})


def _cycle_exit(
    *,
    result: AutoDevResult,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    verdict: ReviewVerdict,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    cycle0_review: Review,
    cycle: int,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
    divergence_state: DivergenceState,
    observer: CycleObserver | None = None,
) -> tuple[AutoDevResult, ReviewVerdict] | None:
    """Return this cycle's terminal result, or ``None`` to run another cycle.

    Convergence is checked BEFORE divergence (#2394): a cycle that cleared
    every open finding exits clean even if the divergence history would
    otherwise have tripped on it. A dirty cycle is handed to *observer*
    (#2816) next, so a ledger stop outranks a simultaneous divergence trip and
    the round is recorded either way.
    """
    if not open_findings:
        return _clean_exit(
            result,
            verdict,
            cycle0_review,
            open_findings,
            cycle,
            snapshot,
            session_id=session_id,
            had_real_commit=had_real_commit,
        )
    ledger_park = _ledger_park(
        observer,
        CycleRound(cycle, verdict, open_findings),
        task=task,
        worktree=worktree,
        session_id=session_id,
        cycle0_review=cycle0_review,
        snapshot=snapshot,
        had_real_commit=had_real_commit,
    )
    if ledger_park is not None:
        return ledger_park
    if not is_diverging(divergence_state):
        return None
    # #2633: computed once so the event and the park text cannot disagree.
    loop_generated = loop_generated_findings(
        worktree, divergence_state, list(open_findings.values())
    )
    emit_divergence_event(
        state=divergence_state, ticket_id=task.ticket_id, loop_generated=loop_generated
    )
    return _park_survivors(
        task=task,
        worktree=worktree,
        session_id=session_id,
        reason=FIX_LOOP_DIVERGING,
        verdict=verdict,
        open_findings=open_findings,
        cycle0_review=cycle0_review,
        cycle_count=cycle,
        retry_eligible=None,
        snapshot=snapshot,
        had_real_commit=had_real_commit,
        extra_details=render_divergence_report(divergence_state, loop_generated),
    )


def _ledger_park(
    observer: CycleObserver | None,
    cycle_round: CycleRound,
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    cycle0_review: Review,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict] | None:
    """Hand a dirty pass to *observer*; park on its stop (#2816), else ``None``.

    A pass with no open finding is never observed (the clean exit, or the
    caller's convergence check, handles it), and no observer means no park.
    """
    if observer is None or not cycle_round.open_findings:
        return None
    stop = observer.record(cycle_round)
    if stop is None:
        return None
    return _park_survivors(
        task=task,
        worktree=worktree,
        session_id=session_id,
        reason=stop.reason,
        verdict=cycle_round.verdict,
        open_findings=dict(cycle_round.open_findings),
        cycle0_review=cycle0_review,
        cycle_count=cycle_round.cycle,
        retry_eligible=stop.retry_eligible,
        snapshot=snapshot,
        had_real_commit=had_real_commit,
        extra_details=stop.details,
    )


def _budget_park(
    remaining: float | None,
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str,
    verdict: ReviewVerdict,
    open_findings: dict[_OpenFindingKey, AcceptedFinding],
    cycle0_review: Review,
    cycle: int,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
) -> tuple[AutoDevResult, ReviewVerdict] | None:
    """The ``codex_budget_exhausted`` park when *cycle* cannot afford its floor."""
    if remaining is None or remaining >= FIX_CYCLE_FLOOR_SECONDS:
        return None
    return _park_survivors(
        task=task,
        worktree=worktree,
        session_id=session_id,
        reason=CODEX_BUDGET_EXHAUSTED,
        verdict=verdict,
        open_findings=open_findings,
        cycle0_review=cycle0_review,
        cycle_count=cycle - 1,
        retry_eligible=True,
        snapshot=snapshot,
        had_real_commit=had_real_commit,
    )


class _LoopInputs(NamedTuple):
    """The per-run inputs every fix cycle reads, resolved once before cycle 1."""

    cycle0_files: frozenset[str]
    scope_tier: ScopeTier
    plan_text: str | None
    ticket_text: str | None
    constraints: OperatorConstraints | None
    allowed_files: frozenset[str] | None


def _loop_inputs(
    worktree: Path,
    task: TicketTask,
    default_branch: str,
    operator_login: str | None,
) -> _LoopInputs:
    """Resolve the fix loop's per-run inputs once, before cycle 1."""
    _, _, cycle0_changed = _capture_diff(worktree, default_branch)
    cycle0_files = frozenset(cycle0_changed)
    plan_text, ticket_text = _load_ticket_context(worktree)
    return _LoopInputs(
        cycle0_files=cycle0_files,
        # #2767 non-ship-path: hint-only tier kept; ship gates use the new resolver
        scope_tier=resolve_tier(task.scope_hint),
        plan_text=plan_text,
        ticket_text=ticket_text,
        constraints=_load_constraints(worktree, task, operator_login, ticket_text),
        # #2485: the file fence every fix cycle is held to — plan manifest plus
        # cycle-0 diff; None (no fence) when the plan has no manifest.
        allowed_files=fix_scope_allowlist(plan_text, cycle0_files),
    )


def _load_constraints(
    worktree: Path,
    task: TicketTask,
    operator_login: str | None,
    ticket_text: str | None,
) -> OperatorConstraints | None:
    """Load the operator's binding constraints once per run (#2633).

    Each fail-open path logs one WARNING so "no constraints" can be told apart
    from "constraints we could not load"; the normal no-marker case is silent.
    """
    if operator_login is None:
        _log.warning(
            "codex fix loop: operator login unresolved (ticket=%s); marker "
            "comments are ignored (fail closed)",
            task.ticket_id,
        )
        return None
    comments = _fetch_ticket_comments(worktree, task.ticket_id)
    if comments is None:
        _log.warning(
            "codex fix loop: operator-constraint comments unavailable "
            "(ticket=%s); continuing without operator constraints",
            task.ticket_id,
        )
        return None
    comment = select_constraint_comment(
        comments, operator_login=operator_login, ticket_text=ticket_text
    )
    if comment is None:
        return None
    constraints = constraints_from_comment(comment)
    tokens, kinds = constraints.forbidden_tokens, constraints.forbidden_kinds
    if not tokens and not kinds:
        _log.warning(
            "codex fix loop: operator comment %s selected as constraints but "
            "yielded no forbidden tokens or kinds (ticket=%s, created_at=%s); "
            "the constraint guard has nothing to enforce",
            constraints.comment_id,
            task.ticket_id,
            constraints.created_at,
        )
    else:
        _log.info(
            "codex fix loop: operator constraints active (ticket=%s, "
            "comment_created_at=%s, tokens=%d, kinds=%d)",
            task.ticket_id,
            constraints.created_at,
            len(tokens),
            len(kinds),
        )
    return constraints


def _should_seed(
    verdict: ReviewVerdict | None,
    result: AutoDevResult,
    seed_findings: Sequence[AcceptedFinding],
    fix_loop_enabled: bool,
) -> TypeGuard[ReviewVerdict]:
    """Whether *seed_findings* are merged into cycle 0's *verdict* (#2807).

    Seed only when cycle 0 produced fixable work or parked on nothing: the
    verdict blocks, or the result carries no blocker. A cycle 0 parked on
    something other than its own findings keeps its park.
    """
    return bool(
        seed_findings
        and fix_loop_enabled
        and verdict is not None
        and (verdict.blocking or result.blocker is None)
    )


def _input_too_large_block(
    task: TicketTask, worktree: Path, exc: ReviewInputTooLargeError
) -> AutoDevResult:
    """The non-retryable ``agent_block`` for a cycle-0 prompt that cannot fit (#2867).

    Raised by the shard planner before any codex call, so nothing was sent;
    a bare requeue would fail identically, hence ``retry_eligible=False``.
    ``str(exc)`` names the file (when one is set) and both sizes.
    """
    details = (
        f"codex review prompt too large ({exc.cause}): {exc}; "
        "nothing was reviewed, truncated or retried; split or shrink the named "
        "file, or fix the oversize input, then requeue"
    )
    return make_codex_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason="agent_block",
        details=details,
        retry_eligible=False,
    )


def _review_cycle_zero(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    reasoning_effort: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
    fix_loop_enabled: bool,
    claim_tier_enabled: bool,
    disposition_drift_check_enabled: bool,
    settled_suppression: Callable[[ReviewVerdict], ReviewVerdict] | None,
    max_prompt_chars: int | None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Cycle 0's ``run_review`` under the lane's prompt limit (#2867).

    A :class:`~cw.codex_review.ReviewInputTooLargeError` (a file that cannot
    fit alone, or prompt overhead over the limit) becomes the named
    ``agent_block`` park with no verdict, the shape the no-documents path
    already returns. A sharded pass logs its shard coverage.
    """
    try:
        result, verdict = run_review(
            runner=runner,
            task=task,
            worktree=worktree,
            default_branch=default_branch,
            model=model,
            reasoning_effort=reasoning_effort,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            session_id=session_id,
            fix_loop_enabled=fix_loop_enabled,
            claim_tier_enabled=claim_tier_enabled,
            disposition_drift_check_enabled=disposition_drift_check_enabled,
            settled_suppression=settled_suppression,
            max_prompt_chars=max_prompt_chars,
        )
    except ReviewInputTooLargeError as exc:
        _log.warning(
            "codex fix loop: cycle 0 review prompt too large (ticket=%s, "
            "cause=%s, file=%s, actual_chars=%d, max_chars=%d); parking as "
            "agent_block",
            task.ticket_id,
            exc.cause,
            exc.file,
            exc.actual_chars,
            exc.max_chars,
        )
        return _input_too_large_block(task, worktree, exc), None
    if verdict is not None and verdict.shards:
        reviewed = sum(1 for shard in verdict.shards if shard.reviewed)
        _log.info(
            "codex fix loop: cycle 0 sharded (ticket=%s, shards=%d, reviewed=%d)",
            task.ticket_id,
            len(verdict.shards),
            reviewed,
        )
    return result, verdict


def run_review_with_fix_loop(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    default_branch: str,
    model: str | None,
    reasoning_effort: str | None,
    wall_clock_budget_seconds: int | None,
    session_id: str,
    fix_loop_enabled: bool,
    claim_tier_enabled: bool = False,
    disposition_drift_check_enabled: bool = True,
    stall_cycles: int = _DIVERGENCE_STALL_CYCLES,
    growth_budget_lines: int = DEFAULT_GROWTH_BUDGET_LINES,
    growth_guard_enabled: bool = True,
    operator_login: str | None = None,
    cumulative_growth_budget_lines: int = DEFAULT_CUMULATIVE_GROWTH_BUDGET_LINES,
    seed_findings: Sequence[AcceptedFinding] = (),
    cycle_observer: CycleObserver | None = None,
    run_growth: RunGrowth | None = None,
    max_prompt_chars: int | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Run the initial review pass plus a bounded MUST_FIX fix loop.

    Drop-in replacement for :func:`cw.codex_review.run_review` (identical
    signature and return shape — both now take ``fix_loop_enabled`` and
    ``claim_tier_enabled``, though this function's own semantics extend beyond
    just threading them through: ``fix_loop_enabled`` also gates whether the
    fix loop itself engages, while ``claim_tier_enabled`` (#2210) and
    ``disposition_drift_check_enabled`` (#2232) are forwarded verbatim to
    cycle 0's ``run_review`` and to every later cycle's
    ``_rereview``, with no decision taken here). One
    shared wall-clock deadline spans the initial pass, every fix invocation,
    and every re-review. A non-blocking or unparseable cycle-0 verdict passes
    straight through with zero fix invocations attempted. When
    ``fix_loop_enabled`` is False and cycle 0 blocks, returns cycle 0's tuple
    unchanged with zero fix cycles attempted. ``stall_cycles`` (#2633) is the
    lane-resolved divergence stall count (``codex_fix_loop_stall_cycles``);
    ``growth_budget_lines`` and ``growth_guard_enabled`` (#2633) set the
    in-file growth budget and its lane-resolved switch, and ``operator_login``
    is the identity whose marker-bearing comment carries binding constraints
    (``None`` fails closed: no constraints). ``cumulative_growth_budget_lines``
    (#2648) is the loop-wide net-line budget per originally-found finding,
    measured from the loop's start commit under the same switch.

    ``seed_findings`` (#2807) are accepted MUST_FIX findings from a dirty
    Claude confirm round. Cycle 0 still runs its full review; the seeds are
    then merged into its verdict (:func:`cw.codex_fix_loop.seed.seed_verdict`)
    so a clean cycle 0 still enters the loop and the first fix prompt carries
    them. They are ignored when the fix loop is disabled, when cycle 0 has no
    verdict, and when cycle 0 parked on something other than its own MUST_FIX
    findings. The default ``()`` leaves the loop unchanged.

    ``cycle_observer`` (#2816) sees cycle 0 (after its snapshot, before the
    first fix) and every re-review that still has open MUST_FIX findings; a
    :class:`~cw.codex_fix_loop.rounds.RoundStop` it returns parks the loop
    with the stop's reason, details and retry flag. Its ``suppress`` is
    forwarded as ``settled_suppression`` to cycle 0 and every re-review.
    ``run_growth`` (#2816) shares the cumulative growth budget's anchor and
    finding union across every loop call of one clean-mode run
    (:func:`cw.codex_fix_loop.growth.start_loop_budget`). Both default to
    ``None``, which leaves the loop unchanged (blocking mode).

    ``max_prompt_chars`` (#2867) is the backend's prompt limit, forwarded to
    cycle 0's ``run_review`` only: a diff over it is reviewed shard by shard
    (#2864), and one that cannot be sharded parks before any codex call as a
    named, non-retryable ``agent_block`` (:func:`_review_cycle_zero`).
    Re-review cycles are not limited: they embed only the delta since the
    previous reviewed head (a follow-up ticket covers them). ``None`` never
    shards.
    """
    deadline = (
        None
        if wall_clock_budget_seconds is None
        else time.monotonic() + wall_clock_budget_seconds
    )
    suppression = None if cycle_observer is None else cycle_observer.suppress
    result, verdict = _review_cycle_zero(
        runner=runner,
        task=task,
        worktree=worktree,
        default_branch=default_branch,
        model=model,
        reasoning_effort=reasoning_effort,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        session_id=session_id,
        fix_loop_enabled=fix_loop_enabled,
        claim_tier_enabled=claim_tier_enabled,
        disposition_drift_check_enabled=disposition_drift_check_enabled,
        settled_suppression=suppression,
        max_prompt_chars=max_prompt_chars,
    )
    if _should_seed(verdict, result, seed_findings, fix_loop_enabled):
        verdict = seed_verdict(verdict, seed_findings)
    if verdict is None or not verdict.blocking or not fix_loop_enabled:
        return result, verdict

    cycle0_review = verdict.review
    inputs = _loop_inputs(worktree, task, default_branch, operator_login)
    # #1837: one ledger object threaded through every cycle, accumulating the
    # findings the loop records instead of acting on.
    debt_ledger: dict[tuple[str, str], DebtRecord] = {}
    open_findings = _track_open_findings(
        {},
        verdict.accepted,
        # Cycle 0 has no prior head to restrict a "genuinely new" finding
        # against, so the admission gate never runs on this call and every
        # MUST_FIX the full-PR pass found still blocks.
        delta_diff=None,
        delta_changed_files=None,
        debt_ledger=debt_ledger,
        previous_reviewed_sha=None,
        reviewed_sha=verdict.reviewed_sha,
        worktree=worktree,
        ticket_id=task.ticket_id,
    )
    verdict = _stamp_debt(verdict, debt_ledger)
    snapshot = _persist_cycle_snapshot(verdict, session_id=session_id, cycle=0)
    # #2816: cycle 0 is a ledger round too; a stop parks before any fix.
    cycle0_park = _ledger_park(
        cycle_observer,
        CycleRound(0, verdict, open_findings),
        task=task,
        worktree=worktree,
        session_id=session_id,
        cycle0_review=cycle0_review,
        snapshot=snapshot,
        had_real_commit=False,
    )
    if cycle0_park is not None:
        return cycle0_park
    baseline_lines = pre_loop_diff_lines(worktree, default_branch)
    divergence_state = initial_divergence_state(
        original_keys=frozenset(open_findings),
        pre_loop_diff_lines=baseline_lines,
        pre_loop_head_sha=verdict.reviewed_sha,
        stall_cycles=stall_cycles,
    )
    # #2648: the loop-wide growth budget, fixed for the whole loop; #2816:
    # anchored at the run's first loop when a run_growth is shared.
    loop_growth_budget = start_loop_budget(
        divergence_state.pre_loop_head_sha,
        divergence_state.original_keys,
        cumulative_growth_budget_lines,
        run_growth,
    )
    # #1723: true iff at least one fix cycle so far produced a real commit
    # (OR'd across cycles) — distinguishes a genuine fix from a fix loop
    # that converged purely because every cycle's fix invocation was a no-op.
    had_real_commit = False

    for cycle in range(1, _MAX_FIX_CYCLES + 1):
        remaining = _remaining_budget(deadline)
        park: tuple[AutoDevResult, ReviewVerdict | None] | None = _budget_park(
            remaining,
            task=task,
            worktree=worktree,
            session_id=session_id,
            verdict=verdict,
            open_findings=open_findings,
            cycle0_review=cycle0_review,
            cycle=cycle,
            snapshot=snapshot,
            had_real_commit=had_real_commit,
        )
        if park is None:
            park, commit_sha = _run_fix_and_commit(
                runner=runner,
                task=task,
                worktree=worktree,
                open_findings=open_findings,
                model=model,
                reasoning_effort=reasoning_effort,
                timeout_seconds=_fix_timeout(remaining),
                session_id=session_id,
                cycle=cycle,
                plan_text=inputs.plan_text,
                ticket_text=inputs.ticket_text,
                verdict=verdict,
                cycle0_files=inputs.cycle0_files,
                scope_tier=inputs.scope_tier,
                cycle0_review=cycle0_review,
                snapshot=snapshot,
                had_real_commit_so_far=had_real_commit,
                default_branch=default_branch,
                allowed_files=inputs.allowed_files,
                growth_budget_lines=growth_budget_lines,
                growth_guard_enabled=growth_guard_enabled,
                constraints=inputs.constraints,
                loop_growth_budget=loop_growth_budget,
            )
        if park is not None:
            return park
        had_real_commit = had_real_commit or commit_sha is not None
        previous_reviewed_sha = verdict.reviewed_sha
        result, verdict, prepared = _rereview(
            runner=runner,
            task=task,
            worktree=worktree,
            default_branch=default_branch,
            model=model,
            reasoning_effort=reasoning_effort,
            remaining=_remaining_budget(deadline),
            session_id=session_id,
            previous_reviewed_sha=previous_reviewed_sha,
            prior_open_findings=[af.finding for af in open_findings.values()],
            claim_tier_enabled=claim_tier_enabled,
            disposition_drift_check_enabled=disposition_drift_check_enabled,
            settled_suppression=suppression,
        )
        if verdict is None:
            # No cycle-N snapshot was persisted (the persist call below is
            # never reached) and this park's details come from
            # `_format_failures_detail`, not from any persisted verdict — so
            # nothing is finalized here (#1763).
            return (
                result.model_copy(
                    update={
                        "friction_highlights": _with_snapshot_pointer(
                            result.friction_highlights, snapshot.pointer
                        )
                    }
                ),
                None,
            )
        pre_open_keys = frozenset(open_findings)
        open_findings = _track_open_findings(
            open_findings,
            verdict.accepted,
            delta_diff=prepared.delta_diff,
            delta_changed_files=prepared.delta_changed_files,
            debt_ledger=debt_ledger,
            previous_reviewed_sha=previous_reviewed_sha,
            reviewed_sha=verdict.reviewed_sha,
            worktree=worktree,
            ticket_id=task.ticket_id,
        )
        verdict = _stamp_debt(verdict, debt_ledger)
        snapshot = _persist_cycle_snapshot(verdict, session_id=session_id, cycle=cycle)
        divergence_state = record_divergence_cycle(
            divergence_state,
            cycle=cycle,
            pre_open_keys=pre_open_keys,
            post_open_keys=frozenset(open_findings),
            net_lines_added=net_lines_for_commit(worktree, commit_sha),
        )
        cycle_exit = _cycle_exit(
            result=result,
            task=task,
            worktree=worktree,
            session_id=session_id,
            verdict=verdict,
            open_findings=open_findings,
            cycle0_review=cycle0_review,
            cycle=cycle,
            snapshot=snapshot,
            had_real_commit=had_real_commit,
            divergence_state=divergence_state,
            observer=cycle_observer,
        )
        if cycle_exit is not None:
            return cycle_exit

    return _park_survivors(
        task=task,
        worktree=worktree,
        session_id=session_id,
        reason=CODEX_MUST_FIX_FINDINGS,
        verdict=verdict,
        open_findings=open_findings,
        cycle0_review=cycle0_review,
        cycle_count=_MAX_FIX_CYCLES,
        retry_eligible=None,
        snapshot=snapshot,
        had_real_commit=had_real_commit,
    )
