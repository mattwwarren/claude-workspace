"""RFC 0005 A2/E1/F3 — StageExecutor seam + ClaudeNativeExecutor + LocalExecutor."""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from cw.auto_dev_result import AutoDevResult
from cw.codex_background import (
    _complete_session_as_unexpected_error,
    _default_background,
    _run_codex_review_and_complete,
    _stamp_session_id_on_running_task,
)
from cw.codex_review import (
    make_codex_blocked,
)
from cw.codex_review._const import _CODEX_VERSION_RE
from cw.codex_runner import CodexRunner, RealCodexRunner
from cw.config import load_state, save_state, sessions_lock
from cw.events import record_event as _record_orchestrator_event
from cw.exceptions import EmitSessionNotFoundError
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)
from cw.local_runner import (
    AIDER_NOT_FOUND,
    ENDPOINT_NOT_CONFIGURED,
    LIVENESS_UNAVAILABLE,
    PLAN_MISSING,
    TASK_CONTEXT_RELATIVE_PATH,
    UNEXPECTED_ERROR,
    AiderRunner,
    GithubIssuePlanFetcher,
    PlanFetcher,
    RealAiderRunner,
    aider_available,
    build_argv,
    build_env,
    build_task_message,
    make_blocked,
    read_process_start_time_ns,
)
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    ClientConfig,
    LastResultSource,
    LocalLivenessHandle,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    TicketTask,
)
from cw.opencode_runner import (
    OPENCODE_NOT_FOUND,
    STAGE4A_MERGE_GATE,
    OpencodeRunner,
    RealOpencodeRunner,
    build_finalize_prompt,
    opencode_available,
)
from cw.opencode_runner import (
    build_argv as build_opencode_argv,
)
from cw.opencode_runner import (
    build_env as build_opencode_env,
)
from cw.opencode_runner import (
    make_blocked as make_opencode_blocked,
)
from cw.plan_files import parse_plan_files_modified
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from cw.result import emit_result_locked
from cw.spawn import spawn_create_impl
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger(__name__)

# Session-level CodexExecutor pre-flight blocker reason codes (RFC 0005 F1).
# Per-role failure reason codes (CODEX_TIMEOUT/CODEX_ERROR/
# CODEX_REVIEW_UNPARSEABLE/CODEX_MUST_FIX_FINDINGS) live in cw.codex_review.
CODEX_NOT_FOUND = "codex_not_found"
CODEX_REVIEW_ONLY = "codex_review_only"

# Capability-probe diagnosis (distinct from cw.codex_review's per-role review
# failure-reason vocabulary): the binary is present but `codex --version`
# could not be confirmed.
CODEX_VERSION_UNKNOWN = "codex_version_unknown"

# Default subprocess timeout for a one-shot, user-invoked probe (e.g. `cw
# doctor`). Callers on a hot path (dispatch's pre-spawn gate) should pass a
# smaller explicit timeout — see the ``timeout_seconds`` parameter below.
_DEFAULT_PROBE_TIMEOUT_SECONDS = 10


class CodexCapabilityDiagnosis(NamedTuple):
    """Result of the shared codex capability probe (#1238).

    ``diagnosis`` is ``None`` when codex is capable (binary present and
    ``codex --version`` parsed), ``CODEX_NOT_FOUND`` when the binary is absent,
    or ``CODEX_VERSION_UNKNOWN`` when the binary is present but ``--version``
    failed/timed out/exited non-zero/produced an unparseable string. ``detail``
    carries the parsed version line on success or a short human-readable failure
    detail on the two failure branches.
    """

    diagnosis: str | None
    detail: str


