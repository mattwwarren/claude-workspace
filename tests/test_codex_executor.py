"""Tests for cw.executor.CodexExecutor — prompt-driven codex review backend.

RFC 0005 F1, rewired for the per-reviewer-role loop (#1236). The executor's job
is pre-flight, delegate Step 3 to ``codex_review.run_review``, persist, post the
consolidated verdict, emit SESSION_COMPLETED, and handle failures. Per-role
disposition and consolidation are covered in test_codex_review.py; here we test
the executor orchestration end-to-end (real FakeCodexRunner + real diff) plus the
delegation/exception seams.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.codex_review import CODEX_MUST_FIX_FINDINGS, CODEX_REVIEW_UNPARSEABLE
from cw.codex_runner import FakeCodexRunner
from cw.config import load_state
from cw.executor import (
    CODEX_NOT_FOUND,
    CODEX_REVIEW_ONLY,
    CODEX_VERSION_UNKNOWN,
    CodexCapabilityDiagnosis,
    CodexExecutor,
    StageExecutor,
    _post_review_comment,
    _resolve_codex_fix_loop_enabled,
    codex_capability_diagnosis,
    resolve_executor,
)
from cw.local_runner import UNEXPECTED_ERROR, make_blocked
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    LaneConfig,
    LastResultSource,
    OrchestratorConfig,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from tests.conftest import find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable


def _persisted_result() -> AutoDevResult:
    """Load the single persisted last_result and validate it."""
    state = load_state()
    result_raw = next(
        (s.last_result for s in state.sessions if s.last_result is not None), None
    )
    assert result_raw is not None
    return AutoDevResult.model_validate(result_raw)


def _git(repo: Path, *args: str) -> None:
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, env=clean_env
    )


def _worktree_with_change(
    make_git_repo: Callable[[str], Path], name: str, *, filename: str, content: str
) -> Path:
    """Return a repo on a feature branch with *content* committed to *filename*."""
    repo = make_git_repo(name)
    _git(repo, "checkout", "-b", "feature")
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", f"add {filename}")
    return repo


def _reviewer_doc(
    *,
    role: str = "Code Quality Reviewer",
    findings: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "reviewer_role": role,
            "status": "ok",
            "detail": "reviewed; no issues found.",
            "findings": findings or [],
        }
    )


def _must_fix_finding(*, file: str, line: int, evidence: str) -> dict[str, object]:
    return {
        "severity": "MUST_FIX",
        "file": file,
        "line_start": line,
        "line_end": line,
        "summary": "Bug here",
        "consequence": "It breaks",
        "suggested_fix": "Fix it",
        "evidence": evidence,
        "confidence": "HIGH",
    }


def _should_fix_finding(*, file: str, line: int, evidence: str) -> dict[str, object]:
    return {
        "severity": "SHOULD_FIX",
        "file": file,
        "line_start": line,
        "line_end": line,
        "summary": "Nit here",
        "consequence": "Minor",
        "suggested_fix": "Polish it",
        "evidence": evidence,
        "confidence": "HIGH",
    }


# ---------------------------------------------------------------------------
# Pre-flight (unchanged)
# ---------------------------------------------------------------------------


def test_codex_executor_wrong_stage_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """spawn() on a non-REVIEW stage → blocked/codex_review_only."""
    worktree = make_git_repo("wt-codex-wrong-stage")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.PLAN)

    executor.spawn(stage=Stage.PLAN, task=task, worktree=worktree, client=client)

    assert len(runner.calls) == 0
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_REVIEW_ONLY


def test_codex_executor_codex_not_found(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """shutil.which('codex') is None → blocked/codex_not_found."""
    worktree = make_git_repo("wt-codex-missing")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree)
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.shutil.which", return_value=None):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert len(runner.calls) == 0
    result = _persisted_result()
    assert result.status == "blocked"
    assert result.stage_reached == "stage3_review"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_NOT_FOUND


# ---------------------------------------------------------------------------
# End-to-end wiring (real FakeCodexRunner + real diff)
# ---------------------------------------------------------------------------


def test_codex_executor_clean_stage_complete(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Every role returns a clean document → stage_complete, verdict posted."""
    worktree = _worktree_with_change(
        make_git_repo, "wt-codex-clean", filename="new.py", content="def broken():\n"
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=_reviewer_doc())
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch("cw.executor._post_review_comment") as post_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert len(runner.calls) >= 1
    # Every selected role is fed its prompt over stdin.
    assert all(call["stdin"] for call in runner.calls)
    post_mock.assert_called_once()
    assert post_mock.call_args.args[0] == "T-1"
    assert "Non-blocking" in post_mock.call_args.args[1]
    result = _persisted_result()
    assert result.status == "stage_complete"
    assert result.stage_reached == "stage3_review"
    assert result.health.recommendation == "PROCEED"
    assert result.health.any_incomplete_risk is False
    assert result.review.must_fix_initial == 0
    assert result.review.should_fix == 0
    assert result.review.deferred == 0
    assert result.review.fix_cycles_used == 0
    # Every selected role invoked codex exec and returned a clean document, so
    # agents_run tracks the per-role loop's call count exactly (#1194 wiring
    # ported onto the per-role path, #1236 Blocker Resolution).
    assert result.review.agents_run == len(runner.calls)
    # Round-trips through the strict validator.
    AutoDevResult.model_validate(result.model_dump(mode="json"))

    state = load_state()
    session = find_completed_session(state)
    assert session.status == SessionStatus.COMPLETED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT


