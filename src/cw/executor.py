"""RFC 0005 A2/E1/F3 — StageExecutor seam + ClaudeNativeExecutor + LocalExecutor."""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from cw.auto_dev_result import AutoDevResult
from cw.codex_fix_loop import run_review_with_fix_loop
from cw.codex_review import (
    STAGE3_REVIEW,
    render_verdict_comment,
)
from cw.codex_runner import CodexRunner, RealCodexRunner
from cw.config import load_state, save_state, sessions_lock
from cw.events import record_event as _record_orchestrator_event
from cw.exceptions import EmitSessionNotFoundError
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)
from cw.gh import post_issue_comment
from cw.local_runner import (
    AIDER_NOT_FOUND,
    ENDPOINT_NOT_CONFIGURED,
    LIVENESS_UNAVAILABLE,
    PLAN_MISSING,
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
from cw.reconcile import AUTO_DEV_LABEL_PREFIX
from cw.result import emit_result_locked
from cw.spawn import spawn_create_impl
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.native_daemon import NativeDaemonClient
    from cw.review_findings import ReviewVerdict

_log = logging.getLogger(__name__)

# Session-level CodexExecutor pre-flight blocker reason codes (RFC 0005 F1).
# Per-role failure reason codes (CODEX_TIMEOUT/CODEX_ERROR/
# CODEX_REVIEW_UNPARSEABLE/CODEX_MUST_FIX_FINDINGS) live in cw.codex_review.
CODEX_NOT_FOUND = "codex_not_found"
CODEX_REVIEW_ONLY = "codex_review_only"

# Capability-probe diagnosis (distinct from codex_review.py's per-role review
# failure-reason vocabulary): the binary is present but `codex --version`
# could not be confirmed.
CODEX_VERSION_UNKNOWN = "codex_version_unknown"

# Matches a dotted major.minor.patch version number anywhere in a
# `codex --version` output line (#1238). Real CLI banners are name-prefixed
# (e.g. ``codex-cli 0.136.0``, confirmed live), not a bare version string like
# ``_check_claude_version``'s sibling probe — search the whole line rather
# than assuming the version is the first whitespace token.
_CODEX_VERSION_RE = re.compile(r"\d+(?:\.\d+){2}")

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
    return _PreflightOK(
        endpoint=config.endpoint,  # narrowed: is-None check above
        model=config.model or "",
        task_message=task_message,
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

    Result delivery bypasses persist_last_result (no sentinel framing). Every
    SESSION_COMPLETED event this class or the harvest path emits carries no
    'stdout' key, so dispatch.py's isinstance(stdout, str) guard is False and
    persist_last_result is skipped; the last_result written via the door
    (source=EXECUTOR_DIRECT) is consumed as-is by consume_completed_sessions.
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
                argv = build_argv(preflight.model, preflight.task_message)
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
            # Persist the blocked result, mark COMPLETED, and emit SESSION_COMPLETED
            # — no "stdout" key so dispatch skips persist_last_result and uses
            # last_result.
            with sessions_lock():
                _complete_session_via_door(
                    sid, completion_result.model_dump(mode="json")
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
                    sid,
                    make_blocked(
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

    REVIEW-only: spawn() returns make_blocked(reason=CODEX_REVIEW_ONLY) if
    called on any stage other than REVIEW. Step 3 delegates to
    ``codex_review.run_review``, which runs a per-reviewer-role loop of generic
    ``codex exec`` calls (each fed a materialized prompt over stdin), validates
    every reviewer's structured output through the ``review_findings`` library,
    and synthesizes a typed AutoDevResult from the consolidated verdict. The
    consolidated verdict is posted as a GitHub issue comment on a clean run.

    Like LocalExecutor, spawn() is synchronous and bypasses persist_last_result:
    the SESSION_COMPLETED event carries no 'stdout' key, so dispatch consumes the
    last_result written here via the door (``emit_result_locked``,
    source=EXECUTOR_DIRECT — RFC 0012 A2, #1458) as-is. Appropriate only for
    max_parallel=1 lanes.
    """

    def __init__(
        self,
        *,
        config: StageExecutorConfig,
        runner: CodexRunner | None = None,
    ) -> None:
        self._config = config
        self._runner: CodexRunner = runner if runner is not None else RealCodexRunner()

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
        verdict: ReviewVerdict | None = None
        if stage != Stage.REVIEW:
            result = make_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=CODEX_REVIEW_ONLY,
                stage_reached=STAGE3_REVIEW,
            )
        elif shutil.which("codex") is None:
            result = make_blocked(
                ticket_id=task.ticket_id,
                worktree=worktree,
                reason=CODEX_NOT_FOUND,
                stage_reached=STAGE3_REVIEW,
            )

        try:
            if result is None:
                # Step 3: Run the per-role review pass + bounded fix loop
                # (delegated to codex_fix_loop, #1392).
                result, verdict = run_review_with_fix_loop(
                    runner=self._runner,
                    task=task,
                    worktree=worktree,
                    default_branch=client.default_branch,
                    model=self._config.model,
                    wall_clock_budget_seconds=wall_clock_budget_seconds,
                    session_id=sid,
                )

            # Step 4: Persist result under sessions_lock.
            with sessions_lock():
                _complete_session_via_door(sid, result.model_dump(mode="json"))

            # Step 4b: Post the consolidated verdict as an issue comment
            # (best-effort). Runs after save_state so a retry on save_state
            # failure does not post a duplicate comment. verdict is None only
            # when every reviewer failed (no documents to render).
            if verdict is not None:
                _post_review_comment(
                    task.ticket_id,
                    render_verdict_comment(verdict),
                    cwd=_git_dir(client),
                )

            # Step 5: Emit SESSION_COMPLETED — no "stdout" key so dispatch skips
            # persist_last_result and uses the last_result written in Step 4.
            _record_orchestrator_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": sid,
                    "ticket_id": task.ticket_id,
                    "session_name": sess.name,
                },
            )
        except Exception:
            # Ensure the session is never left ACTIVE on unexpected errors (e.g.
            # git CalledProcessError from run_review's diff capture). Mark it
            # COMPLETED with a blocked result so reconcile can clean it up.
            # SESSION_COMPLETED is NOT emitted; dispatch's exception handler
            # reverts the task to PENDING, which is the correct recovery path.
            with sessions_lock():
                _complete_session_via_door(
                    sid,
                    make_blocked(
                        ticket_id=task.ticket_id,
                        worktree=worktree,
                        reason=UNEXPECTED_ERROR,
                        stage_reached=STAGE3_REVIEW,
                    ).model_dump(mode="json"),
                    guard_already_completed=True,
                )
            raise

        return sid

    def stage_sentinel_schema(self, _stage: Stage) -> dict[str, Any]:
        return AutoDevResult.model_json_schema()


def _post_review_comment(
    ticket_id: str, review_text: str, *, cwd: Path | None = None
) -> None:
    """Post codex review findings as a GitHub issue comment (best-effort, logged).

    Delegates to the shared ``cw.gh.post_issue_comment`` primitive. A failed
    post is logged at warning (ticket_id, returncode, stderr) rather than
    swallowed silently — for the CODEX_MUST_FIX_FINDINGS path this comment is
    the only destination for the finding text (GitHub #1391).

    *cwd* scopes the gh call to the client's repo (GitHub #1269/#1279).
    """
    result = post_issue_comment(ticket_id, review_text, cwd=cwd)
    if result is None:
        _log.warning("review_comment_post_failed ticket=%s: gh call failed", ticket_id)
        return
    if result.returncode != 0:
        _log.warning(
            "review_comment_post_failed ticket=%s rc=%s: %s",
            ticket_id,
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )
