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
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw import codex_background
from cw.auto_dev_result import AutoDevResult
from cw.codex_background import join_outstanding_codex_threads
from cw.codex_review import CODEX_MUST_FIX_FINDINGS, CODEX_REVIEW_UNPARSEABLE
from cw.codex_runner import FakeCodexRunner
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
from cw.local_runner import UNEXPECTED_ERROR, make_blocked
from cw.models import (
    CODEX_BACKEND,
    ClientConfig,
    LaneConfig,
    LastResultSource,
    QueueItemStatus,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from tests._codex_review_helpers import _mk_codex_proc
from tests.conftest import find_completed_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.codex_runner import CodexRunner


def _sync_codex_executor(
    config: StageExecutorConfig, runner: CodexRunner | None = None
) -> CodexExecutor:
    """A CodexExecutor whose background seam runs inline (#1727).

    Since spawn() hands Step 3/4/4b/5 to a daemon thread, every assertion in
    this file about persisted state / emitted events / posted comments would
    otherwise race the worker. Injecting ``background=lambda fn: fn()`` keeps
    these tests deterministic and keeps them asserting the same observable
    outcomes they asserted when spawn() was synchronous end-to-end. The
    threading seam itself is covered in tests/test_codex_background.py.
    """
    return CodexExecutor(config=config, runner=runner, background=lambda fn: fn())


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
    executor = _sync_codex_executor(config, runner)
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
    executor = _sync_codex_executor(config, runner)
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
    executor = _sync_codex_executor(config, runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch("cw.codex_background._post_review_comment") as post_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert len(runner.calls) >= 1
    # Every selected role is fed its prompt over stdin.
    assert all(call["stdin"] for call in runner.calls)
    post_mock.assert_called_once()
    assert post_mock.call_args.args[0] == "T-1"
    assert "Non-blocking" in post_mock.call_args.args[1]
    # #1705: default lane has no codex_fix_loop_enabled override, so it
    # resolves to the global default (False) — the posted comment must state
    # this history as its own state ("single-pass"/"disabled"), never as
    # flaked/degraded.
    assert "disabled" in post_mock.call_args.args[1].lower()
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
    # -1 for the filesystem-capability probe call (#1709): this test starts on
    # a cold per-test cache, so the first _prepare_review_pass spends one extra
    # runner.run() on the probe before any role runs.
    assert result.review.agents_run == len(runner.calls) - 1
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
    executor = _sync_codex_executor(config, runner)
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
        patch("cw.codex_background._post_review_comment") as post_mock,
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
    # #1705: FakeCodexRunner here never edits the worktree, so 0 of the 1
    # originally-found finding was ever actually resolved across all 5 capped
    # cycles — the posted comment must say so honestly, not silently omit it.
    assert "0 of 1" in post_mock.call_args.args[1]


def test_codex_executor_clean_stage_complete_fix_loop_enabled_states_available(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#1705: a clean review with the fix loop enabled for the lane states
    "available but not needed" — distinct from the disabled-lane wording in
    ``test_codex_executor_clean_stage_complete`` — proving fix_loop_enabled
    threads correctly all the way from ``_resolve_codex_fix_loop_enabled``
    through to the posted GitHub comment, not just through the renderer's own
    unit tests."""
    worktree = _worktree_with_change(
        make_git_repo,
        "wt-codex-clean-loop-enabled",
        filename="new.py",
        content="def broken():\n",
    )
    runner = FakeCodexRunner(returncode=0, output_file_content=_reviewer_doc())
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = _sync_codex_executor(config, runner)
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
        patch("cw.codex_background._post_review_comment") as post_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    post_mock.assert_called_once()
    enabled_body = post_mock.call_args.args[1]
    assert "available" in enabled_body.lower()
    result = _persisted_result()
    assert result.status == "stage_complete"


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
    executor = _sync_codex_executor(config, runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch("cw.codex_background._post_review_comment") as post_mock,
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
    executor = _sync_codex_executor(config)

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
    executor = _sync_codex_executor(config, runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)

    with patch("cw.executor.shutil.which", return_value="/usr/bin/codex"):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    result = _persisted_result()
    assert result.status == "stage_complete"
    assert result.review.must_fix_initial == 0
    assert result.review.should_fix == 1
    assert result.review.deferred == 0
    # -1 for the filesystem-capability probe call (#1709) — see
    # test_codex_executor_clean_stage_complete for the same adjustment.
    assert result.review.agents_run == len(runner.calls) - 1


def test_spawn_delegates_to_fix_loop_not_bare_run_review(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#1392: CodexExecutor.spawn on REVIEW calls run_review_with_fix_loop."""
    worktree = make_git_repo("wt-codex-wiring")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = _sync_codex_executor(config, runner)
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
            "cw.codex_background.run_review_with_fix_loop", return_value=(blocked, None)
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
    executor = _sync_codex_executor(config, runner)
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
            "cw.codex_background.run_review_with_fix_loop", return_value=(blocked, None)
        ) as fix_loop_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    fix_loop_mock.assert_called_once()
    assert fix_loop_mock.call_args.kwargs["fix_loop_enabled"] is True


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
    """Exception in Step 3 → session COMPLETED and the claimed task reverted.

    #1727: Step 3 now runs on a background thread with no caller left to catch
    for it, so the exception is swallowed rather than re-raised — and the
    revert-to-PENDING that dispatch's own ``except`` handler used to perform
    moves into the worker. spawn() itself returns the sid normally.
    """
    worktree = make_git_repo("wt-codex-exc-handler")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = _sync_codex_executor(config, runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-exc", client="test", stage=Stage.REVIEW)
    add_ticket(
        TicketTask(
            ticket_id="T-exc",
            client="test",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
        )
    )

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch(
            "cw.codex_background.run_review_with_fix_loop",
            side_effect=RuntimeError("git boom"),
        ),
    ):
        sid = executor.spawn(
            stage=Stage.REVIEW, task=task, worktree=worktree, client=client
        )

    state = load_state()
    session = find_completed_session(state)
    assert session.id == sid
    assert session.status == SessionStatus.COMPLETED
    assert session.last_result_source == LastResultSource.EXECUTOR_DIRECT
    result = AutoDevResult.model_validate(session.last_result)
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == UNEXPECTED_ERROR
    assert result.stage_reached == "stage3_review"
    assert result.blocker.stage == "stage3_review"
    # R1: session_id was stamped before backgrounding, then cleared by the
    # revert — the task is available for a later tick, not orphaned RUNNING.
    stored = load_dev_queue().tasks[0]
    assert stored.status is QueueItemStatus.PENDING
    assert stored.session_id is None
    assert stored.spawn_error_count == 1


def test_preflight_failure_persist_error_completes_session_and_reraises(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The synchronous pre-flight branch keeps its own guard-and-re-raise.

    Unlike the backgrounded path, dispatch is still on this call stack here,
    so re-raising is correct: dispatch's own handler reverts the claimed task.
    The session must still be driven out of ACTIVE first.
    """
    worktree = make_git_repo("wt-codex-preflight-boom")
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = _sync_codex_executor(config, FakeCodexRunner(returncode=0))
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-pf", client="test", stage=Stage.PLAN)

    calls: list[dict[str, object]] = []

    def _door(**kwargs: object) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            msg = "door boom"
            raise OSError(msg)

    with (
        patch("cw.executor._complete_session_via_door", _door),
        pytest.raises(OSError, match="door boom"),
    ):
        # Stage.PLAN trips the CODEX_REVIEW_ONLY pre-flight guard, so this
        # never reaches the background path at all.
        executor.spawn(stage=Stage.PLAN, task=task, worktree=worktree, client=client)

    # Second call is the guarded blocked-result write from the except branch.
    assert len(calls) == 2
    assert calls[1]["guard_already_completed"] is True
    recovery = AutoDevResult.model_validate(calls[1]["payload"])
    assert recovery.status == "blocked"
    assert recovery.blocker is not None
    assert recovery.blocker.reason == UNEXPECTED_ERROR


def test_spawn_stamps_session_id_before_backgrounding(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """R1: the RUNNING row carries session_id by the time the worker starts.

    Without this, a crash between spawn() returning and dispatch's own
    post-spawn stamp would leave a live codex session with no queue row
    pointing at it — the failure-observability hole #1727 must not open.
    """
    worktree = make_git_repo("wt-codex-stamp")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-stamp", client="test", stage=Stage.REVIEW)
    add_ticket(
        TicketTask(
            ticket_id="T-stamp",
            client="test",
            stage=Stage.REVIEW,
            status=QueueItemStatus.RUNNING,
        )
    )

    seen: dict[str, object] = {}

    def _capture_background(fn: Callable[[], None]) -> None:
        seen["stamped_at_handoff"] = load_dev_queue().tasks[0].session_id
        del fn  # deliberately never run: proves spawn() returns without it

    executor = CodexExecutor(
        config=config, runner=runner, background=_capture_background
    )
    with patch("cw.executor.shutil.which", return_value="/usr/bin/codex"):
        sid = executor.spawn(
            stage=Stage.REVIEW, task=task, worktree=worktree, client=client
        )

    assert seen["stamped_at_handoff"] == sid
    # spawn() returned without the review having run at all.
    assert len(runner.calls) == 0


def test_spawn_returns_before_background_work_completes(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """The ticket's core claim: spawn() does not wait on the review (#1727).

    Uses the real ``_default_background`` daemon thread and blocks the review
    on an Event that is only released after spawn() has already returned.
    """
    worktree = make_git_repo("wt-codex-async")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = CodexExecutor(config=config, runner=runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-async", client="test", stage=Stage.REVIEW)

    entered = threading.Event()
    release = threading.Event()
    blocked = make_blocked(
        ticket_id="T-async",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )

    def _blocking_review(**_kwargs: object) -> tuple[AutoDevResult, None]:
        entered.set()
        release.wait(timeout=10.0)
        return blocked, None

    try:
        with (
            patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
            patch("cw.codex_background.run_review_with_fix_loop", _blocking_review),
        ):
            executor.spawn(
                stage=Stage.REVIEW, task=task, worktree=worktree, client=client
            )

            # spawn() already returned while the review is still blocked.
            assert entered.wait(timeout=5.0)
            assert load_state().sessions[0].status is SessionStatus.ACTIVE
            # R7(b): the in-flight thread is visible to the shutdown drain.
            with codex_background._outstanding_lock:
                assert len(codex_background._outstanding) == 1

            release.set()
            assert join_outstanding_codex_threads(timeout_seconds=5.0) == 0

        assert load_state().sessions[0].status is SessionStatus.COMPLETED
        with codex_background._outstanding_lock:
            assert codex_background._outstanding == []
    finally:
        release.set()
        join_outstanding_codex_threads(timeout_seconds=5.0)


def test_spawn_threads_real_session_id_into_run_review(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """CodexExecutor.spawn passes the real cw session id (its own sid, not a
    fresh uuid) into run_review so diagnostics land under the right dir."""
    worktree = make_git_repo("wt-codex-sid-thread")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND)
    executor = _sync_codex_executor(config, runner)
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
        patch("cw.codex_background.run_review_with_fix_loop", _spy_run_review),
    ):
        sid = executor.spawn(
            stage=Stage.REVIEW, task=task, worktree=worktree, client=client
        )

    assert captured["session_id"] == sid


def test_spawn_threads_reasoning_effort_from_resolved_config(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """#1711: CodexExecutor.spawn reads self._config.reasoning_effort and hands
    it to run_review_with_fix_loop — it is not re-derived downstream."""
    worktree = make_git_repo("wt-codex-effort-thread")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(backend=CODEX_BACKEND, reasoning_effort="medium")
    executor = _sync_codex_executor(config, runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-effort", client="test", stage=Stage.REVIEW)

    blocked = make_blocked(
        ticket_id="T-effort",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )
    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch(
            "cw.codex_background.run_review_with_fix_loop", return_value=(blocked, None)
        ) as fix_loop_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    fix_loop_mock.assert_called_once()
    assert fix_loop_mock.call_args.kwargs["reasoning_effort"] == "medium"


def test_spawn_threads_default_reasoning_effort(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    """A config that never mentions reasoning_effort still pins the field
    default — "high" is the resolved value, not an omission."""
    worktree = make_git_repo("wt-codex-effort-default")
    runner = FakeCodexRunner(returncode=0)
    executor = _sync_codex_executor(StageExecutorConfig(backend=CODEX_BACKEND), runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-effort-d", client="test", stage=Stage.REVIEW)

    blocked = make_blocked(
        ticket_id="T-effort-d",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )
    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch(
            "cw.codex_background.run_review_with_fix_loop", return_value=(blocked, None)
        ) as fix_loop_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert fix_loop_mock.call_args.kwargs["reasoning_effort"] == "high"


def test_spawn_threads_fix_lean_profile_mode(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    worktree = make_git_repo("wt-codex-fix-profile-thread")
    runner = FakeCodexRunner(returncode=0)
    config = StageExecutorConfig(
        backend=CODEX_BACKEND, codex_fix_lean_profile_mode="shadow"
    )
    executor = _sync_codex_executor(config, runner)
    client = ClientConfig(name="test", workspace_path=worktree, default_branch="main")
    task = TicketTask(ticket_id="T-fix-profile", client="test", stage=Stage.REVIEW)
    blocked = make_blocked(
        ticket_id="T-fix-profile",
        worktree=worktree,
        reason=CODEX_REVIEW_UNPARSEABLE,
        stage_reached="stage3_review",
    )

    with (
        patch("cw.executor.shutil.which", return_value="/usr/bin/codex"),
        patch(
            "cw.codex_background.run_review_with_fix_loop", return_value=(blocked, None)
        ) as fix_loop_mock,
    ):
        executor.spawn(stage=Stage.REVIEW, task=task, worktree=worktree, client=client)

    assert fix_loop_mock.call_args.kwargs["fix_lean_profile_mode"] == "shadow"


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