def test_codex_executor_must_fix_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A persistent MUST_FIX finding → fix loop runs to cap → blocked (#1392).

    ``FakeCodexRunner`` returns the same MUST_FIX doc for every call and never
    edits the worktree, so each fix invocation is a no-op the re-review still
    finds blocking — the loop caps at ``_MAX_FIX_CYCLES`` and parks with the
    finding recorded in BOTH ``must_fix_initial`` (cycle-0 snapshot) and
    ``deferred`` (the cross-cycle survivor set).

    A lane-scoped ``codex_fix_loop_enabled=True`` (#1553) is required here:
    the resolved default is False, which would park at cycle 0 instead of
    running the loop to cap — that default-off path is covered by
    ``TestFixLoopDisabledGate`` in ``test_codex_fix_loop.py``.
    """
    worktree = _worktree_with_change(
        make_git_repo, "wt-codex-mf", filename="new.py", content="def broken():\n"
    )
    doc = _reviewer_doc(
        findings=[_must_fix_finding(file="new.py", line=1, evidence="def broken():")]
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=doc)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(
        name="test",
        workspace_path=worktree,
        default_branch="main",
        lanes=[LaneConfig(name="mf-lane", codex_fix_loop_enabled=True)],
    )
    task = TicketTask(
        ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="mf-lane"
    )

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch("cw.executor._post_review_comment") as post_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    result = _persisted_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
    # Every role echoes the identical finding; dedup collapses them to one, and
    # the survivor is counted in both must_fix_initial and deferred at cap.
    assert result.review.must_fix_initial == 1
    assert result.review.should_fix == 0
    assert result.review.deferred == 1
    assert result.review.fix_cycles_used == 5
    assert result.health.fix_loop_escalated is True
    # The survivor verdict is still posted as a blocking comment.
    post_mock.assert_called_once()
    assert "BLOCKING" in post_mock.call_args.args[1]


def test_codex_executor_all_roles_fail_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Unparseable output from every role → blocked, no comment posted."""
    worktree = _worktree_with_change(
        make_git_repo, "wt-codex-fail", filename="new.py", content="def broken():\n"
    )
    runner = FakeCodexRunner(returncode=0, output_file_content="not json{{")
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch("cw.executor._post_review_comment") as post_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    result = _persisted_result()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE
    # No documents → no verdict → nothing to post.
    post_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Delegation / exception seams
# ---------------------------------------------------------------------------


def test_codex_executor_stage_sentinel_schema(tmp_path: Path) -> None:
    """stage_sentinel_schema returns the AutoDevResult JSON schema."""
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config)

    schema = executor.stage_sentinel_schema(Stage.REVIEW)

    assert schema == AutoDevResult.model_json_schema()


def test_codex_executor_should_fix_only_stays_complete(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A SHOULD_FIX-only finding (no MUST_FIX) → stays stage_complete.

    Retargeted from main's #1203 single-shot-path test onto the per-role
    architecture (#1236 Blocker Resolution): the old path fed the executor a
    raw ``{must_fix_initial, should_fix, deferred}`` payload directly; the new
    path always derives those counts from validated per-role findings, so
    "should_fix only" here means one accepted SHOULD_FIX finding and zero
    MUST_FIX findings.
    """
    worktree = _worktree_with_change(
        make_git_repo,
        "wt-codex-should-fix-only",
        filename="new.py",
        content="def broken():\n",
    )
    doc = _reviewer_doc(
        findings=[_should_fix_finding(file="new.py", line=1, evidence="def broken():")]
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=doc)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.shutil.which", return_value="/usr/bin/codex"):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    result = _persisted_result()
    assert result.status == "stage_complete"
    assert result.review.must_fix_initial == 0
    assert result.review.should_fix == 1
    assert result.review.deferred == 0
    assert result.review.agents_run == len(runner.calls)


def test_spawn_delegates_to_fix_loop_not_bare_run_review(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#1392: CodexExecutor.spawn on REVIEW calls run_review_with_fix_loop."""
    worktree = make_git_repo("wt-codex-wiring")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-wire", client="test", stage=Stage.REVIEW)

    blocked = make_blocked(
        ticket_id="T-wire",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )
    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch(
            "cw.executor.run_review_with_fix_loop", return_value=(blocked, None)
        ) as fix_loop_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    fix_loop_mock.assert_called_once()
    # No FakeCodexRunner call happened — the executor delegated the whole review
    # pass to the (patched) fix loop rather than driving codex itself.
    assert len(runner.calls) == 0
    result = _persisted_result()
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE
    # #1553: client has no lanes, so effective_lanes synthesizes the default
    # lane with codex_fix_loop_enabled=None; the resolver falls through to
    # OrchestratorConfig()'s default (default_codex_fix_loop_enabled=False),
    # giving fix_loop_enabled=False here.
    assert fix_loop_mock.call_args.kwargs["fix_loop_enabled"] is False


def test_spawn_threads_codex_fix_loop_enabled_true_from_lane(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#1553: LaneConfig.codex_fix_loop_enabled=True threads fix_loop_enabled=True."""
    worktree = make_git_repo("wt-codex-wiring-enabled")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(
        name="test",
        workspace_path=worktree,
        default_branch="main",
        lanes=[LaneConfig(name="mf-lane", codex_fix_loop_enabled=True)],
    )
    task = TicketTask(
        ticket_id="T-wire-2", client="test", stage=Stage.REVIEW, lane="mf-lane"
    )

    blocked = make_blocked(
        ticket_id="T-wire-2",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )
    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch(
            "cw.executor.run_review_with_fix_loop", return_value=(blocked, None)
        ) as fix_loop_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    fix_loop_mock.assert_called_once()
    assert fix_loop_mock.call_args.kwargs["fix_loop_enabled"] is True


# ---------------------------------------------------------------------------
# _resolve_codex_fix_loop_enabled precedence (#1553)
# ---------------------------------------------------------------------------


def test_resolve_codex_fix_loop_enabled_lane_true_wins_regardless_of_global() -> None:
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp/x"),
        lanes=[LaneConfig(name="trial", codex_fix_loop_enabled=True)],
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="trial")
    config = OrchestratorConfig(default_codex_fix_loop_enabled=False)

    assert _resolve_codex_fix_loop_enabled(client, task, config) is True


def test_resolve_codex_fix_loop_enabled_lane_unset_global_true() -> None:
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp/x"),
        lanes=[LaneConfig(name="trial")],
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="trial")
    config = OrchestratorConfig(default_codex_fix_loop_enabled=True)

    assert _resolve_codex_fix_loop_enabled(client, task, config) is True


def test_resolve_codex_fix_loop_enabled_lane_unset_global_default_false() -> None:
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp/x"),
        lanes=[LaneConfig(name="trial")],
    )
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="trial")
    config = OrchestratorConfig()

    assert _resolve_codex_fix_loop_enabled(client, task, config) is False


