"""Tests for cw.executor.CodexExecutor — detached ``cw codex run`` launcher.

RFC 0005 F1 / RFC 0014 A2 (#2388). ``spawn()`` runs its synchronous pre-flight
(``_codex_preflight``: cw-context.json, stage check, binary check, session-id
stamp), then launches ``cw codex run`` fire-and-forget through the shared
``_spawn_fire_and_forget`` skeleton and records a ``codex``-tagged
``LocalLivenessHandle``. The review itself runs in that subprocess; its
end-to-end outcomes are covered against ``cw.codex_driver`` in
test_codex_driver.py, and the review unit of work in test_codex_background.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.codex_review import (
    _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS,
    CODEX_REVIEW_UNPARSEABLE,
)
from cw.config import load_state
from cw.dev_queue import add_ticket, load_dev_queue
from cw.executor import (
    CODEX_NOT_FOUND,
    CODEX_REVIEW_ONLY,
    CODEX_VERSION_UNKNOWN,
    CodexCapabilityDiagnosis,
    CodexExecutor,
    StageExecutor,
    codex_capability_diagnosis,
    resolve_executor,
)
from cw.executor.core import FakeFireAndForgetRunner
from cw.executor_diagnostics import (
    ExecutorFailure,
    diagnostics_bundle_dir,
    render_bundle_path,
)
from cw.local_runner import LIVENESS_UNAVAILABLE, UNEXPECTED_ERROR, make_blocked
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    CompletionReason,
    LastResultSource,
    LocalLivenessHandle,
    QueueItemStatus,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests._codex_review_helpers import _mk_codex_proc
from tests.conftest import _seed_completed_session, find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from cw.executor.core import FireAndForgetRunner
    from cw.native_daemon import NativeDaemonClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WHICH = "cw.executor.codex.shutil.which"
_CODEX_PATH = "/usr/bin/codex"


def _codex_executor(
    config: StageExecutorConfig | None = None,
    *,
    runner: FireAndForgetRunner | None = None,
    native_daemon: NativeDaemonClient | None = None,
) -> CodexExecutor:
    """A CodexExecutor launching through a fake fire-and-forget runner."""
    return CodexExecutor(
        config=config or StageExecutorConfig(backend=CODEX_BACKEND),
        runner=runner or FakeFireAndForgetRunner(),
        native_daemon=native_daemon,
    )


def _kill_procs(runner: FakeFireAndForgetRunner) -> None:
    for proc in runner.procs:
        proc.kill()
        proc.wait()


@pytest.fixture
def fake_runner() -> Iterator[FakeFireAndForgetRunner]:
    runner = FakeFireAndForgetRunner()
    yield runner
    _kill_procs(runner)


def _persisted_result() -> AutoDevResult:
    """Load the single persisted last_result and validate it."""
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    assert result_raw is not None
    return AutoDevResult.model_validate(result_raw)


def _review_task(ticket_id: str) -> TicketTask:
    return TicketTask(ticket_id=ticket_id, client="test", stage=Stage.REVIEW)


def _client(worktree: Path) -> ClientConfig:
    return ClientConfig(name="test", workspace_path=worktree, default_branch="main")


def _seed_running_row(task: TicketTask) -> None:
    # Same created_at as `task`: the stamp re-finds the spawning task's own
    # row by that identity (#2219), as the claimed row is in real dispatch.
    add_ticket(
        TicketTask(
            ticket_id=task.ticket_id,
            client=task.client,
            stage=task.stage,
            status=QueueItemStatus.RUNNING,
            created_at=task.created_at,
        )
    )


# ---------------------------------------------------------------------------
# Pre-flight (synchronous blocked completions)
# ---------------------------------------------------------------------------


def test_codex_executor_wrong_stage_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """spawn() on a non-REVIEW stage → blocked/codex_review_only, no launch."""
    worktree = make_git_repo("wt-codex-wrong-stage")
    executor = _codex_executor(runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.PLAN)

    executor.spawn(stage=Stage.PLAN, task=task, worktree=worktree, client=client)

    assert fake_runner.calls == []
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_REVIEW_ONLY
    assert result.next_actions == _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS


def test_codex_executor_codex_not_found(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """shutil.which('codex') is None → blocked/codex_not_found, no launch."""
    worktree = make_git_repo("wt-codex-missing")
    executor = _codex_executor(runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)

    with patch(_WHICH, return_value=None):
        executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-1"),
            worktree=worktree,
            client=client,
        )

    assert fake_runner.calls == []
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_NOT_FOUND
    assert result.next_actions == _CODEX_REVIEW_BLOCKED_NEXT_ACTIONS
    session = find_completed_session(load_state())
    assert session.completed_reason == CompletionReason.NORMAL
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    assert session.local_liveness is None


def test_spawn_writes_cw_context_json_on_preflight_failure(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2280: cw-context.json lands even on the CODEX_NOT_FOUND pre-flight path.

    No Stop hook is installed on this path: CodexExecutor never involves a
    Claude session to signal-stop.
    """
    worktree = make_git_repo("wt-codex-context-preflight")
    executor = _codex_executor()

    with patch(_WHICH, return_value=None):
        executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-ctx-pre"),
            worktree=worktree,
            client=ClientConfig(name="test", workspace_path=worktree),
        )

    context_path = worktree / ".claude" / "cw-context.json"
    assert context_path.exists()
    context = json.loads(context_path.read_text())
    assert context["ticket_id"] == "T-ctx-pre"
    assert not (worktree / ".claude" / "settings.local.json").exists()


