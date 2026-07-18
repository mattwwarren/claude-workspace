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
    CodexExecutor,
    StageExecutor,
    _post_review_comment,
    resolve_executor,
)
from cw.local_runner import UNEXPECTED_ERROR, make_blocked
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)

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
            "detail": "",
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
    session = next((s for s in state.sessions if s.last_result is not None), None)
    assert session is not None
    assert session.status == SessionStatus.COMPLETED


def test_codex_executor_must_fix_blocked(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A validating MUST_FIX finding → blocked/codex_must_fix_findings."""
    worktree = _worktree_with_change(
        make_git_repo, "wt-codex-mf", filename="new.py", content="def broken():\n"
    )
    doc = _reviewer_doc(
        findings=[_must_fix_finding(file="new.py", line=1, evidence="def broken():")]
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=doc)
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
    assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
    # #1203's bug (parsed counts must survive onto the blocked sentinel, not
    # fall back to hardcoded 0/0/0/0) re-verified on the per-role path: every
    # role echoes the identical finding, dedup collapses them to exactly one.
    assert result.review.must_fix_initial == 1
    assert result.review.should_fix == 0
    assert result.review.deferred == 0
    assert result.review.fix_cycles_used == 0
    assert result.review.agents_run == len(runner.calls)
    # A blocking verdict is still posted as a comment.
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
        patch("cw.executor.run_review", side_effect=RuntimeError("git boom")),
        pytest.raises(RuntimeError, match="git boom"),
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    state = load_state()
    session = next((s for s in state.sessions if s.last_result is not None), None)
    assert session is not None
    assert session.status == SessionStatus.COMPLETED
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.stage_reached == "stage3_review"
    assert result.blocker.stage == "stage3_review"


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
