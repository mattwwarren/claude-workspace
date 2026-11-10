"""Background execution of ``CodexExecutor``'s blocking review (GitHub #1727).

``CodexExecutor.spawn()`` used to run the whole review — per-role ``codex exec``
subprocesses plus the bounded fix loop, up to the full REVIEW budget — inside
the shared ``dispatch_tick`` call stack, so one client's review stalled the
dispatch loop for every client. This module owns the unit of work that was
moved off that stack (:func:`_run_codex_review_and_complete`, the old Step
3/4/4b/5) plus the daemon thread it runs on.

Threading precedent: ``cw.notify.fire_push_notification`` already fires a
``threading.Thread(daemon=True)`` for the same reason (do not make the caller
wait). This is the second such occurrence in the codebase and deliberately
carries no new abstraction — no thread pool, no executor service. A *third*
occurrence is the trigger to stop and write an ADR for a shared background-work
primitive rather than growing a third bespoke launcher.

This module also owns the registry of still-running review threads that
``run_dispatch_loop``'s shutdown path drains via
:func:`join_outstanding_codex_threads`, so a deploy/restart/``--once`` exit
gets a short, bounded chance to let an almost-done review finish rather than
vanishing mid-``git commit``. Threads still alive at that deadline are
``cw.reconcile.codex_boot``'s problem on the next boot, not the join's.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from cw._git import capture_head_sha
from cw.auto_dev_result import AutoDevResult
from cw.claude_review import RealClaudeRunner
from cw.codex_background_types import LoopKwargs, ReviewOutcome
from cw.codex_clean_confirm import apply_clean_confirm, resume_pending_confirm
from cw.codex_confirm_feedback import (
    feedback_token,
    load_confirm_feedback,
    reenter_feedback,
    with_feedback_tokens,
)
from cw.codex_fix_loop import RunGrowth, run_review_with_fix_loop
from cw.codex_fix_loop.push import push_and_verify_head, remote_branch_tip
from cw.codex_lane_config import (
    _DEFAULT_CODEX_REVIEW_TIER_ENABLED,
    CodexFixLoopResolution,
    _refuse_unguarded_claim_tier,
    _resolve_claim_tier_enabled,
    _resolve_codex_fix_loop,
    _resolve_codex_fix_loop_enabled,
    _resolve_codex_fix_loop_growth_guard_enabled,
    _resolve_codex_fix_loop_stall_cycles,
    _resolve_disposition_drift_check_enabled,
)
from cw.codex_loop_rounds import LoopRoundRecorder, loop_round_recorder
from cw.codex_review import (
    CODEX_MAX_PROMPT_CHARS,
    CODEX_REVIEWER_LABEL,
    CODEX_UNPUSHED_AT_EXIT,
    make_codex_blocked,
    render_verdict_comment,
)
from cw.codex_review._engine_start import engine_round_spec
from cw.codex_review_persist import (
    _persist_review_verdict,
    _persist_structured_review_verdict,
    _persist_unparseable_artifact,
    _post_review_comment,
)
from cw.codex_review_signals import undelivered_operator_context
from cw.codex_stage_completion import (
    clear_stage_blocked_marker,
    complete_session_through_door,
    revert_claimed_row_after_unexpected_error,
    save_stage_blocked_marker,
)
from cw.codex_stage_completion import (
    complete_session_as_unexpected_error as _complete_session_as_unexpected_error,
)
from cw.codex_stage_completion import (
    stamp_session_id_on_running_task as _stamp_session_id_on_running_task,
)
from cw.codex_stage_facts import stamp_stage_facts
from cw.config import load_effective_config
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event as _record_orchestrator_event
from cw.models import OrchestratorEventType
from cw.models.enums import (
    DEFAULT_FIX_SANDBOX_MODE,
    DEFAULT_REVIEWER_SANDBOX_MODE,
    SandboxMode,
)
from cw.operator_identity import resolve_operator_login
from cw.review_finding_dispositions import merge_finding_dispositions
from cw.tracker import resolve_tracker, tracker_kind
from cw.worktree import _checked_out_branch, _git_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from cw.codex_clean_confirm import CleanConfirmOutcome
    from cw.codex_confirm_feedback import ConfirmFeedback
    from cw.codex_runner import CodexRunner
    from cw.models import ClientConfig, TicketTask
    from cw.review_finding_dispositions import FindingDisposition
    from cw.review_findings import AcceptedFinding, ReviewVerdict

# Names re-exported for tests that import them from this module (#3054). The
# lane resolvers and the stamp moved out; only names no code here uses are listed
# (the others are imported above). Migrating those test imports is follow-up debt.
__all__ = [
    "_DEFAULT_CODEX_REVIEW_TIER_ENABLED",
    "CodexFixLoopResolution",
    "_resolve_codex_fix_loop",
    "_stamp_session_id_on_running_task",
]

_log = logging.getLogger(__name__)

# Shared budget for draining outstanding review threads on a graceful exit.
#
# Why 10s: long enough for a thread that has already finished its codex
# subprocesses and its git work and is only doing fast cleanup (the door write,
# the GitHub verdict comment, the SESSION_COMPLETED event) to land. Far too
# short to "wait out" a review still mid-subprocess, which can run to the full
# REVIEW budget — so a thread still alive at this deadline is, by construction,
# nowhere near done, and waiting longer would only delay the deploy/restart this
# join exists to unblock. That thread is cw.reconcile.codex_boot's problem on
# the next process start.
_CODEX_BACKGROUND_JOIN_TIMEOUT_SECONDS: float = 10.0

# Still-running review threads, newest last. Entries are appended before
# ``Thread.start()`` and removed by the thread itself on completion (success or
# exception), so this only ever holds genuinely in-flight work rather than
# accumulating over the process lifetime.
_outstanding: list[threading.Thread] = []
_outstanding_lock = threading.Lock()


def _start_daemon_thread(fn: Callable[[], None], *, name: str) -> None:
    """Run *fn* on a registered daemon thread and return immediately.

    The thread deregisters itself in a ``finally:`` so a raising *fn* cannot
    leave a dead entry behind for :func:`join_outstanding_codex_threads` to
    trip over.
    """
    holder: list[threading.Thread] = []

    def _wrapped() -> None:
        try:
            fn()
        finally:
            with _outstanding_lock:
                if holder and holder[0] in _outstanding:
                    _outstanding.remove(holder[0])

    thread = threading.Thread(target=_wrapped, name=name, daemon=True)
    holder.append(thread)
    with _outstanding_lock:
        _outstanding.append(thread)
    thread.start()


def _sync_finding_dispositions_to_running_task(
    *,
    client_name: str,
    ticket_id: str,
    created_at: datetime,
    dispositions: dict[str, FindingDisposition],
) -> None:
    """Merge *dispositions* into the matching RUNNING dev-queue row (#1838 R1).

    Called by ``codex_review._context._merge_and_persist_finding_dispositions``
    once a review pass has parsed the ticket thread's
    ``REVIEW-FINDING-DISPOSITIONS`` marker, so the adjudication survives this
    session — the record has to outlive the worktree teardown and
    regress/redispatch cycle that a live tracker fetch cannot be relied on to
    re-supply.

    Structurally identical to :func:`_stamp_session_id_on_running_task` above:
    same lock/load/find-matching-RUNNING-row/mutate/save shape, same
    keyword-only signature for the same reason (three interchangeable ``str``
    parameters), same no-op-when-nothing-matches contract. It lives here rather
    than in ``codex_review`` so both direct dev-queue writes the codex path
    performs sit in one module.

    MERGES rather than overwrites, via the ledger's own
    :func:`~cw.review_finding_dispositions.merge_finding_dispositions` — so a
    second call with the same data is a no-op, and an entry the row already
    carries is never dropped because this pass's fetch did not see it (#1838
    R3, forward-only). That merge is also where an entry failing provenance is
    refused (#2210 round 3): it never lands on the row and never replaces a
    valid entry already there, whatever the caller passed. The caller hands
    over only the enforceable delta and reports the refusals itself.

    *created_at* re-finds the caller's own row exactly as
    :func:`_stamp_session_id_on_running_task` does (#2219): required, so a
    duplicate RUNNING row for the same ``(ticket_id, client)`` never receives
    another row's ledger.
    """
    if not dispositions:
        return
    # Deferred: same claim.py -> cw.executor -> this module cycle as above.
    from cw.dispatch.claim import _find_running_row

    with dev_queue_lock():
        store = load_dev_queue()
        matched = _find_running_row(
            store, ticket_id, client_name, created_at=created_at
        )
        if matched is None:
            return
        matched.finding_dispositions = merge_finding_dispositions(
            matched.finding_dispositions, dispositions
        )
        save_dev_queue(store)


def _default_background(fn: Callable[[], None]) -> None:
    """Production ``background`` seam for :class:`~cw.executor.CodexExecutor`."""
    _start_daemon_thread(fn, name="codex-review")


def join_outstanding_codex_threads(timeout_seconds: float | None = None) -> int:
    """Join every in-flight review thread against one shared deadline.

    Returns the number still alive after the deadline — reported on the
    ``DISPATCH_LOOP_EXITED`` event so "the loop exited while N reviews were
    still running" is visible from outside the process.

    *timeout_seconds* is the budget for the whole drain, not per thread. It
    defaults to :data:`_CODEX_BACKGROUND_JOIN_TIMEOUT_SECONDS`, read at call
    time rather than bound as a default argument so the constant stays
    patchable.
    """
    budget = (
        _CODEX_BACKGROUND_JOIN_TIMEOUT_SECONDS
        if timeout_seconds is None
        else timeout_seconds
    )
    with _outstanding_lock:
        snapshot = list(_outstanding)
    deadline = time.monotonic() + budget
    for thread in snapshot:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    with _outstanding_lock:
        return sum(1 for thread in _outstanding if thread.is_alive())


def _refuse_unpushed_clean_exit(
    *,
    task: TicketTask,
    worktree: Path,
    result: AutoDevResult,
    verdict: ReviewVerdict | None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    """Never report a clean review while local HEAD is not on origin (#2354).

    Backstop for the fix loop's per-cycle push: compares local HEAD with a
    freshly fetched ``origin/<branch>`` and, on a mismatch, tries one self-heal
    push. Only a push that also fails replaces the clean result with a
    ``CODEX_UNPUSHED_AT_EXIT`` park and drops *verdict*, so no all-clear
    comment is posted for a run that parked.

    Deliberately narrow: commits only. Uncommitted or untracked paths never
    trip it — a push would carry none of them — and stay on the existing
    ``dirty_worktree`` park path. A detached HEAD has no branch to compare and
    passes through.
    """
    branch = _checked_out_branch(worktree)
    local_sha = capture_head_sha(worktree, strict=False)
    if branch is None or not local_sha:
        return result, verdict
    origin_sha = remote_branch_tip(worktree, branch)
    if origin_sha == local_sha:
        return result, verdict
    try:
        push_and_verify_head(worktree, local_sha)
    except subprocess.CalledProcessError as exc:
        _log.warning(
            "codex_unpushed_at_exit ticket=%s branch=%s head=%s origin=%s: %s",
            task.ticket_id,
            branch,
            local_sha,
            origin_sha,
            exc.stderr or exc,
        )
        blocked = make_codex_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=CODEX_UNPUSHED_AT_EXIT,
            details=(
                f"local HEAD={local_sha} not on origin/{branch} (origin={origin_sha})"
            ),
            retry_eligible=True,
        )
        return blocked, None
    _log.warning(
        "codex review exit self-healed unpushed HEAD ticket=%s branch=%s "
        "head=%s previous_origin=%s",
        task.ticket_id,
        branch,
        local_sha,
        origin_sha,
    )
    return result, verdict


@dataclass
class _ConfirmLoop:
    """One run's loop-then-gate iterations (#2807): fixed inputs, feedback state.

    ``loop`` is ``run_review_with_fix_loop`` with every per-run argument bound
    except the wall-clock budget, the seeds and (#2816) the clean-mode cycle
    observer and ``run_growth``. ``tokens`` collects one
    ``claude_confirm_feedback`` friction line per seeded iteration, plus each
    iteration's loop-recorder lines, appended to the final gated result by the
    caller; ``consumed_round`` is the last confirm round fed back, so a seeded
    loop that parks without recording a round never re-triggers on it.
    ``run_growth`` is the cumulative growth anchor every loop call of this run
    shares (clean mode only).
    """

    task: TicketTask
    worktree: Path
    client: ClientConfig
    sid: str
    deadline: float | None
    fix_loop_enabled: bool
    loop: partial[tuple[AutoDevResult, ReviewVerdict | None]]
    tokens: list[str] = field(default_factory=list)
    consumed_round: int = 0
    run_growth: RunGrowth = field(default_factory=RunGrowth)

    def consume_feedback(
        self, feedback: ConfirmFeedback | None, *, source: str
    ) -> tuple[AcceptedFinding, ...]:
        """Record *feedback* as consumed (round, token, log); return its findings.

        Mutates ``consumed_round`` and ``tokens``. ``None`` is a no-op giving ().
        """
        if feedback is None:
            return ()
        self.consumed_round = feedback.round
        self.tokens.append(feedback_token(feedback))
        _log.info(
            "claude confirm feedback re-entry ticket=%s client=%s session=%s "
            "round=%s findings=%s source=%s",
            self.task.ticket_id,
            self.client.name,
            self.sid,
            feedback.round,
            len(feedback.findings),
            source,
        )
        return feedback.findings

    def iterate(
        self, *, seeds: tuple[AcceptedFinding, ...], budget: int | None
    ) -> CleanConfirmOutcome:
        """One codex loop (plus the unpushed-HEAD guard), then the gate.

        Under clean mode the loop records every dirty cycle through a fresh
        :class:`LoopRoundRecorder` (#2816), whose friction lines join
        ``tokens``.
        """
        runner = RealClaudeRunner()
        recorder = (
            loop_round_recorder(
                task=self.task,
                worktree=self.worktree,
                client=self.client,
                sid=self.sid,
                runner=runner,
                deadline=self.deadline,
                seed_findings=seeds,
            )
            if self.client.review.is_clean
            else None
        )
        result, verdict = self._loop_then_guard(
            seeds=seeds, budget=budget, recorder=recorder
        )
        if recorder is not None:
            self.tokens.extend(recorder.tokens)
        return apply_clean_confirm(
            task=self.task,
            worktree=self.worktree,
            client=self.client,
            sid=self.sid,
            runner=runner,
            deadline=self.deadline,
            result=result,
            verdict=verdict,
        )

    def reentry(self, gated: CleanConfirmOutcome) -> ConfirmFeedback | None:
        """The Claude feedback to re-enter the loop with after *gated*, if any."""
        return reenter_feedback(
            gated,
            task=self.task,
            worktree=self.worktree,
            client=self.client,
            fix_loop_enabled=self.fix_loop_enabled,
            after_round=self.consumed_round,
            deadline=self.deadline,
        )

    def remaining_budget(self, original: int | None) -> int | None:
        """A re-entered loop's budget: what is left of the thread's deadline."""
        if self.deadline is None:
            return original
        return max(int(self.deadline - time.monotonic()), 0)

    def _loop_then_guard(
        self,
        *,
        seeds: tuple[AcceptedFinding, ...],
        budget: int | None,
        recorder: LoopRoundRecorder | None,
    ) -> tuple[AutoDevResult, ReviewVerdict | None]:
        """The codex loop, then the unpushed-HEAD guard (fix loop or clean mode, G1).

        ``seed_findings`` is passed only when there are seeds, and
        ``cycle_observer``/``run_growth`` only with a *recorder* (clean mode,
        #2816), so a blocking-mode call is exactly the call made before #2807.
        """
        kwargs: LoopKwargs = {}
        if seeds:
            kwargs["seed_findings"] = seeds
        if recorder is not None:
            kwargs["cycle_observer"] = recorder
            kwargs["run_growth"] = self.run_growth
        result, verdict = self.loop(wall_clock_budget_seconds=budget, **kwargs)
        if result.blocker is None and (
            self.fix_loop_enabled or self.client.review.is_clean
        ):
            result, verdict = _refuse_unpushed_clean_exit(
                task=self.task, worktree=self.worktree, result=result, verdict=verdict
            )
        return result, verdict


def _loop_with_feedback(
    run: _ConfirmLoop, wall_clock_budget_seconds: int | None
) -> CleanConfirmOutcome:
    """Loop and gate, re-entering the loop on each dirty Claude confirm (#2807).

    A requeue after a dirty confirm seeds the first loop. After each gate,
    :func:`~cw.codex_confirm_feedback.reenter_feedback` decides whether the
    Claude findings go back into another loop. Bounded by
    ``effective_max_cycles + 1`` loops (the ledger's own ``max_cycles`` stop,
    oscillation and the shared deadline usually end it sooner); the last
    gated outcome is returned.
    """
    first = load_confirm_feedback(
        task=run.task,
        worktree=run.worktree,
        client=run.client,
        fix_loop_enabled=run.fix_loop_enabled,
    )
    gated = run.iterate(
        seeds=run.consume_feedback(first, source="requeue"),
        budget=wall_clock_budget_seconds,
    )
    for _ in range(run.client.review.effective_max_cycles):
        feedback = run.reentry(gated)
        if feedback is None:
            break
        gated = run.iterate(
            seeds=run.consume_feedback(feedback, source="reentry"),
            budget=run.remaining_budget(wall_clock_budget_seconds),
        )
    return gated


def _review_and_confirm(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    wall_clock_budget_seconds: int | None,
    deadline: float | None,
    sid: str,
    config_model: str | None,
    config_reasoning_effort: str | None,
    config_reviewer_sandbox_mode: SandboxMode = DEFAULT_REVIEWER_SANDBOX_MODE,
    config_fix_sandbox_mode: SandboxMode = DEFAULT_FIX_SANDBOX_MODE,
) -> ReviewOutcome:
    """Step 3: codex review + fix loop (#1392), then the clean-mode gate (#2791).

    fix_loop_enabled (#1553) resolves here, not through the StageExecutor
    Protocol. A clean-mode ledger awaiting a confirm at HEAD skips the loop
    (:func:`resume_pending_confirm`). The unpushed-HEAD guard runs before the
    gate on the loop path, and on the resumed result, for the fix loop and
    for every clean-mode client (G1); blocking mode is unchanged. On the loop
    path a dirty Claude confirm re-enters the loop with its findings as seeds
    (#2807, :func:`_loop_with_feedback`) unless ``review.confirm_reentry`` is
    off.
    """
    config = load_effective_config()
    fix_loop_enabled = _resolve_codex_fix_loop_enabled(client, task, config)
    # #2210: the ledger's fuzzy claim tier resolves from the same three
    # already-loaded objects, on the same hop, and is threaded as one
    # keyword defaulting to False all the way down to the backstop.
    claim_tier_enabled = _resolve_claim_tier_enabled(client, task, config)
    # #2232: resolved on the same hop, from the same three objects, so
    # this is the one point in the codebase where both gates are in hand
    # for the SAME task and lane — the earliest a refusal can compare
    # them, and before either reaches the review.
    disposition_drift_check_enabled = _resolve_disposition_drift_check_enabled(
        client, task, config
    )
    _refuse_unguarded_claim_tier(
        lane=task.lane,
        claim_tier_enabled=claim_tier_enabled,
        drift_check_enabled=disposition_drift_check_enabled,
    )
    engine = engine_round_spec(client, task, worktree)
    if isinstance(engine, AutoDevResult):  # #3216: refused before any codex call
        return ReviewOutcome(engine, None, False, CODEX_REVIEWER_LABEL)
    resumed = resume_pending_confirm(
        task=task,
        worktree=worktree,
        client=client,
        sid=sid,
        runner=RealClaudeRunner(),
        deadline=deadline,
    )
    if resumed is not None:
        result, verdict = resumed.result, resumed.verdict
        if result.blocker is None:
            result, verdict = _refuse_unpushed_clean_exit(
                task=task, worktree=worktree, result=result, verdict=verdict
            )
        return ReviewOutcome(
            result=result,
            verdict=verdict,
            fix_loop_footer=fix_loop_enabled and resumed.fix_loop_footer,
            reviewer_label=resumed.reviewer_label,
        )
    # #3056: the run-level friction list the post-fix quality gates append to.
    gate_tokens: list[str] = []
    loop = partial(
        run_review_with_fix_loop,
        runner=runner,
        task=task,
        worktree=worktree,
        default_branch=client.default_branch,
        model=config_model,
        reasoning_effort=config_reasoning_effort,
        reviewer_sandbox_mode=config_reviewer_sandbox_mode,
        fix_sandbox_mode=config_fix_sandbox_mode,
        session_id=sid,
        fix_loop_enabled=fix_loop_enabled,
        claim_tier_enabled=claim_tier_enabled,
        disposition_drift_check_enabled=disposition_drift_check_enabled,
        stall_cycles=_resolve_codex_fix_loop_stall_cycles(client, task, config),
        growth_budget_lines=config.codex_fix_loop_growth_budget_lines,
        cumulative_growth_budget_lines=(
            config.codex_fix_loop_cumulative_growth_budget_lines
        ),
        growth_guard_enabled=_resolve_codex_fix_loop_growth_guard_enabled(
            client, task, config
        ),
        # #2633: resolved only when the loop runs (gh api user otherwise
        # never needs to run); outside every lock.
        operator_login=resolve_operator_login(client) if fix_loop_enabled else None,
        # #2867: cycle 0 shards a diff over the codex prompt limit, or parks
        # a diff that cannot be sharded as a non-retryable agent_block.
        max_prompt_chars=CODEX_MAX_PROMPT_CHARS,
        gate_tokens=gate_tokens,
        engine=engine,
    )
    run = _ConfirmLoop(
        task=task,
        worktree=worktree,
        client=client,
        sid=sid,
        deadline=deadline,
        fix_loop_enabled=fix_loop_enabled,
        loop=loop,
    )
    gated = _loop_with_feedback(run, wall_clock_budget_seconds)
    return ReviewOutcome(
        # #2807: after the FINAL gate: a dirty-confirm park is the Claude
        # round's own result and would drop a token added before the gate.
        result=with_feedback_tokens(gated.result, [*run.tokens, *gate_tokens]),
        verdict=gated.verdict,
        fix_loop_footer=fix_loop_enabled and gated.fix_loop_footer,
        reviewer_label=gated.reviewer_label,
    )


def _run_codex_review_and_complete(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    wall_clock_budget_seconds: int | None,
    sid: str,
    sess_name: str,
    config_model: str | None,
    config_reasoning_effort: str | None,
    config_reviewer_sandbox_mode: SandboxMode = DEFAULT_REVIEWER_SANDBOX_MODE,
    config_fix_sandbox_mode: SandboxMode = DEFAULT_FIX_SANDBOX_MODE,
) -> None:
    """Run the review, persist its result, post the verdict, emit completion.

    This is ``CodexExecutor.spawn()``'s former Step 3/4/4b/5, verbatim in
    behavior, relocated onto a background thread. The only substantive change
    is the failure branch: there is no longer a caller on this stack to catch a
    re-raised exception, so instead of re-raising, this reverts the claimed
    task itself — the recovery dispatch's own ``except`` handler used to
    perform.

    Marker lifecycle (#1742): an :class:`~cw.dispatch_state.ExecutorBlockedMarker`
    is written on entry and cleared in a ``finally:``, so an operator reading
    ``cw dev-queue status``/``cw doctor`` during a long review sees
    ``[BLOCKED — codex review, ...]`` rather than ``[STALE]`` and knows not to
    restart the loop out from under this thread.
    """
    # #2791: one deadline for the whole thread, shared by the codex loop and
    # the clean-mode Claude confirm (None stays None: no wall-clock budget).
    deadline = (
        None
        if wall_clock_budget_seconds is None
        else time.monotonic() + wall_clock_budget_seconds
    )
    try:
        # #1742: mark the loop as legitimately busy for the whole review. Set
        # outside any sessions_lock() block, honouring dispatch_state_lock()'s
        # documented ordering (sessions_lock -> dispatch_state_lock only).
        save_stage_blocked_marker(
            client_name=client.name, ticket_id=task.ticket_id, session_id=sid
        )
        # #3055 (RFC 0018 S3, Case B): a non-GitHub (or unset) tracker means
        # the review below runs without the operator's tracker context. Once
        # per session, before Step 3; best-effort, never raises OSError.
        undelivered_operator_context(
            task=task,
            client=client.name,
            tracker=tracker_kind(client.workspace_path),
            session_id=sid,
        )

        # Step 3: per-role review pass + bounded fix loop (#1392), then the
        # clean-mode confirm gate (#2791).
        result, verdict, fix_loop_footer, reviewer_label = _review_and_confirm(
            runner=runner,
            task=task,
            worktree=worktree,
            client=client,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            deadline=deadline,
            sid=sid,
            config_model=config_model,
            config_reasoning_effort=config_reasoning_effort,
            config_reviewer_sandbox_mode=config_reviewer_sandbox_mode,
            config_fix_sandbox_mode=config_fix_sandbox_mode,
        )

        # #3056: stamp plan_source, fork_point_sha and the fix-loop commits
        # once, here, so FINALIZE reads real values; no lock is held yet.
        result = stamp_stage_facts(
            result,
            task=task,
            worktree=worktree,
            default_branch=client.default_branch,
            session_id=sid,
        )

        # Step 4: persist result under sessions_lock.
        complete_session_through_door(sid, result.model_dump(mode="json"))

        # Step 4b: post the consolidated verdict (best-effort). After Step 4 so
        # a retry on a persist failure cannot post a duplicate comment. verdict
        # is None only when every reviewer failed (no documents to render).
        if verdict is not None:
            # #2791: a Claude-built verdict is titled Claude, and the codex fix
            # loop's footer never applies to it.
            review_text = render_verdict_comment(
                verdict,
                fix_loop_enabled=fix_loop_footer,
                reviewer_label=reviewer_label,
            )
            # #2095: the durable copy is written first, unconditionally, so a
            # tracker the daemon cannot post to still leaves a record.
            artifact_path = _persist_review_verdict(
                worktree,
                review_text,
                ticket_id=task.ticket_id,
                reviewed_sha=verdict.reviewed_sha,
            )
            _persist_structured_review_verdict(
                worktree, verdict, ticket_id=task.ticket_id
            )
            _post_review_comment(
                task.ticket_id,
                review_text,
                client=client.name,
                session_id=sid,
                kind="verdict",
                reviewed_sha=verdict.reviewed_sha,
                cwd=_git_dir(client),
                tracker=resolve_tracker(client.workspace_path),
                artifact_path=artifact_path,
            )
        elif result.blocker is not None:
            # #2280: verdict is None because every reviewer failed -- there is
            # no ReviewVerdict to render, but result.blocker.details already
            # carries the per-role "role (reason)" summary
            # (_format_failures_detail) plus a diagnostics-bundle pointer.
            # Reused verbatim as both the worktree artifact and the ticket
            # comment text, mirroring the verdict-present branch's shape.
            head_sha = capture_head_sha(worktree, strict=False)
            artifact_path = _persist_unparseable_artifact(
                worktree,
                result.blocker,
                ticket_id=task.ticket_id,
                reviewed_sha=head_sha,
                reviewer_label=reviewer_label,
            )
            _post_review_comment(
                task.ticket_id,
                result.blocker.details,
                client=client.name,
                session_id=sid,
                kind="unparseable",
                reviewed_sha=head_sha or None,
                cwd=_git_dir(client),
                tracker=resolve_tracker(client.workspace_path),
                artifact_path=artifact_path,
            )

        # Step 5: emit SESSION_COMPLETED — no result payload; dispatch reads
        # the last_result written through the door in Step 4.
        _record_orchestrator_event(
            OrchestratorEventType.SESSION_COMPLETED,
            {
                "session_id": sid,
                "ticket_id": task.ticket_id,
                "session_name": sess_name,
            },
        )
    except Exception:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md (4-part):
        # 1. This is the top frame of a daemon thread. Any escaping exception
        #    (git CalledProcessError from the diff capture, a codex subprocess
        #    fault, an I/O error on the door write) would be printed by the
        #    threading excepthook and then silently discarded, leaving the
        #    session ACTIVE forever and the task RUNNING forever.
        # 2. Logging: _log.exception captures the full traceback.
        # 3. Non-critical to the caller: dispatch_tick returned long ago; no
        #    other work depends on this thread's outcome.
        # 4. Paired test: tests/test_codex_background.py
        #    test_run_codex_review_and_complete_exception_path.
        _log.exception(
            "codex review thread failed session=%s ticket=%s", sid, task.ticket_id
        )
        _complete_session_as_unexpected_error(sid, task, worktree)
        revert_claimed_row_after_unexpected_error(
            client_name=client.name, ticket_id=task.ticket_id, session_id=sid
        )
    finally:
        # #1742: fires on every exit path — the return out of ``try``, the
        # return out of ``except``, and a BaseException the except clause does
        # not catch. The review is over either way, so the marker must go.
        clear_stage_blocked_marker(client.name, task.ticket_id)