def test_spawn_writes_cw_context_json_before_launch(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """#2280: cw-context.json is on disk by the time ``cw codex run`` launches."""
    worktree = make_git_repo("wt-codex-context-launch")
    seen: dict[str, object] = {}
    original_launch = fake_runner.launch

    def _launch(
        worktree_arg: Path, argv: list[str], env: dict[str, str]
    ) -> subprocess.Popen[bytes]:
        seen["context_exists"] = (worktree / ".claude" / "cw-context.json").exists()
        return original_launch(worktree_arg, argv, env)

    executor = _codex_executor(runner=fake_runner)
    with (
        patch(_WHICH, return_value=_CODEX_PATH),
        patch.object(fake_runner, "launch", _launch),
    ):
        executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-ctx-run"),
            worktree=worktree,
            client=_client(worktree),
        )

    assert seen["context_exists"] is True
    context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
    assert context["ticket_id"] == "T-ctx-run"
    assert not (worktree / ".claude" / "settings.local.json").exists()


def test_spawn_second_attempt_prior_attempts_summary_reflects_first_codex_park(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2280: prior_attempts_summary picks up a codex-origin park on retry."""
    worktree = make_git_repo("wt-codex-retry")
    _seed_completed_session(
        tmp_path,
        tmp_config_dir,
        ticket_id="T-retry",
        client="test",
        status=SessionStatus.COMPLETED,
        last_result={
            "status": "blocked",
            "stage_reached": "stage3_review",
            "blocker": {
                "stage": "stage3_review",
                "reason": CODEX_REVIEW_UNPARSEABLE,
                "details": "reviewer (codex_timeout)",
            },
        },
    )
    executor = _codex_executor()
    task = TicketTask(
        ticket_id="T-retry", client="test", stage=Stage.REVIEW, attempts=1
    )

    with patch(_WHICH, return_value=None):
        executor.spawn(
            stage=Stage.REVIEW, task=task, worktree=worktree, client=_client(worktree)
        )

    context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
    summary = context["world_state_snapshot"]["prior_attempts_summary"]
    assert len(summary) == 1
    assert summary[0]["blocker_reason"] == CODEX_REVIEW_UNPARSEABLE


# ---------------------------------------------------------------------------
# Fire-and-forget launch of `cw codex run`
# ---------------------------------------------------------------------------


def test_codex_executor_launch_records_liveness_and_returns_active(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """Pre-flight passes → session ACTIVE with a codex LocalLivenessHandle."""
    worktree = make_git_repo("wt-codex-launch-active")
    executor = _codex_executor(runner=fake_runner)

    with patch(_WHICH, return_value=_CODEX_PATH):
        sid = executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-100"),
            worktree=worktree,
            client=_client(worktree),
        )

    session = next(s for s in load_state().sessions if s.id == sid)
    assert session.status == SessionStatus.ACTIVE
    assert isinstance(session.local_liveness, LocalLivenessHandle)
    assert session.local_liveness.pid == fake_runner.procs[0].pid
    assert session.local_liveness.start_time_ns > 0
    assert session.local_liveness.backend == "codex"
    assert session.last_result is None
    assert len(fake_runner.calls) == 1
    assert fake_runner.calls[0]["cwd"] == worktree


def test_spawn_returns_before_launched_job_completes(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """spawn() does not wait on the review (#1727, #2388).

    FakeFireAndForgetRunner launches a real ``sleep 60``; spawn() returning
    while it is still alive proves the launch is fire-and-forget. No thread
    and no in-process registry is involved any more.
    """
    worktree = make_git_repo("wt-codex-async")
    executor = _codex_executor(runner=fake_runner)

    with patch(_WHICH, return_value=_CODEX_PATH):
        sid = executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-async"),
            worktree=worktree,
            client=_client(worktree),
        )

    assert fake_runner.procs[0].poll() is None
    session = next(s for s in load_state().sessions if s.id == sid)
    assert session.status is SessionStatus.ACTIVE
    assert session.last_result is None


def test_codex_executor_spawn_argv_invokes_cw_codex_run(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """The launched argv is ``python -m cw codex run`` for this ticket/session."""
    # Two worktrees: a second spawn into the first's worktree would (rightly)
    # trip _write_hook_context's live-session conflict guard.
    worktree = make_git_repo("wt-codex-argv")
    worktree_budget = make_git_repo("wt-codex-argv-budget")
    executor = _codex_executor(runner=fake_runner)
    task = _review_task("T-argv")

    with patch(_WHICH, return_value=_CODEX_PATH):
        sid = executor.spawn(
            stage=Stage.REVIEW, task=task, worktree=worktree, client=_client(worktree)
        )
        sid_budget = executor.spawn(
            stage=Stage.REVIEW,
            task=task,
            worktree=worktree_budget,
            client=_client(worktree_budget),
            wall_clock_budget_seconds=120,
        )

    expected = [
        sys.executable,
        "-m",
        "cw",
        "codex",
        "run",
        "T-argv",
        "--stage",
        "review",
        "--session-id",
    ]
    assert fake_runner.calls[0]["argv"] == [*expected, sid]
    assert fake_runner.calls[1]["argv"] == [
        *expected,
        sid_budget,
        "--wall-clock-budget-seconds",
        "120",
    ]


def test_codex_executor_env_is_full_os_environ(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job inherits the full environment — unlike aider/opencode's allowlist.

    The child is another ``cw`` process: it must resolve the same
    XDG-derived config/state dirs as the parent.
    """
    monkeypatch.setenv("CW_TEST_CODEX_ENV_SENTINEL", "inherited")
    worktree = make_git_repo("wt-codex-env")
    executor = _codex_executor(runner=fake_runner)

    with patch(_WHICH, return_value=_CODEX_PATH):
        executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-env"),
            worktree=worktree,
            client=_client(worktree),
        )

    env = fake_runner.calls[0]["env"]
    assert isinstance(env, dict)
    assert env["CW_TEST_CODEX_ENV_SENTINEL"] == "inherited"


def test_spawn_stamps_session_id_before_launch(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """R1: the RUNNING row carries session_id by the time the job launches.

    ``cw codex run`` re-finds its task by (ticket_id, session_id), and a crash
    between spawn() returning and dispatch's own post-spawn stamp would
    otherwise leave a live codex job with no queue row pointing at it.
    """
    worktree = make_git_repo("wt-codex-stamp")
    task = _review_task("T-stamp")
    _seed_running_row(task)
    seen: dict[str, object] = {}
    original_launch = fake_runner.launch

    def _launch(
        worktree_arg: Path, argv: list[str], env: dict[str, str]
    ) -> subprocess.Popen[bytes]:
        seen["stamped_at_launch"] = load_dev_queue().tasks[0].session_id
        return original_launch(worktree_arg, argv, env)

    executor = _codex_executor(runner=fake_runner)
    with (
        patch(_WHICH, return_value=_CODEX_PATH),
        patch.object(fake_runner, "launch", _launch),
    ):
        sid = executor.spawn(
            stage=Stage.REVIEW, task=task, worktree=worktree, client=_client(worktree)
        )

    assert seen["stamped_at_launch"] == sid
    assert len(fake_runner.calls) == 1


def test_codex_executor_liveness_unavailable_marks_session_completed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """Start-time unreadable → orphan killed, COMPLETED, codex runtime_error bundle."""
    worktree = make_git_repo("wt-codex-liveness")
    executor = _codex_executor(runner=fake_runner)

    with (
        patch(_WHICH, return_value=_CODEX_PATH),
        patch("cw.executor.core.read_process_start_time_ns", return_value=None),
    ):
        sid = executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-liveness"),
            worktree=worktree,
            client=_client(worktree),
        )

    assert fake_runner.procs[0].poll() is not None
    session = find_completed_session(load_state())
    assert session.id == sid
    assert session.local_liveness is None
    result = AutoDevResult.model_validate(session.last_result)
    assert result.blocker is not None
    assert result.blocker.reason == LIVENESS_UNAVAILABLE
    assert result.stage_reached == "stage3_review"
    [path] = list(diagnostics_bundle_dir(sid).glob("codex-runtime_error-*.json"))
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.executor_name == "codex"
    # No prompt text in the argv, so nothing is redacted.
    assert "--session-id" in failure.argv_sanitized


def test_codex_executor_launch_exception_marks_session_completed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """launch() raises OSError → COMPLETED/CRASHED + UNEXPECTED_ERROR, re-raised."""
    worktree = make_git_repo("wt-codex-launch-exc")
    executor = _codex_executor(runner=fake_runner)

    with (
        patch(_WHICH, return_value=_CODEX_PATH),
        patch.object(fake_runner, "launch", side_effect=OSError("exec boom")),
        pytest.raises(OSError, match="exec boom"),
    ):
        executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-exc"),
            worktree=worktree,
            client=_client(worktree),
        )

    session = find_completed_session(load_state())
    assert session.status == SessionStatus.COMPLETED
    assert session.completed_reason == CompletionReason.CRASHED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.stage_reached == "stage3_review"
    assert result.blocker.details == (
        "unexpected error during codex launch "
        f"[diagnostics: {render_bundle_path(session.id)}]"
    )


