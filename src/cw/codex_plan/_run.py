"""The codex PLAN stage, end to end (#3060, RFC 0018 C1).

:func:`run_plan_stage_and_complete` is what ``cw codex run --stage plan``
runs. Everything happens inside :func:`cw.codex_stage_completion.run_guarded_stage`,
so a stage fault completes the session as ``unexpected_error`` at
``stage1_plan`` and returns the claimed row to PENDING. The ordered flow:

1. Resolve the tracker. A non-GitHub tracker must have a usable codex MCP
   server (the run-time backstop of the pre-dispatch gate), or the run blocks
   ``tracker_mcp_unavailable_pre_dispatch``.
2. Resolve cw's command-doc tree, prefetch the GitHub ticket, read a resumable
   draft and render the prompt; any failure blocks ``plan_missing_context``.
3. Launch one read-only ``codex exec`` turn with no timeout (ADR-0014).
4. Parse the ``-o`` document strictly and stamp the sentinel.
5. Choose the plan text: a resumed draft whose fingerprint the row approved
   is taken verbatim; otherwise the model's text, cleaned.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from cw.auto_dev_result.schema._vocab import PLAN_MISSING_CONTEXT_BLOCKER_REASON
from cw.backend_mcp_config import McpState, declared_server_names, read_backend_mcp
from cw.codex_plan._argv import build_plan_argv, read_tools_for, require_read_only_argv
from cw.codex_plan._gates import (
    CODEX_PLAN_ARTIFACT_MISSING,
    ApprovalCheck,
    GateFailure,
    check_artifact,
    check_large_tier_approval,
    check_plan_files,
    check_scope_tier,
    has_approval_evidence,
)
from cw.codex_plan._post import post_plan_comment, render_park_comment
from cw.codex_plan._prompt import (
    PLAN_TURN_SCHEMA,
    command_docs_root,
    prefetch_ticket,
    render_plan_prompt,
)
from cw.codex_plan._result import (
    DECISIONS_NOT_RECORDED_PREFIX,
    MODEL_TEXT_DISCARDED_FRICTION,
    PARK_STATUSES,
    PLAN_BLOCKED,
    PLAN_MD_RELATIVE_PATH,
    PLAN_NO_OP,
    RESUME_FRICTION,
    PlanDraftError,
    PlanTurnFailure,
    ResumeDraft,
    clean_plan_text,
    parse_plan_turn,
    plan_blocked,
    read_resume_draft,
    stamp_plan_result,
    write_plan_draft,
    write_plan_md,
)
from cw.codex_plan._stations import run_plan_stations, stamp_markers
from cw.codex_review import (
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    STAGE1_PLAN,
    _codex_scratch_dir,
    _is_model_capacity_error,
)
from cw.codex_stage_completion import complete_session_through_door, run_guarded_stage
from cw.contract_manifest import RenderError
from cw.dev_queue import _PRE_DISPATCH_TRACKER_MCP_REASON
from cw.dev_queue.lifecycle import _plan_body_signoff_ok
from cw.dev_queue.operator_constraints import record_plan_constraints
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import CODEX_BACKEND, OrchestratorEventType, Stage
from cw.models.enums import StageIdentifier
from cw.plan_decisions import parse_plan_decisions
from cw.plan_fingerprint import PLAN_DRAFT_RELATIVE_PATH, compute_plan_draft_fingerprint
from cw.tracker import TrackerClass, tracker_kind
from cw.tracker_io import TrackerIoConfigError
from cw.tracker_io._argv import validate_server_name

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)

NO_OP: Final = PLAN_NO_OP

#: The stage identifiers the driver's stage events carry.
STAGE_INTAKE: Final = StageIdentifier.S0_INTAKE.value
STAGE_PLAN_GENERATED: Final = StageIdentifier.S1_PLAN_GENERATED.value
STAGE_PLAN_REVIEWED: Final = StageIdentifier.S1_PLAN_REVIEWED.value

_SCHEMA_NAME: Final = "plan-schema.json"
_OUTPUT_NAME: Final = "plan-output.json"


@dataclass(frozen=True)
class _McpTarget:
    """The turn's tracker read path: GitHub prefetch, or one MCP server."""

    server: str | None = None
    tracker: str | None = None

    @property
    def github(self) -> bool:
        return self.server is None


