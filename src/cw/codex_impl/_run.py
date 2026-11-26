"""The codex IMPL stage, end to end (#3061, RFC 0018 C2).

:func:`run_impl_stage_and_complete` is what ``cw codex run --stage impl`` runs.
Everything happens inside :func:`cw.codex_stage_completion.run_guarded_stage`,
so a stage fault completes the session as ``unexpected_error`` at
``stage2_impl`` and returns the claimed row to PENDING. The ordered flow:

1. Pre-turn: the snapshot refresh, the plan (recovered when absent), the
   rendered prompt, the live-PR bind of any review feedback, the branch guards
   and the read-only default-branch probe. Any failure blocks before a turn.
2. The clean-start baseline, the conflict merge (repair rows only), and the
   pinned quality gates.
3. One write-capable ``codex exec`` turn with no timeout (ADR-0014). The turn
   never stages, commits or pushes.
4. cw measures the turn, then runs gate 1 (empty diff), the file fence, the
   growth guard (repair rows), gate 2 (scope conformance), the quality gates
   and gate 4 (ruff and mypy on the touched Python files, #3531). A refused
   turn is preserved under a ref and the tree is restored.
5. cw makes the one commit (with the ``Auto-Dev-Stage: impl-complete``
   trailer), pushes it and verifies origin's tip, stamps the sentinel and
   completes through the door.

Every ``blocked`` result goes through :meth:`_Run.blocked`, which emits one
``stage.errored`` event. The stage events are driver-emitted, never
model-emitted. No tracker post is made.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from cw._git import git_output
from cw.auto_dev_result.schema._vocab import PLAN_MISSING_CONTEXT_BLOCKER_REASON
from cw.backend_mcp_config import declared_server_names
from cw.bindings_block import bindings_block_for
from cw.codex_fix_loop.commit import _git_commit, _staged_paths
from cw.codex_fix_loop.cycle_restore import preserve_and_reset
from cw.codex_fix_loop.gates import GateScope, plan_cycle_gates
from cw.codex_fix_loop.hook_failure import as_hook_failure
from cw.codex_impl._argv import build_impl_argv, require_impl_sandbox_argv
from cw.codex_impl._post import STAGE_IMPL_STARTED, early_stamp, finish
from cw.codex_impl._prompt import IMPL_DOCS, IMPL_TURN_SCHEMA, render_impl_prompt
from cw.codex_impl._result import (
    IMPL_NOT_PUSHED,
    ImplTurnFailure,
    impl_blocked,
    impl_commit_message,
    impl_empty_diff_result,
    parse_impl_turn,
)
from cw.codex_impl._resume import (
    advance_payload,
    bound_operator_comments,
    resume_note,
)
from cw.codex_impl._type_gate import run_type_check_gate
from cw.codex_lane_config import _resolve_codex_fix_loop_growth_guard_enabled
from cw.codex_plan._prompt import command_docs_root, ticket_text_from_snapshot
from cw.codex_review import (
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    STAGE2_IMPL,
    _codex_scratch_dir,
    _is_model_capacity_error,
)
from cw.codex_review_signals import record_event_best_effort
from cw.codex_stage_completion import (
    STAGE_FAULTS,
    complete_session_through_door,
    run_guarded_stage,
    with_usage_friction,
)
from cw.codex_usage import NO_USAGE, UsageAccumulator
from cw.config import load_effective_config
from cw.contract_manifest import RenderError
from cw.dev_queue import _PRE_DISPATCH_TRACKER_MCP_REASON
from cw.local_runner import GitFacts, git_facts
from cw.models import CODEX_BACKEND, OrchestratorEventType
from cw.models.bindings import Harness
from cw.models.branch import resolve_base_branch, resolve_feature_branch
from cw.models.enums import StageIdentifier
from cw.plan_files import resolve_review_scope_tier
from cw.pr_hydrate import _resolve_repo_slug
from cw.scope_grants import carry_scope_grants
from cw.stage_verification.baseline import (
    CycleBaseline,
    DirtyStart,
    capture_cycle_baseline,
    cycle_touched_paths,
    stage_cycle,
)
from cw.stage_verification.fence import (
    check_fix_fence,
    fix_scope_allowlist,
    staged_set_breach,
)
from cw.stage_verification.growth import check_growth_budget
from cw.stage_verification.impl_block import (
    IMPL_BLOCKED,
    IMPL_FAILED,
    ImplBlock,
)
from cw.stage_verification.impl_feedback import (
    MergeOutcome,
    ProbeOutcome,
    merge_default_on_conflict,
    probe_default_branch,
    verify_review_feedback,
)
from cw.stage_verification.impl_gates import (
    approved_extra,
    check_empty_diff,
    check_on_branch,
    check_plan_present,
    commit_failure_block,
    dirty_start_block,
    fence_block,
    no_plan_sensitive_block,
    note_plan_identity,
    repair_allowed_files,
    run_quality_gates,
    run_scope_conformance,
    staged_totals,
)
from cw.stage_verification.impl_merge_adopt import (
    conflict_fingerprints,
    merge_unconcluded_block,
    merge_unresolved_block,
)
from cw.stage_verification.impl_resume import Disposition, plan_reentry
from cw.stage_verification.impl_type_gate import PreparePin, pin_prepare
from cw.stage_verification.posted_text import redact_and_cap
from cw.stage_verification.push import push_and_verify_head
from cw.ticket_snapshot import context_for_client, snapshot_for_stage
from cw.ticket_snapshot._plan import (
    PLAN_RECOVERED_FRICTION,
    plan_identity_unresolved,
    recover_plan,
)
from cw.tolerated_paths import tolerated_paths
from cw.tracker_io import TrackerIoConfigError, courier_refresher

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_fix_loop.gates import GatePlan
    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import ClientConfig, SandboxMode, TicketTask
    from cw.stage_verification.impl_resume import ReentryPlan
    from cw.ticket_snapshot import TicketSnapshot

_log = logging.getLogger(__name__)

#: The stage identifiers the driver's stage events carry.
STAGE_PLAN_REVIEWED: Final = StageIdentifier.S1_PLAN_REVIEWED.value

_SCHEMA_NAME: Final = "impl-schema.json"
_OUTPUT_NAME: Final = "impl-output.json"
_CYCLE: Final = 0
_STDERR_TAIL_CHARS: Final = 500
_PLAN_RELATIVE: Final = ".cw/plan.md"
_NO_CHANGE_DETAILS: Final = (
    "repair_turn_made_no_change: the repair turn left the tree unchanged, so "
    "there is nothing to commit; the feedback was not addressed"
)
_RESUME_NO_CHANGE_DETAILS: Final = (
    "resume_turn_made_no_change: the resumed turn left the tree unchanged, so "
    "there is nothing to commit; the existing commits were not advanced"
)


@dataclass(frozen=True)
class _Pre:
    """What the pre-turn steps established."""

    plan_text: str | None
    facts: GitFacts
    prompt: str
    needs_merge: bool
    allowed: frozenset[str]
    approved: frozenset[str]
    friction: list[str]
    resume: ReentryPlan | None = None


@dataclass(frozen=True)
class _Flow:
    """The turn-ready state: baseline, merge, pinned gates and friction.

    ``prepare`` is the default branch's ``tests.prepare``, read before the turn.
    """

    pre: _Pre
    baseline: CycleBaseline
    base: str
    merge: MergeOutcome | None
    gate_plan: GatePlan
    scope: GateScope
    prepare: PreparePin
    tokens: list[str] = field(default_factory=list)
    conflict_before: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Run:
    """Everything one IMPL run needs; methods are the flow's steps."""

    runner: CodexRunner
    task: TicketTask
    client: ClientConfig
    worktree: Path
    sid: str
    sess_name: str
    model: str | None
    reasoning_effort: str | None
    sandbox_mode: SandboxMode
    usage: UsageAccumulator = NO_USAGE

    @property
    def repair(self) -> bool:
        """Whether the row carries review feedback (a repair entry)."""
        return self.task.review_feedback is not None

    # Both codex executors carry this; their _Run classes differ only in harness.
    @property
    def base_branch(self) -> str:
        """The row's merge target: its base, else the client default (S3b)."""
        return resolve_base_branch(self.client, self.task)

    # -- events -------------------------------------------------------------

    def emit(
        self,
        event_type: OrchestratorEventType,
        stage: str,
        *,
        prev_stage: str,
        error_kind: str | None = None,
    ) -> None:
        """Record one stage event, best-effort: telemetry never changes the result."""
        payload: dict[str, object] = {
            "session_id": self.sid,
            "ticket_id": self.task.ticket_id,
            "stage": stage,
            "prev_stage": prev_stage,
        }
        if error_kind is not None:
            payload["error_kind"] = error_kind
        payload["started_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        record_event_best_effort(event_type, payload, self.task.ticket_id)

    # -- blocks -------------------------------------------------------------

    def blocked(
        self,
        block: ImplBlock,
        *,
        restore: CycleBaseline | None = None,
        base: str | None = None,
    ) -> AutoDevResult:
        """A cw-built blocked exit; emits its one ``stage.errored``.

        With *restore* (the clean-start baseline) the refused turn's edits are
        measured against *base*, pinned under ``refs/cw/refused-cycles/`` and
        the worktree is reset, so the next run starts clean.
        """
        measured = None
        if restore is not None:
            measured = self._measure_and_restore(restore, base, block.reason)
        if block.status == IMPL_BLOCKED:
            result = impl_blocked(
                ticket_id=self.task.ticket_id,
                worktree=self.worktree,
                reason=block.reason,
                details=block.details,
                retry_eligible=block.retry_eligible,
                measured=measured,
            )
        else:
            result = impl_empty_diff_result(
                ticket_id=self.task.ticket_id,
                worktree=self.worktree,
                details=block.details,
                measured=measured,
            )
        self._errored(block.reason)
        return result

    def refused(self, block: ImplBlock, flow: _Flow) -> AutoDevResult:
        """A block after the turn ran: measure, preserve under a ref and restore."""
        return self.blocked(block, restore=flow.baseline, base=flow.base)

    def pass_through(self, result: AutoDevResult, flow: _Flow) -> AutoDevResult:
        """A model-reported ``blocked`` result: restore the tree, keep its reason."""
        reason = result.blocker.reason if result.blocker is not None else IMPL_FAILED
        self._measure_and_restore(flow.baseline, flow.base, reason)
        self._errored(reason)
        return result

    def _errored(self, reason: str) -> None:
        self.emit(
            OrchestratorEventType.STAGE_ERRORED,
            STAGE_IMPL_STARTED,
            prev_stage=STAGE_PLAN_REVIEWED,
            error_kind=reason,
        )

    def _measure_and_restore(
        self, baseline: CycleBaseline, base: str | None, reason: str
    ) -> tuple[int, int] | None:
        """Measure the staged edits, then preserve and reset (never raises)."""
        measured = None
        try:
            stage_cycle(self.worktree, baseline.review_artifacts)
            measured = staged_totals(self.worktree, base or baseline.head_sha)
        except (subprocess.CalledProcessError, OSError):
            _log.warning(
                "codex impl: could not measure the refused turn", exc_info=True
            )
        preserve_and_reset(
            self.worktree,
            review_artifacts=baseline.review_artifacts,
            ticket=self.task.ticket_id,
            client=self.client.name,
            cycle=_CYCLE,
            reason=reason,
        )
        return measured

    # -- steps --------------------------------------------------------------

    def prompt(
        self,
        snapshot: TicketSnapshot | None,
        plan_text: str | None,
        allowed_files: frozenset[str] | None,
        resume_note: str | None,
        merge_note: str | None,
    ) -> str | ImplBlock:
        """The rendered prompt, or a ``plan_missing_context`` block.

        The ticket text is the stage-entry snapshot's; none is a block.
        """
        docs_root = command_docs_root(IMPL_DOCS)
        if docs_root is None:
            return ImplBlock(
                PLAN_MISSING_CONTEXT_BLOCKER_REASON,
                "cw's command-doc tree (packaged cw/data/commands or the cw "
                "source checkout's .claude/commands) is not available",
            )
        if snapshot is None:
            return ImplBlock(
                PLAN_MISSING_CONTEXT_BLOCKER_REASON,
                f"no usable ticket snapshot for {self.task.ticket_id}: the row has "
                "no digest or its stored snapshot failed the trust read",
            )
        ticket = ticket_text_from_snapshot(snapshot)
        try:
            return render_impl_prompt(
                task=self.task,
                worktree=self.worktree,
                docs_root=docs_root,
                ticket=ticket,
                plan_text=plan_text,
                allowed_files=allowed_files,
                resume_note=resume_note,
                merge_note=merge_note,
                bindings_block=bindings_block_for(
                    self.client, Harness.CODEX, self.task.ticket_id
                ),
            )
        except RenderError as exc:
            return ImplBlock(
                PLAN_MISSING_CONTEXT_BLOCKER_REASON, f"render failed: {exc}"
            )

    def turn(self, prompt: str, scratch: Path) -> CodexRunResult | ImplBlock:
        """Launch the one write-capable IMPL turn in the worktree, with no timeout."""
        schema = scratch / _SCHEMA_NAME
        schema.write_text(json.dumps(IMPL_TURN_SCHEMA), encoding="utf-8")
        try:
            argv = build_impl_argv(
                schema_path=schema,
                output_path=scratch / _OUTPUT_NAME,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                sandbox_mode=self.sandbox_mode,
                other_servers=declared_server_names(CODEX_BACKEND) or (),
            )
        except TrackerIoConfigError as exc:
            return ImplBlock(_PRE_DISPATCH_TRACKER_MCP_REASON, str(exc))
        require_impl_sandbox_argv(argv, self.sandbox_mode)
        return self.runner.run(self.worktree, argv, None, stdin=prompt)

    @staticmethod
    def turn_failure(run: CodexRunResult) -> ImplBlock | None:
        """A failed launch: model capacity (retryable) or ``codex_error``."""
        if run.returncode == 0 and not run.timed_out:
            return None
        if _is_model_capacity_error(run):
            return ImplBlock(
                CODEX_MODEL_CAPACITY,
                "the IMPL turn hit a model capacity limit",
                retry_eligible=True,
            )
        tail = redact_and_cap(run.stderr.strip()[-_STDERR_TAIL_CHARS:])
        return ImplBlock(CODEX_ERROR, f"the IMPL turn exited {run.returncode}: {tail}")


# -- pre-turn ----------------------------------------------------------------


def _read_plan(worktree: Path) -> str | None:
    try:
        return (worktree / _PLAN_RELATIVE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _merge_note(files: tuple[str, ...], row_base: str | None) -> str | None:
    """The merge-conflict note; names ``origin/<base>`` only for a based row."""
    if not files:
        return None
    listing = "\n".join(f"- {path}" for path in files)
    target = f"origin/{row_base}" if row_base is not None else "origin's default branch"
    return (
        f"{target} conflicts with this branch in:\n"
        f"{listing}\n"
        "cw has started the merge and left the conflicts in the worktree. "
        "Resolve them (edit the files; do not commit, stage or abort)."
    )


def _guards(run: _Run) -> tuple[str | None, GitFacts] | ImplBlock:
    """The plan and branch guards: ``(plan text, git facts)`` or the block."""
    plan_text = _read_plan(run.worktree)
    block = check_plan_present(plan_text, run.task)
    if block is None:
        expected = resolve_feature_branch(run.task.ticket_id, run.client, run.task)
        # stays on client.default_branch: the default-branch refusal is the trust root.
        block = check_on_branch(
            run.worktree,
            expected,
            default_branch=run.client.default_branch,
            base_branch=run.base_branch,
        )
    if block is None:
        block = verify_review_feedback(run.task, run.worktree)
    if block is not None:
        return block
    return plan_text, git_facts(run.worktree, run.base_branch)


def _allowed_files(
    run: _Run, plan_text: str | None, facts: GitFacts, approved: list[str]
) -> frozenset[str] | ImplBlock | None:
    """The file fence: the plan's manifest, or a no-plan repair's branch files."""
    if plan_text is not None:
        return fix_scope_allowlist(plan_text, approved)
    allowed = repair_allowed_files(
        run.worktree, fork_point=facts["fork_point"], approved=approved
    )
    if allowed is None:
        return ImplBlock(
            IMPL_FAILED,
            "no-plan repair entry has no validated files from the reviewed "
            "branch or an approved scope-drift allowance; cw will not launch "
            "an unbounded repair turn",
        )
    return allowed


def _resume_note(run: _Run, reentry: ReentryPlan | None, facts: GitFacts) -> str | None:
    """The resume section body for a re-entry, else ``None``."""
    if reentry is None:
        return None
    files = repair_allowed_files(
        run.worktree, fork_point=facts["fork_point"], approved=()
    )
    return resume_note(reentry, facts, files or ())


def _assemble(
    run: _Run,
    snapshot: TicketSnapshot | None,
    recovered: list[str],
    guarded: tuple[str | None, GitFacts],
    reentry: ReentryPlan | None,
) -> _Pre | ImplBlock:
    """The probe, the scope fence and the rendered prompt for a turn-bound run."""
    plan_text, facts = guarded
    adopted = reentry is not None and reentry.merge is not None
    probe = (
        ProbeOutcome()
        if adopted
        else probe_default_branch(
            run.worktree, base_branch=run.base_branch, repair=run.repair
        )
    )
    if probe.block is not None:
        return probe.block
    # #3772: the detached driver re-loads the stored row, whose transport the
    # spawn-success stamp cleared, so re-derive it from the verified ledger here
    # (before the gate reads it). No-op outside IMPL or with an empty ledger.
    carry_scope_grants(
        run.task,
        run.client,
        run.worktree,
        repo=_resolve_repo_slug(run.worktree) if run.task.scope_grants else None,
    )
    approved, approval_friction = approved_extra(run.worktree, run.task)
    allowed = _allowed_files(run, plan_text, facts, approved)
    if isinstance(allowed, ImplBlock):
        return allowed
    prompt = run.prompt(
        snapshot,
        plan_text,
        allowed,
        _resume_note(run, reentry, facts),
        _merge_note(probe.conflicted_files, run.task.base_branch),
    )
    if isinstance(prompt, ImplBlock):
        return prompt
    friction = [
        *recovered,
        *probe.friction,
        *approval_friction,
        *(reentry.friction if reentry is not None else ()),
    ]
    return _Pre(
        plan_text,
        facts,
        prompt,
        probe.needs_merge,
        allowed or frozenset(),
        frozenset(approved),
        friction,
        reentry,
    )


def _prepare(run: _Run, scratch: Path) -> _Pre | ImplBlock:
    """Step 1: refresh, recovery, the guards, the re-entry plan and the prompt."""
    snapshot = snapshot_for_stage(
        run.task,
        run.client,
        run.worktree,
        stage="impl",
        courier=courier_refresher(run.runner),
    )
    # recover_plan itself skips a present plan and an adopted repair entry.
    ctx = context_for_client(run.client, run.worktree, resolve_login=False)
    source = recover_plan(run.task, run.client, run.worktree, snapshot, ctx)
    recovered = [f"{PLAN_RECOVERED_FRICTION}: {source}"] if source else []
    guarded = _guards(run)
    if isinstance(guarded, ImplBlock):
        return note_plan_identity(
            guarded, unresolved=plan_identity_unresolved(snapshot, ctx)
        )
    plan_text, facts = guarded
    reentry = plan_reentry(
        task=run.task,
        worktree=run.worktree,
        base_branch=run.base_branch,
        facts=facts,
        comments=bound_operator_comments(snapshot, ctx),
        scratch=scratch,
    )
    if isinstance(reentry, ImplBlock):
        return reentry
    if reentry is not None and reentry.disposition is Disposition.ADVANCE:
        friction = [*recovered, *reentry.friction]
        return _Pre(
            plan_text, facts, "", False, frozenset(), frozenset(), friction, reentry
        )
    return _assemble(run, snapshot, recovered, guarded, reentry)


def _begin_merge(run: _Run, pre: _Pre) -> MergeOutcome | ImplBlock | None:
    """The adopted merge, else the conflict merge cw starts (repair rows only)."""
    if pre.resume is not None and pre.resume.merge is not None:
        return pre.resume.merge.outcome
    if not pre.needs_merge:
        return None
    return merge_default_on_conflict(run.worktree, base_branch=run.base_branch)


def _start(run: _Run, pre: _Pre) -> _Flow | ImplBlock:
    """Step 2: the clean-start baseline, the merge and the pinned gates."""
    adopted = pre.resume.merge if pre.resume is not None else None
    baseline = capture_cycle_baseline(
        run.worktree,
        tolerated=tolerated_paths(run.client),
        merge_files=adopted.outcome.merge_files if adopted is not None else None,
    )
    if isinstance(baseline, DirtyStart):
        return dirty_start_block(baseline)
    merge = _begin_merge(run, pre)
    if isinstance(merge, ImplBlock):
        return merge
    scope = GateScope(
        ticket_id=run.task.ticket_id,
        client=run.client.name,
        session_id=run.sid,
        cycle=_CYCLE,
    )
    return _Flow(
        pre=pre,
        baseline=baseline,
        base=pre.facts["fork_point"] or baseline.head_sha,
        merge=merge,
        gate_plan=plan_cycle_gates(run.worktree, scope),
        scope=scope,
        # stays on client.default_branch: tests.prepare runs on the host, so
        # only the trust-root branch may supply it (#3531).
        prepare=pin_prepare(run.worktree, run.client.default_branch),
        conflict_before=(
            conflict_fingerprints(run.worktree, merge.conflicted)
            if merge is not None
            else {}
        ),
    )


# -- post-turn ---------------------------------------------------------------


def _growth_block(run: _Run, flow: _Flow) -> ImplBlock | None:
    """The growth guard: repair rows only, governed by the lane switch."""
    config = load_effective_config()
    if not run.repair or not _resolve_codex_fix_loop_growth_guard_enabled(
        run.client, run.task, config
    ):
        return None
    baseline = flow.baseline
    if flow.merge is not None:
        baseline = CycleBaseline(
            baseline.head_sha, flow.merge.growth_tree, baseline.review_artifacts
        )
    breach = check_growth_budget(
        run.worktree,
        baseline,
        open_findings=[],
        budget_lines=config.codex_fix_loop_growth_budget_lines,
        cycle=_CYCLE,
    )
    return fence_block(breach) if breach is not None else None


def _merge_files(flow: _Flow) -> frozenset[str]:
    return flow.merge.merge_files if flow.merge is not None else frozenset()


def _no_change_block(run: _Run) -> ImplBlock:
    """The turn changed nothing: a repair row's wording, else the resume's."""
    details = _NO_CHANGE_DETAILS if run.repair else _RESUME_NO_CHANGE_DETAILS
    return ImplBlock(IMPL_FAILED, details)


def _fence_blocks(run: _Run, flow: _Flow, touched: set[str]) -> ImplBlock | None:
    """Gate 1 (empty diff), the file fence and the growth guard."""
    if (run.repair or flow.pre.resume is not None) and not touched:
        return _no_change_block(run)
    block = check_empty_diff(run.worktree, flow.base)
    if block is not None:
        return block
    if flow.pre.plan_text is None:
        files, lines = staged_totals(run.worktree, flow.base)
        tier = resolve_review_scope_tier(None, run.task.scope_hint, (files, lines))
        if tier is None:
            return ImplBlock(
                IMPL_FAILED,
                "no-plan repair entry has no resolvable scope tier for its "
                "sensitive-path check",
            )
        # stays on client.default_branch: the sensitive-files registry is the root.
        block = no_plan_sensitive_block(
            run.worktree,
            touched=touched,
            allowed_files=flow.pre.allowed | _merge_files(flow),
            scope_tier=tier,
            default_branch=run.client.default_branch,
        )
        if block is not None:
            return block
    allowed = fix_scope_allowlist(
        flow.pre.plan_text, flow.pre.approved | _merge_files(flow)
    )
    if flow.pre.plan_text is None:
        allowed = flow.pre.allowed | _merge_files(flow)
    breach = check_fix_fence(
        run.worktree,
        base_branch=run.base_branch,
        allowed_files=allowed,
        finding_files=frozenset(),
        cycle=_CYCLE,
        touched=touched,
    )
    if breach is not None:
        return fence_block(breach)
    return _growth_block(run, flow)


def _gate_blocks(
    run: _Run, flow: _Flow, touched: set[str], scratch: Path
) -> ImplBlock | None:
    """Gate 2 (scope conformance), the repo's quality gates, then gate 4.

    Gate 2 is handed the touched list minus the conflict merge's merge-side
    files: cw's merge brought those in, the model did not edit them (the fence
    and the growth guard still cover them). A resumed run adds the files of the
    commits already on the branch.
    """
    scoped = touched - _merge_files(flow)
    if flow.pre.resume is not None:
        # The earlier commits are scope-checked too (#3529).
        scoped |= (
            repair_allowed_files(
                run.worktree, fork_point=flow.pre.facts["fork_point"], approved=()
            )
            or frozenset()
        )
    block, friction = run_scope_conformance(
        run.worktree,
        task=run.task,
        plan_text=flow.pre.plan_text,
        plan_path=run.worktree / _PLAN_RELATIVE,
        touched=scoped,
        scratch=scratch,
    )
    flow.tokens.extend(friction)
    if block is not None:
        return block
    return run_quality_gates(
        flow.gate_plan,
        worktree=run.worktree,
        baseline=flow.baseline,
        scope=flow.scope,
        tokens=flow.tokens,
        touched=touched,
    ) or run_type_check_gate(run, flow, touched)


def _commit(run: _Run, flow: _Flow, touched: set[str]) -> ImplBlock | None:
    """Stage, check the staged set, and make cw's one commit (one re-stage retry)."""
    artifacts = flow.baseline.review_artifacts
    stage_cycle(run.worktree, artifacts)
    staged = _staged_paths(run.worktree)
    # An accepted gate rewrite can change the measured set (#3686); the
    # pre-gate set only bounds it.
    measured = frozenset(cycle_touched_paths(run.worktree, flow.baseline)) & touched
    if staged != measured:
        breach = staged_set_breach(
            measured, staged, _CYCLE, start_head=flow.baseline.head_sha
        )
        return fence_block(breach)
    if not measured:
        return _no_change_block(run)
    message = impl_commit_message(run.task, repair=run.repair)
    try:
        _git_commit(run.worktree, message)
    except subprocess.CalledProcessError:
        # A hook that rewrites files exits non-zero on the run it changes
        # something; re-stage what it rewrote and retry exactly once.
        stage_cycle(run.worktree, artifacts)
        try:
            _git_commit(run.worktree, message)
        except subprocess.CalledProcessError as exc:
            return commit_failure_block(as_hook_failure(run.worktree, exc, _CYCLE))
    return None


def _push(run: _Run) -> ImplBlock | None:
    """Push HEAD and verify origin's tip; ``impl_not_pushed`` on any failure."""
    sha = git_output(["rev-parse", "HEAD"], cwd=run.worktree).strip()
    try:
        push_and_verify_head(run.worktree, sha)
    except subprocess.CalledProcessError as exc:
        tail = redact_and_cap(
            (exc.stderr or exc.stdout or "").strip()[-_STDERR_TAIL_CHARS:]
        )
        return ImplBlock(
            IMPL_NOT_PUSHED,
            f"the IMPL commit {sha[:12]} was made locally but not pushed "
            f"(git exit {exc.returncode}): {tail}. The local commit remains.",
        )
    return None


def _merge_marker_block(run: _Run, flow: _Flow) -> ImplBlock | None:
    """A conflicted path the turn left unresolved; before anything is staged."""
    if flow.merge is None or not flow.merge.conflicted:
        return None
    return merge_unresolved_block(
        run.worktree, flow.merge.conflicted, flow.conflict_before
    )


def _after_turn(
    run: _Run, flow: _Flow, payload: dict[str, Any], scratch: Path
) -> AutoDevResult:
    """Steps 4-5 for a parsed, schema-valid, completing turn."""
    # First, before the first staging: cw's `git add -A` (reached through
    # cycle_touched_paths) marks every unmerged path resolved, and the turn
    # cannot write the index, so the check is only discriminating until then.
    unresolved = _merge_marker_block(run, flow)
    if unresolved is not None:
        return run.refused(unresolved, flow)
    touched = cycle_touched_paths(run.worktree, flow.baseline)
    block = (
        _fence_blocks(run, flow, touched)
        or _gate_blocks(run, flow, touched, scratch)
        or _commit(run, flow, touched)
    )
    if block is not None:
        return run.refused(block, flow)
    # The commit exists, so `blocked` (never `refused`, which would reset it).
    unconcluded = merge_unconcluded_block(run.worktree)
    if unconcluded is not None:
        return run.blocked(unconcluded)
    pushed = _push(run)
    if pushed is not None:
        return run.blocked(pushed)
    return finish(run, flow.pre, flow.tokens, payload)


def _turn_head_block(run: _Run, flow: _Flow) -> ImplBlock | None:
    """Refuse a turn that moved HEAD before cw could stage its changes."""
    try:
        head = git_output(["rev-parse", "HEAD"], cwd=run.worktree).strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        return ImplBlock(
            IMPL_FAILED,
            "cw could not verify HEAD after the IMPL turn; no commit or push "
            f"was attempted: {redact_and_cap(str(exc))}",
        )
    if head == flow.baseline.head_sha:
        return None
    # A model-created commit (or any other HEAD movement) cannot be safely
    # restored by the cycle ref; preserve the non-restorable staged-set breach
    # wording and leave the branch for operator inspection.
    breach = staged_set_breach(
        frozenset({"HEAD"}),
        frozenset(),
        _CYCLE,
        start_head=flow.baseline.head_sha,
    )
    return fence_block(breach)


def _run_turn(run: _Run, flow: _Flow, scratch: Path) -> AutoDevResult:
    """Step 3: launch the turn, parse it, and hand a completing turn on."""
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_IMPL_STARTED,
        prev_stage=STAGE_PLAN_REVIEWED,
    )
    turn = run.turn(flow.pre.prompt, scratch)
    if isinstance(turn, ImplBlock):
        return run.blocked(turn)
    head_block = _turn_head_block(run, flow)
    if head_block is not None:
        return run.blocked(head_block)
    failed = run.turn_failure(turn)
    if failed is not None:
        return run.refused(failed, flow)
    parsed = parse_impl_turn(turn.output_file_content)
    if isinstance(parsed, ImplTurnFailure):
        block = ImplBlock(parsed.reason, parsed.details)
        return run.refused(block, flow)
    early = early_stamp(run, flow.pre, parsed)
    if isinstance(early, ImplBlock):
        return run.refused(early, flow)
    return (
        run.pass_through(early, flow)
        if early.status == IMPL_BLOCKED
        else _after_turn(run, flow, parsed, scratch)
    )


def _advance(run: _Run, pre: _Pre) -> AutoDevResult:
    """The ``advance`` disposition: no turn, no gates; push HEAD and stamp from git.

    Reached only when HEAD itself carries the ``impl-complete`` trailer, so a
    gated run already declared the tip complete.
    """
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_IMPL_STARTED,
        prev_stage=STAGE_PLAN_REVIEWED,
    )
    pushed = _push(run)
    if pushed is not None:
        return run.blocked(pushed)
    return finish(run, pre, [], advance_payload(pre.facts))