# ---------------------------------------------------------------------------
# Exception seams on the synchronous pre-flight path
# ---------------------------------------------------------------------------


def test_preflight_failure_persist_error_completes_session_and_reraises(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A failing blocked-result write is guarded, then re-raised.

    Dispatch is still on this call stack, so re-raising is correct: its own
    handler reverts the claimed task. The session is still driven out of
    ACTIVE first by the shared skeleton's crash completion.
    """
    worktree = make_git_repo("wt-codex-preflight-boom")
    executor = _codex_executor()
    task = TicketTask(ticket_id="T-pf", client="test", stage=Stage.PLAN)

    calls: list[dict[str, object]] = []

    def _door(sid: str, payload: dict[str, object], **kwargs: object) -> None:
        calls.append({"sid": sid, "payload": payload, **kwargs})
        if len(calls) == 1:
            msg = "door boom"
            raise OSError(msg)

    with (
        patch("cw.executor.core._complete_session_via_door", _door),
        pytest.raises(OSError, match="door boom"),
    ):
        # Stage.PLAN trips the CODEX_REVIEW_ONLY pre-flight guard.
        executor.spawn(
            stage=Stage.PLAN, task=task, worktree=worktree, client=_client(worktree)
        )

    # Second call is the guarded blocked-result write from the except branch.
    assert len(calls) == 2
    assert calls[1]["guard_already_completed"] is True
    recovery = AutoDevResult.model_validate(calls[1]["payload"])
    assert recovery.status == "blocked"
    assert recovery.blocker is not None
    assert recovery.blocker.reason == UNEXPECTED_ERROR


def test_spawn_write_hook_context_failure_completes_session_and_reraises(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    fake_runner: FakeFireAndForgetRunner,
) -> None:
    """#2280: a `_write_hook_context` raise must not leak the session ACTIVE.

    The session is persisted ACTIVE before the pre-flight runs; the shared
    skeleton's ``try:`` now covers the pre-flight (#2388), so the raise lands
    in its crash completion rather than propagating with the session ACTIVE.
    """
    worktree = make_git_repo("wt-codex-hook-context-boom")
    executor = _codex_executor(runner=fake_runner)

    with (
        patch(
            "cw.executor.codex._write_hook_context", side_effect=OSError("hook boom")
        ),
        pytest.raises(OSError, match="hook boom"),
    ):
        executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-hc"),
            worktree=worktree,
            client=_client(worktree),
        )

    assert fake_runner.calls == []
    state = load_state()
    assert not any(s.status == SessionStatus.ACTIVE for s in state.sessions)
    session = find_completed_session(state)
    assert session.status == SessionStatus.COMPLETED
    # #2280 round 2: the full terminal record, not just status.
    assert session.completed_at is not None
    assert session.completed_reason == CompletionReason.CRASHED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.blocker.details == (
        "unexpected error during codex launch "
        f"[diagnostics: {render_bundle_path(session.id)}]"
    )


# ---------------------------------------------------------------------------
# Wiring / resolution
# ---------------------------------------------------------------------------


def test_codex_executor_stage_sentinel_schema() -> None:
    """stage_sentinel_schema returns the AutoDevResult JSON schema."""
    schema = _codex_executor().stage_sentinel_schema(Stage.REVIEW)

    assert schema == AutoDevResult.model_json_schema()


def test_resolve_executor_returns_codex_executor(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """resolve_executor returns CodexExecutor when backend=codex."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.REVIEW: StageExecutorConfig(backend=CODEX_BACKEND)}
        ),
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    executor = resolve_executor(task, client)

    assert isinstance(executor, CodexExecutor)
    assert isinstance(executor, StageExecutor)