def codex_capability_diagnosis(
    *, timeout_seconds: int = _DEFAULT_PROBE_TIMEOUT_SECONDS
) -> CodexCapabilityDiagnosis:
    """Probe codex CLI presence and ``codex --version`` (no live review).

    Mirrors ``doctor._check_claude_version``'s subprocess/timeout/parse shape as
    a single reusable helper. No version floor is enforced: capability requires
    only binary presence plus a successfully-parsed ``--version`` string. This
    is the single home of the probe logic — ``doctor._check_codex_capability``
    and dispatch's pre-spawn capability gate are thin call sites over it, so the
    subprocess/parse logic isn't duplicated across two already-oversized modules.

    ``timeout_seconds`` defaults to a one-shot-invocation-appropriate 10s
    (matching ``_check_claude_version``); dispatch's hot-path caller passes a
    smaller value since this runs synchronously inside the dispatch tick loop.
    """
    if shutil.which("codex") is None:
        return CodexCapabilityDiagnosis(CODEX_NOT_FOUND, "codex binary not found")
    try:
        proc = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return CodexCapabilityDiagnosis(CODEX_VERSION_UNKNOWN, "codex binary not found")
    except subprocess.TimeoutExpired:
        return CodexCapabilityDiagnosis(
            CODEX_VERSION_UNKNOWN, f"codex --version timed out ({timeout_seconds}s)"
        )

    output = proc.stdout or proc.stderr or ""
    version_line = output.splitlines()[0] if output else ""
    if proc.returncode != 0:
        return CodexCapabilityDiagnosis(
            CODEX_VERSION_UNKNOWN,
            f"codex --version exited {proc.returncode}: {version_line}",
        )
    if _CODEX_VERSION_RE.search(version_line) is None:
        return CodexCapabilityDiagnosis(
            CODEX_VERSION_UNKNOWN, f"could not parse version: {version_line}"
        )
    return CodexCapabilityDiagnosis(None, version_line)


def resolve_executor_config(
    stage: Stage,
    task: TicketTask,
    client: ClientConfig,
) -> StageExecutorConfig:
    """Return the effective StageExecutorConfig for a stage, with lane override (E1).

    Three-level priority: lane stage config > client stage config > default.
    """
    if task.lane:
        for lane_cfg in client.effective_lanes:
            if lane_cfg.name == task.lane and lane_cfg.pipeline is not None:
                lane_stage_config = lane_cfg.pipeline.executors.get(stage)
                if lane_stage_config is not None:
                    return lane_stage_config
                break
    return client.pipeline.executors.get(stage, StageExecutorConfig())


def resolve_executor(
    task: TicketTask,
    client: ClientConfig,
    *,
    native_daemon: NativeDaemonClient | None = None,
) -> StageExecutor:
    """Return the executor for task.stage, selected by backend (RFC 0005 E1)."""
    config = resolve_executor_config(task.stage, task, client)
    if config.backend == LOCAL_BACKEND:
        return LocalExecutor(config=config)
    if config.backend == CODEX_BACKEND:
        return CodexExecutor(config=config)
    if config.backend == CLAUDE_NATIVE_BACKEND:
        return ClaudeNativeExecutor(config=config, native_daemon=native_daemon)
    if config.backend == OPENCODE_BACKEND:
        return OpencodeExecutor(config=config)
    msg = f"unknown executor backend: {config.backend!r}"
    raise ValueError(msg)


@runtime_checkable
class StageExecutor(Protocol):
    """Protocol for executing a single pipeline stage (RFC 0005 A2).

    # Invariant: a ``StageExecutor.spawn()`` that blocks the calling thread must
    # not be on the shared ``dispatch_tick`` path unless its session carries a
    # ``surface_ref``-equivalent liveness handle (bound to process start-time)
    # that phantom/harvest detection can use for crash recovery. LocalExecutor
    # satisfies this via ``Session.local_liveness`` (RFC 0005 F3, #888): it
    # launches aider fire-and-forget, records the handle, and returns without
    # blocking, so reconcile/local can recover the session if cw dies mid-run.
    #
    # CodexExecutor is an accepted, documented exception (#1727): it no longer
    # blocks the caller (it hands the review to a ``cw.codex_background`` daemon
    # thread and returns) but still carries no liveness handle, so its session
    # is not crash-recoverable via harvest. Unchanged from when the call was
    # synchronous — a thread dies with its process exactly as a blocking call
    # did — so this is not a regression; closing it is Option A/B territory (a
    # real subprocess surface), out of scope. The blast radius is bounded, not
    # closed, from two sides: ``run_dispatch_loop``'s shutdown path
    # bounded-joins outstanding codex threads before DISPATCH_LOOP_EXITED
    # (deploy/restart/``--once``), and ``cw.reconcile.codex_boot`` flags any
    # codex session still ACTIVE at the next boot (the crash/SIGKILL path a
    # join cannot reach).
    """

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
        wall_clock_budget_seconds: int | None = None,
        parent: str | None = None,
    ) -> str:
        """Spawn the stage session; return the cw session id."""
        ...

    def stage_sentinel_schema(self, stage: Stage) -> dict[str, Any]:
        """Return the expected JSON schema for this stage's result sentinel."""
        ...