@dataclass(frozen=True)
class _Exit:
    """What a run ends with: the result, and the comment to post (if any)."""

    result: AutoDevResult
    post_text: str | None = None
    artifact_path: Path | None = None


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

    # -- events -------------------------------------------------------------

    def emit(
        self,
        event_type: OrchestratorEventType,
        stage: str,
        *,
        prev_stage: str | None = None,
        error_kind: str | None = None,
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
        payload["started_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _record_best_effort(event_type, payload, self.task.ticket_id)

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
        kind = tracker_kind(self.client.workspace_path)
        if kind.tracker_class is not TrackerClass.OTHER or kind.system is None:
            return _McpTarget()
        gate = self.client.tracker_mcp_gate
        if gate is None:
            return self._mcp_blocked(f"tracker {kind.system!r}: tracker_mcp_gate unset")
        server = (gate.servers or {}).get(CODEX_BACKEND, "")
        try:
            validate_server_name(server)
            read_tools_for(kind.system)
        except TrackerIoConfigError as exc:
            return self._mcp_blocked(f"tracker {kind.system!r}: {exc}")
        reading = read_backend_mcp(CODEX_BACKEND, server, self.client.workspace_path)
        if reading.state in (McpState.ABSENT, McpState.DISABLED):
            state = reading.state.value
            return self._mcp_blocked(
                f"codex MCP server {server!r} is {state}: {reading.reason}"
            )
        return _McpTarget(server=server, tracker=kind.system)

    def _mcp_blocked(self, details: str) -> _Exit:
        return self.blocked(
            STAGE_PLAN_GENERATED, _PRE_DISPATCH_TRACKER_MCP_REASON, details
        )

    def prompt(
        self, target: _McpTarget, resume: ResumeDraft | None, approval: bool
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
                approval_matches=approval,
            )
        except RenderError as exc:
            return self.blocked(
                STAGE_PLAN_GENERATED,
                PLAN_MISSING_CONTEXT_BLOCKER_REASON,
                f"render failed: {exc}",
            )

    def turn(self, prompt: str, target: _McpTarget) -> CodexRunResult | _Exit:
        """Launch the one read-only PLAN turn in a fresh scratch directory."""
        scratch = _codex_scratch_dir(uuid.uuid4().hex)
        try:
            schema = scratch / _SCHEMA_NAME
            schema.write_text(json.dumps(PLAN_TURN_SCHEMA), encoding="utf-8")
            try:
                argv = build_plan_argv(
                    schema_path=schema,
                    output_path=scratch / _OUTPUT_NAME,
                    model=self.model,
                    reasoning_effort=self.reasoning_effort,
                    mcp_server=target.server,
                    tracker=target.tracker,
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


def _record_best_effort(
    event_type: OrchestratorEventType, payload: dict[str, object], ticket_id: str
) -> None:
    try:
        record_event(event_type, payload, correlation_id=ticket_id)
    except (OSError, CwError):
        _log.warning(
            "codex plan event %s not recorded ticket=%s",
            event_type.value,
            ticket_id,
            exc_info=True,
        )


def _approval_matches(task: TicketTask, resume: ResumeDraft | None) -> bool:
    return (
        resume is not None
        and has_approval_evidence(task)
        and task.plan_approved_fingerprint == resume.fingerprint
    )


def _choose_text(
    model_text: str, resume: ResumeDraft | None, approval: bool
) -> tuple[str, list[str]]:
    """The plan text cw uses, and the friction tokens the choice records."""
    friction = [RESUME_FRICTION] if resume is not None else []
    cleaned = clean_plan_text(model_text)
    if resume is None or not approval:
        return cleaned, friction
    if cleaned.strip() != resume.body.strip():
        friction.append(MODEL_TEXT_DISCARDED_FRICTION)
    return resume.body, friction


def _gate(result: AutoDevResult, plan_text: str) -> GateFailure | None:
    if result.status in {NO_OP, PLAN_BLOCKED}:
        return None
    failure = check_artifact(plan_text)
    if failure is not None:
        return failure
    return check_plan_files(plan_text) or check_scope_tier(plan_text, result.scope.tier)


def _with_friction(result: AutoDevResult, tokens: list[str]) -> AutoDevResult:
    if not tokens:
        return result
    return result.model_copy(
        update={"friction_highlights": [*result.friction_highlights, *tokens]}
    )


def _bound(
    run: _Run, result: AutoDevResult, *, bind_draft: bool = True
) -> AutoDevResult:
    """*result* with its fingerprint bound to the on-disk draft."""
    return stamp_plan_result(
        result.model_dump(mode="json"),
        ticket_id=run.task.ticket_id,
        worktree=run.worktree,
        bind_draft=bind_draft,
    )


def _write_draft_best_effort(run: _Run, plan_text: str) -> bool:
    try:
        write_plan_draft(run.worktree, plan_text)
    except PlanDraftError:
        raise
    except OSError:
        _log.warning("codex plan draft not written ticket=%s", run.task.ticket_id)
        return False
    return True


def _draft_fingerprint(plan_text: str, *, written: bool) -> str | None:
    """Return the fingerprint only when this run wrote the draft."""
    if not written:
        return None
    body = clean_plan_text(plan_text)
    if not body.endswith("\n"):
        body += "\n"
    return compute_plan_draft_fingerprint(body)


def _record_decisions(run: _Run, plan_text: str) -> str | None:
    """Record the plan's decisions on the row; a friction token on any shortfall."""
    if run.task.stage != Stage.PLAN:
        return None
    decisions = parse_plan_decisions(plan_text)
    if not decisions:
        return None
    try:
        outcome = record_plan_constraints(
            run.task.ticket_id, run.client.name, decisions, bounded=True
        )
    except (CwError, OSError) as exc:
        return f"{DECISIONS_NOT_RECORDED_PREFIX}{exc}"
    if outcome["unrecorded"]:
        reason = outcome["unrecorded_reason"] or "size cap"
        return f"{DECISIONS_NOT_RECORDED_PREFIX}{reason}"
    return None


def _park(run: _Run, plan_text: str, check: ApprovalCheck) -> _Exit:
    """A park exit: write the draft, run advisory stations, render the comment."""
    draft_written = _write_draft_best_effort(run, plan_text)
    result = _bound(run, check.result, bind_draft=draft_written)
    stations = run_plan_stations(
        runner=run.runner,
        worktree=run.worktree,
        plan_text=plan_text,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        session_id=run.sid,
        binding=False,
    )
    comment = render_park_comment(
        ticket_id=run.task.ticket_id,
        client_name=run.client.name,
        result=result,
        draft_body=clean_plan_text(plan_text),
        approved_fingerprint=run.task.plan_approved_fingerprint,
        advisory_findings=stations.findings,
        mismatch=check.mismatch is not None,
    )
    return _Exit(result, comment, run.worktree / PLAN_DRAFT_RELATIVE_PATH)


def _complete(run: _Run, result: AutoDevResult, plan_text: str) -> _Exit:
    """A completing exit: binding stations, the stamped plan, the decisions."""
    plan_path = run.worktree / PLAN_MD_RELATIVE_PATH
    if plan_path.exists():
        try:
            old_text = plan_path.read_text(encoding="utf-8")
            old_fingerprint = compute_plan_draft_fingerprint(clean_plan_text(old_text))
        except OSError as exc:
            old_fingerprint = f"unreadable ({exc})"
        new_fingerprint = compute_plan_draft_fingerprint(clean_plan_text(plan_text))
        return run.blocked(
            STAGE_PLAN_GENERATED,
            CODEX_PLAN_ARTIFACT_MISSING,
            "refusing to replace existing authoritative .cw/plan.md; "
            f"old fingerprint: {old_fingerprint}; "
            f"proposed replacement fingerprint: {new_fingerprint}; "
            "an explicit re-plan/regress operation must clear or version the "
            "existing artifact first",
        )
    stations = run_plan_stations(
        runner=run.runner,
        worktree=run.worktree,
        plan_text=plan_text,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        session_id=run.sid,
        binding=True,
    )
    if stations.block is not None:
        draft_written = _write_draft_best_effort(run, plan_text)
        block = stations.block
        return run.blocked(
            STAGE_PLAN_REVIEWED,
            block.reason,
            block.details,
            retry_eligible=block.retry_eligible,
            plan_draft_fingerprint=_draft_fingerprint(plan_text, written=draft_written),
        )
    marked = stamp_markers(clean_plan_text(plan_text), today=datetime.now(UTC).date())
    if not _plan_body_signoff_ok(marked):
        draft_written = _write_draft_best_effort(run, plan_text)
        return run.blocked(
            STAGE_PLAN_REVIEWED,
            CODEX_PLAN_ARTIFACT_MISSING,
            "stamped plan does not carry both sign-off markers",
            plan_draft_fingerprint=_draft_fingerprint(plan_text, written=draft_written),
        )
    draft_written = False
    try:
        write_plan_draft(run.worktree, plan_text)
        draft_written = True
        result = _bound(run, result)
        plan_path = write_plan_md(run.worktree, marked)
        written = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        return run.blocked(
            STAGE_PLAN_REVIEWED,
            CODEX_PLAN_ARTIFACT_MISSING,
            f".cw/plan.md: {exc}",
            plan_draft_fingerprint=_draft_fingerprint(plan_text, written=draft_written),
        )
    if not _plan_body_signoff_ok(written):
        # An existing plan is refused before this point, so what is on disk is
        # what this run just wrote: removing it never discards an authoritative
        # plan, and leaves no invalid one behind.
        try:
            plan_path.unlink(missing_ok=True)
        except OSError:
            _log.warning(
                "codex plan invalid artifact cleanup failed ticket=%s",
                run.task.ticket_id,
                exc_info=True,
            )
        return run.blocked(
            STAGE_PLAN_REVIEWED,
            CODEX_PLAN_ARTIFACT_MISSING,
            ".cw/plan.md does not carry both sign-off markers",
            plan_draft_fingerprint=_draft_fingerprint(plan_text, written=draft_written),
        )
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_PLAN_REVIEWED,
        prev_stage=STAGE_PLAN_GENERATED,
    )
    try:
        (run.worktree / PLAN_DRAFT_RELATIVE_PATH).unlink(missing_ok=True)
    except OSError:
        _log.warning(
            "codex plan draft cleanup failed ticket=%s",
            run.task.ticket_id,
            exc_info=True,
        )
    token = _record_decisions(run, written)
    result = _with_friction(result, [token] if token else [])
    return _Exit(result, marked, plan_path)


def _after_turn(
    run: _Run, plan_text_raw: str, result: AutoDevResult, resume: ResumeDraft | None
) -> _Exit:
    """Steps 5-7 for a parsed turn."""
    if result.status == NO_OP:
        return _Exit(result)
    if result.status == PLAN_BLOCKED:
        reason = result.blocker.reason if result.blocker is not None else PLAN_BLOCKED
        run.emit(
            OrchestratorEventType.STAGE_ERRORED, STAGE_PLAN_GENERATED, error_kind=reason
        )
        return _Exit(result)
    approval = _approval_matches(run.task, resume)
    plan_text, friction = _choose_text(plan_text_raw, resume, approval)
    result = _with_friction(result, friction)
    failure = _gate(result, plan_text)
    if failure is not None:
        return run.gate_blocked(failure, STAGE_PLAN_GENERATED)
    check = check_large_tier_approval(
        result, run.task, compute_plan_draft_fingerprint(plan_text)
    )
    if check.result.status in PARK_STATUSES:
        return _park(run, plan_text, check)
    return _complete(run, check.result, plan_text)


def _plan(run: _Run) -> _Exit:
    """Steps 1-4, then :func:`_after_turn`."""
    target = run.mcp_target()
    if isinstance(target, _Exit):
        return target
    resume = read_resume_draft(run.worktree)
    prompt = run.prompt(target, resume, _approval_matches(run.task, resume))
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
    return _after_turn(run, parsed.plan_markdown, parsed.result, resume)


def _execute(run: _Run) -> None:
    """Plan, complete through the door, post, then emit ``session.completed``."""
    exit_ = _plan(run)
    complete_session_through_door(run.sid, exit_.result.model_dump(mode="json"))
    if exit_.post_text is not None:
        post_plan_comment(
            task=run.task,
            client=run.client,
            worktree=run.worktree,
            sid=run.sid,
            runner=run.runner,
            text=exit_.post_text,
            artifact_path=exit_.artifact_path,
        )
    _record_best_effort(
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
) -> None:
    """Run the codex PLAN stage for *task* and complete session *sid*.

    *model* and *reasoning_effort* are the PLAN stage's resolved
    ``StageExecutorConfig`` values; no sandbox or timeout setting is read.
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
    )
    run_guarded_stage(
        client_name=client.name,
        task=task,
        worktree=worktree,
        sid=sid,
        stage_reached=STAGE1_PLAN,
        body=lambda: _execute(run),
    )


__all__ = ["run_plan_stage_and_complete"]
