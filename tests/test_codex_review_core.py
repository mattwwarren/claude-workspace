"""Tests for cw.codex_review.core — the ``run_review`` orchestration (#1236,
#1392)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_review import _prepare_review_pass, run_review
from tests._codex_review_helpers import (
    _finding_payload,
    _git,
    _ok_result,
    _SequencedRunner,
    _task,
)
from tests.conftest import _make_reviewer_doc

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_run_review_threads_session_id_to_run_codex_role(
    make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = make_git_repo("wt-run-review-thread")
    captured: dict[str, object] = {}

    def _spy_run_codex_role(**kwargs: object) -> tuple[object, object]:
        captured["session_id"] = kwargs["session_id"]
        return _make_reviewer_doc(), None

    monkeypatch.setattr("cw.codex_review._roles._run_codex_role", _spy_run_codex_role)
    run_review(
        runner=_SequencedRunner([]),
        task=_task(),
        worktree=worktree,
        default_branch="main",
        model=None,
        wall_clock_budget_seconds=None,
        session_id="sess-thread",
    )
    assert captured["session_id"] == "sess-thread"


# ---------------------------------------------------------------------------
# _prepare_review_pass extraction (#1392)
# ---------------------------------------------------------------------------


class TestPrepareReviewPass:
    def test_run_review_delegates_through_prepared_inputs(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # run_review's output is unchanged by the extraction: a clean per-role
        # pass over the prepared inputs still yields stage_complete, and a
        # real non-empty (non-blocking) finding survives diff-based validation
        # and is reflected in the consolidated verdict.
        repo = make_git_repo("wt-prepare-run")
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")

        prepared = _prepare_review_pass(_task(), repo, "main")
        results = [_ok_result() for _ in prepared.roles]
        results[0] = _ok_result(
            findings=[
                _finding_payload(
                    severity="SHOULD_FIX", file="mod.py", line_start=1, line_end=1
                )
            ]
        )
        runner = _SequencedRunner(results)
        result, verdict = run_review(
            runner=runner,
            task=_task(),
            worktree=repo,
            default_branch="main",
            model=None,
            wall_clock_budget_seconds=None,
            session_id="sess-prepare-run",
        )
        assert result.status == "stage_complete"
        assert verdict is not None
        assert verdict.blocking is False
        assert verdict.review.should_fix == 1
        assert len(runner.calls) == len(prepared.roles)
