"""Tests for cw.executor.local — LocalExecutor (aider backend).

RFC 0005 F3.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.exceptions import EmitSessionNotFoundError
from cw.executor import (
    LocalExecutor,
    StageExecutor,
    _local_preflight,
    _PreflightOK,
    resolve_executor,
)
from cw.executor_diagnostics import (
    ExecutorFailure,
    diagnostics_bundle_dir,
    render_bundle_path,
)
from cw.local_runner import (
    _PATH_FREE_TASK_INSTRUCTION,
    AIDER_NOT_FOUND,
    ENDPOINT_NOT_CONFIGURED,
    LIVENESS_UNAVAILABLE,
    PLAN_MISSING,
    TASK_CONTEXT_RELATIVE_PATH,
    UNEXPECTED_ERROR,
    FakeAiderRunner,
)
from cw.models import (
    LOCAL_BACKEND,
    ClientConfig,
    LastResultSource,
    LocalLivenessHandle,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.result import EmitOutcome
from tests.conftest import commit_tracked_file, find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# RFC 0005 F3 — LocalExecutor + resolve_executor LOCAL_BACKEND
# ---------------------------------------------------------------------------


def _make_local_client(tmp_path: Path, *, endpoint: str | None = None) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={
                Stage.IMPL: StageExecutorConfig(
                    backend=LOCAL_BACKEND,
                    model="qwen2.5-coder:32b",
                    endpoint=endpoint,
                )
            }
        ),
    )


def test_resolve_executor_returns_local_executor(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """resolve_executor returns LocalExecutor when backend=local."""
    client = _make_local_client(tmp_path, endpoint="http://localhost:1234/v1")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    executor = resolve_executor(task, client)

    assert isinstance(executor, LocalExecutor)
    assert isinstance(executor, StageExecutor)


def test_local_executor_blocked_endpoint_none(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """endpoint=None → blocked/endpoint_not_configured before runner is called."""
    worktree = make_git_repo("wt-local-ep-none")
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == ENDPOINT_NOT_CONFIGURED


def test_local_executor_blocked_aider_not_found(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """aider_available() is False → blocked/aider_not_found."""
    worktree = make_git_repo("wt-local-aider-missing")
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.local.aider_available", return_value=False):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == AIDER_NOT_FOUND
    assert result.blocker.retry_eligible is True


def test_local_executor_blocked_plan_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Absent .cw/plan.md → blocked/plan_missing."""
    worktree = make_git_repo("wt-local-plan-missing")
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.local.aider_available", return_value=True):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == PLAN_MISSING


def test_local_executor_spawn_runner_path(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Happy path: pre-flight passes → launch() called once, session left ACTIVE.

    The fire-and-forget launch records a liveness handle and returns the sid; it
    does NOT block, synthesize a result, or write last_result — reconcile/local
    harvest completes the session once the process exits.
    """
    worktree = make_git_repo("wt-local-runner-path")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-99", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.local.aider_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        call = fake_runner.calls[0]
        argv = cast("list[str]", call["argv"])
        assert "openai/qwen" in " ".join(argv)

        state = load_state()
        session = next((s for s in state.sessions if s.id == sid), None)
        assert session is not None
        # Session stays ACTIVE; liveness handle recorded; no result synthesized.
        assert session.status == SessionStatus.ACTIVE
        assert isinstance(session.local_liveness, LocalLivenessHandle)
        assert session.local_liveness.pid == fake_runner.procs[0].pid
        assert session.last_result is None
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_executor_stage_sentinel_schema(tmp_path: Path) -> None:
    """LocalExecutor.stage_sentinel_schema returns AutoDevResult JSON schema."""
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config)

    schema = executor.stage_sentinel_schema(Stage.IMPL)

    assert schema == AutoDevResult.model_json_schema()


def test_local_executor_launch_records_liveness_and_returns_active(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Pre-flight passes → session ACTIVE with a LocalLivenessHandle; sid returned."""
    worktree = make_git_repo("wt-local-launch-active")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-100", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.local.aider_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert isinstance(sid, str)
        state = load_state()
        session = next((s for s in state.sessions if s.id == sid), None)
        assert session is not None
        assert session.status == SessionStatus.ACTIVE
        assert isinstance(session.local_liveness, LocalLivenessHandle)
        # Start-time was captured (live process) — a positive ns value.
        assert session.local_liveness.start_time_ns > 0
        assert session.last_result is None
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_executor_exception_handler_marks_session_completed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """launch() raises OSError → session COMPLETED + UNEXPECTED_ERROR, re-raised."""
    worktree = make_git_repo("wt-local-exc-handler")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-exc", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.local.aider_available", return_value=True),
        patch.object(fake_runner, "launch", side_effect=OSError("exec boom")),
        pytest.raises(OSError, match="exec boom"),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    state = load_state()
    session = find_completed_session(state)
    assert session.status == SessionStatus.COMPLETED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert (
        result.blocker.details == "unexpected error during aider launch "
        f"[diagnostics: {render_bundle_path(session.id)}]"
    )


