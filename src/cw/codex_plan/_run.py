"""The codex PLAN stage, end to end (#3060, RFC 0018 C1).

:func:`run_plan_stage_and_complete` is what ``cw codex run --stage plan``
runs. Everything happens inside :func:`cw.codex_stage_completion.run_guarded_stage`,
so a stage fault completes the session as ``unexpected_error`` at
``stage1_plan`` and returns the claimed row to PENDING. The ordered flow:

1. Resolve the tracker. A non-GitHub tracker must have a usable codex MCP
   server (the run-time backstop of the pre-dispatch gate), or the run blocks
   ``tracker_mcp_unavailable_pre_dispatch``.
2. Read the tracker state (this run's live GitHub snapshot, or the row's
   stored snapshot elsewhere) and a resumable draft. A resumed draft goes
   through the resume gate (:mod:`cw.codex_plan._resume`): the resolutions
   delta and its revision, then the approved-fingerprint fast path, which
   runs no turn at all.
3. Otherwise resolve cw's command-doc tree, prefetch the GitHub ticket and
   render the prompt (any failure blocks ``plan_missing_context``), then
   launch one read-only ``codex exec`` turn with no timeout (ADR-0014).
4. Parse the ``-o`` document strictly and stamp the sentinel.
5. The model's text, cleaned, is the plan; cw validates its settlement
   claims, settles the tracker fingerprint, checks the deferred stubs and
   runs the round counter and cap.
6. Gate set B, then the two review stations (binding on a completing exit,
   advisory on a park), then the artifacts and the fingerprint binding.
7. Record plan decisions, complete through the door, post, and emit
   ``session.completed``.

Every ``blocked`` result goes through :meth:`_Run.blocked`, which emits one
``stage.errored`` event. The three stage events are driver-emitted, never
model-emitted.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from cw.auto_dev_result.schema._vocab import (
    AMBIGUITY_SCAN_UNCONVERGED_BLOCKER_REASON,
    DEFERRED_STUB_UNRESOLVED_BLOCKER_REASON,
    PLAN_MISSING_CONTEXT_BLOCKER_REASON,
)
from cw.backend_mcp_config import McpState, declared_server_names, read_backend_mcp
from cw.bindings_block import bindings_block_for, effective_tickets_system
from cw.codex_plan._argv import build_plan_argv, read_tools_for, require_read_only_argv
from cw.codex_plan._exits import (
    _complete,
    _consolidated_block,
    _Exit,
    _gate,
    _park,
    _with_friction,
)
from cw.codex_plan._post import post_plan_comment
from cw.codex_plan._prompt import (
    PLAN_TURN_SCHEMA,
    ResumeFacts,
    command_docs_root,
    prefetch_ticket,
    render_plan_prompt,
)
from cw.codex_plan._result import (
    PARK_STATUSES,
    PLAN_BLOCKED,
    PLAN_NO_OP,
    RESUME_FRICTION,
    STAGE_PLAN_GENERATED,
    STAGE_SCAN_SKIPPED,
    PlanTurnFailure,
    clean_plan_text,
    on_disk_leading_block,
    parse_plan_turn,
    plan_blocked,
    read_resume_draft,
)
from cw.codex_plan._resume import (
    EXIT_STATUSES,
    GateOutcome,
    RoundBook,
    TrackerState,
    cap_check,
    resume_gate,
    settle_fingerprint,
    synthesize_fast_path_result,
    tracker_state,
    unconverged_details,
)
from cw.codex_plan._settle import (
    SettleOutcome,
    unresolved_stubs,
    validate_settlements,
)
from cw.codex_review import (
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    STAGE1_PLAN,
    _codex_scratch_dir,
    _is_model_capacity_error,
)
from cw.codex_review_signals import record_event_best_effort
from cw.codex_stage_completion import (
    complete_session_through_door,
    flush_late_usage,
    run_guarded_stage,
    with_usage_friction,
)
from cw.codex_usage import NO_USAGE, UsageAccumulator
from cw.contract_manifest import RenderError
from cw.dev_queue import _PRE_DISPATCH_TRACKER_MCP_REASON
from cw.exceptions import CwError
from cw.models import CODEX_BACKEND, OrchestratorEventType
from cw.models.bindings import Harness, TicketsBinding
from cw.models.enums import StageIdentifier
from cw.plan_fingerprint import compute_plan_draft_fingerprint
from cw.stage_verification.plan_gates import (
    ApprovalCheck,
    GateFailure,
    check_large_tier_approval,
)
from cw.ticket_snapshot import context_for_client, record_snapshot_from_github
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.tracker_io import TrackerIoConfigError
from cw.tracker_io._argv import validate_server_name

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_plan._result import PlanSettlement, ResumeDraft
    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import ClientConfig, TicketTask
    from cw.ticket_snapshot import SnapshotContext, TicketSnapshot

_log = logging.getLogger(__name__)


def _record_ticket_snapshot(
    ticket_id: str, client_name: str, cwd: Path
) -> TicketSnapshot | None:
    """Fetch and record this run's GitHub snapshot; ``None`` on failure (non-fatal).

    The returned snapshot is the one the resume gate reads (#3464): never the
    row's stored snapshot, which has no freshness check and after a failed
    fetch would be the previous round's. The PLAN prompt is still built from
    ``prefetch_ticket``, a separate ``gh`` call, so the two can differ if the
    issue changed between calls (named T1 limitation, source skew). Sources
    for the log are the row (``task.ticket_id``, ``task.client``).
    """
    try:
        return record_snapshot_from_github(ticket_id, client_name, cwd).snapshot
    except (CwError, OSError):
        _log.warning(
            "codex plan: ticket snapshot not recorded ticket_id=%s client=%s",
            ticket_id,
            client_name,
            exc_info=True,
        )
        return None


#: The intake stage identifier (the ``prev_stage`` of the generated event).
STAGE_INTAKE: Final = StageIdentifier.S0_INTAKE.value

_SCHEMA_NAME: Final = "plan-schema.json"
_OUTPUT_NAME: Final = "plan-output.json"


@dataclass(frozen=True)
class _McpTarget:
    """The turn's tracker read path: GitHub prefetch, or one MCP server."""

    server: str | None = None
    tools: tuple[str, str] | None = None

    @property
    def github(self) -> bool:
        return self.server is None


@dataclass(frozen=True)
class _Run:
    """Everything one PLAN run needs; methods are the flow's steps."""

    runner: CodexRunner
    task: TicketTask
    client: ClientConfig
    worktree: Path
    sid: str
    sess_name: str
    model: str | None
    reasoning_effort: str | None
    usage: UsageAccumulator = NO_USAGE
    book: RoundBook = field(default_factory=RoundBook)

    # -- events -------------------------------------------------------------

    def emit(
        self,
        event_type: OrchestratorEventType,
        stage: str,
        *,
        prev_stage: str | None = None,
        error_kind: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Record one stage event, best-effort: telemetry never changes the result."""
        payload: dict[str, object] = {
            "session_id": self.sid,
            "ticket_id": self.task.ticket_id,
            "stage": stage,
        }
        if prev_stage is not None:
            payload["prev_stage"] = prev_stage
        if error_kind is not None:
            payload["error_kind"] = error_kind
        if reason is not None:
            payload["reason"] = reason
        payload["started_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        record_event_best_effort(event_type, payload, self.task.ticket_id)

    def blocked(
        self,
        stage: str,
        reason: str,
        details: str,
        *,
        retry_eligible: bool = False,
        plan_draft_fingerprint: str | None = None,
    ) -> _Exit:
        """A cw-built blocked exit; emits its one ``stage.errored``."""
        result = plan_blocked(
            ticket_id=self.task.ticket_id,
            worktree=self.worktree,
            reason=reason,
            details=details,
            retry_eligible=retry_eligible,
            plan_draft_fingerprint=plan_draft_fingerprint,
        )
        self.emit(OrchestratorEventType.STAGE_ERRORED, stage, error_kind=reason)
        return _Exit(result)

    def gate_blocked(self, failure: GateFailure, stage: str) -> _Exit:
        return self.blocked(stage, failure.reason, failure.details)

    # -- steps --------------------------------------------------------------

    def mcp_target(self) -> _McpTarget | _Exit:
        """The tracker read path, or the run-time tracker-MCP backstop's block."""
        system = resolve_tracker(self.client.workspace_path) or None
        tickets = self.client.bindings.tickets
        effective_system = effective_tickets_system(tickets, system)
        if effective_system == TRACKER_GITHUB_ISSUES:
            return _McpTarget()
        if tickets is None:
            if not system:
                return _McpTarget()
            return self._mcp_blocked(f"tracker {system!r}: tracker_mcp_gate unset")
        return self._binding_target(tickets, effective_system)

    def _binding_target(
        self, tickets: TicketsBinding, effective_system: str | None
    ) -> _McpTarget | _Exit:
        """The codex MCP read target named by a tickets binding."""
        tracker = effective_system or ""
        server = tickets.mcp_server(CODEX_BACKEND) or ""
        try:
            validate_server_name(server)
            tools = read_tools_for(tracker, tickets, CODEX_BACKEND)
        except TrackerIoConfigError as exc:
            return self._mcp_blocked(f"tracker {tracker!r}: {exc}")
        reading = read_backend_mcp(CODEX_BACKEND, server, self.client.workspace_path)
        if reading.state in (McpState.ABSENT, McpState.DISABLED):
            state = reading.state.value
            return self._mcp_blocked(
                f"codex MCP server {server!r} is {state}: {reading.reason}"
            )
        return _McpTarget(server=server, tools=tools)

    def _mcp_blocked(self, details: str) -> _Exit:
        return self.blocked(
            STAGE_PLAN_GENERATED, _PRE_DISPATCH_TRACKER_MCP_REASON, details
        )

    def prompt(
        self, target: _McpTarget, resume: ResumeDraft | None, facts: ResumeFacts
    ) -> str | _Exit:
        """The rendered prompt, or a ``plan_missing_context`` block."""
        docs_root = command_docs_root()
        if docs_root is None:
            return self.blocked(
                STAGE_PLAN_GENERATED,
                PLAN_MISSING_CONTEXT_BLOCKER_REASON,
                "cw's command-doc tree (packaged cw/data/commands or the cw "
                "source checkout's .claude/commands) is not available",
            )
        ticket = None
        if target.github:
            ticket = prefetch_ticket(
                self.task.ticket_id, cwd=self.client.workspace_path
            )
            if ticket is None:
                return self.blocked(
                    STAGE_PLAN_GENERATED,
                    PLAN_MISSING_CONTEXT_BLOCKER_REASON,
                    f"gh could not fetch ticket {self.task.ticket_id} body/comments",
                )
        try:
            return render_plan_prompt(
                ticket_id=self.task.ticket_id,
                docs_root=docs_root,
                ticket=ticket,
                resume=resume,
                bindings_block=bindings_block_for(
                    self.client, Harness.CODEX, self.task.ticket_id
                ),
                facts=facts,
            )
        except RenderError as exc:
            return self.blocked(
                STAGE_PLAN_GENERATED,
                PLAN_MISSING_CONTEXT_BLOCKER_REASON,
                f"render failed: {exc}",
            )

    def turn(
        self,
        prompt: str,
        target: _McpTarget,
        *,
        schema: dict[str, Any] = PLAN_TURN_SCHEMA,
    ) -> CodexRunResult | _Exit:
        """Launch one read-only turn (the scan, or a revision) in a fresh scratch dir.

        *schema* is the strict ``-o`` schema: the scan's by default, the
        revision turn's for :func:`cw.codex_plan._revise.revise_plan`.
        """
        scratch = _codex_scratch_dir(uuid.uuid4().hex)
        try:
            schema_path = scratch / _SCHEMA_NAME
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            try:
                argv = build_plan_argv(
                    schema_path=schema_path,
                    output_path=scratch / _OUTPUT_NAME,
                    model=self.model,
                    reasoning_effort=self.reasoning_effort,
                    mcp_server=target.server,
                    read_tools=target.tools,
                    other_servers=declared_server_names(CODEX_BACKEND) or (),
                )
            except TrackerIoConfigError as exc:
                return self._mcp_blocked(str(exc))
            require_read_only_argv(argv)
            return self.runner.run(self.worktree, argv, None, stdin=prompt)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def turn_failure(self, run: CodexRunResult) -> _Exit | None:
        """A failed launch: model capacity (retryable) or ``codex_error``."""
        if run.returncode == 0 and not run.timed_out:
            return None
        if _is_model_capacity_error(run):
            return self.blocked(
                STAGE_PLAN_GENERATED,
                CODEX_MODEL_CAPACITY,
                "the PLAN turn hit a model capacity limit",
                retry_eligible=True,
            )
        tail = run.stderr.strip()[-500:]
        return self.blocked(
            STAGE_PLAN_GENERATED,
            CODEX_ERROR,
            f"the PLAN turn exited {run.returncode}: {tail}",
        )


@dataclass(frozen=True)
class _Round:
    """What the round read before its turn: the tracker state and the draft."""

    state: TrackerState
    resume: ResumeDraft | None
    settlements: tuple[PlanSettlement, ...] = ()
    row_delta: bool = False


def _settle_claims(
    run: _Run, result: AutoDevResult, round_: _Round
) -> tuple[AutoDevResult, SettleOutcome]:
    """Validate the turn's settlement claims; accepted ones join the book."""
    if round_.resume is None:
        return result, SettleOutcome()
    state = round_.state
    outcome = validate_settlements(
        round_.settlements,
        park=state.park,
        settled=run.book.block.settled,
        comment_ids=[str(comment["id"]) for comment in state.observation.comments],
        row_rounds=[resolution.round for resolution in run.task.operator_resolutions],
    )
    run.book.update(settled=(*run.book.block.settled, *outcome.settled_entries()))
    return _with_friction(result, outcome.friction(state.park.ordinal)), outcome


def _route(
    run: _Run,
    plan_text: str,
    check: ApprovalCheck,
    outcome: SettleOutcome,
    round_: _Round,
) -> _Exit:
    """The stub check, then the cap, then park or complete."""
    stubs = unresolved_stubs(plan_text)
    if stubs:
        return _consolidated_block(
            run,
            plan_text,
            check,
            reason=DEFERRED_STUB_UNRESOLVED_BLOCKER_REASON,
            details="deferred premise stub(s) still unresolved after the scan: "
            + "; ".join(stubs),
            note="A deferred premise is still a PENDING stub after this scan, "
            "so the round blocks instead of parking or completing:\n\n"
            + "\n".join(stubs),
        )
    status = check.result.status
    if status not in PARK_STATUSES:
        return _complete(run, check.result, plan_text)
    if cap_check(run.book, status):
        details = unconverged_details(check.result, round_.state.observation)
        return _consolidated_block(
            run,
            plan_text,
            check,
            reason=AMBIGUITY_SCAN_UNCONVERGED_BLOCKER_REASON,
            details=details,
            note=f"The round cap is reached: {details}. Answer the items above, "
            "then re-dispatch.",
        )
    evidence = outcome.evidence()
    if evidence is not None and status in EXIT_STATUSES:
        stamped = check.result.model_copy(
            update={"resolution_consumed": True, "resolution_evidence": evidence}
        )
        check = replace(check, result=stamped)
    return _park(run, plan_text, check)


def _after_turn(
    run: _Run, plan_text_raw: str, result: AutoDevResult, round_: _Round
) -> _Exit:
    """Steps 5-7 for a parsed turn (or the fast path's synthesized result)."""
    if result.status == PLAN_NO_OP:
        return _Exit(result)
    if result.status == PLAN_BLOCKED:
        reason = result.blocker.reason if result.blocker is not None else PLAN_BLOCKED
        run.emit(
            OrchestratorEventType.STAGE_ERRORED, STAGE_PLAN_GENERATED, error_kind=reason
        )
        return _Exit(result)
    plan_text = clean_plan_text(plan_text_raw)
    friction = [RESUME_FRICTION] if round_.resume is not None else []
    result = _with_friction(result, [*friction, *round_.state.source.friction])
    failure = _gate(result, plan_text)
    if failure is not None:
        return run.gate_blocked(failure, STAGE_PLAN_GENERATED)
    check = check_large_tier_approval(
        result, run.task, compute_plan_draft_fingerprint(plan_text)
    )
    settled, outcome = _settle_claims(run, check.result, round_)
    settle_fingerprint(run.book, round_.state.observation, row_delta=round_.row_delta)
    return _route(run, plan_text, replace(check, result=settled), outcome, round_)


def _tracker_snapshot(
    run: _Run, target: _McpTarget, ctx: SnapshotContext
) -> TicketSnapshot | None:
    """This run's live GitHub snapshot, or the row's stored one elsewhere.

    On GitHub the stored snapshot is never read: a failed live fetch is the
    unobserved shape, not the previous round's snapshot.
    """
    if target.github:
        return _record_ticket_snapshot(
            run.task.ticket_id, run.task.client, run.client.workspace_path
        )
    return ctx.resolve(run.task, "plan")


def _fast_path(run: _Run, gate: GateOutcome, round_: _Round) -> _Exit:
    """The approved draft, taken with no turn: cw synthesizes the sentinel."""
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_PLAN_GENERATED,
        prev_stage=STAGE_INTAKE,
    )
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_SCAN_SKIPPED,
        prev_stage=STAGE_PLAN_GENERATED,
        reason=gate.reason,
    )
    result = synthesize_fast_path_result(run, gate.body)
    return _after_turn(run, gate.body, result, round_)


