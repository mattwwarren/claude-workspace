"""Evidence-gap holds carried across rounds by the engine (#3823).

A held gap is an ``operator_action`` finding stamped ``held="evidence_gap"``.
It is not new-accepted: a held-only round stops the ledger for the operator and
names the gap, a mixed round repairs the fix first, and the gap stays carried
until a release or a settle. Driven through the real engine, no mocks.
"""

from __future__ import annotations

from cw.review_convergence import (
    ConvergenceLedger,
    HeldEntry,
    RoundFinding,
    SettledEntry,
    UnreadFindings,
    outstanding_held,
)
from cw.review_convergence._findings import carried_held, classify_findings
from cw.review_convergence._identity import FindingIdentity, finding_identity
from tests._review_convergence_helpers import fresh_ledger, record, round_finding
from tests.test_review_convergence import _reconcile

GAP_RATIONALE = "run pytest in the sandbox to confirm the regression"
GAP_SUMMARY = "Could not run the test suite"


def held_gap() -> RoundFinding:
    """A MUST_FIX evidence-gap hold, as ``from_verdict`` produces it."""
    return RoundFinding(
        severity="MUST_FIX",
        file="N/A",
        summary=GAP_SUMMARY,
        outcome="operator_action",
        rationale=GAP_RATIONALE,
        held="evidence_gap",
    )


def gap_identity() -> FindingIdentity:
    return finding_identity("MUST_FIX", "N/A", GAP_SUMMARY)


def real_fix() -> RoundFinding:
    return round_finding("MUST_FIX", "Real bug", file="src/cw/foo.py")


class TestClassifyHeld:
    def test_held_goes_to_held_not_new_accepted(self) -> None:
        classified = classify_findings(fresh_ledger(), [held_gap()], 1)
        assert classified.held == [held_gap()]
        assert classified.new_operator == []
        assert classified.new_accepted == 0

    def test_contest_stays_operator_action(self) -> None:
        contest = round_finding("MUST_FIX", "Contested", outcome="operator_action")
        classified = classify_findings(fresh_ledger(), [contest], 1)
        assert classified.new_operator == [contest]
        assert classified.held == []

    def test_re_raised_as_fix_supersedes_prior_memory(self) -> None:
        prior = [HeldEntry(identity=gap_identity(), rationale=GAP_RATIONALE)]
        fix = round_finding("MUST_FIX", GAP_SUMMARY, file="N/A")
        classified = classify_findings(fresh_ledger(), [fix], 2)
        assert carried_held(prior, classified, {}) == []

    def test_settled_identity_prunes_prior_memory(self) -> None:
        prior = [HeldEntry(identity=gap_identity(), rationale=GAP_RATIONALE)]
        classified = classify_findings(fresh_ledger(), [], 2)
        ledger = fresh_ledger().model_copy(update={"held": prior})
        assert outstanding_held(ledger) == prior
        assert carried_held(prior, classified, {}) == prior
        settled_entry = SettledEntry(
            identity=gap_identity(),
            outcome="reject",
            rationale="operator settled",
            round=1,
            origin="operator",
        )
        assert carried_held(prior, classified, {gap_identity(): settled_entry}) == []


class TestHeldCarriedAcrossRounds:
    def test_held_only_round_stops_for_the_operator(self) -> None:
        ledger, report = record(fresh_ledger(), held_gap(), sha="sha-1")
        assert report.decision == "stop_operator_action"
        assert ledger.status == "stopped"
        assert ledger.stop_identities == [gap_identity()]
        assert ledger.held == [
            HeldEntry(identity=gap_identity(), rationale=GAP_RATIONALE)
        ]
        row = ledger.rounds[-1]
        assert row.clean is False
        assert row.new_accepted == 0
        assert row.new_held == 1
        assert ledger.last_clean_sha is None

    def test_mixed_round_repairs_first_and_keeps_the_gap(self) -> None:
        ledger, report = record(fresh_ledger(), held_gap(), real_fix(), sha="sha-1")
        assert report.decision == "continue"
        assert ledger.status == "in_progress"
        assert ledger.fix_cycles_used == 1
        assert len(ledger.fixed) == 1
        assert [entry.identity for entry in ledger.held] == [gap_identity()]

    def test_empty_following_round_still_stops(self) -> None:
        ledger, _ = record(fresh_ledger(), held_gap(), real_fix(), sha="sha-1")
        ledger, report = record(ledger, sha="sha-2")
        assert report.decision == "stop_operator_action"
        assert ledger.status == "stopped"
        assert ledger.last_clean_sha is None
        assert ledger.awaiting_confirm_sha is None

    def test_failed_role_on_held_only_round_does_not_raise(self) -> None:
        unread = UnreadFindings(
            rejected_must_fix=0,
            discarded_run_failures=0,
            failed_roles=("Data Safety Reviewer",),
        )
        ledger, report = record(fresh_ledger(), held_gap(), sha="sha-1", unread=unread)
        assert report.decision == "stop_operator_action"
        assert ledger.rounds[-1].clean is False


class TestHeldRelease:
    def test_same_tip_release_clears_the_held_memory(self) -> None:
        ledger, _ = record(fresh_ledger(), held_gap(), sha="sha-1")
        released = _reconcile(ledger, head="sha-1")
        assert released.ledger.status == "in_progress"
        assert released.ledger.held == []

    def test_in_progress_held_ledger_resumes_with_held_intact(self) -> None:
        ledger, _ = record(fresh_ledger(), held_gap(), real_fix(), sha="sha-1")
        resumed = _reconcile(ledger, head="sha-1")
        assert [entry.identity for entry in resumed.ledger.held] == [gap_identity()]


class TestLegacyAdditivity:
    def test_held_free_round_dumps_without_held_keys(self) -> None:
        ledger, _ = record(fresh_ledger(), real_fix(), sha="sha-1")
        dumped = ledger.model_dump(mode="json")
        assert "held" not in dumped
        assert all("new_held" not in row for row in dumped["rounds"])

    def test_held_ledger_round_trips(self) -> None:
        ledger, _ = record(fresh_ledger(), held_gap(), sha="sha-1")
        restored = ConvergenceLedger.model_validate(ledger.model_dump(mode="json"))
        assert restored == ledger