class ClaudeNativeExecutor:
    """StageExecutor backed by claude --bg via spawn_create_impl (RFC 0005 A2).

    # Why: A2 seam — wraps spawn_create_impl without modifying it.
    # --model forwarding uses client.model_copy to avoid double-flag.
    # stage_sentinel_schema bridges to AutoDevResult until A3 per-stage schemas.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        native_daemon: NativeDaemonClient | None = None,
    ) -> None:
        self._config = config
        self._native_daemon = native_daemon

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
        wall_clock_budget_seconds: int | None = None,
        parent: str | None = None,
    ) -> str:
        effective_model = self._config.model or client.worker_model
        effective_client = client.model_copy(update={"worker_model": effective_model})
        return spawn_create_impl(
            client=effective_client,
            worktree=worktree,
            prompt=f"/auto-dev-{stage.value} {task.ticket_id} --headless",
            label=f"{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}",
            ticket_id=task.ticket_id,
            lane=task.lane,
            headless=True,
            purpose=SessionPurpose.IMPL,
            permission_mode=None,
            parent=parent,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
            native_daemon=self._native_daemon,
            task=task,
        )

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        # Why: per-stage result models do not exist until A3 (#614).
        # Bridge via the current monolith sentinel until then.
        return AutoDevResult.model_json_schema()


class _PreflightOK(NamedTuple):
    """Resolved launch parameters returned by _local_preflight on success."""

    endpoint: str
    model: str
    task_message: str
    # The plan's ``## Files Modified`` manifest — aider's explicit edit set
    # (#1905). Empty when the plan has no manifest section.
    files: list[str]
    # The materialised read-only task-context file passed to aider as --read.
    read_only_path: Path


def _local_preflight(
    config: StageExecutorConfig,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
) -> AutoDevResult | _PreflightOK:
    """Run LocalExecutor pre-flight checks (endpoint/aider/plan availability).

    Returns a blocked ``AutoDevResult`` on the first failing check; returns
    ``_PreflightOK`` with the resolved launch parameters when all checks pass.
    The discriminated return lets callers use ``isinstance(_PreflightOK)`` to
    narrow without ``or ""`` guards on the resolved values. Kept synchronous
    (Addendum 1 Alt A): pre-flight failures complete the session inline, before
    any fire-and-forget launch.
    """
    if config.endpoint is None:
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=ENDPOINT_NOT_CONFIGURED,
        )
    if not aider_available():
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=AIDER_NOT_FOUND,
            retry_eligible=True,
            retry_delay_seconds=0,
        )
    plan_fetcher: PlanFetcher | None = None
    if resolve_tracker(client.workspace_path) == TRACKER_GITHUB_ISSUES:
        plan_fetcher = GithubIssuePlanFetcher()
    task_message = build_task_message(
        worktree,
        ticket_id=task.ticket_id,
        plan_fetcher=plan_fetcher,
    )
    if task_message is None:
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=PLAN_MISSING,
        )
    # Both files below are guaranteed on disk by build_task_message's own
    # materialise-before-return contract (it writes .cw/plan.md on a tracker
    # fetch, and the task-context file unconditionally on the success path).
    # The re-read is still suppressed: an unreadable plan degrades to "no
    # manifest" (zero --file flags, aider's own heuristic), never to a crash.
    plan_text = ""
    with contextlib.suppress(OSError):
        plan_text = (worktree / ".cw" / "plan.md").read_text(encoding="utf-8")
    return _PreflightOK(
        endpoint=config.endpoint,  # narrowed: is-None check above
        model=config.model or "",
        task_message=task_message,
        files=parse_plan_files_modified(plan_text),
        read_only_path=worktree / TASK_CONTEXT_RELATIVE_PATH,
    )


class LocalExecutor:
    """StageExecutor backed by a fire-and-forget aider subprocess (RFC 0005 F3).

    spawn() is non-blocking on the launch path: after synchronous pre-flight
    checks, it launches aider via ``AiderRunner.launch`` (Popen, no wait),
    records a ``Session.local_liveness`` handle (PID + /proc start-time), leaves
    the session ACTIVE, and returns the sid immediately. The aider run completes
    asynchronously; reconcile/local harvest later detects the dead process,
    synthesizes an AutoDevResult from git facts, and completes the session. This
    keeps the shared ``dispatch_tick`` thread from blocking for the full run.

    Pre-flight failures (endpoint/aider/plan missing) stay synchronous: they
    persist a blocked result to Session.last_result via the door
    (``emit_result_locked``, source=EXECUTOR_DIRECT — RFC 0012 A2, #1458), mark
    the session COMPLETED, and emit SESSION_COMPLETED before returning — the
    launch never happens.

    Result delivery bypasses stdout-sentinel parsing entirely. Every
    SESSION_COMPLETED event this class or the harvest path emits carries no
    result payload; the last_result written onto the session by the door
    (``emit_result_locked``, source=EXECUTOR_DIRECT) is consumed as-is by
    consume_completed_sessions.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: AiderRunner | None = None,
    ) -> None:
        self._config = config
        self._runner: AiderRunner = runner if runner is not None else RealAiderRunner()

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
        wall_clock_budget_seconds: int | None = None,
        parent: str | None = None,
    ) -> str:
        # parent is intentionally unused; aider runs have no parent-session
        # concept. wall_clock_budget_seconds is unused on the fire-and-forget
        # launch path — the harvest sweep, not a blocking timeout, bounds the run.
        del parent, wall_clock_budget_seconds
        # Step 1: Create Session with all required fields.
        sess = Session(
            name=f"{client.name}/{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}",
            client=client.name,
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=client.workspace_path,
            worktree_path=worktree,
            stage=stage,
            lane=task.lane,
        )
        sid = sess.id
        with sessions_lock():
            state = load_state()
            state.sessions.append(sess)
            save_state(state)

        # Step 2: Pre-flight checks (synchronous, Addendum 1 Alt A).
        # _PreflightOK → all checks passed; AutoDevResult → blocked.
        preflight = _local_preflight(self._config, task, worktree, client)

        # Captured for diagnostics: empty until the launch path builds it, so
        # the generic-except branch (which may fire before build_argv) can still
        # persist an argv-less bundle.
        argv: list[str] = []
        try:
            if isinstance(preflight, _PreflightOK):
                # Step 3: Launch aider fire-and-forget (pre-flight all passed).
                # Capture the PID + start-time as a liveness handle, leave the
                # session ACTIVE, and return — reconcile/local harvest completes
                # it once the process exits. NEVER block on the run here.
                argv = build_argv(
                    preflight.model,
                    preflight.task_message,
                    preflight.files,
                    preflight.read_only_path,
                )
                env = build_env(preflight.endpoint)
                proc = self._runner.launch(worktree, argv, env)
                start_time_ns = read_process_start_time_ns(proc.pid)
                if start_time_ns is not None:
                    with sessions_lock():
                        state = load_state()
                        target = next((s for s in state.sessions if s.id == sid), None)
                        if target is not None:
                            target.local_liveness = LocalLivenessHandle(
                                pid=proc.pid,
                                start_time_ns=start_time_ns,
                            )
                            save_state(state)
                    return sid
                # /proc/<pid>/stat unreadable immediately after launch — process
                # may have exited before exec or /proc is unavailable. Storing 0
                # would make every liveness check return False, triggering
                # premature harvest while aider is still running. Kill any orphan
                # and fall through to the blocked completion path so the dispatch
                # retry path requeues the task (no stale liveness handle stored).
                with contextlib.suppress(OSError):
                    proc.kill()
                    proc.wait()
                liveness_detail = f"process {proc.pid} start-time unavailable"
                # Post-spawn failure (process exited before exec / /proc gone),
                # classified runtime_error per the inline comment above — not a
                # spawn_error, the launch itself succeeded.
                _persist_aider_runtime_error_diagnostics(
                    session_id=sid, argv=argv, details=liveness_detail
                )
                completion_result: AutoDevResult = make_blocked(
                    ticket_id=task.ticket_id,
                    worktree=worktree,
                    reason=LIVENESS_UNAVAILABLE,
                    details=append_diagnostics_pointer(liveness_detail, session_id=sid),
                )
            else:
                completion_result = preflight

            # Pre-flight blocked OR proc stat unreadable: complete synchronously.
            # Write the blocked result through the door (_complete_session_via_door,
            # RFC 0012 A2), mark COMPLETED, and emit SESSION_COMPLETED — dispatch
            # reads last_result from the session directly, no payload needed.
            with sessions_lock():
                _complete_session_via_door(
                    sid=sid, payload=completion_result.model_dump(mode="json")
                )
            _record_orchestrator_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": sid,
                    "ticket_id": task.ticket_id,
                    "session_name": sess.name,
                },
            )
        except Exception:
            # Ensure the session is never left ACTIVE on unexpected errors during
            # launch (FileNotFoundError/OSError from Popen despite the
            # aider_available check, or a save_state failure). Mark it COMPLETED
            # with a blocked result so reconcile can clean it up. SESSION_COMPLETED
            # is NOT emitted; dispatch's exception handler reverts the task to
            # PENDING, which is the correct recovery path.
            unexpected_error_detail = "unexpected error during aider launch"
            _persist_aider_runtime_error_diagnostics(
                session_id=sid,
                argv=argv,
                details=unexpected_error_detail,
            )
            with sessions_lock():
                _complete_session_via_door(
                    sid=sid,
                    payload=make_blocked(
                        ticket_id=task.ticket_id,
                        worktree=worktree,
                        reason=UNEXPECTED_ERROR,
                        details=append_diagnostics_pointer(
                            unexpected_error_detail, session_id=sid
                        ),
                    ).model_dump(mode="json"),
                    guard_already_completed=True,
                )
            raise

        return sid

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()