def test_local_executor_proc_stat_unreadable_marks_session_completed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """read_process_start_time_ns returns None → session COMPLETED, no exception.

    When /proc/<pid>/stat is unreadable immediately after launch (process exited
    before exec or /proc transiently unavailable), storing start_time_ns=0 would
    make every liveness check return False — triggering premature harvest while
    aider is still running. Instead the orphan is killed and the session completes
    synchronously with LIVENESS_UNAVAILABLE so dispatch retries (no liveness handle
    persisted, no exception propagated).
    """
    import contextlib

    worktree = make_git_repo("wt-proc-unreadable")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-proc", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.local.aider_available", return_value=True),
        patch("cw.executor.local.read_process_start_time_ns", return_value=None),
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    # FakeAiderRunner spawned a sleep process; the None path kills it but
    # suppress in case it already exited.
    for proc in fake_runner.procs:
        with contextlib.suppress(OSError):
            proc.kill()
            proc.wait()

    state = load_state()
    session = find_completed_session(state)
    assert session.status == SessionStatus.COMPLETED
    assert session.local_liveness is None  # no stale handle recorded
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == LIVENESS_UNAVAILABLE
    # details carries the liveness detail plus a diagnostics-bundle pointer
    # (#1239) — exact match, since the session id is now captured.
    assert result.blocker.details == (
        f"process {fake_runner.procs[-1].pid} start-time unavailable "
        f"[diagnostics: {render_bundle_path(sid)}]"
    )