def test_codex_executor_threads_native_daemon_into_write_hook_context(
    tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
) -> None:
    """CodexExecutor.native_daemon (#2077) reaches _write_hook_context's
    `daemon` kwarg exactly like ClaudeNativeExecutor's."""
    worktree = make_git_repo("wt-codex-native-daemon")
    daemon = FakeNativeDaemonClient()
    executor = _codex_executor(native_daemon=daemon)

    calls: list[dict[str, object]] = []

    def _capture(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    with patch("cw.executor.codex._write_hook_context", _capture):
        executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-nd"),
            worktree=worktree,
            client=_client(worktree),
        )

    assert calls[0]["daemon"] is daemon


def test_codex_executor_threads_merge_gate_ignore_paths_into_write_hook_context(
    tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
) -> None:
    """#2431: the codex spawn path stamps the client's ignore list too."""
    worktree = make_git_repo("wt-codex-mg-ignore")
    executor = _codex_executor()
    client = ClientConfig(
        name="test",
        workspace_path=worktree,
        default_branch="main",
        merge_gate_ignore_paths=["mypy-baseline.txt"],
    )

    calls: list[dict[str, object]] = []

    def _capture(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    with patch("cw.executor.codex._write_hook_context", _capture):
        executor.spawn(
            stage=Stage.REVIEW,
            task=_review_task("T-mg"),
            worktree=worktree,
            client=client,
        )

    assert calls[0]["merge_gate_ignore_paths"] == ["mypy-baseline.txt"]


def test_resolve_executor_threads_native_daemon_into_codex_executor(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """#2077: resolve_executor no longer drops *native_daemon* for the codex
    backend (it already threaded it to ClaudeNativeExecutor)."""
    client = ClientConfig(
        name="test",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.REVIEW: StageExecutorConfig(backend=CODEX_BACKEND)}
        ),
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)
    daemon = FakeNativeDaemonClient()

    executor = resolve_executor(task, client, native_daemon=daemon)

    assert isinstance(executor, CodexExecutor)
    assert executor._native_daemon is daemon


