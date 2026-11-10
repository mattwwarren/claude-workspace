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
operator login (:mod:`cw.codex_fix_loop.constraints`), merged additively with
the row's ``cw review constrain`` entries (#2964), which apply on every
tracker and with an unresolved login. A comment posted, or an entry recorded,
while the loop runs is not seen until the next run.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_fix_loop.commit import _run_fix_and_commit
from cw.codex_fix_loop.constraints import (
    constraint_rules,
    constraints_from_comment,
    select_constraint_comment,
    with_row_constraints,
)
from cw.codex_fix_loop.convergence import _track_open_findings
from cw.codex_fix_loop.coverage import NO_DELTA_GAP
from cw.codex_fix_loop.divergence import (
    _DIVERGENCE_STALL_CYCLES,
    attributed_new_finding_count,
    emit_divergence_event,
    initial_divergence_state,
    is_diverging,
    loop_generated_findings,
    net_lines_for_commit,
    pre_loop_diff_lines,
    record_divergence_cycle,
    render_divergence_report,
    total_anchor_lines,
)
from cw.codex_fix_loop.fence import fix_scope_allowlist
from cw.codex_fix_loop.growth import (
    DEFAULT_CUMULATIVE_GROWTH_BUDGET_LINES,
    DEFAULT_GROWTH_BUDGET_LINES,
    start_loop_budget,
)
from cw.codex_fix_loop.park import (
    _clean_exit,
    _contested_exit,
    _park_survivors,
    _refuse_unread_coverage,
)
from cw.codex_fix_loop.review_pass import _rereview, _review_cycle_zero
from cw.codex_fix_loop.rounds import CycleRound
from cw.codex_fix_loop.seed import seed_verdict
from cw.codex_fix_loop.snapshot import _persist_cycle_snapshot, _with_snapshot_pointer
from cw.codex_review import (
    _MIN_ROLE_TIMEOUT_SECONDS,
    CODEX_BUDGET_EXHAUSTED,
    CODEX_MUST_FIX_FINDINGS,
    FIX_LOOP_DIVERGING,
    _capture_diff,
    _fetch_ticket_comments,
    _load_ticket_context,
)
from cw.codex_review_signals import constraints_source
from cw.local_runner import resolve_tier
from cw.models.enums import (
    DEFAULT_FIX_SANDBOX_MODE,
    DEFAULT_REVIEWER_SANDBOX_MODE,
    SandboxMode,
)
from cw.models.operator_constraint import split_constraints
from cw.review_convergence import unread_from_verdict
from cw.review_debt import dedupe_debt
from cw.review_findings import has_contested_decision
from cw.tracker import tracker_kind

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import TypeGuard

    from cw.auto_dev_result import (
        AutoDevResult,
        OperatorConstraintsSource,
        Review,
        ScopeTier,
    )
    from cw.codex_fix_loop.constraints import OperatorConstraints
    from cw.codex_fix_loop.convergence import _OpenFindingKey
    from cw.codex_fix_loop.coverage import DeltaGap
    from cw.codex_fix_loop.divergence import DivergenceState
    from cw.codex_fix_loop.growth import RunGrowth
    from cw.codex_fix_loop.rounds import CycleObserver
    from cw.codex_fix_loop.snapshot import _PersistedSnapshot
    from cw.codex_runner import CodexRunner
    from cw.models import TicketTask
    from cw.review_convergence import UnreadFindings
    from cw.review_findings import (
        AcceptedFinding,
        DebtRecord,
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


def _fix_timeout(remaining: float | None) -> int | None:
    """Per-fix-invocation timeout, floored at the per-role minimum."""
    return None if remaining is None else max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS)


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
    cycle0_unread: UnreadFindings,
    delta_gap: DeltaGap,
    cycle: int,
    snapshot: _PersistedSnapshot,
    had_real_commit: bool,
    divergence_state: DivergenceState,
    observer: CycleObserver | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None] | None:
    """Return this cycle's terminal result, or ``None`` to run another cycle.

    Convergence is checked BEFORE divergence (#2394): a cycle that cleared
    every open finding exits clean even if the divergence history would
    otherwise have tripped on it. A dirty cycle is handed to *observer*
    (#2816) next, so a ledger stop outranks a simultaneous divergence trip and
    the round is recorded either way.

    A clean exit is refused when cycle 0 left part of the diff unread
    (*cycle0_unread*, #2880) or a sharded re-review left part of its delta
    unread (*delta_gap*, #2877): this cycle's re-review covered only its own
    delta, so it cannot clear a failed role or an unreviewed shard from an
    earlier pass. A decided-constraint contest (#2984) parks before all else.
    """
    if has_contested_decision(verdict):
        return _contested_exit(
            task=task,
            worktree=worktree,
            session_id=session_id,
            verdict=verdict,
            open_findings=open_findings,
            cycle0_review=cycle0_review,
            cycle_count=cycle,
            snapshot=snapshot,
            had_real_commit=had_real_commit,
        )
    if not open_findings:
        return _refuse_unread_coverage(
            _clean_exit(
                result,
                verdict,
                cycle0_review,
                open_findings,
                cycle,
                snapshot,
                session_id=session_id,
                had_real_commit=had_real_commit,
            ),
            cycle0_unread,
            task=task,
            worktree=worktree,
            cycle=cycle,
            delta_gap=delta_gap,
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
    # #3055: where `constraints` came from, stamped on the terminal Review.
    constraints_source: OperatorConstraintsSource


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
    constraints = _load_constraints(worktree, task, operator_login, ticket_text)
    return _LoopInputs(
        cycle0_files=cycle0_files,
        # #2767 non-ship-path: hint-only tier kept; ship gates use the new resolver
        scope_tier=resolve_tier(task.scope_hint),
        plan_text=plan_text,
        ticket_text=ticket_text,
        constraints=constraints,
        # #2485: the file fence every fix cycle is held to — plan manifest plus
        # cycle-0 diff; None (no fence) when the plan has no manifest.
        allowed_files=fix_scope_allowlist(plan_text, cycle0_files),
        # #3055 (A16): the fix loop's own worktree is the root the comment
        # loader read. A row-only result has body == "", so body (not
        # comment_id, which may be None) tells a selected comment apart.
        constraints_source=constraints_source(
            has_comment=constraints is not None and bool(constraints.body),
            row_entries=len(task.operator_constraints),
            tracker=tracker_kind(worktree),
        ),
    )


def _load_constraints(
    worktree: Path,
    task: TicketTask,
    operator_login: str | None,
    ticket_text: str | None,
) -> OperatorConstraints | None:
    """Load the operator's binding constraints once per run (#2633, #2964).

    The resolutions comment (:func:`_load_comment_constraints`) and the row's
    ``cw review constrain`` entries are merged additively: the row never skips
    the comment fetch, and the comment never shadows the row. The row applies
    on every tracker and with an unresolved login, because its authority is
    where the command ran. *task* is the row snapshot the driver loaded at
    start, so an entry recorded mid-run is seen on the next run.
    """
    comment = _load_comment_constraints(worktree, task, operator_login, ticket_text)
    entries = task.operator_constraints
    if not entries:
        return comment
    # #2984: plan-derived entries are prompt text only; only operator ones rule.
    operator = split_constraints(entries)[0]
    row_rules = [rule for entry in operator for rule in constraint_rules(entry.text)]
    tokens = frozenset(t for rule in row_rules for t in rule.tokens)
    kinds = frozenset(k for rule in row_rules for k in rule.kinds)
    if operator and not tokens and not kinds:
        _log.warning(
            "codex fix loop: row constraints for %s yielded no forbidden tokens "
            "or kinds (entries=%d); the constraint guard has nothing to enforce",
            task.ticket_id,
            len(operator),
        )
    elif operator:
        _log.info(
            "codex fix loop: row constraints active (ticket=%s, entries=%d, "
            "tokens=%d, kinds=%d)",
            task.ticket_id,
            len(operator),
            len(tokens),
            len(kinds),
        )
    return with_row_constraints(comment, entries)


def _load_comment_constraints(
    worktree: Path,
    task: TicketTask,
    operator_login: str | None,
    ticket_text: str | None,
) -> OperatorConstraints | None:
    """Load the resolutions-comment constraints (#2633).

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
    reviewer_sandbox_mode: SandboxMode = DEFAULT_REVIEWER_SANDBOX_MODE,
    fix_sandbox_mode: SandboxMode = DEFAULT_FIX_SANDBOX_MODE,
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
    cycle 0's ``run_review`` and to every re-review (#2877): a diff or delta
    over it is reviewed shard by shard (#2864), and one that cannot be sharded
    parks before any codex call for that pass as a named, non-retryable
    ``agent_block`` (:func:`_review_cycle_zero`, :func:`_rereview`) naming
    the cycle. ``None`` never shards.

    A cycle 0 that left part of the diff unread (a failed reviewer role or an
    unreviewed shard, #2880), or a sharded re-review that left part of its
    delta unread (:class:`~cw.codex_fix_loop.coverage.DeltaGap`, #2877), never
    exits clean: a later delta-only re-review cannot clear it, so a loop that
    would otherwise exit clean parks as ``codex_review_partial``
    (:func:`~cw.codex_fix_loop.park._refuse_unread_coverage`).

    ``reviewer_sandbox_mode`` and ``fix_sandbox_mode`` (#1767) are the
    resolved per-role codex sandbox modes: the first reaches cycle 0 and every
    re-review (probe and roles), the second every fix invocation.
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
        reviewer_sandbox_mode=reviewer_sandbox_mode,
    )
    if _should_seed(verdict, result, seed_findings, fix_loop_enabled):
        verdict = seed_verdict(verdict, seed_findings)
    if (
        verdict is None
        or not verdict.blocking
        or not fix_loop_enabled
        or has_contested_decision(verdict)  # #2984: the whole verdict parks
    ):
        return result, verdict

    cycle0_review = verdict.review
    # #2880: re-reviews cover only the delta, so what cycle 0 left unread is
    # carried to the clean exit as a value.
    cycle0_unread = unread_from_verdict(verdict)
    # #2877: and what a sharded re-review left unread of its own delta.
    delta_gap = NO_DELTA_GAP
    inputs = _loop_inputs(worktree, task, default_branch, operator_login)
    cycle0_review = cycle0_review.model_copy(
        update={"operator_constraints_source": inputs.constraints_source}
    )
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
        # #2666: the relative threshold rides the growth-guard switch; None keeps
        # the absolute threshold (switch off, or an unanchored original finding).
        original_anchor_lines=(
            total_anchor_lines(open_findings.values()) if growth_guard_enabled else None
        ),
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
                fix_sandbox_mode=fix_sandbox_mode,
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
            cycle=cycle,
            claim_tier_enabled=claim_tier_enabled,
            disposition_drift_check_enabled=disposition_drift_check_enabled,
            settled_suppression=suppression,
            max_prompt_chars=max_prompt_chars,
            reviewer_sandbox_mode=reviewer_sandbox_mode,
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
        delta_gap = delta_gap.fold(verdict, cycle)
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
            loop_attributed=attributed_new_finding_count(
                worktree,
                divergence_state,
                pre_open_keys=pre_open_keys,
                open_findings=open_findings,
            ),
        )
        cycle_exit = _cycle_exit(
            result=result,
            task=task,
            worktree=worktree,
            session_id=session_id,
            verdict=verdict,
            open_findings=open_findings,
            cycle0_review=cycle0_review,
            cycle0_unread=cycle0_unread,
            delta_gap=delta_gap,
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