def test_local_executor_emit_session_not_found_logs_and_skips(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """emit_result_locked raises EmitSessionNotFoundError → spawn() returns
    normally and the session's status is not force-completed (R4).

    Exercises _complete_session_via_door's not-found catch on the LocalExecutor
    main completion path (Site 1, pre-flight-blocked branch).
    """
    worktree = make_git_repo("wt-local-emit-not-found")
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch(
        "cw.executor.core.emit_result_locked",
        side_effect=EmitSessionNotFoundError("not found", session_id="ignored"),
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = next((s for s in state.sessions if s.id == sid), None)
    assert session is not None
    assert session.status == SessionStatus.ACTIVE
    assert session.last_result is None


def test_local_executor_door_refusal_still_completes_session(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Door refusal (terminal result already recorded by another writer) still
    transitions the session to COMPLETED and emits SESSION_COMPLETED (R5) --
    refusal affects only the last_result write, not status/event bookkeeping.
    """
    worktree = make_git_repo("wt-local-door-refusal")
    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=None)
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    def _refuse(
        payload: dict[str, object], sid: str, *, source: LastResultSource
    ) -> EmitOutcome:
        del payload, source
        return EmitOutcome(
            session_id=sid,
            result=None,
            prior_status="shipped",
            refused=True,
            existing_result={"status": "shipped"},
            existing_source=LastResultSource.STOP_HOOK_HARVEST,
        )

    with (
        patch("cw.executor.core.emit_result_locked", side_effect=_refuse),
        patch("cw.executor.local._record_orchestrator_event") as record_mock,
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    state = load_state()
    session = next((s for s in state.sessions if s.id == sid), None)
    assert session is not None
    assert session.status == SessionStatus.COMPLETED
    record_mock.assert_called_once()


def test_local_executor_liveness_unavailable_persists_runtime_error_diagnostics(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The LIVENESS_UNAVAILABLE branch persists a runtime_error bundle (#1239)."""
    import contextlib

    worktree = make_git_repo("wt-liveness-diag")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-liveness-diag", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.local.aider_available", return_value=True),
        patch("cw.executor.local.read_process_start_time_ns", return_value=None),
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    for proc in fake_runner.procs:
        with contextlib.suppress(OSError):
            proc.kill()
            proc.wait()

    # Filename now carries an occurred_at timestamp suffix (#1330 item 7).
    [path] = list(diagnostics_bundle_dir(sid).glob("aider-runtime_error-*.json"))
    assert path.exists()
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.category == "runtime_error"
    assert failure.executor_name == "aider"
    # Aider argv is redacted wholesale on the --message value.
    assert "--message" in failure.argv_sanitized
    idx = failure.argv_sanitized.index("--message")
    assert failure.argv_sanitized[idx + 1].startswith("<redacted:")


def test_local_executor_unexpected_error_persists_diagnostics(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The generic except branch persists a runtime_error bundle (#1239)."""
    worktree = make_git_repo("wt-unexpected-diag")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-unexpected-diag", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.local.aider_available", return_value=True),
        patch.object(fake_runner, "launch", side_effect=OSError("exec boom")),
        pytest.raises(OSError, match="exec boom"),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    # spawn raised, so recover sid from the created session in state.
    session = next(s for s in load_state().sessions if s.last_result is not None)
    # Filename now carries an occurred_at timestamp suffix (#1330 item 7).
    [path] = list(diagnostics_bundle_dir(session.id).glob("aider-runtime_error-*.json"))
    assert path.exists()
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.category == "runtime_error"
    assert failure.executor_name == "aider"


# ---------------------------------------------------------------------------
# RFC 0005 F3 #896 — LocalExecutor fetches plan from GitHub tracker fallback
# ---------------------------------------------------------------------------


def _write_tracker_config(workspace: Path, tracker: str) -> None:
    """Write a minimal .claude/project-config.yaml for the given tracker."""
    config_dir = workspace / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "project-config.yaml").write_text(
        f"tracking:\n  primary:\n    system: {tracker}\n",
        encoding="utf-8",
    )


def test_local_executor_fetches_plan_from_tracker_when_absent(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No .cw/plan.md + github-issues tracker + fetcher returns plan → aider runs."""
    workspace = make_git_repo("wt-tracker-fetch-workspace")
    worktree = make_git_repo("wt-tracker-fetch")
    _write_tracker_config(workspace, "github-issues")

    plan_body = "## Plan\n\nDo the thing.\n<!-- plan-spec-reviewed: 2026-01-01 v1 -->"

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    try:
        with (
            patch("cw.executor.local.aider_available", return_value=True),
            patch(
                "cw.executor.GithubIssuePlanFetcher.fetch",
                return_value=plan_body,
            ),
        ):
            executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        # aider was launched (pre-flight passed the plan check)
        assert len(fake_runner.calls) == 1
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_executor_plan_missing_when_tracker_returns_none(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No .cw/plan.md + github-issues tracker + fetcher returns None → plan_missing."""
    workspace = make_git_repo("wt-tracker-none-workspace")
    worktree = make_git_repo("wt-tracker-none")
    _write_tracker_config(workspace, "github-issues")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.local.aider_available", return_value=True),
        patch("cw.executor.GithubIssuePlanFetcher.fetch", return_value=None),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == PLAN_MISSING


def test_local_executor_no_tracker_no_plan_is_plan_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No .cw/plan.md, no tracker config → plan_missing (no fetch attempted)."""
    workspace = make_git_repo("wt-no-tracker-workspace")
    worktree = make_git_repo("wt-no-tracker")
    # No .claude/project-config.yaml in workspace → resolve_tracker returns None

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    with patch("cw.executor.local.aider_available", return_value=True):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    result = AutoDevResult.model_validate(result_raw)
    assert result.blocker is not None
    assert result.blocker.reason == PLAN_MISSING


def test_local_executor_launch_reached_after_tracker_fetch(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Plan fetched from tracker → launch() reached and liveness recorded.

    The fire-and-forget launch no longer synthesizes a result inline, so there
    is no plan_source threading to assert; the observable contract is that
    pre-flight passed (aider was launched) and the session carries a handle.
    """
    workspace = make_git_repo("wt-plansrc-workspace")
    worktree = make_git_repo("wt-plansrc")
    _write_tracker_config(workspace, "github-issues")

    plan_body = "## Plan\n\nDo the thing.\n<!-- plan-spec-reviewed: 2026-01-01 v1 -->"

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=workspace)
    task = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)

    try:
        with (
            patch("cw.executor.local.aider_available", return_value=True),
            patch("cw.executor.GithubIssuePlanFetcher.fetch", return_value=plan_body),
        ):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        state = load_state()
        session = next((s for s in state.sessions if s.id == sid), None)
        assert session is not None
        assert isinstance(session.local_liveness, LocalLivenessHandle)
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_preflight_success_returns_preflight_ok(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """_local_preflight returns _PreflightOK on all-checks-pass.

    Locks in the discriminated-union contract: callers narrow with
    isinstance(_PreflightOK) instead of testing the first element for None.
    """
    worktree = make_git_repo("wt-preflight-ok")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-ok", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.local.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert result.endpoint == "http://localhost:1234/v1"
    assert result.model == "qwen"
    # Post-#1905: the plan body reaches the model through the read-only
    # task-context file, never through the mention-scanned --message string.
    assert result.task_message == _PATH_FREE_TASK_INSTRUCTION
    assert "do the thing" not in result.task_message
    assert "do the thing" in (worktree / TASK_CONTEXT_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )


# The ticket's own reproduction shape (#1905): plan prose that names paths the
# implementation must NOT edit, which aider's mention scan used to force-add.
_EXCLUSION_PLAN = """## Summary

Rework the staleness monitor.

**EXPLICITLY OUT OF SCOPE:** `core/database.py`, `etl_mcp/api/x.py`

## Touch-point Contract
- `core/models/auth.py:5-13` for a TypedDict shape

## Files Modified
- src/real_target.py
"""


def _local_spawn_argv(
    worktree: Path,
    plan_text: str,
) -> list[str]:
    """Run a full LocalExecutor.spawn through FakeAiderRunner; return the argv."""
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text(plan_text, encoding="utf-8")

    fake_runner = FakeAiderRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-files", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.local.aider_available", return_value=True):
            executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )
        return cast("list[str]", fake_runner.calls[0]["argv"])
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_preflight_threads_files_modified_into_preflight_ok(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The plan's file manifest is parsed and threaded onto _PreflightOK (#1905)."""
    worktree = make_git_repo("wt-preflight-files")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text(
        "## Files Modified\n- src/cw/a.py\n- tests/test_a.py\n", encoding="utf-8"
    )

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-files", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.local.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert result.files == ["src/cw/a.py", "tests/test_a.py"]


def test_local_preflight_files_empty_without_files_modified_heading(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A plan with no manifest section falls back to zero --file flags (A1)."""
    worktree = make_git_repo("wt-preflight-no-files")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-nofiles", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.local.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert result.files == []


def test_local_preflight_threads_aiderignore_path_into_preflight_ok(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A non-empty manifest threads a materialised aiderignore path onto
    _PreflightOK (#1915)."""
    worktree = make_git_repo("wt-preflight-aiderignore")
    commit_tracked_file(worktree, "core/database.py")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text(
        "## Files Modified\n- src/real_target.py\n", encoding="utf-8"
    )

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-aiderignore", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.local.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert isinstance(result.aiderignore_path, Path)
    assert "/core/database.py" in result.aiderignore_path.read_text(encoding="utf-8")


def test_local_preflight_aiderignore_path_none_without_manifest(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No manifest section → no aiderignore emitted (mirrors the empty-files
    fallback contract, #1915)."""
    worktree = make_git_repo("wt-preflight-aiderignore-none")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-aiderignore-none", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.local.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert result.aiderignore_path is None


def test_local_preflight_threads_read_only_path_into_preflight_ok(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """_PreflightOK carries the materialised read-only task-context path."""
    worktree = make_git_repo("wt-preflight-readonly")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan body here", encoding="utf-8")
    (cw_dir / "context.json").write_text(
        '{"title": "T", "body": "B"}', encoding="utf-8"
    )

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-ro", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.local.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert result.read_only_path == worktree / TASK_CONTEXT_RELATIVE_PATH
    context = result.read_only_path.read_text(encoding="utf-8")
    assert "plan body here" in context
    assert "T" in context


def test_local_executor_spawn_passes_file_flags_to_argv(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """End-to-end: each manifest path reaches aider's argv as a --file pair."""
    worktree = make_git_repo("wt-spawn-file-flags")
    argv = _local_spawn_argv(
        worktree, "## Files Modified\n- src/cw/a.py\n- tests/test_a.py\n"
    )

    first = argv.index("--file")
    assert argv[first : first + 4] == [
        "--file",
        "src/cw/a.py",
        "--file",
        "tests/test_a.py",
    ]


def test_local_executor_spawn_passes_read_flag_to_argv(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """End-to-end: the argv aider actually receives carries no excluded path.

    The spawn-level counterpart of test_build_task_message_is_path_free_*:
    closes the loop from "the unit function is path-free" to "the subprocess
    argv is path-free" (#1905).
    """
    worktree = make_git_repo("wt-spawn-read-flag")
    argv = _local_spawn_argv(worktree, _EXCLUSION_PLAN)

    read_idx = argv.index("--read")
    assert argv[read_idx + 1] == str(worktree / TASK_CONTEXT_RELATIVE_PATH)
    message = argv[argv.index("--message") + 1]
    for path in ("core/database.py", "etl_mcp", "auth.py"):
        assert path not in message


def test_local_executor_spawn_passes_aiderignore_flag_to_argv(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """End-to-end: the argv aider actually receives blocks every tracked file
    outside the manifest via --aiderignore. This is the test that most
    directly encodes #1915's acceptance criterion — a model reply echoing a
    non-manifest path cannot cause a chat-file addition, because the excluded
    files never enter aider's addable-file universe in the first place."""
    worktree = make_git_repo("wt-spawn-aiderignore")
    commit_tracked_file(worktree, "core/database.py")
    commit_tracked_file(worktree, "etl_mcp/api/x.py")
    argv = _local_spawn_argv(worktree, _EXCLUSION_PLAN)

    assert "--aiderignore" in argv
    idx = argv.index("--aiderignore")
    aiderignore_path = Path(argv[idx + 1])
    lines = aiderignore_path.read_text(encoding="utf-8").splitlines()
    assert "/core/database.py" in lines
    assert "/etl_mcp/api/x.py" in lines
    assert "/src/real_target.py" not in lines


def test_local_preflight_ok_model_none_defaults_to_empty_string(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """_PreflightOK.model is '' when config.model is None."""
    worktree = make_git_repo("wt-preflight-model-none")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan", encoding="utf-8")

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model=None, endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-mnone", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.local.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client)

    assert isinstance(result, _PreflightOK)
    assert result.model == ""
