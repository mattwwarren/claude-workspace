"""Tests for cw.codex_driver — the ``cw codex run`` subprocess entry point (#2386).

RFC 0014 S1 / ADR-0018: a detached ``cw codex run`` subprocess replaces the
in-process daemon thread (``cw.codex_background``) for the codex review stage.
This module owns the driver's testable core, ``run_codex_review_stage``, which
re-derives the session/task/client/executor-config the thread path used to
receive via Python closure, then calls
``cw.codex_background._run_codex_review_and_complete`` directly — the same
payload the thread path produces for the same inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.codex_driver import (
    STAGE_IMPL,
    STAGE_REVIEW,
    run_codex_review_stage,
)
from cw.codex_fix_loop.commit import _build_fix_codex_argv
from cw.codex_review import CODEX_MUST_FIX_FINDINGS, CODEX_REVIEW_UNPARSEABLE
from cw.codex_runner import FakeCodexRunner
from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.local_runner import make_blocked
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    CwState,
    DevQueueStore,
    LaneConfig,
    LastResultSource,
    QueueItemStatus,
    ReasoningEffort,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _make_daemon_session, _write_global_toggle, git_in

if TYPE_CHECKING:
    from collections.abc import Callable


_FIX_LOOP_LANE = "mf-lane"


def _reviewer_doc(findings: list[dict[str, object]] | None = None) -> str:
    return json.dumps(
        {
            "reviewer_role": "Code Quality Reviewer",
            "status": "ok",
            "detail": "reviewed; no issues found.",
            "findings": findings or [],
        }
    )


def _finding(
    *, severity: str, file: str, line: int, evidence: str
) -> dict[str, object]:
    return {
        "severity": severity,
        "file": file,
        "line_start": line,
        "line_end": line,
        "summary": "Issue here",
        "consequence": "It matters",
        "suggested_fix": "Fix it",
        "evidence": evidence,
        "confidence": "HIGH",
    }


def _worktree_with_change(
    make_worktree_with_change: Callable[..., Path], name: str
) -> Path:
    """A repo on ``feature`` pushed to a bare origin, with ``new.py`` committed."""
    return make_worktree_with_change(name, filename="new.py", content="def broken():\n")


def _persisted_result() -> AutoDevResult:
    session = load_state().sessions[0]
    assert session.status is SessionStatus.COMPLETED
    assert session.last_result_source is LastResultSource.EXECUTOR_DIRECT
    return AutoDevResult.model_validate(session.last_result)


def _seed_client(
    *,
    tmp_config_dir: Path,
    worktree: Path,
    client: ClientConfig,
    ticket_id: str = "T-1",
    session_id: str = "sess-1",
) -> None:
    """Seed Session + RUNNING row for an in-memory *client* (patched get_client).

    The on-disk ``clients.yaml`` still names the client so any config lookup
    beyond ``cw.codex_driver.get_client`` resolves; the lane/pipeline shape
    the review path needs comes from *client* via the patch.
    """
    _seed(
        tmp_config_dir=tmp_config_dir,
        worktree=worktree,
        client_name=client.name,
        ticket_id=ticket_id,
        session_id=session_id,
        lane=client.lanes[0].name if client.lanes else None,
    )


def _seed(
    *,
    tmp_config_dir: Path,
    worktree: Path,
    client_name: str = "test",
    ticket_id: str = "T-1",
    session_id: str = "sess-1",
    lane: str | None = None,
) -> None:
    """Seed a Session + a matching RUNNING dev-queue row, plus clients.yaml."""
    write_clients_yaml(ClientSpec(client_name, str(worktree)))
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id=session_id,
                    client=client_name,
                    worktree_path=worktree,
                )
            ]
        )
    )
    from cw.dev_queue import save_dev_queue

    task = TicketTask(
        ticket_id=ticket_id,
        client=client_name,
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
    )
    if lane is not None:
        task = task.model_copy(update={"lane": lane})
    save_dev_queue(DevQueueStore(tasks=[task]))


def _fix_loop_client(worktree: Path, *, lane_flag: bool | None = True) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=worktree,
        default_branch="main",
        lanes=[LaneConfig(name=_FIX_LOOP_LANE, codex_fix_loop_enabled=lane_flag)],
        # #2633: resolve_operator_login returns this override, so a fix-loop
        # run never reaches cached_gh_login's real `gh api user`.
        operator_github_login="operator-login",
    )


def _workspace_write_calls(runner: FakeCodexRunner) -> list[list[str]]:
    """Runner calls whose argv carries ``workspace-write`` (the fix pass sandbox)."""
    return [
        argv
        for call in runner.calls
        if isinstance(argv := call["argv"], list) and "workspace-write" in argv
    ]


def _run_stage(runner: FakeCodexRunner) -> None:
    run_codex_review_stage(
        ticket_id="T-1",
        session_id="sess-1",
        wall_clock_budget_seconds=None,
        runner=runner,
    )


def test_run_codex_review_stage_completes_session_via_door_with_fake_runner(
    tmp_config_dir: Path,
    make_worktree_with_change: Callable[..., Path],
) -> None:
    """The driver's core reproduces the thread path's completion payload.

    Every role returns a clean document → stage_complete, the non-blocking
    verdict is posted, and — with no lane override the fix loop resolves to
    the global default (off) — the comment states that as "disabled" (#1705).
    Ported from ``test_codex_executor.py`` when CodexExecutor stopped running
    the review in-process (#2388).
    """
    worktree = _worktree_with_change(make_worktree_with_change, "wt-driver-clean")
    _seed(tmp_config_dir=tmp_config_dir, worktree=worktree)
    runner = FakeCodexRunner(returncode=0, output_file_content=_reviewer_doc())

    with patch("cw.codex_background._post_review_comment") as post_mock:
        _run_stage(runner)

    result = _persisted_result()
    assert result.status == "stage_complete"
    assert result.stage_reached == "stage3_review"
    assert result.health.recommendation == "PROCEED"
    assert result.review.must_fix_initial == 0
    assert result.review.should_fix == 0
    assert result.review.fix_cycles_used == 0
    # Every selected role is fed its prompt over stdin.
    assert all(call["stdin"] for call in runner.calls)
    # -1 for the filesystem-capability probe call (#1709) on a cold cache.
    assert result.review.agents_run == len(runner.calls) - 1
    post_mock.assert_called_once()
    assert post_mock.call_args.args[0] == "T-1"
    body = post_mock.call_args.args[1]
    assert "Non-blocking" in body
    assert "disabled" in body.lower()


def test_run_codex_review_stage_must_fix_runs_fix_loop_to_cap_and_parks(
    tmp_config_dir: Path,
    make_worktree_with_change: Callable[..., Path],
) -> None:
    """A persistent MUST_FIX finding on a fix-loop lane → loop caps → blocked.

    FakeCodexRunner never edits the worktree, so the survivor is counted in
    both must_fix_initial and deferred, and the posted comment says 0 of 1
    were resolved (#1392, #1705).
    """
    worktree = _worktree_with_change(make_worktree_with_change, "wt-driver-mf")
    client = _fix_loop_client(worktree)
    _seed_client(tmp_config_dir=tmp_config_dir, worktree=worktree, client=client)
    doc = _reviewer_doc(
        [_finding(severity="MUST_FIX", file="new.py", line=1, evidence="def broken():")]
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=doc)

    with (
        patch("cw.codex_driver.get_client", return_value=client),
        patch("cw.codex_background._post_review_comment") as post_mock,
    ):
        _run_stage(runner)

    result = _persisted_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
    assert result.review.must_fix_initial == 1
    assert result.review.deferred == 1
    assert result.review.fix_cycles_used == 5
    assert result.health.fix_loop_escalated is True
    post_mock.assert_called_once()
    body = post_mock.call_args.args[1]
    assert "BLOCKING" in body
    assert "0 of 1" in body


def test_run_codex_review_stage_lane_false_over_global_true_never_builds_fix_argv(
    tmp_config_dir: Path,
    make_worktree_with_change: Callable[..., Path],
) -> None:
    """#2541: lane ``false`` beats global ``true`` -> zero workspace-write fix passes.

    A blocking cycle-0 verdict parks on CODEX_MUST_FIX_FINDINGS without ever
    building a fix argv, invoking a workspace-write codex, or moving HEAD.
    """
    worktree = _worktree_with_change(make_worktree_with_change, "wt-driver-optout")
    client = _fix_loop_client(worktree, lane_flag=False)
    _seed_client(tmp_config_dir=tmp_config_dir, worktree=worktree, client=client)
    _write_global_toggle(tmp_config_dir, "default_codex_fix_loop_enabled", "true")
    doc = _reviewer_doc(
        [_finding(severity="MUST_FIX", file="new.py", line=1, evidence="def broken():")]
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=doc)
    head_before = git_in(worktree, "rev-parse", "HEAD")

    with (
        patch("cw.codex_driver.get_client", return_value=client),
        patch("cw.codex_background._post_review_comment") as post_mock,
        patch(
            "cw.codex_fix_loop.commit._build_fix_codex_argv",
            wraps=_build_fix_codex_argv,
        ) as argv_mock,
    ):
        _run_stage(runner)

    argv_mock.assert_not_called()
    assert _workspace_write_calls(runner) == []
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
    assert result.review.fix_cycles_used == 0
    assert result.health.fix_loop_escalated is not True
    assert git_in(worktree, "rev-parse", "HEAD") == head_before
    post_mock.assert_called_once()
    assert "BLOCKING" in post_mock.call_args.args[1]


@pytest.mark.parametrize(
    ("lane_flag", "global_flag", "expect_fix"),
    [
        (False, "true", False),
        (None, "true", True),
        (True, "false", True),
    ],
    ids=["lane-false-global-true", "lane-unset-global-true", "lane-true-global-false"],
)
def test_run_codex_review_stage_fix_loop_resolution_matrix(
    tmp_config_dir: Path,
    make_worktree_with_change: Callable[..., Path],
    lane_flag: bool | None,
    global_flag: str,
    expect_fix: bool,
) -> None:
    """#2541: positive controls proving the argv spy is aimed at the real call."""
    worktree = _worktree_with_change(
        make_worktree_with_change, f"wt-driver-matrix-{lane_flag}-{global_flag}"
    )
    client = _fix_loop_client(worktree, lane_flag=lane_flag)
    _seed_client(tmp_config_dir=tmp_config_dir, worktree=worktree, client=client)
    _write_global_toggle(tmp_config_dir, "default_codex_fix_loop_enabled", global_flag)
    doc = _reviewer_doc(
        [_finding(severity="MUST_FIX", file="new.py", line=1, evidence="def broken():")]
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=doc)

    with (
        patch("cw.codex_driver.get_client", return_value=client),
        patch("cw.codex_background._post_review_comment"),
        patch(
            "cw.codex_fix_loop.commit._build_fix_codex_argv",
            wraps=_build_fix_codex_argv,
        ) as argv_mock,
    ):
        _run_stage(runner)

    result = _persisted_result()
    if expect_fix:
        assert argv_mock.call_count >= 1
        assert len(_workspace_write_calls(runner)) >= 1
        assert result.review.fix_cycles_used == 5
    else:
        argv_mock.assert_not_called()
        assert _workspace_write_calls(runner) == []
        assert result.review.fix_cycles_used == 0


def test_run_codex_review_stage_clean_with_fix_loop_enabled_states_available(
    tmp_config_dir: Path,
    make_worktree_with_change: Callable[..., Path],
) -> None:
    """#1705: lane fix-loop enablement threads through to the posted comment."""
    worktree = _worktree_with_change(
        make_worktree_with_change, "wt-driver-clean-loop-enabled"
    )
    client = _fix_loop_client(worktree)
    _seed_client(tmp_config_dir=tmp_config_dir, worktree=worktree, client=client)
    runner = FakeCodexRunner(returncode=0, output_file_content=_reviewer_doc())

    with (
        patch("cw.codex_driver.get_client", return_value=client),
        patch("cw.codex_background._post_review_comment") as post_mock,
    ):
        _run_stage(runner)

    assert _persisted_result().status == "stage_complete"
    post_mock.assert_called_once()
    assert "available" in post_mock.call_args.args[1].lower()


def test_run_codex_review_stage_all_roles_unparseable_parks_with_artifact(
    tmp_config_dir: Path,
    make_worktree_with_change: Callable[..., Path],
) -> None:
    """Unparseable output from every role → blocked; per-role summary posted (#2280)."""
    worktree = _worktree_with_change(make_worktree_with_change, "wt-driver-fail")
    _seed(tmp_config_dir=tmp_config_dir, worktree=worktree)
    runner = FakeCodexRunner(returncode=0, output_file_content="not json{{")

    with patch("cw.codex_background._post_review_comment") as post_mock:
        _run_stage(runner)

    result = _persisted_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE
    post_mock.assert_called_once()
    assert post_mock.call_args.args[1] == result.blocker.details
    artifact = post_mock.call_args.kwargs["artifact_path"]
    assert artifact == worktree / ".claude" / "review-verdict-unparseable.md"
    assert artifact.exists()


def test_run_codex_review_stage_should_fix_only_stays_complete(
    tmp_config_dir: Path,
    make_worktree_with_change: Callable[..., Path],
) -> None:
    """A SHOULD_FIX-only finding (no MUST_FIX) → stays stage_complete."""
    worktree = _worktree_with_change(
        make_worktree_with_change, "wt-driver-should-fix-only"
    )
    _seed(tmp_config_dir=tmp_config_dir, worktree=worktree)
    doc = _reviewer_doc(
        [
            _finding(
                severity="SHOULD_FIX", file="new.py", line=1, evidence="def broken():"
            )
        ]
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=doc)

    with patch("cw.codex_background._post_review_comment"):
        _run_stage(runner)

    result = _persisted_result()
    assert result.status == "stage_complete"
    assert result.review.must_fix_initial == 0
    assert result.review.should_fix == 1
    assert result.review.deferred == 0
    assert result.review.agents_run == len(runner.calls) - 1


def test_run_codex_review_stage_threads_reasoning_effort_from_resolved_executor_config(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The REVIEW stage config's model/reasoning_effort reach the review.

    A detached subprocess has no closure over the executor's config, so the
    driver re-resolves it from the client; this pins that the resolved
    values — not defaults — are what ``_run_codex_review_and_complete`` gets,
    and that the session id threaded through is the driver's own.
    """
    worktree = make_git_repo("wt-driver-effort")
    client = ClientConfig(
        name="test",
        workspace_path=worktree,
        default_branch="main",
        pipeline=StagePipelineConfig(
            executors={
                Stage.REVIEW: StageExecutorConfig(
                    backend=CODEX_BACKEND,
                    model="gpt-test",
                    reasoning_effort=ReasoningEffort.MAX,
                )
            }
        ),
    )
    _seed_client(tmp_config_dir=tmp_config_dir, worktree=worktree, client=client)
    captured: dict[str, object] = {}

    def _spy_run_review(**kwargs: object) -> tuple[AutoDevResult, None]:
        captured.update(kwargs)
        blocked = make_blocked(
            ticket_id="T-1",
            worktree=worktree,
            reason=CODEX_REVIEW_UNPARSEABLE,
            stage_reached="stage3_review",
        )
        return blocked, None

    with (
        patch("cw.codex_driver.get_client", return_value=client),
        patch("cw.codex_background.run_review_with_fix_loop", _spy_run_review),
        patch("cw.codex_background._post_review_comment"),
    ):
        _run_stage(FakeCodexRunner(returncode=0))

    assert captured["session_id"] == "sess-1"
    assert captured["reasoning_effort"] == "max"
    assert captured["model"] == "gpt-test"


def test_run_codex_review_stage_no_running_task_raises(
    tmp_config_dir: Path,
    make_worktree_with_change: Callable[..., Path],
) -> None:
    """A session with no matching RUNNING dev-queue row is refused."""
    worktree = _worktree_with_change(make_worktree_with_change, "wt-driver-no-task")
    write_clients_yaml(ClientSpec("test", str(worktree)))
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(id="sess-2", client="test", worktree_path=worktree)
            ]
        )
    )

    with pytest.raises(CwError, match="T-missing"):
        run_codex_review_stage(
            ticket_id="T-missing",
            session_id="sess-2",
            wall_clock_budget_seconds=None,
        )


def test_run_codex_review_stage_unknown_session_raises(
    tmp_config_dir: Path,
) -> None:
    """No session at all in state → CwError."""
    with pytest.raises(CwError, match="sess-unknown"):
        run_codex_review_stage(
            ticket_id="T-1",
            session_id="sess-unknown",
            wall_clock_budget_seconds=None,
        )


def test_run_codex_review_stage_no_worktree_path_raises(
    tmp_config_dir: Path,
) -> None:
    """A session with no worktree_path cannot be reviewed → CwError."""
    write_clients_yaml(ClientSpec("test", "/tmp/does-not-matter"))
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(id="sess-3", client="test", worktree_path=None)
            ]
        )
    )
    from cw.dev_queue import save_dev_queue

    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id="T-1",
                    client="test",
                    stage=Stage.REVIEW,
                    status=QueueItemStatus.RUNNING,
                    session_id="sess-3",
                )
            ]
        )
    )

    with pytest.raises(CwError, match="worktree"):
        run_codex_review_stage(
            ticket_id="T-1",
            session_id="sess-3",
            wall_clock_budget_seconds=None,
        )


def test_codex_driver_module_not_imported_by_executor() -> None:
    """D-1 process-boundary invariant: cw.executor never imports cw.codex_driver.

    codex_driver.py owns the ``cw codex run`` subprocess entry point and is
    invoked only as a subprocess of CodexExecutor.spawn() (RFC 0014 S1) — a
    module-level (or any) import the other way would collapse that boundary.
    """
    repo_root = Path(__file__).resolve().parent.parent
    executor_files = list((repo_root / "src" / "cw" / "executor").rglob("*.py"))
    assert executor_files, "expected to find files under src/cw/executor"
    for path in executor_files:
        text = path.read_text(encoding="utf-8")
        assert "codex_driver" not in text, f"{path} references codex_driver"


def test_stage_constants_are_stage_agnostic_strings() -> None:
    assert STAGE_REVIEW == "review"
    assert STAGE_IMPL == "impl"
