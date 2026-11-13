"""The codex FINALIZE stage, end to end (#3062, RFC 0018 C3).

:func:`run_finalize_stage_and_complete` is what ``cw codex run --stage
finalize`` runs. Everything happens inside
:func:`cw.codex_stage_completion.run_guarded_stage`, so a stage fault
completes the session as ``unexpected_error`` at ``stage4a_merge_gate`` and
returns the claimed row to PENDING. The ordered flow:

1. The tracker-MCP backstop, the scope tier and the plan source.
2. The must-fix override check, the checked-out-branch guard and the
   Pre-Stage Detector Guard (a MERGED PR ships without gates; CLOSED blocks
   ``pr_already_terminal``), with the live-identity bind on a found PR.
3. Step 4.0 / 4.0b, the fence.
4. Create path: the merge gate, the quality gates, the default-branch probe,
   the ahead-push, the one read-only turn, ``gh pr create`` and the bind.
   Reuse path: the quality gates and, only when overridden, the turn and the
   ``## Operator override`` body edit.
5. Mergeability, the fence again (immediately before the arm), the seam
   check, the arm and the verification.
6. Stage events, the cw-stamped sentinel through the door, the PR-link post
   and ``session.completed``.

Every subprocess runs before the door, so none runs under ``sessions_lock``
(ADR-0019). Every step is idempotent behind the Detector Guard, so a
requeued or boot-orphaned run reuses its PR (R6).
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from cw._git import git_output
from cw.auto_dev_result.schema._vocab import PLAN_MISSING_CONTEXT_BLOCKER_REASON
from cw.backend_mcp_config import McpState, declared_server_names, read_backend_mcp
from cw.codex_finalize._argv import (
    build_finalize_argv,
    require_read_only_finalize_argv,
)
from cw.codex_finalize._arm import arm, check_automerge_allowed, verify_pr_state
from cw.codex_finalize._ci_evidence import (
    CiContext,
    DeferredGate,
    awaiting_ci_evidence_block,
    ci_pr_info,
    release_ci_hold,
)
from cw.codex_finalize._gates import (
    check_branch,
    gate_scope,
    resolve_tier,
    run_approval_fence,
    run_finalize_quality_gates,
    run_must_fix_override,
)
from cw.codex_finalize._merge_gate import run_merge_gate
from cw.codex_finalize._post import post_pr_link_comment
from cw.codex_finalize._pr import (
    PR_MERGED,
    BoundPr,
    DetectedPr,
    apply_override_section,
    bind_live_pr,
    check_mergeability,
    create_pr_for,
    detect_branch_pr,
    probe_default,
    push_ahead,
)
from cw.codex_finalize._prompt import (
    FINALIZE_DOCS,
    FINALIZE_TURN_SCHEMA,
    PLAN_RELATIVE,
    read_artifact,
    render_finalize_prompt,
)
from cw.codex_finalize._result import (
    OPERATOR_UNAVAILABLE,
    PRIOR_PIPELINE_PR_OPEN,
    FinalizeBlock,
    FinalizeFacts,
    FinalizeTurnDocument,
    compose_pr_title,
    finalize_blocked,
    finalize_merge_gate_blocked,
    finalize_shipped,
    parse_finalize_turn,
)
from cw.codex_fix_loop.posted_text import redact_and_cap
from cw.codex_plan._prompt import command_docs_root, prefetch_ticket
from cw.codex_review import (
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    STAGE4A_MERGE_GATE,
    STAGE4B_PR_CREATE,
    STAGE5_POST_CREATE,
    _codex_scratch_dir,
    _is_model_capacity_error,
)
from cw.codex_review_signals import record_event_best_effort
from cw.codex_stage_completion import (
    STAGE_FAULTS,
    complete_session_through_door,
    flush_late_usage,
    run_guarded_stage,
    with_usage_friction,
)
from cw.codex_stage_facts import resolve_plan_source
from cw.codex_usage import NO_USAGE, UsageAccumulator
from cw.contract_manifest import RenderError
from cw.dev_queue import _PRE_DISPATCH_TRACKER_MCP_REASON
from cw.gh import edit_pr_body, fetch_pr_view
from cw.local_runner import git_facts
from cw.models import (
    CODEX_BACKEND,
    HOOK_CONTEXT_RELATIVE_PATH,
    OrchestratorEventType,
)
from cw.models.branch import resolve_feature_branch
from cw.models.enums import StageIdentifier
from cw.tracker import TrackerClass, tracker_kind
from cw.tracker_io import TrackerIoConfigError
from cw.tracker_io._argv import validate_server_name

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import StageReached
    from cw.codex_runner import CodexRunner, CodexRunResult
    from cw.models import ClientConfig, TicketTask

_log = logging.getLogger(__name__)

#: The stage identifiers the driver's stage events carry.
STAGE_REVIEW_COMPLETE: Final = StageIdentifier.S3_REVIEW_COMPLETE.value
STAGE_PR_CREATED: Final = StageIdentifier.S4_PR_CREATED.value
STAGE_CI_WAITING: Final = StageIdentifier.S5_CI_WAITING.value
STAGE_DONE: Final = StageIdentifier.DONE.value

_SCHEMA_NAME: Final = "finalize-schema.json"
_OUTPUT_NAME: Final = "finalize-output.json"
_BODY_NAME: Final = "pr-body.md"
_STDERR_TAIL_CHARS: Final = 500
_GH_TIMEOUT: Final = 30

Payload = dict[str, Any]


@dataclass
class _State:
    """What the steps established; ``friction`` accumulates in order."""

    facts: FinalizeFacts
    head: str
    branch: str
    override_json: str | None
    friction: list[str] = field(default_factory=list)
    deferred: tuple[DeferredGate, ...] = ()

    def stamped(self) -> FinalizeFacts:
        """The facts with the accumulated friction."""
        return replace(self.facts, friction=list(self.friction))


@dataclass(frozen=True)
class _Run:
    """Everything one FINALIZE run needs; methods are the flow's helpers."""

    runner: CodexRunner
    task: TicketTask
    client: ClientConfig
    worktree: Path
    sid: str
    sess_name: str
    model: str | None
    reasoning_effort: str | None
    usage: UsageAccumulator = NO_USAGE

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

    def blocked(
        self,
        block: FinalizeBlock,
        state: _State | None,
        stage: StageReached | None = None,
    ) -> Payload:
        """A cw-built blocked exit; emits its one ``stage.errored``."""
        if stage is not None:
            block = replace(block, stage=stage)
        payload = finalize_blocked(
            block,
            ticket_id=self.task.ticket_id,
            worktree=self.worktree,
            facts=state.stamped() if state is not None else None,
        )
        self.emit(
            OrchestratorEventType.STAGE_ERRORED,
            STAGE_PR_CREATED,
            prev_stage=STAGE_REVIEW_COMPLETE,
            error_kind=block.reason,
        )
        return payload

    def tracker_block(self) -> FinalizeBlock | None:
        """A non-GitHub tracker needs a usable ``servers.codex`` for the A2 post."""
        kind = tracker_kind(self.client.workspace_path)
        if kind.tracker_class is not TrackerClass.OTHER or kind.system is None:
            return None
        server = self.client.tracker_mcp_server(CODEX_BACKEND) or ""
        why = None
        try:
            validate_server_name(server)
        except TrackerIoConfigError as exc:
            why = str(exc)
        if why is None:
            reading = read_backend_mcp(
                CODEX_BACKEND, server, self.client.workspace_path
            )
            if reading.state in (McpState.ABSENT, McpState.DISABLED):
                why = f"codex MCP server {server!r} is {reading.state.value}"
        if why is None:
            return None
        return FinalizeBlock(
            _PRE_DISPATCH_TRACKER_MCP_REASON, f"tracker {kind.system!r}: {why}"
        )

    def turn(self, prompt: str, scratch: Path) -> CodexRunResult:
        """Launch the one read-only FINALIZE turn, with no timeout (ADR-0014)."""
        schema = scratch / _SCHEMA_NAME
        schema.write_text(json.dumps(FINALIZE_TURN_SCHEMA), encoding="utf-8")
        argv = build_finalize_argv(
            schema_path=schema,
            output_path=scratch / _OUTPUT_NAME,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            other_servers=declared_server_names(CODEX_BACKEND) or (),
        )
        require_read_only_finalize_argv(argv)
        return self.runner.run(self.worktree, argv, None, stdin=prompt)