def _persist_aider_runtime_error_diagnostics(
    *, session_id: str, argv: list[str], details: str
) -> None:
    """Write a ``runtime_error`` diagnostics bundle for a LocalExecutor failure.

    Covers both post-spawn LocalExecutor.spawn failure branches
    (LIVENESS_UNAVAILABLE and the generic ``except``). *argv* is passed
    through raw — ``ExecutorFailure``'s own ``argv_sanitized`` field_validator
    redacts aider's ``--message`` value (full ticket+plan text) wholesale
    (#1330 item 4). Never raises (persist swallows OSError).
    """
    failure = build_executor_failure(
        category="runtime_error",
        executor_name="aider",
        session_id=session_id,
        argv=argv,
        stdout_excerpt="",
        stderr_excerpt=details,
    )
    persist_diagnostics_bundle(
        session_id=session_id,
        role_slug="aider",
        failure=failure,
    )


class _OpencodePreflightOK(NamedTuple):
    """Resolved launch parameters returned by _opencode_preflight on success."""

    argv: list[str]
    env: dict[str, str]


def _opencode_preflight(
    config: StageExecutorConfig,
    task: TicketTask,
    worktree: Path,
    client: ClientConfig,
) -> AutoDevResult | _OpencodePreflightOK:
    """Run OpencodeExecutor pre-flight checks for the FINALIZE stage (#1670).

    Returns a blocked ``AutoDevResult`` on binary-missing; returns
    ``_OpencodePreflightOK`` with the resolved argv + env (finalize prompt)
    when the binary is available. Non-FINALIZE stages are stage-blocked in
    spawn() before reaching here. The finalize prompt instructs opencode to
    read and follow the existing ``auto-dev-finalize.md`` skill (R6) — no
    plan fetch is needed for FINALIZE.
    """
    del client  # unused: FINALIZE builds its prompt from ticket_id, not the tracker
    if not opencode_available():
        return make_opencode_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=OPENCODE_NOT_FOUND,
            retry_eligible=True,
            retry_delay_seconds=0,
        )
    prompt = build_finalize_prompt(task.ticket_id)
    return _OpencodePreflightOK(
        argv=build_opencode_argv(config.model, worktree, prompt),
        env=build_opencode_env(),
    )