def _impl(run: _Run, scratch: Path) -> AutoDevResult:
    """The whole flow: pre-turn, baseline, the turn and everything after it."""
    pre = _prepare(run, scratch)
    if isinstance(pre, ImplBlock):
        return run.blocked(pre)
    if pre.resume is not None and pre.resume.disposition is Disposition.ADVANCE:
        return _advance(run, pre)
    flow = _start(run, pre)
    if isinstance(flow, ImplBlock):
        return run.blocked(flow)
    return _run_turn(run, flow, scratch)


def _execute(run: _Run) -> None:
    """Run the flow, complete through the door, then emit ``session.completed``."""
    scratch = _codex_scratch_dir(uuid.uuid4().hex)
    try:
        result = _impl(run, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    payload = with_usage_friction(result.model_dump(mode="json"), run.usage)
    try:
        complete_session_through_door(run.sid, payload, usage=run.usage)
    except STAGE_FAULTS:
        # A pushed IMPL commit must not be stranded by a transient completion
        # failure. The door is idempotent: a retry either writes the result or
        # observes the first write and still completes the session.
        _log.warning(
            "codex impl: retrying completion after post-push failure "
            "session=%s ticket=%s",
            run.sid,
            run.task.ticket_id,
            exc_info=True,
        )
        complete_session_through_door(run.sid, payload, usage=run.usage)
    record_event_best_effort(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "session_id": run.sid,
            "ticket_id": run.task.ticket_id,
            "session_name": run.sess_name,
        },
        run.task.ticket_id,
    )


def run_impl_stage_and_complete(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    sid: str,
    sess_name: str,
    model: str | None,
    reasoning_effort: str | None,
    sandbox_mode: SandboxMode,
    usage: UsageAccumulator = NO_USAGE,
) -> None:
    """Run the codex IMPL stage for *task* and complete session *sid*.

    *model*, *reasoning_effort* and *sandbox_mode* are the IMPL stage's
    resolved ``StageExecutorConfig`` values; *sandbox_mode* is the lane's
    ``fix_sandbox_mode`` (the turn edits files). No timeout is read.
    """
    run = _Run(
        runner=runner,
        task=task,
        client=client,
        worktree=worktree,
        sid=sid,
        sess_name=sess_name,
        model=model,
        reasoning_effort=reasoning_effort,
        sandbox_mode=sandbox_mode,
        usage=usage,
    )
    run_guarded_stage(
        client_name=client.name,
        task=task,
        worktree=worktree,
        sid=sid,
        stage_reached=STAGE2_IMPL,
        body=lambda: _execute(run),
        usage=usage,
    )


__all__ = ["run_impl_stage_and_complete"]