def _turn_failure(run: CodexRunResult) -> FinalizeBlock | None:
    """A failed launch: model capacity (retryable) or ``codex_error``."""
    if run.returncode == 0 and not run.timed_out:
        return None
    if _is_model_capacity_error(run):
        return FinalizeBlock(
            CODEX_MODEL_CAPACITY,
            "the FINALIZE turn hit a model capacity limit",
            retry_eligible=True,
        )
    tail = redact_and_cap(run.stderr.strip()[-_STDERR_TAIL_CHARS:])
    return FinalizeBlock(
        CODEX_ERROR, f"the FINALIZE turn exited {run.returncode}: {tail}"
    )


def _context(worktree: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _branch_facts(state: _State) -> str:
    git = state.facts.git
    commits = "\n".join(f"- {sha}" for sha in git["commits"]) or "- (none)"
    return (
        f"branch: {state.branch}\n"
        f"fork_point: {git['fork_point'] or '(unresolved)'}\n"
        f"head: {state.head}\n"
        f"files changed: {git['files']}\n"
        f"lines changed: {git['lines_actual']}\n"
        f"commits (newest first):\n{commits}"
    )


def _document(
    run: _Run, state: _State, scratch: Path
) -> FinalizeTurnDocument | FinalizeBlock:
    """Render the prompt, run the read-only turn and parse its document."""
    docs_root = command_docs_root(FINALIZE_DOCS)
    if docs_root is None:
        return FinalizeBlock(
            PLAN_MISSING_CONTEXT_BLOCKER_REASON,
            "cw's command-doc tree (packaged cw/data/commands or the cw source "
            "checkout's .claude/commands) is not available",
        )
    github = (
        tracker_kind(run.client.workspace_path).tracker_class is not TrackerClass.OTHER
    )
    ticket = (
        prefetch_ticket(run.task.ticket_id, cwd=run.client.workspace_path)
        if github
        else None
    )
    try:
        prompt = render_finalize_prompt(
            ticket_id=run.task.ticket_id,
            docs_root=docs_root,
            worktree=run.worktree,
            ticket=ticket,
            branch_facts=_branch_facts(state),
            override_json=state.override_json,
        )
    except RenderError as exc:
        return FinalizeBlock(
            PLAN_MISSING_CONTEXT_BLOCKER_REASON, f"render failed: {exc}"
        )
    turn = run.turn(prompt, scratch)
    failed = _turn_failure(turn)
    if failed is not None:
        return failed
    return parse_finalize_turn(turn.output_file_content)


def _pr_body(doc: FinalizeTurnDocument, state: _State) -> str:
    body = doc.pr_body.rstrip()
    if doc.adjudication_section.strip():
        body += "\n\n" + doc.adjudication_section.strip()
    if state.override_json is not None:
        body = apply_override_section(body, doc.override_section or state.override_json)
    return body.rstrip() + "\n"


# -- ship --------------------------------------------------------------------


def _fence(run: _Run, state: _State) -> Payload | None:
    """Step 4.0/4.0b; a park payload, or ``None`` with its friction recorded."""
    outcome = run_approval_fence(
        run.worktree,
        state.stamped(),
        tier=state.facts.tier,
        ticket_id=run.task.ticket_id,
        client_name=run.client.name,
    )
    if outcome.park is not None:
        return outcome.park
    state.friction.extend(outcome.friction)
    return None


def _arm_and_ship(run: _Run, state: _State, bound: BoundPr) -> Payload:
    """Mergeability, the re-fence, the arm and the verification, then ship."""
    blocked, friction = check_mergeability(run.worktree, bound.url)
    if blocked is not None:
        return run.blocked(blocked, state, STAGE5_POST_CREATE)
    state.friction.extend(friction)
    park = _fence(run, state)
    if park is not None:
        return park
    pr_info: dict[str, object] = {"number": bound.number, "url": bound.url}
    allowed = check_automerge_allowed(run.worktree, pr_info)
    if isinstance(allowed, FinalizeBlock):
        return run.blocked(allowed, state, STAGE5_POST_CREATE)
    armed = False
    if allowed:
        result = arm(
            run.worktree, pr_number=bound.number, head_sha=state.head, pr_info=pr_info
        )
        if result.block is not None:
            return run.blocked(result.block, state, STAGE5_POST_CREATE)
        armed = result.armed
    blocked, friction = verify_pr_state(
        run.worktree,
        pr_url=bound.url,
        head_sha=state.head,
        armed_claim=armed,
        pr_info=pr_info,
    )
    if blocked is not None:
        return run.blocked(blocked, state, STAGE5_POST_CREATE)
    state.friction.extend(friction)
    return _shipped(run, state, bound, auto_merge=armed)


def _shipped(run: _Run, state: _State, bound: BoundPr, *, auto_merge: bool) -> Payload:
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_CI_WAITING,
        prev_stage=STAGE_PR_CREATED,
    )
    run.emit(
        OrchestratorEventType.STAGE_ENTERED, STAGE_DONE, prev_stage=STAGE_CI_WAITING
    )
    return finalize_shipped(
        state.stamped(),
        pr_number=bound.number,
        pr_url=bound.url,
        auto_merge=auto_merge,
        base=run.client.default_branch,
    )


