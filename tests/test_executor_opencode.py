"""Tests for cw.executor.opencode — OpencodeExecutor (#1669)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state
from cw.executor import OpencodeExecutor, resolve_executor
from cw.executor.core import FakeFireAndForgetRunner, _PreflightOK
from cw.executor.opencode import _opencode_preflight
from cw.local_runner import LIVENESS_UNAVAILABLE, UNEXPECTED_ERROR
from cw.models import (
    OPENCODE_BACKEND,
    ClientConfig,
    LastResultSource,
    MustFixOverride,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.models.client import ReviewConfig, TrackerMcpGateConfig
from cw.opencode_runner import (
    OPENCODE_NOT_FOUND,
    STAGE4A_MERGE_GATE,
    OpencodeRunner,
    build_stage_prompt,
)
from cw.tracker import TrackerClass, TrackerKind
from cw.worktree import resolve_worker_tmpdir
from tests.conftest import _write_tracker_config, find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# OpencodeExecutor (#1669)
# ---------------------------------------------------------------------------


def test_resolve_executor_opencode(tmp_config_dir: Path) -> None:
    """resolve_executor returns OpencodeExecutor for OPENCODE_BACKEND."""
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp"),
        pipeline=StagePipelineConfig(
            executors={Stage.IMPL: StageExecutorConfig(backend=OPENCODE_BACKEND)}
        ),
    )
    executor = resolve_executor(task, client)
    assert isinstance(executor, OpencodeExecutor)


def test_opencode_executor_unsupported_stage_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """HARDEN stage → blocked/opencode_harden_not_implemented."""
    worktree = make_git_repo("wt-opencode-wrong-stage")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.HARDEN)

    executor.spawn(stage=Stage.HARDEN, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.stage_reached == STAGE4A_MERGE_GATE
    assert result.blocker is not None
    assert result.blocker.reason == "opencode_harden_not_implemented"


def test_opencode_executor_blocked_binary_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """opencode_available() is False → blocked/opencode_not_found."""
    worktree = make_git_repo("wt-opencode-binary-missing")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.opencode.opencode_available", return_value=False):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND
    assert result.blocker.retry_eligible is True
    assert result.stage_reached == "stage2_impl"


def test_opencode_executor_blocked_binary_missing_plan_stage_marker(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """PLAN-stage binary-missing failure carries stage1_plan, never stage2_impl.

    A stage2_impl marker on a PLAN-stage blocked sentinel classifies as a
    later-stage self-escalation and dispatch walks task.stage forward
    PLAN→IMPL (_resolve_stage_walk), silently skipping planning.
    """
    worktree = make_git_repo("wt-opencode-binary-missing-plan")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.PLAN)

    with patch("cw.executor.opencode.opencode_available", return_value=False):
        executor.spawn(stage=Stage.PLAN, task=task, worktree=worktree, client=client)

    assert len(fake_runner.calls) == 0
    state = load_state()
    session = find_completed_session(state)
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND
    assert result.stage_reached == "stage1_plan"
    assert result.blocker.stage == "stage1_plan"


def test_opencode_executor_threads_review_config_into_write_hook_context(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2787: the opencode spawn path stamps the client's review block too."""
    worktree = make_git_repo("wt-opencode-review-config")
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=FakeFireAndForgetRunner())
    review = ReviewConfig(convergence="clean", confirm_backend="none")
    client = ClientConfig(name="test", workspace_path=worktree, review=review)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.FINALIZE)

    calls: list[dict[str, object]] = []

    def _capture(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    with (
        patch("cw.executor.opencode._write_hook_context", _capture),
        patch("cw.executor.opencode.opencode_available", return_value=False),
    ):
        executor.spawn(
            stage=Stage.FINALIZE, task=task, worktree=worktree, client=client
        )

    assert calls[0]["review_config"] == review


def test_opencode_executor_spawn_impl_stage(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """IMPL stage spawn → launch() called with impl prompt, session left ACTIVE.

    Verifies that opencode is no longer FINALIZE-only: the IMPL stage
    pre-flight builds a self-contained impl prompt (the auto-dev-impl.md
    command file requires Claude Code-only machinery, so the prompt must
    not point at it).
    """
    worktree = make_git_repo("wt-opencode-impl-stage")

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="genhealth/glm-5.2")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.opencode.opencode_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        call = fake_runner.calls[0]
        assert call["argv"][0] == "opencode"
        assert "--auto" in call["argv"]
        prompt = call["argv"][-1]
        assert "auto-dev-impl.md" not in prompt
        assert "T-1" in prompt
        assert "stage2_impl" in prompt
        assert "Auto-Dev-Stage: impl-complete" in prompt
        assert "--format" in call["argv"]
        assert "--pure" not in call["argv"]
        assert "--dir" not in call["argv"]
        assert "--model" in call["argv"]

        state = load_state()
        session = next(s for s in state.sessions if s.id == sid)
        assert session.status == SessionStatus.ACTIVE
        assert session.local_liveness is not None
        assert session.local_liveness.pid > 0
        assert session.local_liveness.backend == "opencode"
        assert session.last_result is None
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_opencode_executor_spawn_liveness_unavailable(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """start_time None → blocked/liveness_unavailable, session COMPLETED."""
    worktree = make_git_repo("wt-opencode-liveness-unavailable")

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    try:
        with (
            patch("cw.executor.opencode.opencode_available", return_value=True),
            patch("cw.executor.core.read_process_start_time_ns", return_value=None),
        ):
            executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        state = load_state()
        session = find_completed_session(state)
        result = AutoDevResult.model_validate(session.last_result)
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == LIVENESS_UNAVAILABLE
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_opencode_executor_exception_handler(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Unexpected error during launch → blocked/unexpected_error, session COMPLETED."""

    _boom_msg = "boom"

    class _ExplodingRunner:
        def launch(self, *args: object) -> object:
            raise RuntimeError(_boom_msg)

    worktree = make_git_repo("wt-opencode-explode")

    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(
        config=config, runner=cast("OpencodeRunner", _ExplodingRunner())
    )
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with (
        patch("cw.executor.opencode.opencode_available", return_value=True),
        pytest.raises(RuntimeError, match=_boom_msg),
    ):
        executor.spawn(stage=Stage.IMPL, task=task, worktree=worktree, client=client)

    state = load_state()
    session = find_completed_session(state)
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR


def test_opencode_executor_spawn_threads_session_id_into_prompt(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The sid the spawn skeleton creates threads into the pre-flight prompt.

    _spawn_fire_and_forget creates the session and obtains sid BEFORE calling
    preflight_fn() -- this proves the sid embedded in the prompt's
    --session-id is the SAME one the session was created under, not a
    placeholder or a mismatched value.
    """
    worktree = make_git_repo("wt-opencode-session-id")

    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    try:
        with patch("cw.executor.opencode.opencode_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.IMPL, task=task, worktree=worktree, client=client
            )

        assert len(fake_runner.calls) == 1
        prompt = fake_runner.calls[0]["argv"][-1]
        assert f"--session-id {sid}" in prompt
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_opencode_preflight_env_tmpdir_resolves_against_worktree(
    tmp_path: Path,
) -> None:
    """Preflight threads the worktree into build_env's TMPDIR helper (#2470)."""
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    client = ClientConfig(name="test", workspace_path=tmp_path)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)

    with patch("cw.executor.opencode.opencode_available", return_value=True):
        result = _opencode_preflight(
            config, task, tmp_path, client, Stage.IMPL, _sess()
        )

    assert isinstance(result, _PreflightOK)
    assert result.env["TMPDIR"] == str(resolve_worker_tmpdir(tmp_path))


@pytest.mark.parametrize(
    ("tracker", "gate", "expected_tracker", "expected_server"),
    [
        (
            "linear",
            TrackerMcpGateConfig(enabled=True, servers={"opencode": "lin"}),
            TrackerKind(TrackerClass.OTHER, "linear"),
            "lin",
        ),
        (
            "linear",
            TrackerMcpGateConfig(enabled=True, plugin_id="p@m"),
            TrackerKind(TrackerClass.OTHER, "linear"),
            None,
        ),
        (
            "github-issues",
            None,
            TrackerKind(TrackerClass.GITHUB, "github-issues"),
            None,
        ),
        (None, None, TrackerKind(TrackerClass.UNSET, None), None),
    ],
)
def test_opencode_preflight_threads_tracker_and_server(
    tmp_path: Path,
    tracker: str | None,
    gate: TrackerMcpGateConfig | None,
    expected_tracker: TrackerKind,
    expected_server: str | None,
) -> None:
    """#3055: the prompt builder gets the workspace tracker and the gate's
    opencode server (None when no gate or no server is named)."""
    if tracker is not None:
        _write_tracker_config(tmp_path, tracker)
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    client = ClientConfig(name="test", workspace_path=tmp_path, tracker_mcp_gate=gate)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.PLAN)

    with (
        patch("cw.executor.opencode.opencode_available", return_value=True),
        patch(
            "cw.executor.opencode.build_stage_prompt", return_value="prompt"
        ) as build_mock,
    ):
        result = _opencode_preflight(
            config, task, tmp_path, client, Stage.PLAN, _sess()
        )

    assert isinstance(result, _PreflightOK)
    kwargs = build_mock.call_args.kwargs
    assert kwargs["tracker"] == expected_tracker
    assert kwargs["tracker_server"] == expected_server


def _sess() -> Session:
    return Session(
        name="test/finalize",
        client="test",
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        workspace_path=Path("/tmp"),
    )


def test_opencode_preflight_refreshes_stale_cw_context(tmp_path: Path) -> None:
    """#2653: a reused worktree's stale cw-context.json is rewritten per spawn.

    The new session id lands (so a bare ``cw result emit`` targets it) and the
    operator's must_fix_override is delivered to check_must_fix_override.py.
    """
    context_path = tmp_path / ".claude" / "cw-context.json"
    context_path.parent.mkdir(parents=True)
    context_path.write_text(
        json.dumps({"session_id": "stale-id", "must_fix_override": None}),
        encoding="utf-8",
    )
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    client = ClientConfig(name="test", workspace_path=tmp_path)
    override = MustFixOverride(
        actor="operator",
        reason="operator says ok",
        reviewed_sha="a" * 40,
        finding_ids=[("a.py", "fp")],
        recorded_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    task = TicketTask(
        ticket_id="T-1", client="test", stage=Stage.FINALIZE, must_fix_override=override
    )
    sess = _sess()

    with patch("cw.executor.opencode.opencode_available", return_value=True):
        _opencode_preflight(config, task, tmp_path, client, Stage.FINALIZE, sess)

    ctx = json.loads(context_path.read_text(encoding="utf-8"))
    assert ctx["session_id"] == sess.id
    assert ctx["queue_metadata"]["must_fix_override"] == override.model_dump(
        mode="json"
    )


def test_opencode_executor_stage_sentinel_schema(tmp_path: Path) -> None:
    """stage_sentinel_schema returns AutoDevResult JSON schema."""
    config = StageExecutorConfig(backend=OPENCODE_BACKEND)
    executor = OpencodeExecutor(config=config)
    schema = executor.stage_sentinel_schema(Stage.IMPL)
    assert "properties" in schema
    assert "status" in schema["properties"]


# ---------------------------------------------------------------------------
# Clean-mode REVIEW (#2797 guard, lifted by #2806)
# ---------------------------------------------------------------------------


def _review_client(worktree: Path, convergence: str) -> ClientConfig:
    review = ReviewConfig.model_validate({"convergence": convergence})
    return ClientConfig(name="test", workspace_path=worktree, review=review)


def _spawn_review_launched(worktree: Path, client: ClientConfig) -> tuple[str, str]:
    """Spawn REVIEW with opencode available; assert it launched, no blocked park.

    Returns the session id and the prompt the runner was launched with.
    """
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)
    try:
        with patch("cw.executor.opencode.opencode_available", return_value=True):
            sid = executor.spawn(
                stage=Stage.REVIEW, task=task, worktree=worktree, client=client
            )
        assert len(fake_runner.calls) == 1
        session = next(s for s in load_state().sessions if s.id == sid)
        assert session.status == SessionStatus.ACTIVE
        assert session.last_result is None
        argv = fake_runner.calls[0]["argv"]
        assert isinstance(argv, list)
        return sid, str(argv[-1])
    finally:
        for proc in fake_runner.procs:
            proc.kill()
            proc.wait()


def test_opencode_review_stage_clean_client_launches_with_clean_prompt(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2806: a clean client's opencode REVIEW spawn runs the converge prompt."""
    worktree = make_git_repo("wt-opencode-clean-review")
    _sid, prompt = _spawn_review_launched(worktree, _review_client(worktree, "clean"))
    assert "cw review converge init" in prompt
    assert "--loop-backend opencode" in prompt
    ctx = json.loads(
        (worktree / ".claude" / "cw-context.json").read_text(encoding="utf-8")
    )
    assert ctx["review"]["convergence"] == "clean"


def test_opencode_review_blocking_client_prompt_unchanged(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Blocking mode reaches the runner with the byte-identical blocking prompt."""
    worktree = make_git_repo("wt-opencode-blocking-review")
    sid, prompt = _spawn_review_launched(
        worktree, ClientConfig(name="test", workspace_path=worktree)
    )
    assert prompt == build_stage_prompt("review", "T-1", worktree, session_id=sid)
    assert "cw review converge" not in prompt


@pytest.mark.parametrize("stage", [Stage.PLAN, Stage.IMPL], ids=lambda s: s.value)
def test_opencode_clean_client_non_review_stages_unchanged(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    stage: Stage,
) -> None:
    """Clean mode changes only REVIEW: other stages reach the binary probe."""
    worktree = make_git_repo(f"wt-opencode-clean-{stage.value}")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    client = _review_client(worktree, "clean")
    task = TicketTask(ticket_id="T-1", client="test", stage=stage)

    with patch("cw.executor.opencode.opencode_available", return_value=False):
        executor.spawn(stage=stage, task=task, worktree=worktree, client=client)

    result = AutoDevResult.model_validate(
        find_completed_session(load_state()).last_result
    )
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND


def test_opencode_clean_review_without_binary_blocks_opencode_not_found(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """With no guard left, a clean REVIEW spawn on a host without opencode is
    the ordinary retry-eligible ``opencode_not_found`` park, never the #2797
    ``clean_review_unsupported_backend`` block.
    """
    worktree = make_git_repo("wt-opencode-clean-review-nobinary")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.opencode.opencode_available", return_value=False):
        executor.spawn(
            stage=Stage.REVIEW,
            task=task,
            worktree=worktree,
            client=_review_client(worktree, "clean"),
        )

    assert len(fake_runner.calls) == 0
    session = find_completed_session(load_state())
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND
    assert result.blocker.retry_eligible is True


# ---------------------------------------------------------------------------
# Repo review engine guard (#3216)
# ---------------------------------------------------------------------------


def _engine_client(worktree: Path) -> ClientConfig:
    review = ReviewConfig.model_validate(
        {"convergence": "clean", "engine": {"skill": "repo-review"}}
    )
    return ClientConfig(name="test", workspace_path=worktree, review=review)


@pytest.mark.parametrize("binary_available", [True, False])
def test_opencode_engine_review_is_refused_before_the_binary_probe(
    binary_available: bool,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """An engine-configured REVIEW fails closed whether or not opencode exists."""
    worktree = make_git_repo("wt-opencode-engine-refused")
    fake_runner = FakeFireAndForgetRunner()
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    executor = OpencodeExecutor(config=config, runner=fake_runner)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch(
        "cw.executor.opencode.opencode_available", return_value=binary_available
    ) as probe:
        executor.spawn(
            stage=Stage.REVIEW,
            task=task,
            worktree=worktree,
            client=_engine_client(worktree),
        )

    probe.assert_not_called()
    assert len(fake_runner.calls) == 0
    session = find_completed_session(load_state())
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == "review_engine_unsupported_backend"
    assert result.blocker.retry_eligible is False
    assert result.blocker.stage == "stage3_review"
    assert "cause=backend_unsupported" in result.blocker.details
    assert "opencode" in result.blocker.details
    assert "skill=repo-review" in result.blocker.details


def test_opencode_engine_guard_is_review_only(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The engine runs only in REVIEW: an IMPL spawn on the same client proceeds."""
    worktree = make_git_repo("wt-opencode-engine-impl")
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.IMPL)
    client = _engine_client(worktree)
    sess = _sess()

    with patch("cw.executor.opencode.opencode_available", return_value=True):
        result = _opencode_preflight(config, task, worktree, client, Stage.IMPL, sess)

    assert isinstance(result, _PreflightOK)


def test_opencode_review_without_an_engine_is_not_refused_by_the_engine_guard(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A clean client with no ``review.engine`` still reaches the binary probe."""
    worktree = make_git_repo("wt-opencode-no-engine")
    config = StageExecutorConfig(backend=OPENCODE_BACKEND, model="m")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.opencode.opencode_available", return_value=False):
        result = _opencode_preflight(
            config,
            task,
            worktree,
            _review_client(worktree, "clean"),
            Stage.REVIEW,
            _sess(),
        )

    assert isinstance(result, AutoDevResult)
    assert result.blocker is not None
    assert result.blocker.reason == OPENCODE_NOT_FOUND
