"""The codex clean-confirm gate and its feedback reader key on the row's base (#3840).

RFC 0020 S3b: the confirm gate's fork point (the ledger's identity key and
diff base), the pinned-base verification and the confirm review's merge target
all come from ``resolve_base_branch(client, task)``. ``default_branch`` passed
to the confirm review stays the client default: it is the trust root the
review configuration is read from. A null-base row resolves to the client
default, so its behavior is unchanged. The repo has ``origin/epic/x`` and
``origin/main`` diverged, with the feature branch cut from ``epic/x``, so the
two merge-bases are different commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cw import codex_clean_confirm
from cw.claude_review import ClaudeRunResult
from cw.codex_clean_confirm import apply_clean_confirm
from cw.codex_confirm_feedback import load_confirm_feedback
from cw.codex_review import synthesize_codex_review_result
from cw.models import ClientConfig
from cw.models.client import ReviewConfig
from tests._claude_review_helpers import RoleAwareClaudeRunner, review_envelope
from tests._codex_review_helpers import _doc_json
from tests._review_convergence_helpers import (
    consolidated_verdict,
    dirty_confirm_ledger,
    live_merge_base,
)
from tests._shared.base_branch_builders import (
    ROW_BASE,
    TRUST_ROOT,
    base_row,
    diverged_base_worktree,
)
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.review_builders import _make_diff, _make_reviewer_doc

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask


@pytest.fixture(autouse=True)
def _claude_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: "/x/claude")


@pytest.fixture
def wt(make_git_repo: Callable[[str], Path]) -> Path:
    """A feature branch cut from ``origin/<ROW_BASE>``, diverged from main."""
    repo = diverged_base_worktree(make_git_repo, "clean-confirm-based")
    commit_tracked_file(repo, "feature.py")
    git_in(repo, "push", "origin", "feature")
    return repo


def _client(wt: Path) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=wt,
        default_branch=TRUST_ROOT,
        review=ReviewConfig.model_validate(
            {"convergence": "clean", "confirm_backend": None}
        ),
    )


def _row(base_branch: str | None) -> TicketTask:
    return base_row(base_branch, ticket_id="T-1", client="test")


def _clean_role(role: str) -> ClaudeRunResult:
    return ClaudeRunResult(0, review_envelope(_doc_json(role=role)), "")


def _head(wt: Path) -> str:
    return git_in(wt, "rev-parse", "HEAD")


class _Seen:
    """What the gate handed to the diff-base seam and to the confirm review."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.resolve: list[str] = []
        self.verify: list[str] = []
        self.review: list[dict[str, Any]] = []
        real_resolve = codex_clean_confirm.resolve_diff_base
        real_verify = codex_clean_confirm.verify_diff_base
        real_review = codex_clean_confirm.run_claude_review

        def _resolve(worktree: Path, branch: str, **kwargs: Any) -> str:
            self.resolve.append(branch)
            return real_resolve(worktree, branch, **kwargs)

        def _verify(worktree: Path, pin: str | None, branch: str) -> str:
            self.verify.append(branch)
            return real_verify(worktree, pin, branch)

        def _review(**kwargs: Any) -> object:
            self.review.append(kwargs)
            return real_review(**kwargs)

        monkeypatch.setattr("cw.codex_clean_confirm.resolve_diff_base", _resolve)
        monkeypatch.setattr("cw.codex_clean_confirm.verify_diff_base", _verify)
        monkeypatch.setattr("cw.codex_clean_confirm.run_claude_review", _review)


def _clean_codex_round(wt: Path, task: TicketTask, base_branch: str) -> tuple[Any, Any]:
    result, verdict = synthesize_codex_review_result(
        task=task,
        worktree=wt,
        documents=[_make_reviewer_doc(reviewer_role="Code Quality Reviewer")],
        failures=[],
        diff=_make_diff("x = 1", files={"feature.py": [1]}),
        reviewed_sha=_head(wt),
        session_id="s-codex",
        default_branch=base_branch,
        fix_loop_enabled=True,
    )
    assert result.blocker is None
    assert verdict is not None
    return result, verdict


class TestConfirmGate:
    @pytest.mark.parametrize(
        ("row_base", "expected"),
        [(ROW_BASE, ROW_BASE), (None, TRUST_ROOT)],
        ids=["base-row", "null-row"],
    )
    def test_fork_point_pin_and_confirm_review_follow_the_row_base(
        self,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        row_base: str | None,
        expected: str,
    ) -> None:
        # Precondition: the two merge-bases are different commits.
        assert live_merge_base(wt, ROW_BASE) != live_merge_base(wt, TRUST_ROOT)
        task = _row(row_base)
        seen = _Seen(monkeypatch)
        result, verdict = _clean_codex_round(wt, task, expected)

        outcome = apply_clean_confirm(
            task=task,
            worktree=wt,
            client=_client(wt),
            sid="sid-base",
            runner=RoleAwareClaudeRunner(_clean_role),
            deadline=None,
            result=result,
            verdict=verdict,
        )

        merge_base = live_merge_base(wt, expected)
        assert outcome.result.review.converged is True
        # The fork point and the pinned-base verification read the row's base.
        assert seen.resolve
        assert set(seen.resolve) == {expected}
        assert seen.verify == [expected]
        # The confirm review diffs the row's base from the pin; its review
        # configuration stays on the client default.
        (call,) = seen.review
        assert call["base_branch"] == expected
        assert call["default_branch"] == TRUST_ROOT
        assert call["base_ref"] == merge_base


def _claude_must_fix(wt: Path) -> Any:
    return consolidated_verdict(
        accepted=[("MUST_FIX", "claude finding")], sha=_head(wt)
    )


class TestConfirmFeedbackFork:
    @staticmethod
    def _load(wt: Path, row_base: str | None) -> object:
        return load_confirm_feedback(
            task=_row(row_base),
            worktree=wt,
            client=_client(wt),
            fix_loop_enabled=True,
        )

    def test_a_base_row_reads_the_ledger_keyed_on_the_base_merge_base(
        self, wt: Path
    ) -> None:
        dirty_confirm_ledger(
            wt, _claude_must_fix(wt), fork_point=live_merge_base(wt, ROW_BASE)
        )

        assert self._load(wt, ROW_BASE) is not None
        # A null row resolves to main: a different fork point, so no feedback.
        assert self._load(wt, None) is None

    def test_a_null_row_reads_the_ledger_keyed_on_the_default_merge_base(
        self, wt: Path
    ) -> None:
        dirty_confirm_ledger(
            wt, _claude_must_fix(wt), fork_point=live_merge_base(wt, TRUST_ROOT)
        )

        assert self._load(wt, None) is not None
        assert self._load(wt, ROW_BASE) is None