# -- paths -------------------------------------------------------------------


def _bind(
    run: _Run, state: _State, number: int, url: str, *, require_head: bool
) -> BoundPr | FinalizeBlock:
    return bind_live_pr(
        run.worktree,
        pr_number=number,
        pr_url=url,
        branch=state.branch,
        head_sha=state.head,
        require_head_match=require_head,
    )


def _ci_context(run: _Run, state: _State, pr: BoundPr | None = None) -> CiContext:
    """What the CI-evidence consult needs: the base, the head and any bound PR."""
    return CiContext(
        base=run.client.default_branch,
        pr_number=pr.number if pr is not None else None,
        pr_url=pr.url if pr is not None else None,
        head=state.head,
    )


def _gates(
    run: _Run, state: _State, path: str, *, pr: BoundPr | None = None
) -> FinalizeBlock | None:
    gated = run_finalize_quality_gates(
        run.worktree,
        scope=gate_scope(run.task.ticket_id, run.client.name, run.sid),
        path=path,
        ci=_ci_context(run, state, pr),
    )
    state.friction.extend(gated.friction)
    state.deferred = gated.deferred
    return gated.block


def _client_branch_prefix(run: _Run) -> str:
    """The client's branch prefix, read through the resolver (the seam owner).

    The resolver ignores a recorded branch when no row is passed, so the result
    is always ``<prefix>/<ticket>`` and its head segment is the prefix.
    """
    return resolve_feature_branch(run.task.ticket_id, run.client).rsplit("/", 1)[0]