def test_resolve_codex_fix_loop_enabled_unmatched_lane_falls_through_to_global() -> None:
    client = ClientConfig(
        name="test",
        workspace_path=Path("/tmp/x"),
        lanes=[LaneConfig(name="trial", codex_fix_loop_enabled=True)],
    )
    task = TicketTask(
        ticket_id="T-1", client="test", stage=Stage.REVIEW, lane="no-such-lane"
    )
    config = OrchestratorConfig(default_codex_fix_loop_enabled=True)

    assert _resolve_codex_fix_loop_enabled(client, task, config) is True


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


def test_codex_executor_exception_handler_marks_session_completed(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """Uncaught exception in Step 3 → session COMPLETED, exception re-raised."""
    worktree = make_git_repo("wt-codex-exc-handler")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-exc", client="test", stage=Stage.REVIEW)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch(
            "cw.executor.run_review_with_fix_loop",
            side_effect=RuntimeError("git boom"),
        ),
        pytest.raises(RuntimeError, match="git boom"),
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    state = load_state()
    session = find_completed_session(state)
    assert session.status == SessionStatus.COMPLETED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.stage_reached == "stage3_review"
    assert result.blocker.stage == "stage3_review"


def test_spawn_threads_real_session_id_into_run_review(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """CodexExecutor.spawn passes the real cw session id (its own sid, not a
    fresh uuid) into run_review so diagnostics land under the right dir."""
    worktree = make_git_repo("wt-codex-sid-thread")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-sid", client="test", stage=Stage.REVIEW)

    captured: dict[str, object] = {}

    def _spy_run_review(**kwargs: object) -> tuple[AutoDevResult, None]:
        captured["session_id"] = kwargs["session_id"]
        blocked = make_blocked(
            ticket_id="T-sid",
            worktree=worktree,
            reason=CODEX_REVIEW_UNPARSEABLE,
            stage_reached="stage3_review",
        )
        return blocked, None

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch("cw.executor.run_review_with_fix_loop", _spy_run_review),
    ):
        sid = executor.spawn(
            stage=Stage.REVIEW, task=task, worktree=worktree, client=client
        )

    assert captured["session_id"] == sid