class OpencodeExecutor:
    """StageExecutor backed by a fire-and-forget opencode subprocess (#1669).

    FINALIZE-only: spawn() returns make_blocked(reason=opencode_<stage>_not_implemented)
    if called on any stage other than FINALIZE (#1670 R5). The FINALIZE stage
    materializes a prompt that instructs opencode to read and follow the
    existing ``auto-dev-finalize.md`` skill (R6) and emit the sentinel with the
    correct ``stage_reached`` marker (R1).

    spawn() is non-blocking on the launch path: after synchronous pre-flight
    checks, it launches opencode via ``OpencodeRunner.launch`` (Popen, no wait),
    records a ``Session.local_liveness`` handle (PID + start-time), leaves the
    session ACTIVE, and returns the sid immediately. The opencode run completes
    asynchronously; reconcile/local harvest later detects the dead process,
    parses the JSONL log for the sentinel, and completes the session.

    Pre-flight failures (binary missing) stay synchronous: they persist a
    blocked result to Session.last_result via the door
    (``emit_result_locked``, source=EXECUTOR_DIRECT — RFC 0012 A2), mark the
    session COMPLETED, and emit SESSION_COMPLETED before returning.

    opencode has no ``--output-schema`` (probe-confirmed, #1669 R3); the result
    travels as free-form text in ``text`` event payloads, harvested via the
    ``<<<AUTO_DEV_RESULT>>>`` sentinel pattern. Appropriate only for
    max_parallel=1 lanes (mirrors CodexExecutor).
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: OpencodeRunner | None = None,
    ) -> None:
        self._config = config
        self._runner: OpencodeRunner = (
            runner if runner is not None else RealOpencodeRunner()
        )

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
        wall_clock_budget_seconds: int | None = None,
        parent: str | None = None,
    ) -> str:
        del parent, wall_clock_budget_seconds
        sess = Session(
            name=f"{client.name}/{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}",
            client=client.name,
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=client.workspace_path,
            worktree_path=worktree,
            stage=stage,
            lane=task.lane,
        )
        sid = sess.id
        with sessions_lock():
            state = load_state()
            state.sessions.append(sess)
            save_state(state)

        preflight: AutoDevResult | _OpencodePreflightOK
        if stage != Stage.FINALIZE:
            preflight = make_opencode_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=f"opencode_{stage.value}_not_implemented",
                stage_reached=STAGE4A_MERGE_GATE,
            )
        else:
            preflight = _opencode_preflight(self._config, task, worktree, client)
        argv: list[str] = []
        try:
            if isinstance(preflight, _OpencodePreflightOK):
                argv = preflight.argv
                proc = self._runner.launch(worktree, argv, preflight.env)
                start_time_ns = read_process_start_time_ns(proc.pid)
                if start_time_ns is not None:
                    with sessions_lock():
                        state = load_state()
                        target = next((s for s in state.sessions if s.id == sid), None)
                        if target is not None:
                            target.local_liveness = LocalLivenessHandle(
                                pid=proc.pid,
                                start_time_ns=start_time_ns,
                            )
                            save_state(state)
                    return sid
                with contextlib.suppress(OSError):
                    proc.kill()
                    proc.wait()
                liveness_detail = f"process {proc.pid} start-time unavailable"
                _persist_opencode_runtime_error_diagnostics(
                    session_id=sid, argv=argv, details=liveness_detail
                )
                completion_result = make_opencode_blocked(
                    ticket_id=task.ticket_id,
                    worktree=worktree,
                    reason=LIVENESS_UNAVAILABLE,
                    details=append_diagnostics_pointer(liveness_detail, session_id=sid),
                )
            else:
                completion_result = preflight

            with sessions_lock():
                _complete_session_via_door(
                    sid=sid, payload=completion_result.model_dump(mode="json")
                )
            _record_orchestrator_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": sid,
                    "ticket_id": task.ticket_id,
                    "session_name": sess.name,
                },
            )
        except Exception:
            unexpected_error_detail = "unexpected error during opencode launch"
            _persist_opencode_runtime_error_diagnostics(
                session_id=sid,
                argv=argv,
                details=unexpected_error_detail,
            )
            with sessions_lock():
                _complete_session_via_door(
                    sid=sid,
                    payload=make_opencode_blocked(
                        ticket_id=task.ticket_id,
                        worktree=worktree,
                        reason=UNEXPECTED_ERROR,
                        details=append_diagnostics_pointer(
                            unexpected_error_detail, session_id=sid
                        ),
                    ).model_dump(mode="json"),
                    guard_already_completed=True,
                )
            raise

        return sid

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()


def _persist_opencode_runtime_error_diagnostics(
    *, session_id: str, argv: list[str], details: str
) -> None:
    """Write a ``runtime_error`` diagnostics bundle for an OpencodeExecutor failure.

    Mirrors ``_persist_aider_runtime_error_diagnostics``. *argv* is passed
    through ``redact_argv`` (executor_name="opencode") which redacts the
    trailing prompt positional wholesale (#1669). Never raises.
    """
    failure = build_executor_failure(
        category="runtime_error",
        executor_name="opencode",
        session_id=session_id,
        argv=argv,
        stdout_excerpt="",
        stderr_excerpt=details,
    )
    persist_diagnostics_bundle(
        session_id=session_id,
        role_slug="opencode",
        failure=failure,
    )


def _complete_session_via_door(
    sid: str,
    payload: dict[str, Any],
    *,
    guard_already_completed: bool = False,
) -> None:
    """Route SID's terminal write through the door (RFC 0012 D-A1/D-S3,
    source=EXECUTOR_DIRECT), then transition status to COMPLETED.

    Caller MUST already hold sessions_lock(). Shared by all four
    LocalExecutor/CodexExecutor direct-write sites (#1458) so the
    door-call + not-found-catch + status-transition shape isn't
    duplicated four times.

    Session-not-found (EmitSessionNotFoundError) is logged at debug and
    swallowed -- preserves the pre-migration silent no-op when SID has no
    matching session (R4).

    guard_already_completed=True re-checks SID's status before the door
    call and skips entirely if already COMPLETED -- the exception-handler
    idempotency guard the two ``except Exception:`` sites carried before
    this migration (R1). The two main-path sites (each the session's
    first and only completion write) pass the default False.

    Door refusal (a terminal result already recorded by another writer)
    is a normal, non-raising return -- the status transition below still
    runs regardless (R5): refusal affects only the last_result write, not
    the executor's status/event bookkeeping.
    """
    if guard_already_completed:
        state = load_state()
        target = next((s for s in state.sessions if s.id == sid), None)
        if target is None or target.status == SessionStatus.COMPLETED:
            return
    try:
        emit_result_locked(payload, sid, source=LastResultSource.EXECUTOR_DIRECT)
    except EmitSessionNotFoundError:
        _log.debug("executor_direct emit skipped: session %s not found", sid)
        return
    state = load_state()
    target = next((s for s in state.sessions if s.id == sid), None)
    if target is not None:
        target.status = SessionStatus.COMPLETED
        save_state(state)


class CodexExecutor:
    """StageExecutor backed by prompt-driven ``codex exec`` reviewers (#1236).

    REVIEW-only: spawn() returns make_codex_blocked(reason=CODEX_REVIEW_ONLY) if
    called on any stage other than REVIEW. Step 3 delegates to
    ``codex_review.run_review``, which runs a per-reviewer-role loop of generic
    ``codex exec`` calls (each fed a materialized prompt over stdin), validates
    every reviewer's structured output through the ``review_findings`` library,
    and synthesizes a typed AutoDevResult from the consolidated verdict. The
    consolidated verdict is posted as a GitHub issue comment on a clean run.

    spawn() is NOT synchronous (#1727). Pre-flight (Steps 1-2) runs on the
    caller's thread; once it passes, the review is handed to a
    ``cw.codex_background`` daemon thread and spawn() returns the session id
    immediately. Blocking here would freeze the shared ``dispatch_tick`` stack
    for the whole review, stalling every other client and lane — see the
    StageExecutor Protocol invariant above.

    Like LocalExecutor, it bypasses stdout-sentinel parsing: the
    SESSION_COMPLETED event carries no result payload, so dispatch consumes the
    last_result written via the door (``emit_result_locked``,
    source=EXECUTOR_DIRECT — RFC 0012 A2, #1458) as-is.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: CodexRunner | None = None,
        background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._config = config
        self._runner: CodexRunner = runner if runner is not None else RealCodexRunner()
        # Testability seam for the threading handoff: tests inject
        # ``lambda fn: fn()`` to run the review inline and keep their
        # assertions deterministic.
        self._background = background if background is not None else _default_background

    def spawn(
        self,
        *,
        stage: Stage,
        task: TicketTask,
        worktree: Path,
        client: ClientConfig,
        wall_clock_budget_seconds: int | None = None,
        parent: str | None = None,
    ) -> str:
        # parent is intentionally unused; codex has no parent-session concept.
        del parent
        # Step 1: Create Session with all required fields.
        sess = Session(
            name=f"{client.name}/{AUTO_DEV_LABEL_PREFIX}{task.ticket_id}",
            client=client.name,
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=client.workspace_path,
            worktree_path=worktree,
            stage=stage,
            lane=task.lane,
        )
        sid = sess.id
        with sessions_lock():
            state = load_state()
            state.sessions.append(sess)
            save_state(state)

        # Step 2: Pre-flight checks (first match assigns result).
        result: AutoDevResult | None = None
        if stage != Stage.REVIEW:
            result = make_codex_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=CODEX_REVIEW_ONLY,
            )
        elif shutil.which("codex") is None:
            result = make_codex_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=CODEX_NOT_FOUND,
            )

        if result is not None:
            # Pre-flight failed: nothing to review, so persist + emit inline.
            # This branch is cheap and has no subprocess in it, so keeping it
            # on the caller's thread costs dispatch nothing.
            try:
                with sessions_lock():
                    _complete_session_via_door(
                        sid=sid, payload=result.model_dump(mode="json")
                    )
                _record_orchestrator_event(
                    OrchestratorEventType.SESSION_COMPLETED,
                    {
                        "session_id": sid,
                        "ticket_id": task.ticket_id,
                        "session_name": sess.name,
                    },
                )
            except Exception:
                # Never leave the session ACTIVE on an unexpected error. The
                # re-raise is correct *here* (unlike on the background path):
                # dispatch is still on this stack and its own handler reverts
                # the claimed task to PENDING.
                _complete_session_as_unexpected_error(sid, task, worktree)
                raise
            return sid

        # Pre-flight passed. Stamp session_id onto the still-RUNNING dev-queue
        # row *before* backgrounding (#1727 R1): dispatch stamps it too, but
        # only after spawn() returns, so a crash in that window would otherwise
        # leave a live codex session with no queue row pointing at it and no
        # way to attribute the failure. Deliberately narrower than dispatch's
        # own post-spawn stamp — session_id only, not the error-counter reset
        # or stage_base_ref — so backoff semantics keep a single owner.
        _stamp_session_id_on_running_task(
            client_name=client.name, ticket_id=task.ticket_id, session_id=sid
        )

        # Steps 3/4/4b/5 run off the dispatch_tick call stack (#1727).
        self._background(
            lambda: _run_codex_review_and_complete(
                runner=self._runner,
                task=task,
                worktree=worktree,
                client=client,
                wall_clock_budget_seconds=wall_clock_budget_seconds,
                sid=sid,
                sess_name=sess.name,
                config_model=self._config.model,
            )
        )
        return sid

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()