def _create_prelude(run: _Run, state: _State) -> Payload | None:
    """Merge gate, quality gates, the default-branch probe and the ahead-push."""
    context = _context(run.worktree)
    ignore = context.get("merge_gate_ignore_paths")
    gate = run_merge_gate(
        run.worktree,
        enabled=context.get("merge_gate_enabled") is not False,
        ignore_paths=[p for p in ignore if isinstance(p, str)]
        if isinstance(ignore, list)
        else [],
        branch=state.branch,
        branch_prefix=_client_branch_prefix(run),
        fork_point=state.facts.git["fork_point"] or state.head,
        client_name=run.client.name,
    )
    state.friction.extend(gate.friction)
    if gate.block is not None:
        return run.blocked(gate.block, state)
    if gate.conflicts:
        run.emit(
            OrchestratorEventType.STAGE_ERRORED,
            STAGE_PR_CREATED,
            prev_stage=STAGE_REVIEW_COMPLETE,
            error_kind=PRIOR_PIPELINE_PR_OPEN,
        )
        return finalize_merge_gate_blocked(
            state.stamped(), details="; ".join(gate.conflicts)
        )
    block = _gates(run, state, "create")
    if block is None:
        block, friction = probe_default(
            run.worktree, default_branch=run.client.default_branch, pr_exists=False
        )
        state.friction.extend(friction)
    if block is None:
        block = push_ahead(run.worktree, branch=state.branch, head_sha=state.head)
    return run.blocked(block, state, STAGE4B_PR_CREATE) if block is not None else None