def test_post_review_comment_suppresses_oserror() -> None:
    """_post_review_comment swallows OSError from a missing gh binary."""
    with patch("cw.gh._sp.run", side_effect=FileNotFoundError("no gh")):
        _post_review_comment("T-1", "findings")


def test_post_review_comment_suppresses_timeout() -> None:
    """_post_review_comment swallows TimeoutExpired when gh hangs."""
    import subprocess as _subprocess

    with patch(
        "cw.gh._sp.run",
        side_effect=_subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        _post_review_comment("T-1", "findings")


def test_post_review_comment_forwards_cwd() -> None:
    """#1279: _post_review_comment scopes the gh call to the client's repo."""
    want_cwd = Path("/some/client-a/repo")
    with patch("cw.executor.post_issue_comment") as post_mock:
        _post_review_comment("T-1", "findings", cwd=want_cwd)
    post_mock.assert_called_once_with("T-1", "findings", cwd=want_cwd)


def test_post_review_comment_logs_on_none_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """gh call couldn't run at all (missing binary / timeout) -> warning, not silent."""
    with (
        patch("cw.executor.post_issue_comment", return_value=None),
        caplog.at_level("WARNING"),
    ):
        _post_review_comment("T-1", "findings", cwd=None)
    assert any(
        "T-1" in r.message and "gh call failed" in r.message for r in caplog.records
    )


def test_post_review_comment_logs_on_nonzero_returncode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-zero gh exit -> warning carries ticket_id, returncode, and stderr."""
    fake_result = subprocess.CompletedProcess(
        args=["gh"], returncode=1, stdout=b"", stderr=b"invalid issue format"
    )
    with (
        patch("cw.executor.post_issue_comment", return_value=fake_result),
        caplog.at_level("WARNING"),
    ):
        _post_review_comment("T-1", "findings", cwd=None)
    assert any(
        "T-1" in r.message and "1" in r.message and "invalid issue format" in r.message
        for r in caplog.records
    )


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


def _mk_codex_proc(
    stdout: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


class TestCodexCapabilityDiagnosis:
    """Direct tests for the shared codex capability probe (#1238).

    ``shutil.which`` is patched via this file's established
    ``patch("cw.executor.shutil.which", ...)`` idiom; the ``codex --version``
    subprocess is patched at ``cw.executor.subprocess.run``.
    """

    def test_binary_absent_returns_not_found(self) -> None:
        with patch("cw.executor.shutil.which", return_value=None):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_NOT_FOUND
        assert "not found" in probe.detail

    def test_version_filenotfound_returns_version_unknown(self) -> None:
        with (
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch("cw.executor.subprocess.run", side_effect=FileNotFoundError("gone")),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_VERSION_UNKNOWN

    def test_version_timeout_returns_version_unknown(self) -> None:
        with (
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=10),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_VERSION_UNKNOWN
        assert "timed out" in probe.detail

    def test_nonzero_returncode_returns_version_unknown(self) -> None:
        with (
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.subprocess.run",
                return_value=_mk_codex_proc("boom\n", returncode=3),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_VERSION_UNKNOWN
        assert "3" in probe.detail

    def test_unparseable_version_returns_version_unknown(self) -> None:
        with (
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.subprocess.run",
                return_value=_mk_codex_proc("not-a-version\n"),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis == CODEX_VERSION_UNKNOWN
        assert "could not parse" in probe.detail

    def test_parseable_version_returns_capable(self) -> None:
        with (
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.subprocess.run",
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
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.subprocess.run",
                return_value=_mk_codex_proc("codex-cli 0.136.0\n"),
            ),
        ):
            probe = codex_capability_diagnosis()
        assert probe.diagnosis is None
        assert probe.detail == "codex-cli 0.136.0"

    def test_timeout_seconds_passed_to_subprocess_run(self) -> None:
        """The hot-path caller (dispatch's gate) needs to override the timeout."""
        with (
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.subprocess.run",
                return_value=_mk_codex_proc("codex-cli 0.136.0\n"),
            ) as mock_run,
        ):
            codex_capability_diagnosis(timeout_seconds=3)
        assert mock_run.call_args.kwargs["timeout"] == 3

    def test_default_timeout_passed_to_subprocess_run(self) -> None:
        """`cw doctor`'s one-shot call site relies on the 10s default."""
        with (
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "cw.executor.subprocess.run",
                return_value=_mk_codex_proc("codex-cli 0.136.0\n"),
            ) as mock_run,
        ):
            codex_capability_diagnosis()
        assert mock_run.call_args.kwargs["timeout"] == 10
