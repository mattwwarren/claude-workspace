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
    SnapshotPlanFetcher,
    StageExecutor,
    _local_preflight,
    _PreflightOK,
    resolve_executor,
)
from cw.executor.core import FakeFireAndForgetRunner
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
from cw.opencode_runner import stage_entry_marker
from cw.result import EmitOutcome
from cw.stage_preflight import PreflightReason
from cw.ticket_snapshot import SnapshotContext, TicketSnapshot, normalize_ticket_id
from cw.worktree import resolve_worker_tmpdir
from tests._shared.git_repos import commit_tracked_file
from tests._shared.session_seeding import find_completed_session
from tests._shared.stage_preflight_builders import (
    engine_client,
    repair_row,
    review_client,
)
from tests._shared.ticket_context import make_comment, make_snapshot_from, seeded_row

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
    fake_runner = FakeFireAndForgetRunner()
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
    fake_runner = FakeFireAndForgetRunner()
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
    fake_runner = FakeFireAndForgetRunner()
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

    fake_runner = FakeFireAndForgetRunner()
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
        assert session.local_liveness.backend == "aider"
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

    fake_runner = FakeFireAndForgetRunner()
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

    fake_runner = FakeFireAndForgetRunner()
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

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-proc", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.local.aider_available", return_value=True),
        patch("cw.executor.core.read_process_start_time_ns", return_value=None),
    ):
        sid = executor.spawn(
            stage=Stage.IMPL, task=task, worktree=worktree, client=client
        )

    # FakeFireAndForgetRunner spawned a sleep process; the None path kills it but
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
    fake_runner = FakeFireAndForgetRunner()
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
    fake_runner = FakeFireAndForgetRunner()
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
        patch("cw.executor.core._record_orchestrator_event") as record_mock,
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

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-liveness-diag", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.local.aider_available", return_value=True),
        patch("cw.executor.core.read_process_start_time_ns", return_value=None),
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

    fake_runner = FakeFireAndForgetRunner()
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


_PLAN_BODY = "## Plan\n\nDo the thing.\n<!-- plan-spec-reviewed: 2026-01-01 v1 -->"
_OPERATOR = "operator"


def _plan_snapshot(
    *,
    body: str = _PLAN_BODY,
    author_id: str | None = _OPERATOR,
    agent_authored: bool = False,
    author_conflict: bool = False,
) -> TicketSnapshot:
    """A snapshot of ticket 896 whose one comment is *body* by *author_id*."""
    comment = make_comment(
        "p1",
        body=body,
        author_id=author_id,
        agent_authored=agent_authored,
        author_conflict=author_conflict,
    )
    return make_snapshot_from(
        [comment],
        ticket_id=normalize_ticket_id("896"),
        client="test",
    )


def test_local_executor_fetches_plan_from_snapshot_when_absent(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """No .cw/plan.md + github-issues row on a plan snapshot -> aider runs (no gh)."""
    workspace = make_git_repo("wt-tracker-fetch-workspace")
    worktree = make_git_repo("wt-tracker-fetch")
    _write_tracker_config(workspace, "github-issues")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(
        name="test", workspace_path=workspace, operator_github_login=_OPERATOR
    )
    task = seeded_row(
        _plan_snapshot(), ticket_id="896", client="test", stage=Stage.IMPL
    )

    try:
        with patch("cw.executor.local.aider_available", return_value=True):
            executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        # aider was launched (pre-flight passed the plan check) and the plan
        # was materialised from the snapshot, with no gh launch.
        assert len(fake_runner.calls) == 1
        assert (worktree / ".cw" / "plan.md").read_text(encoding="utf-8") == _PLAN_BODY
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_local_executor_github_row_without_a_digest_is_plan_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Accepted narrowing (RFC 0019 T2): no snapshot digest, no live gh fallback."""
    workspace = make_git_repo("wt-tracker-none-workspace")
    worktree = make_git_repo("wt-tracker-none")
    _write_tracker_config(workspace, "github-issues")

    fake_runner = FakeFireAndForgetRunner()
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

    fake_runner = FakeFireAndForgetRunner()
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


def test_local_executor_launch_reached_after_snapshot_plan(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Plan recovered from the snapshot -> launch() reached and liveness recorded.

    The fire-and-forget launch no longer synthesizes a result inline, so there
    is no plan_source threading to assert; the observable contract is that
    pre-flight passed (aider was launched) and the session carries a handle.
    """
    workspace = make_git_repo("wt-plansrc-workspace")
    worktree = make_git_repo("wt-plansrc")
    _write_tracker_config(workspace, "github-issues")

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="m", endpoint="http://localhost:1234/v1"
    )
    executor = LocalExecutor(config=config, runner=fake_runner)
    client = ClientConfig(
        name="test", workspace_path=workspace, operator_github_login=_OPERATOR
    )
    task = seeded_row(
        _plan_snapshot(), ticket_id="896", client="test", stage=Stage.IMPL
    )

    try:
        with patch("cw.executor.local.aider_available", return_value=True):
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


