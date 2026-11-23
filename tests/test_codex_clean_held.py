"""The ``review_operator_actionable`` park for a carried evidence gap (#3823)."""

from __future__ import annotations

from cw.codex_clean_held import HELD_BLOCKER_REASON, HELD_HEADLINE, held_stop_blocker
from cw.codex_review import STAGE3_REVIEW
from cw.review_convergence import ConvergenceLedger, SettledEntry
from tests._review_convergence_helpers import (
    GAP_RATIONALE,
    fresh_ledger,
    gap_identity,
    held_gap,
    record,
)


def _stopped_held_ledger() -> ConvergenceLedger:
    ledger, _ = record(fresh_ledger(), held_gap(), sha="sha-1")
    return ledger


class TestHeldStopBlocker:
    def test_reason_stage_and_retry(self) -> None:
        blocker = held_stop_blocker(_stopped_held_ledger())
        assert blocker.reason == HELD_BLOCKER_REASON == "review_operator_actionable"
        assert blocker.stage == STAGE3_REVIEW
        assert blocker.retry_eligible is False

    def test_details_name_each_gap_and_its_unrun_check(self) -> None:
        blocker = held_stop_blocker(_stopped_held_ledger())
        lines = blocker.details.splitlines()
        assert lines[0] == HELD_HEADLINE
        assert lines[1].startswith("- MUST_FIX N/A: ")
        assert f"(unrun check: {GAP_RATIONALE})" in lines[1]
        assert lines[-1].startswith("release: ")

    def test_settled_gap_is_not_named(self) -> None:
        ledger = _stopped_held_ledger()
        settled = SettledEntry(
            identity=gap_identity(),
            outcome="reject",
            rationale="ran the check by hand",
            round=1,
            origin="operator",
        )
        blocker = held_stop_blocker(ledger.model_copy(update={"settled": [settled]}))
        assert ledger.held
        assert GAP_RATIONALE not in blocker.details
        assert blocker.details.splitlines()[0] == HELD_HEADLINE
        assert not any(line.startswith("- ") for line in blocker.details.splitlines())

    def test_empty_held_still_yields_a_valid_blocker(self) -> None:
        blocker = held_stop_blocker(fresh_ledger())
        assert blocker.reason == HELD_BLOCKER_REASON
        assert blocker.details.splitlines()[0] == HELD_HEADLINE