def test_stage_executor_protocol_comment_does_not_call_codex_an_exception() -> None:
    """RFC 0014 A2: CodexExecutor now satisfies the liveness invariant.

    The StageExecutor Protocol comment must no longer carve codex out as an
    accepted exception to it.
    """
    core_text = (_REPO_ROOT / "src" / "cw" / "executor" / "core.py").read_text(
        encoding="utf-8"
    )
    assert "accepted, documented exception" not in core_text


# ---------------------------------------------------------------------------
# make_blocked (shared with LocalExecutor)
# ---------------------------------------------------------------------------


def test_make_blocked_backward_compat(tmp_path: Path) -> None:
    """make_blocked without stage_reached defaults to stage2_impl."""
    result = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="some_reason")
    assert result.stage_reached == "stage2_impl"
    assert result.blocker is not None
    assert result.blocker.stage == "stage2_impl"


def test_make_blocked_stage_reached_override(tmp_path: Path) -> None:
    """make_blocked propagates an explicit stage_reached to both fields."""
    result = make_blocked(
        ticket_id="T-1",
        worktree=tmp_path,
        reason="some_reason",
        stage_reached="stage3_review",
    )
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.stage == "stage3_review"


def test_make_blocked_next_actions_backward_compat(tmp_path: Path) -> None:
    """make_blocked without next_actions defaults to the LocalExecutor label —
    the 13 existing callers that don't pass it must see no behavior change
    (#1835)."""
    result = make_blocked(ticket_id="T-1", worktree=tmp_path, reason="some_reason")
    assert result.next_actions == ["user_resolve_local_executor_failure"]