def _create(run: _Run, state: _State, scratch: Path) -> Payload:
    """The no-PR path: prelude, the turn, ``gh pr create`` and the bind."""
    stopped = _create_prelude(run, state)
    if stopped is not None:
        return stopped
    doc = _document(run, state, scratch)
    if isinstance(doc, FinalizeBlock):
        return run.blocked(doc, state, STAGE4B_PR_CREATE)
    body_file = scratch / _BODY_NAME
    body_file.write_text(_pr_body(doc, state), encoding="utf-8")
    created = create_pr_for(
        run.worktree,
        base=run.client.default_branch,
        branch=state.branch,
        title=compose_pr_title(doc.pr_title, run.task.ticket_id),
        body_file=body_file,
    )
    if isinstance(created, FinalizeBlock):
        return run.blocked(created, state, STAGE4B_PR_CREATE)
    bound = _bind(run, state, created[0], created[1], require_head=True)
    if isinstance(bound, FinalizeBlock):
        return run.blocked(bound, state, STAGE5_POST_CREATE)
    if state.deferred:
        # Mapped gates belong to CI: the PR exists but is never armed here.
        parked = awaiting_ci_evidence_block(
            "deferred", _ci_context(run, state, bound), deferred=state.deferred
        )
        return run.blocked(parked, state, STAGE5_POST_CREATE)
    return _arm_and_ship(run, state, bound)


def _edit_override(
    run: _Run, state: _State, bound: BoundPr, scratch: Path
) -> FinalizeBlock | None:
    """Reuse path, overridden only: rewrite the ``## Operator override`` region."""
    doc = _document(run, state, scratch)
    if isinstance(doc, FinalizeBlock):
        return doc
    view = fetch_pr_view(
        bound.url, timeout=_GH_TIMEOUT, fields="body", cwd=run.worktree
    )
    body = view.get("body") if view is not None else None
    if not isinstance(body, str):
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr view {bound.url} could not read the PR body",
            retry_eligible=True,
        )
    body_file = scratch / _BODY_NAME
    section = doc.override_section or state.override_json or ""
    body_file.write_text(apply_override_section(body, section), encoding="utf-8")
    done = edit_pr_body(bound.url, body_file=body_file, cwd=run.worktree)
    if done is None or done.returncode != 0:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh pr edit {bound.url} could not write the operator override section",
            retry_eligible=True,
        )
    return None


def _reuse(run: _Run, state: _State, bound: BoundPr, scratch: Path) -> Payload:
    """The OPEN-PR path: gates, probe, override edit, then the arm."""
    block = _gates(run, state, "reuse", pr=bound)
    if block is None:
        block, friction = probe_default(
            run.worktree, default_branch=run.client.default_branch, pr_exists=True
        )
        state.friction.extend(friction)
    if block is None and state.override_json is not None:
        block = _edit_override(run, state, bound, scratch)
    if block is None:
        # A hold left by a headless create must not strand this pass's arm.
        block = release_ci_hold(
            run.worktree, ci_pr_info(_ci_context(run, state, bound)), head=state.head
        )
    if block is not None:
        return run.blocked(block, state, STAGE5_POST_CREATE)
    return _arm_and_ship(run, state, bound)