def _scan(run: _Run, target: _McpTarget, round_: _Round) -> _Exit:
    """Steps 3-4: render, launch and parse the scan turn."""
    facts = ResumeFacts(
        park_ordinal=round_.state.park.ordinal,
        settled_ids=tuple(e.partition(": ")[0] for e in run.book.block.settled),
        scan_round=run.book.block.scan_round,
    )
    prompt = run.prompt(target, round_.resume, facts)
    if isinstance(prompt, _Exit):
        return prompt
    turn = run.turn(prompt, target)
    if isinstance(turn, _Exit):
        return turn
    failed = run.turn_failure(turn)
    if failed is not None:
        return failed
    parsed = parse_plan_turn(
        turn.output_file_content, ticket_id=run.task.ticket_id, worktree=run.worktree
    )
    if isinstance(parsed, PlanTurnFailure):
        return run.blocked(STAGE_PLAN_GENERATED, parsed.reason, parsed.details)
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_PLAN_GENERATED,
        prev_stage=STAGE_INTAKE,
    )
    round_ = replace(round_, settlements=parsed.settlements)
    return _after_turn(run, parsed.plan_markdown, parsed.result, round_)


def _plan(run: _Run) -> _Exit:
    """Steps 1-2 (the tracker state and the resume gate), then the scan."""
    target = run.mcp_target()
    if isinstance(target, _Exit):
        return target
    ctx = context_for_client(run.client, run.worktree, resolve_login=False)
    if target.github:
        # The binding-first target read the ticket from GitHub (an unset
        # tracker included), so its live snapshot binds by the GitHub rules.
        ctx = replace(ctx, github=True)
    state = tracker_state(_tracker_snapshot(run, target, ctx), ctx, run.task)
    resume = read_resume_draft(run.worktree)
    round_ = _Round(state=state, resume=resume)
    if resume is None:
        return _scan(run, target, round_)
    gate = resume_gate(run, target, resume, state)
    if isinstance(gate, _Exit):
        return gate
    run = gate.run
    round_ = replace(round_, row_delta=gate.row_delta)
    if gate.body != resume.body:
        round_ = replace(round_, resume=replace(resume, body=gate.body))
    if gate.fast_path:
        return _fast_path(run, gate, round_)
    return _scan(run, target, round_)


