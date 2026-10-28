"""Tests for ``cw.claude_review.core.run_claude_review`` (#2791).

A real feature-branch worktree, the real ``_prepare_review_pass`` and the real
synthesis; only the ``claude -p`` call is faked, answering each role with an
envelope built from ``tests/_claude_review_helpers``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.claude_review import (
    CLAUDE_BACKEND_DETAILS_PREFIX,
    ClaudeRunResult,
    run_claude_review,
)
from cw.codex_review import (
    BACKEND_DOWN_REASONS,
    CODEX_ERROR,
    CODEX_MODEL_CAPACITY,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_REVIEW_WORKTREE_MUTATED,
)
from cw.codex_review._context._agent_spec import _REVIEWER_ROLE_AGENT_FILES
from cw.gh import AGENT_COMMENT_MARKER
from cw.local_runner import TICKET_BODY_KEY, TICKET_TITLE_KEY
from cw.models import CONTEXT_JSON_RELATIVE_PATH
from tests._claude_review_helpers import (
    RoleAwareClaudeRunner as _RoleAwareRunner,
)
from tests._claude_review_helpers import error_envelope, review_envelope
from tests._codex_review_helpers import _doc_json, _finding_payload, _task, _write
from tests.conftest import git_in
from tests.test_codex_review_context import _disposition_ledger
from tests.test_review_convergence_docs import CONFIRM_PROMPT_PAYLOAD

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.claude_review import ClaudeReviewRound


def _clean(role: str) -> ClaudeRunResult:
    return ClaudeRunResult(
        returncode=0, stdout=review_envelope(_doc_json(role=role)), stderr=""
    )


def _must_fix(role: str) -> ClaudeRunResult:
    finding = _finding_payload(
        severity="MUST_FIX",
        file="mod.py",
        line_start=1,
        line_end=1,
        summary="broken function",
        evidence="def broken():",
    )
    return ClaudeRunResult(
        returncode=0,
        stdout=review_envelope(_doc_json(role=role, findings=[finding])),
        stderr="",
    )


def _backend_error(_role: str) -> ClaudeRunResult:
    return ClaudeRunResult(returncode=0, stdout=error_envelope(), stderr="")


def _timeout(_role: str) -> ClaudeRunResult:
    return ClaudeRunResult(returncode=-1, stdout="", stderr="", timed_out=True)


@pytest.fixture
def wt(make_git_repo: Callable[..., Path]) -> Path:
    repo = make_git_repo("claude-core")
    git_in(repo, "checkout", "-b", "feature")
    (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
    git_in(repo, "add", "mod.py")
    git_in(repo, "commit", "-m", "add mod.py")
    return repo


def _review(
    wt: Path,
    runner: _RoleAwareRunner,
    *,
    base_ref: str | None = None,
    budget: int | None = None,
) -> ClaudeReviewRound:
    return run_claude_review(
        runner=runner,
        task=_task(),
        worktree=wt,
        default_branch="main",
        base_ref=base_ref,
        model=None,
        wall_clock_budget_seconds=budget,
        session_id="confirm-T-1",
    )


class TestRunClaudeReview:
    def test_a_clean_round(self, wt: Path) -> None:
        runner = _RoleAwareRunner(_clean)
        round_ = _review(wt, runner)
        assert round_.backend_down is False
        assert round_.result.blocker is None
        assert round_.result.status == "stage_complete"
        assert round_.verdict is not None
        assert round_.verdict.reviewed_sha == git_in(wt, "rev-parse", "HEAD")
        assert runner.roles

    def test_must_fix_is_labeled_claude(self, wt: Path) -> None:
        round_ = _review(wt, _RoleAwareRunner(_must_fix))
        blocker = round_.result.blocker
        assert blocker is not None
        assert blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert blocker.details.startswith(
            CLAUDE_BACKEND_DETAILS_PREFIX + "## Claude Review Verdict"
        )
        assert "Codex Review Verdict" not in blocker.details
        assert round_.verdict is not None
        assert round_.verdict.blocking is True
        assert round_.backend_down is False

    def test_every_role_backend_error_is_backend_down(self, wt: Path) -> None:
        round_ = _review(wt, _RoleAwareRunner(_backend_error))
        assert round_.backend_down is True
        assert round_.verdict is None
        blocker = round_.result.blocker
        assert blocker is not None
        assert blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert blocker.details.startswith(CLAUDE_BACKEND_DETAILS_PREFIX)

    def test_every_role_timing_out_is_not_backend_down(self, wt: Path) -> None:
        round_ = _review(wt, _RoleAwareRunner(_timeout))
        assert round_.backend_down is False
        assert round_.verdict is None
        assert round_.result.blocker is not None
        assert round_.result.blocker.retry_eligible is True

    def test_a_mixed_backend_error_and_timeout_round_is_not_backend_down(
        self, wt: Path
    ) -> None:
        answered: list[str] = []

        def _first_errors_rest_time_out(role: str) -> ClaudeRunResult:
            answered.append(role)
            return _backend_error(role) if len(answered) == 1 else _timeout(role)

        runner = _RoleAwareRunner(_first_errors_rest_time_out)
        round_ = _review(wt, runner)
        assert len(runner.roles) > 1
        assert round_.backend_down is False
        assert round_.verdict is None

    def test_the_backend_down_reason_set_is_the_two_backend_side_reasons(
        self,
    ) -> None:
        assert {CODEX_ERROR, CODEX_MODEL_CAPACITY} == BACKEND_DOWN_REASONS

    def test_an_exhausted_budget_is_not_backend_down(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.claude_review._roles.time.monotonic", lambda: 1_000_000.0
        )
        runner = _RoleAwareRunner(_clean)
        round_ = _review(wt, runner, budget=1)
        assert runner.roles == []
        assert round_.backend_down is False
        blocker = round_.result.blocker
        assert blocker is not None
        assert blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert blocker.retry_eligible is True

    def test_the_codex_capability_probe_never_runs(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_probe(**_kwargs: object) -> object:
            msg = "the codex capability probe must not run for a Claude round"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.codex_review._context.core._probe_filesystem_capability", _no_probe
        )
        runner = _RoleAwareRunner(_clean)
        round_ = _review(wt, runner)
        assert round_.verdict is not None
        assert len(runner.roles) == len(set(runner.roles))
        assert all("CW_CODEX_FS_PROBE" not in p for p in runner.prompts)

    def test_base_ref_is_the_diff_base_and_default_branch_the_scope_base(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fork = git_in(wt, "rev-parse", "HEAD")
        (wt / "later.py").write_text("x = 1\n", encoding="utf-8")
        git_in(wt, "add", "later.py")
        git_in(wt, "commit", "-m", "add later.py")
        scope_bases: list[str] = []

        def _spy_scope(_worktree: Path, branch: str) -> None:
            scope_bases.append(branch)

        monkeypatch.setattr(
            "cw.codex_review._verdict._synthesis.compute_branch_diff_scope",
            _spy_scope,
        )
        runner = _RoleAwareRunner(_clean)
        _review(wt, runner, base_ref=fork)
        assert runner.prompts
        assert all("later.py" in p for p in runner.prompts)
        assert all("mod.py" not in p for p in runner.prompts)
        assert scope_bases == ["main"]

    def test_a_mutating_role_parks_without_a_verdict(self, wt: Path) -> None:
        def _hook_writes(worktree: Path) -> None:
            (worktree / "hook.txt").write_text("x\n", encoding="utf-8")

        runner = _RoleAwareRunner(_clean, side_effect=_hook_writes)
        round_ = _review(wt, runner)
        assert len(runner.roles) == 1
        assert round_.verdict is None
        assert round_.backend_down is False
        blocker = round_.result.blocker
        assert blocker is not None
        assert blocker.reason == CODEX_REVIEW_WORKTREE_MUTATED
        assert blocker.retry_eligible is False
        assert blocker.details == (
            f"backend=claude; role={runner.roles[0]}; git status changed: ?? hook.txt"
        )
        # Left as found for the operator to inspect.
        assert (wt / "hook.txt").exists()


#: The ``CONFIRM_PROMPT_PAYLOAD`` items (the operator-facing egress disclosure),
#: each with the markers seeded for it below. The test rebuilds the constant from
#: these keys, so an item added to or reworded in the disclosure fails here until
#: this table and the prompt builder agree with it.
_PLAN_ITEM = "the approved plan (`.cw/plan.md`)"
_TICKET_ITEM = "the ticket title and body"
_COMMENTS_ITEM = (
    "the ticket's comments (every comment the pipeline did not post itself, "
    "fetched live from the tracker)"
)
_DISPOSITIONS_ITEM = "the finding dispositions an operator already settled"
_CONFIG_ITEM = (
    "the repo's review configuration (reviewer agent specs, "
    "`.claude/review-extras.md`, the `CLAUDE.md` quality gates and ruff settings)"
)
_PAYLOAD_ITEMS = (
    _PLAN_ITEM,
    _TICKET_ITEM,
    _COMMENTS_ITEM,
    _DISPOSITIONS_ITEM,
    _CONFIG_ITEM,
)
_PAYLOAD_MARKERS = {
    _PLAN_ITEM: ("PLAN-MARKER-7f3a",),
    _TICKET_ITEM: ("TICKET-TITLE-MARKER-7f3a", "TICKET-BODY-MARKER-7f3a"),
    _COMMENTS_ITEM: ("OPERATOR-COMMENT-MARKER-7f3a",),
    _DISPOSITIONS_ITEM: ("SETTLED-RATIONALE-MARKER-7f3a",),
    _CONFIG_ITEM: (
        "REVIEW-EXTRAS-MARKER-7f3a",
        "QUALITY-GATES-MARKER-7f3a",
        "RUFF-IGNORE-MARKER-ZZ9",
    ),
}
_PIPELINE_COMMENT_MARKER = "PIPELINE-COMMENT-MARKER-7f3a"


class TestConfirmPromptPayload:
    """#2791: ``CONFIRM_PROMPT_PAYLOAD`` matches what the Claude prompt carries."""

    def test_the_disclosed_items_are_exactly_what_the_role_prompts_carry(
        self, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (
            ", ".join(_PAYLOAD_ITEMS[:-1]) + ", and " + _PAYLOAD_ITEMS[-1]
            == CONFIRM_PROMPT_PAYLOAD
        )
        assert set(_PAYLOAD_MARKERS) == set(_PAYLOAD_ITEMS)
        _write(wt / ".cw" / "plan.md", "# Plan\nPLAN-MARKER-7f3a\n")
        _write(
            wt / CONTEXT_JSON_RELATIVE_PATH,
            json.dumps(
                {
                    TICKET_TITLE_KEY: "TICKET-TITLE-MARKER-7f3a",
                    TICKET_BODY_KEY: "TICKET-BODY-MARKER-7f3a",
                }
            ),
        )
        _write(
            wt / ".claude" / "project-config.yaml",
            "tracking:\n  primary:\n    system: github-issues\n",
        )
        _write(wt / ".claude" / "review-extras.md", "REVIEW-EXTRAS-MARKER-7f3a\n")
        _write(wt / "CLAUDE.md", "## Quality Gates\nQUALITY-GATES-MARKER-7f3a\n")
        _write(
            wt / "pyproject.toml",
            '[tool.ruff.lint]\nignore = ["RUFF-IGNORE-MARKER-ZZ9"]\n',
        )
        for filename in _REVIEWER_ROLE_AGENT_FILES.values():
            _write(wt / ".claude" / "agents" / filename, f"AGENT-SPEC-{filename}\n")
        monkeypatch.setattr(
            "cw.codex_review._context.core.fetch_issue_comments",
            lambda *_a, **_kw: [
                {
                    "author": {"login": "operator"},
                    "body": "OPERATOR-COMMENT-MARKER-7f3a",
                },
                {
                    "author": {"login": "operator"},
                    "body": f"{_PIPELINE_COMMENT_MARKER}\n{AGENT_COMMENT_MARKER}",
                },
            ],
        )
        task = _task().model_copy(
            update={
                "finding_dispositions": _disposition_ledger(
                    rationale="SETTLED-RATIONALE-MARKER-7f3a"
                )
            }
        )
        runner = _RoleAwareRunner(_clean)
        run_claude_review(
            runner=runner,
            task=task,
            worktree=wt,
            default_branch="main",
            base_ref=None,
            model=None,
            wall_clock_budget_seconds=None,
            session_id="confirm-T-1",
        )
        assert runner.prompts
        for role, prompt in zip(runner.roles, runner.prompts, strict=True):
            for markers in _PAYLOAD_MARKERS.values():
                for marker in markers:
                    assert marker in prompt, (role, marker)
            assert f"AGENT-SPEC-{_REVIEWER_ROLE_AGENT_FILES[role]}" in prompt
            assert _PIPELINE_COMMENT_MARKER not in prompt
