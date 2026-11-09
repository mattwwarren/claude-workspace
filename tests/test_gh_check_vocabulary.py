"""Tests for the shared GitHub check-status vocabulary (#2958).

``cw.gh_check_vocabulary`` is the single home for the CheckRun / StatusContext
vocabulary that adoption intake (fail-closed allow-lists) and pr_hydrate
(lenient classification subsets) both consume.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cw.gh_check_vocabulary import (
    CHECK_RUN_CONCLUSIONS,
    CHECK_RUN_STATUSES,
    FAILED_CHECKRUN_CONCLUSIONS,
    FAILED_STATUS_CONTEXT_STATES,
    INFRA_ONLY_CHECKRUN_CONCLUSIONS,
    PENDING_CHECKRUN_STATUSES,
    STATUS_CONTEXT_STATES,
    CheckRunConclusion,
    CheckRunStatus,
    StatusContextState,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REVIEW_MONITOR_ATTENTION = (
    _REPO_ROOT / ".claude" / "scripts" / "review_monitor_lib" / "attention.py"
)


class TestBehaviourPin:
    """Each set equals the literal members it replaced (#2958 behaviour-neutral)."""

    def test_check_run_statuses_match_adopt_allow_list(self) -> None:
        assert (
            frozenset(
                {
                    "QUEUED",
                    "IN_PROGRESS",
                    "COMPLETED",
                    "PENDING",
                    "REQUESTED",
                    "WAITING",
                }
            )
            == CHECK_RUN_STATUSES
        )

    def test_check_run_conclusions_match_adopt_allow_list(self) -> None:
        assert (
            frozenset(
                {
                    "ACTION_REQUIRED",
                    "CANCELLED",
                    "FAILURE",
                    "NEUTRAL",
                    "SKIPPED",
                    "STALE",
                    "STARTUP_FAILURE",
                    "SUCCESS",
                    "TIMED_OUT",
                }
            )
            == CHECK_RUN_CONCLUSIONS
        )

    def test_status_context_states_match_adopt_allow_list(self) -> None:
        assert (
            frozenset({"ERROR", "EXPECTED", "FAILURE", "PENDING", "SUCCESS"})
            == STATUS_CONTEXT_STATES
        )

    def test_failed_checkrun_conclusions_match_attention_subset(self) -> None:
        assert (
            frozenset(
                {
                    "FAILURE",
                    "TIMED_OUT",
                    "CANCELLED",
                    "ACTION_REQUIRED",
                    "STALE",
                    "STARTUP_FAILURE",
                }
            )
            == FAILED_CHECKRUN_CONCLUSIONS
        )

    def test_infra_only_checkrun_conclusions_match_attention_subset(self) -> None:
        assert (
            frozenset({"CANCELLED", "STARTUP_FAILURE"})
            == INFRA_ONLY_CHECKRUN_CONCLUSIONS
        )

    def test_pending_checkrun_statuses_match_attention_subset(self) -> None:
        assert (
            frozenset({"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"})
            == PENDING_CHECKRUN_STATUSES
        )

    def test_failed_status_context_states_match_summarize_literal(self) -> None:
        # Replaces the literal ("FAILURE", "ERROR") in summarize_status_checks.
        assert frozenset({"FAILURE", "ERROR"}) == FAILED_STATUS_CONTEXT_STATES


class TestSubsetInvariants:
    def test_failed_conclusions_are_known_conclusions(self) -> None:
        assert FAILED_CHECKRUN_CONCLUSIONS <= CHECK_RUN_CONCLUSIONS

    def test_infra_only_conclusions_are_failed_conclusions(self) -> None:
        assert INFRA_ONLY_CHECKRUN_CONCLUSIONS <= FAILED_CHECKRUN_CONCLUSIONS

    def test_pending_statuses_are_known_statuses(self) -> None:
        assert PENDING_CHECKRUN_STATUSES <= CHECK_RUN_STATUSES

    def test_completed_is_known_but_not_pending(self) -> None:
        assert CheckRunStatus.COMPLETED in CHECK_RUN_STATUSES
        assert CheckRunStatus.COMPLETED not in PENDING_CHECKRUN_STATUSES

    def test_failed_status_context_states_are_known_states(self) -> None:
        assert FAILED_STATUS_CONTEXT_STATES <= STATUS_CONTEXT_STATES

    def test_status_context_pending_is_known_and_pending(self) -> None:
        assert StatusContextState.PENDING in STATUS_CONTEXT_STATES
        assert StatusContextState.PENDING not in FAILED_STATUS_CONTEXT_STATES


class TestEnumHygiene:
    def test_check_run_status_members_name_equals_value(self) -> None:
        for member in CheckRunStatus:
            assert member.name == member.value
            assert member.value in CHECK_RUN_STATUSES

    def test_check_run_conclusion_members_name_equals_value(self) -> None:
        for member in CheckRunConclusion:
            assert member.name == member.value
            assert member.value in CHECK_RUN_CONCLUSIONS

    def test_status_context_state_members_name_equals_value(self) -> None:
        for member in StatusContextState:
            assert member.name == member.value
            assert member.value in STATUS_CONTEXT_STATES

    def test_enums_compare_equal_to_plain_strings(self) -> None:
        # Hash and equality against plain str are what the set membership
        # checks in adoption and hydration rely on.
        assert "COMPLETED" in CHECK_RUN_STATUSES
        assert CheckRunStatus.COMPLETED == "COMPLETED"
        assert hash(CheckRunStatus.COMPLETED) == hash("COMPLETED")


class TestReviewMonitorScriptDrift:
    """The review-monitor script keeps a hand copy it cannot import ``cw`` to share.

    AST-extract the two frozensets it hand-duplicates and pin them to the
    shared sets, so a one-sided edit fails here.
    """

    @staticmethod
    def _script_frozenset(name: str) -> frozenset[str]:
        tree = ast.parse(
            _REVIEW_MONITOR_ATTENTION.read_text(encoding="utf-8"),
            filename=str(_REVIEW_MONITOR_ATTENTION),
        )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "frozenset"
                and len(node.value.args) == 1
            ):
                literal = ast.literal_eval(node.value.args[0])
                assert isinstance(literal, set | frozenset | list | tuple)
                return frozenset(str(item) for item in literal)
        message = f"{name} frozenset not found in review-monitor script"
        raise AssertionError(message)

    def test_failed_conclusions_copy_matches_shared_set(self) -> None:
        copy = self._script_frozenset("_FAILED_CHECKRUN_CONCLUSIONS")
        assert copy
        assert copy == FAILED_CHECKRUN_CONCLUSIONS

    def test_pending_statuses_copy_matches_shared_set(self) -> None:
        copy = self._script_frozenset("_PENDING_CHECKRUN_STATUSES")
        assert copy
        assert copy == PENDING_CHECKRUN_STATUSES