def _execute(run: _Run) -> None:
    """Plan, complete through the door, post, then emit ``session.completed``."""
    exit_ = _plan(run)
    payload = with_usage_friction(exit_.result.model_dump(mode="json"), run.usage)
    complete_session_through_door(run.sid, payload, usage=run.usage)
    if exit_.post_text is not None:
        post_plan_comment(
            task=run.task,
            client=run.client,
            worktree=run.worktree,
            sid=run.sid,
            text=exit_.post_text,
            artifact_path=exit_.artifact_path,
            kind=exit_.kind,
        )
    flush_late_usage(run.sid, run.usage)
    record_event_best_effort(
        OrchestratorEventType.SESSION_COMPLETED,
        {
            "session_id": run.sid,
            "ticket_id": run.task.ticket_id,
            "session_name": run.sess_name,
        },
        run.task.ticket_id,
    )


def run_plan_stage_and_complete(
    *,
    runner: CodexRunner,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
    sid: str,
    sess_name: str,
    model: str | None,
    reasoning_effort: str | None,
    usage: UsageAccumulator = NO_USAGE,
) -> None:
    """Run the codex PLAN stage for *task* and complete session *sid*.

    *model* and *reasoning_effort* are the PLAN stage's resolved
    ``StageExecutorConfig`` values; no sandbox or timeout setting is read.
    *usage* (#3063) is the run's accumulator; its recording runner is already
    inside *runner*.
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
        usage=usage,
        book=RoundBook(on_disk_leading_block(worktree)),
    )
    run_guarded_stage(
        client_name=client.name,
        task=task,
        worktree=worktree,
        sid=sid,
        stage_reached=STAGE1_PLAN,
        body=lambda: _execute(run),
        usage=usage,
    )


__all__ = ["run_plan_stage_and_complete"]