# ---------------------------------------------------------------------------
# SnapshotPlanFetcher (RFC 0019 T2): the approved plan from the stored snapshot
# ---------------------------------------------------------------------------


def _fetch_plan(
    snapshot: TicketSnapshot,
    ctx: SnapshotContext,
    *,
    login: str | None = _OPERATOR,
    ticket_id: str = "896",
) -> str | None:
    task = seeded_row(snapshot, ticket_id="896", client="test")
    return SnapshotPlanFetcher(task, ctx, gh_login=login).fetch(ticket_id)


def test_snapshot_plan_fetcher_github_returns_the_last_gh_login_plan(
    tmp_config_dir: Path,
) -> None:
    older = make_comment("p1", body=_PLAN_BODY + "\nold", author_id=_OPERATOR)
    newer = make_comment("p2", body=_PLAN_BODY + "\nnew", author_id=_OPERATOR)
    snapshot = make_snapshot_from(
        [older, newer], ticket_id=normalize_ticket_id("896"), client="test"
    )
    assert _fetch_plan(snapshot, SnapshotContext(github=True)) == _PLAN_BODY + "\nnew"


@pytest.mark.parametrize(
    ("snapshot", "login"),
    [
        (_plan_snapshot(author_id="someone-else"), _OPERATOR),
        (_plan_snapshot(body="no marker here"), _OPERATOR),
        (_plan_snapshot(), None),
    ],
    ids=["not-the-gh-login", "no-marker", "no-gh-login"],
)
def test_snapshot_plan_fetcher_github_binds_nothing_untrusted(
    tmp_config_dir: Path, snapshot: TicketSnapshot, login: str | None
) -> None:
    assert _fetch_plan(snapshot, SnapshotContext(github=True), login=login) is None


def test_snapshot_plan_fetcher_github_ignores_the_agent_marker_on_the_trusted_login(
    tmp_config_dir: Path,
) -> None:
    agent = _plan_snapshot(agent_authored=True)
    assert _fetch_plan(agent, SnapshotContext(github=True)) == _PLAN_BODY


def test_snapshot_plan_fetcher_mcp_binds_operator_ids_only(
    tmp_config_dir: Path,
) -> None:
    ctx = SnapshotContext(operator_ids=("OPERATOR",))
    assert _fetch_plan(_plan_snapshot(), ctx) == _PLAN_BODY
    assert _fetch_plan(_plan_snapshot(author_id="other"), ctx) is None
    assert _fetch_plan(_plan_snapshot(author_conflict=True), ctx) is None
    assert _fetch_plan(_plan_snapshot(), SnapshotContext()) is None


def test_snapshot_plan_fetcher_is_bound_to_its_own_ticket(
    tmp_config_dir: Path,
) -> None:
    ctx = SnapshotContext(github=True)
    assert _fetch_plan(_plan_snapshot(), ctx, ticket_id="897") is None


def test_snapshot_plan_fetcher_without_a_usable_snapshot_is_none(
    tmp_config_dir: Path,
) -> None:
    ctx = SnapshotContext(github=True)
    no_digest = TicketTask(ticket_id="896", client="test", stage=Stage.IMPL)
    untrusted = no_digest.model_copy(update={"snapshot_digest": "0" * 64})
    assert SnapshotPlanFetcher(no_digest, ctx, gh_login=_OPERATOR).fetch("896") is None
    assert SnapshotPlanFetcher(untrusted, ctx, gh_login=_OPERATOR).fetch("896") is None