def _found(run: _Run, state: _State, found: DetectedPr, scratch: Path) -> Payload:
    """A PR the Detector Guard found: MERGED ships, OPEN is reused."""
    merged = found.state == PR_MERGED
    bound = _bind(run, state, found.number, found.url, require_head=not merged)
    if isinstance(bound, FinalizeBlock):
        return run.blocked(bound, state, STAGE5_POST_CREATE)
    if merged:
        return _shipped(run, state, bound, auto_merge=True)
    park = _fence(run, state)
    if park is not None:
        return park
    return _reuse(run, state, bound, scratch)


# -- the flow ----------------------------------------------------------------


def _prepare(run: _Run) -> _State | Payload:
    """Tracker backstop, tier, plan source, override and the branch guard."""
    block = run.tracker_block()
    if block is not None:
        return run.blocked(block, None)
    git = git_facts(run.worktree, run.client.default_branch)
    tier = resolve_tier(run.task, read_artifact(run.worktree, PLAN_RELATIVE), git)
    if isinstance(tier, FinalizeBlock):
        return run.blocked(tier, None)
    plan_source, plan_friction = resolve_plan_source(run.task, run.worktree)
    facts = FinalizeFacts(run.task.ticket_id, run.worktree, git, tier, plan_source)
    head = git_output(["rev-parse", "HEAD"], cwd=run.worktree).strip()
    branch = resolve_feature_branch(run.task.ticket_id, run.client, run.task)
    state = _State(facts, head, branch, None)
    if plan_friction:
        state.friction.append(plan_friction)
    override = run_must_fix_override(run.worktree, head)
    state.friction.extend(override.friction)
    if override.block is not None:
        return run.blocked(override.block, state)
    state.override_json = override.override_json
    wrong = check_branch(run.worktree, branch)
    return run.blocked(wrong, state) if wrong is not None else state


def _finalize(run: _Run, scratch: Path) -> Payload:
    """The whole flow, returning the payload the door persists."""
    run.emit(
        OrchestratorEventType.STAGE_ENTERED,
        STAGE_PR_CREATED,
        prev_stage=STAGE_REVIEW_COMPLETE,
    )
    state = _prepare(run)
    if not isinstance(state, _State):
        return state
    found = detect_branch_pr(run.worktree, state.branch)
    if isinstance(found, FinalizeBlock):
        return run.blocked(found, state)
    if found is not None:
        return _found(run, state, found, scratch)
    park = _fence(run, state)
    if park is not None:
        return park
    return _create(run, state, scratch)


def _execute(run: _Run) -> None:
    """Run the flow, complete through the door, post the link, then complete."""
    scratch = _codex_scratch_dir(uuid.uuid4().hex)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        payload = _finalize(run, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    payload = with_usage_friction(payload, run.usage)
    try:
        complete_session_through_door(run.sid, payload, usage=run.usage)
    except STAGE_FAULTS:
        # An armed PR must not be stranded by a transient completion failure;
        # the door is idempotent, so one retry either writes or observes it.
        _log.warning(
            "codex finalize: retrying completion session=%s ticket=%s",
            run.sid,
            run.task.ticket_id,
            exc_info=True,
        )
        complete_session_through_door(run.sid, payload, usage=run.usage)
    pr = payload.get("pr")
    if payload.get("status") == "shipped" and isinstance(pr, dict):
        post_pr_link_comment(
            task=run.task,
            client=run.client,
            worktree=run.worktree,
            sid=run.sid,
            runner=run.runner,
            pr_url=str(pr.get("url")),
            plan_source=str(payload.get("plan_source")),
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


def run_finalize_stage_and_complete(
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
    """Run the codex FINALIZE stage for *task* and complete session *sid*.

    *model* and *reasoning_effort* are the FINALIZE stage's resolved
    ``StageExecutorConfig`` values. The turn is read-only, so no sandbox knob
    is read, and no timeout is read (ADR-0014).
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
    )
    run_guarded_stage(
        client_name=client.name,
        task=task,
        worktree=worktree,
        sid=sid,
        stage_reached=STAGE4A_MERGE_GATE,
        body=lambda: _execute(run),
        usage=usage,
    )


__all__ = ["run_finalize_stage_and_complete"]
