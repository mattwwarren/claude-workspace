"""Tests for ``cw.review_engine.validate``, the pure engine-round core (R1, #3216).

The claude-native adapter (``record.py``) is pinned unchanged by
``tests/test_review_engine_record.py``; this file pins the core both lanes share.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.review_engine.constants import ENGINE_ROLE, EngineRoundFailure
from cw.review_engine.validate import (
    SummaryFailure,
    ValidatedSummary,
    engine_precheck,
    load_summary,
    summary_failure,
    validate_engine_summary,
)
from cw.review_engine_summary_io import load_review_engine_summary
from tests._review_engine_helpers import (
    EngineRepo,
    engine_finding,
    make_engine_repo,
    make_summary,
    write_summary,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SKILL = "repo-review"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo)


def _summary(tmp_path: Path, head: str, **overrides: object) -> Path:
    findings = overrides.pop("findings", None)
    assert findings is None or isinstance(findings, list)
    return write_summary(
        tmp_path / "round", make_summary(findings, head_sha=head, **overrides)
    )


class TestPrecheck:
    def test_clean_tree_at_head_passes(self, repo: EngineRepo) -> None:
        assert engine_precheck(repo.worktree, repo.head) is None

    def test_head_mismatch(self, repo: EngineRepo) -> None:
        failure = engine_precheck(repo.worktree, "b" * 40)
        assert failure is not None
        assert failure.reason is EngineRoundFailure.ENGINE_HEAD_MISMATCH
        assert failure.detail == (
            f"engine_head_mismatch: summary=- expected={'b' * 12} head={repo.head[:12]}"
        )

    def test_dirty_tree(self, repo: EngineRepo) -> None:
        tracked = repo.worktree / "src" / "a.py"
        tracked.write_text("x = 2\n", encoding="utf-8")
        failure = engine_precheck(repo.worktree, repo.head)
        assert failure is not None
        assert failure.reason is EngineRoundFailure.ENGINE_WORKTREE_MUTATED


class TestLoadSummary:
    def test_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "absent.json"
        failure = load_summary(path)
        assert failure == SummaryFailure(
            EngineRoundFailure.ENGINE_NO_SUMMARY, f"engine_no_summary: {path}"
        )

    def test_invalid_file(self, tmp_path: Path) -> None:
        path = write_summary(tmp_path, b"{not json")
        failure = load_summary(path)
        assert isinstance(failure, SummaryFailure)
        assert failure.reason is EngineRoundFailure.ENGINE_SUMMARY_INVALID
        assert failure.detail.startswith("engine_summary_invalid: not_json at ")

    def test_valid_file(self, tmp_path: Path) -> None:
        path = _summary(tmp_path, "a" * 40)
        assert load_summary(path) == load_review_engine_summary(path)


class TestSummaryFailure:
    HEAD = "c" * 40

    def test_the_failure_reason_defaults_to_none(self) -> None:
        """#3446: the 2-arg form is unchanged; the 3-arg form carries a reason."""
        reason = EngineRoundFailure.ENGINE_AGGREGATION_FAILED
        plain = SummaryFailure(reason, "d")
        typed = SummaryFailure(reason, "d", failure_reason="codex_timeout")

        assert plain.failure_reason is None
        assert plain == SummaryFailure(reason, "d", failure_reason=None)
        assert typed.failure_reason == "codex_timeout"
        assert typed != plain

    def _loaded(self, tmp_path: Path, **overrides: object) -> object:
        return load_review_engine_summary(_summary(tmp_path, self.HEAD, **overrides))

    def test_head_mismatch(self, tmp_path: Path) -> None:
        summary = load_review_engine_summary(_summary(tmp_path, "d" * 40))
        failure = summary_failure(summary, self.HEAD)
        assert failure is not None
        assert failure.reason is EngineRoundFailure.ENGINE_HEAD_MISMATCH
        assert "summary=dddddddddddd" in failure.detail

    @pytest.mark.parametrize("coverage", ["PARTIAL", "FAILED"])
    def test_incomplete_coverage(self, tmp_path: Path, coverage: str) -> None:
        summary = load_review_engine_summary(
            _summary(tmp_path, self.HEAD, coverage=coverage)
        )
        failure = summary_failure(summary, self.HEAD)
        assert failure == SummaryFailure(
            EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE,
            f"engine_coverage_incomplete: coverage={coverage}",
        )

    def test_fail_without_a_gating_finding(self, tmp_path: Path) -> None:
        summary = load_review_engine_summary(
            _summary(
                tmp_path, self.HEAD, findings=[engine_finding("NIT")], verdict="FAIL"
            )
        )
        failure = summary_failure(summary, self.HEAD)
        assert failure is not None
        assert failure.reason is EngineRoundFailure.ENGINE_SUMMARY_INVALID
        assert "fail_without_gating_finding" in failure.detail

    def test_good_summary(self, tmp_path: Path) -> None:
        summary = load_review_engine_summary(_summary(tmp_path, self.HEAD))
        assert summary_failure(summary, self.HEAD) is None


class TestValidateEngineSummary:
    HEAD = "e" * 40

    def test_success_maps_the_document(self, tmp_path: Path) -> None:
        findings = [engine_finding("MUST_FIX"), engine_finding("NIT", rule_id="a.b")]
        path = _summary(tmp_path, self.HEAD, findings=findings)
        outcome = validate_engine_summary(path, expect_head=self.HEAD, skill=SKILL)
        assert isinstance(outcome, ValidatedSummary)
        assert outcome.document.reviewer_role == ENGINE_ROLE
        assert len(outcome.document.findings) == 2
        assert outcome.summary.verdict == "FAIL"

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        outcome = validate_engine_summary(
            tmp_path / "none.json", expect_head=self.HEAD, skill=SKILL
        )
        assert isinstance(outcome, SummaryFailure)
        assert outcome.reason is EngineRoundFailure.ENGINE_NO_SUMMARY

    def test_summary_check_fails(self, tmp_path: Path) -> None:
        path = _summary(tmp_path, self.HEAD, coverage="PARTIAL")
        outcome = validate_engine_summary(path, expect_head=self.HEAD, skill=SKILL)
        assert isinstance(outcome, SummaryFailure)
        assert outcome.reason is EngineRoundFailure.ENGINE_COVERAGE_INCOMPLETE