def test_make_blocked_next_actions_override(tmp_path: Path) -> None:
    """make_blocked propagates an explicit next_actions override, proving the
    new plumbing round-trips through Pydantic validation (#1835)."""
    result = make_blocked(
        ticket_id="T-1",
        worktree=tmp_path,
        reason="some_reason",
        next_actions=["user_resolve_something_else"],
    )
    assert result.next_actions == ["user_resolve_something_else"]


class TestCodexCapabilityDiagnosis:
    """Direct tests for the shared codex capability probe (#1238).

    ``shutil.which`` is patched via this file's established
    ``patch("cw.executor.core.shutil.which", ...)`` idiom; the ``codex --version``
    subprocess is patched at ``cw.executor.subprocess.run``.
    """

    def test_binary_absent_returns_not_found(self) -> None:
        with patch("cw.executor.core.shutil.which", return_value=None):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_NOT_FOUND
        assert "not found" in probe.detail

    def test_version_filenotfound_returns_version_unknown(self) -> None:
        with (
            patch("cw.executor.core.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.core.subprocess.run", side_effect=FileNotFoundError("gone")
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_VERSION_UNKNOWN

    def test_version_timeout_returns_version_unknown(self) -> None:
        with (
            patch("cw.executor.core.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.core.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=10),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_VERSION_UNKNOWN
        assert "timed out" in probe.detail

    def test_nonzero_returncode_returns_version_unknown(self) -> None:
        with (
            patch("cw.executor.core.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.core.subprocess.run",
                return_value=_mk_codex_proc("boom\n", returncode=3),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_VERSION_UNKNOWN
        assert "3" in probe.detail

    def test_unparseable_version_returns_version_unknown(self) -> None:
        with (
            patch("cw.executor.core.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.core.subprocess.run",
                return_value=_mk_codex_proc("not-a-version\n"),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_VERSION_UNKNOWN
        assert "could not parse" in probe.detail

    def test_parseable_version_returns_capable(self) -> None:
        with (
            patch("cw.executor.core.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.core.subprocess.run",
                return_value=_mk_codex_proc("0.144.5\n"),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis is None
        assert probe.detail == "0.144.5"
        assert isinstance(probe, CodexCapabilityDiagnosis)

    def test_name_prefixed_version_returns_capable(self) -> None:
        """Real `codex --version` output is name-prefixed, not a bare version.

        Captured live on a snap-installed `codex-cli 0.136.0` — the real CLI
        prints ``codex-cli 0.136.0``, not ``0.136.0`` alone (#1238 review
        finding: a first-whitespace-token parse misdiagnosed this shape as
        CODEX_VERSION_UNKNOWN on every real install).
        """
        with (
            patch("cw.executor.core.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.core.subprocess.run",
                return_value=_mk_codex_proc("codex-cli 0.136.0\n"),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis is None
        assert probe.detail == "codex-cli 0.136.0"

    def test_timeout_seconds_passed_to_subprocess_run(self) -> None:
        """The hot-path caller (dispatch's gate) needs to override the timeout."""
        with (
            patch("cw.executor.core.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.core.subprocess.run",
                return_value=_mk_codex_proc("codex-cli 0.136.0\n"),
            ) as mock_run,
        ):
            codex_capability_diagnosis(timeout_seconds=3)
        assert mock_run.call_args.kwargs["timeout"] == 3

    def test_default_timeout_passed_to_subprocess_run(self) -> None:
        """`cw doctor`'s one-shot call site relies on the 10s default."""
        with (
            patch("cw.executor.core.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.core.subprocess.run",
                return_value=_mk_codex_proc("codex-cli 0.136.0\n"),
            ) as mock_run,
        ):
            codex_capability_diagnosis()
        assert mock_run.call_args.kwargs["timeout"] == 10