def test_local_preflight_success_returns_preflight_ok(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """_local_preflight returns _PreflightOK(argv, env) on all-checks-pass.

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
        result = _local_preflight(config, task, worktree, client, Stage.IMPL)

    assert isinstance(result, _PreflightOK)
    assert result.env["OPENAI_API_BASE"] == "http://localhost:1234/v1"
    # #2470: preflight threads the worktree into build_env's TMPDIR helper.
    assert result.env["TMPDIR"] == str(resolve_worker_tmpdir(worktree))
    assert _flag_value(result.argv, "--model") == "openai/qwen"
    # Post-#1905: the plan body reaches the model through the read-only
    # task-context file, never through the mention-scanned --message string.
    message = _flag_value(result.argv, "--message")
    assert message == _PATH_FREE_TASK_INSTRUCTION
    assert "do the thing" not in message
    assert "do the thing" in (worktree / TASK_CONTEXT_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )


def _flag_value(argv: list[str], flag: str) -> str:
    """Return the value following the first *flag* in *argv*."""
    return argv[argv.index(flag) + 1]


def _flag_values(argv: list[str], flag: str) -> list[str]:
    """Return every value following an occurrence of *flag* in *argv*."""
    return [argv[i + 1] for i, token in enumerate(argv) if token == flag]


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
    """Run a full LocalExecutor.spawn through FakeFireAndForgetRunner; return argv."""
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text(plan_text, encoding="utf-8")

    fake_runner = FakeFireAndForgetRunner()
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
    """The plan's file manifest is parsed and threaded into argv --file (#1905)."""
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
        result = _local_preflight(config, task, worktree, client, Stage.IMPL)

    assert isinstance(result, _PreflightOK)
    assert _flag_values(result.argv, "--file") == ["src/cw/a.py", "tests/test_a.py"]


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
        result = _local_preflight(config, task, worktree, client, Stage.IMPL)

    assert isinstance(result, _PreflightOK)
    assert "--file" not in result.argv


def test_local_preflight_threads_aiderignore_path_into_preflight_ok(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A non-empty manifest threads a materialised aiderignore path into
    argv --aiderignore (#1915)."""
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
        result = _local_preflight(config, task, worktree, client, Stage.IMPL)

    assert isinstance(result, _PreflightOK)
    aiderignore_path = Path(_flag_value(result.argv, "--aiderignore"))
    assert "/core/database.py" in aiderignore_path.read_text(encoding="utf-8")


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
        result = _local_preflight(config, task, worktree, client, Stage.IMPL)

    assert isinstance(result, _PreflightOK)
    assert "--aiderignore" not in result.argv


def test_local_preflight_threads_read_only_path_into_preflight_ok(
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """_PreflightOK.argv carries the materialised read-only task-context path."""
    worktree = make_git_repo("wt-preflight-readonly")
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("plan body here", encoding="utf-8")
    (cw_dir / "context.json").write_text(
        '{"ticket_title": "T", "ticket_body": "B"}', encoding="utf-8"
    )

    config = StageExecutorConfig(
        backend=LOCAL_BACKEND, model="qwen", endpoint="http://localhost:1234/v1"
    )
    task = TicketTask(ticket_id="T-ro", client="test", stage=Stage.IMPL)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch("cw.executor.local.aider_available", return_value=True):
        result = _local_preflight(config, task, worktree, client, Stage.IMPL)

    assert isinstance(result, _PreflightOK)
    read_only_path = Path(_flag_value(result.argv, "--read"))
    assert read_only_path == worktree / TASK_CONTEXT_RELATIVE_PATH
    context = read_only_path.read_text(encoding="utf-8")
    assert "plan body here" in context
    assert "## Ticket: T" in context


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
    """config.model None → argv --model is the bare 'openai/' prefix."""
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
        result = _local_preflight(config, task, worktree, client, Stage.IMPL)

    assert isinstance(result, _PreflightOK)
    assert _flag_value(result.argv, "--model") == "openai/"


# ---------------------------------------------------------------------------
# Stage pre-flight (RFC 0019 H2): local declares IMPL only (#3219)
# ---------------------------------------------------------------------------

_ENDPOINT = "http://localhost:1234/v1"
_UNDECLARED = (Stage.HARDEN, Stage.PLAN, Stage.REVIEW, Stage.FINALIZE)


def _local_config() -> StageExecutorConfig:
    return StageExecutorConfig(backend=LOCAL_BACKEND, model="m", endpoint=_ENDPOINT)


def _preflight_with_plan(
    worktree: Path, client: ClientConfig, task: TicketTask
) -> AutoDevResult | _PreflightOK:
    """Run ``_local_preflight`` with aider available and a plan on disk."""
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "plan.md").write_text("do the thing", encoding="utf-8")
    with patch("cw.executor.local.aider_available", return_value=True):
        return _local_preflight(_local_config(), task, worktree, client, task.stage)


@pytest.mark.parametrize("stage", _UNDECLARED, ids=lambda s: s.value)
@pytest.mark.parametrize("convergence", ["blocking", "clean"])
def test_local_undeclared_stage_parks_stage_unsupported(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    stage: Stage,
    convergence: str,
) -> None:
    """Any non-IMPL local spawn fails closed before any agent runs (#3219).

    Neither ``aider_available`` nor a plan file is arranged: the pre-flight
    runs first, so the park is deterministic on a host without aider. A clean
    REVIEW spawn parks on the stage, not on clean review (stage wins).
    """
    worktree = make_git_repo(f"wt-local-{stage.value}-{convergence}")
    fake_runner = FakeFireAndForgetRunner()
    executor = LocalExecutor(config=_local_config(), runner=fake_runner)
    task = TicketTask(ticket_id="T-1", client="test", stage=stage)

    executor.spawn(
        stage=stage,
        task=task,
        worktree=worktree,
        client=review_client(worktree, convergence),
    )

    assert len(fake_runner.calls) == 0
    session = find_completed_session(load_state())
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    marker = stage_entry_marker(stage.value)
    assert result.status == "blocked"
    assert result.stage_reached == marker
    assert result.blocker is not None
    assert result.blocker.reason == PreflightReason.STAGE_UNSUPPORTED
    assert result.blocker.retry_eligible is (None if stage is Stage.HARDEN else False)
    assert result.blocker.stage == marker
    assert "harness=local" in result.blocker.details
    assert f"executors.{stage.value}" in result.blocker.details


def test_local_engine_review_parks_on_the_stage_first(
    make_git_repo: Callable[[str], Path],
) -> None:
    """No endpoint, no aider, no plan, an engine client: the stage decides."""
    worktree = make_git_repo("wt-local-engine-first")
    config = StageExecutorConfig(backend=LOCAL_BACKEND, model="m")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.local.aider_available", return_value=False):
        result = _local_preflight(
            config, task, worktree, engine_client(worktree), Stage.REVIEW
        )

    assert isinstance(result, AutoDevResult)
    assert result.blocker is not None
    assert result.blocker.reason == PreflightReason.STAGE_UNSUPPORTED


@pytest.mark.parametrize("convergence", ["blocking", "clean"])
def test_local_impl_stage_unchanged(
    make_git_repo: Callable[[str], Path], convergence: str
) -> None:
    """IMPL is declared: a plain IMPL pre-flight passes in either mode."""
    worktree = make_git_repo(f"wt-local-impl-{convergence}")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)
    result = _preflight_with_plan(worktree, review_client(worktree, convergence), task)
    assert isinstance(result, _PreflightOK)


def test_local_impl_on_an_engine_client_passes(
    make_git_repo: Callable[[str], Path],
) -> None:
    """The engine need is REVIEW-only: an engine client's IMPL proceeds."""
    worktree = make_git_repo("wt-local-engine-impl")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)
    result = _preflight_with_plan(worktree, engine_client(worktree), task)
    assert isinstance(result, _PreflightOK)


def test_local_adopted_repair_row_keeps_its_own_reason(
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2944: an adopted IMPL repair row parks, engine client or not."""
    worktree = make_git_repo("wt-local-repair-guard")

    result = _local_preflight(
        _local_config(),
        repair_row(Stage.IMPL),
        worktree,
        engine_client(worktree),
        Stage.IMPL,
    )

    assert isinstance(result, AutoDevResult)
    assert result.blocker is not None
    assert result.blocker.reason == "repair_entry_unsupported_backend"
    assert result.blocker.retry_eligible is False
    assert result.stage_reached == "stage2_impl"
    assert "backend=local" in result.blocker.details


def test_local_feedback_row_without_a_branch_runs(
    make_git_repo: Callable[[str], Path],
) -> None:
    """``blocks_adopted``: a feedback row with no adoption branch is not parked."""
    worktree = make_git_repo("wt-local-feedback-no-branch")
    result = _preflight_with_plan(
        worktree,
        review_client(worktree, "blocking"),
        repair_row(Stage.IMPL, branch=None),
    )
    assert isinstance(result, _PreflightOK)
