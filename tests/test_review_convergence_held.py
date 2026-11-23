"""Evidence-gap holds carried across rounds by the engine (#3823).

A held gap is an ``operator_action`` finding stamped ``held="evidence_gap"``.
It is not new-accepted: a held-only round stops the ledger for the operator and
names the gap, a mixed round repairs the fix first, and the gap stays carried
until a release that named it or a settle. Driven through the real engine, no
mocks.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.review_convergence import (
    ConvergenceError,
    ConvergenceLedger,
    HeldEntry,
    SettledEntry,
    SettleTarget,
    UnreadFindings,
    operator_settle,
    outstanding_held,
    override_confirm_round,
    record_delta_round,
)
from cw.review_convergence._findings import carried_held, classify_findings
from cw.review_convergence._identity import finding_identity
from tests._review_convergence_helpers import (
    GAP_RATIONALE,
    GAP_SUMMARY,
    NOW,
    ROSTER,
    confirmed_ledger,
    engine_pass_ledger,
    fresh_ledger,
    gap_identity,
    held_gap,
    old_ledger_json,
    operator_finding,
    real_fix,
    reconcile_ledger,
    record,
    round_finding,
)

if TYPE_CHECKING:
    from cw.review_convergence import (
        FindingIdentity,
        RecordableRoundKind,
        RoundFinding,
        RoundReport,
    )

_SHA = "sha-0"
_KINDS = ["loop", "confirm"]
_COVERAGE_RULE = "coverage.unrun-check"
_FINGERPRINT = "fp-coverage-gap"


def _coverage_gap() -> RoundFinding:
    """A fingerprinted ``coverage.*`` evidence gap: never deferrable (R13)."""
    return held_gap().model_copy(
        update={"fingerprint": _FINGERPRINT, "rule_id": _COVERAGE_RULE}
    )


def _coverage_gap_identity() -> FindingIdentity:
    return finding_identity("MUST_FIX", "N/A", GAP_SUMMARY, fingerprint=_FINGERPRINT)


def _open_ledger(kind: RecordableRoundKind) -> ConvergenceLedger:
    """A ledger whose next round is of *kind*: a fresh one, or one awaiting confirm."""
    if kind == "loop":
        return fresh_ledger()
    ledger = fresh_ledger(confirm="claude", loop_backend="codex")
    ledger, report = record(ledger, sha=_SHA, backend="codex")
    assert report.decision == "confirm_required"
    return ledger


def _round(
    ledger: ConvergenceLedger,
    kind: RecordableRoundKind,
    *findings: RoundFinding,
    sha: str,
    unread: UnreadFindings | None = None,
) -> tuple[ConvergenceLedger, RoundReport]:
    backend = ledger.confirm_backend if kind == "confirm" else ledger.loop_backend
    return record(ledger, *findings, sha=sha, kind=kind, backend=backend, unread=unread)


def _settle_gap(ledger: ConvergenceLedger) -> ConvergenceLedger:
    """*ledger* with the held gap settled, as ``converge operator-settle`` does."""
    identity = gap_identity()
    settled, report = operator_settle(
        ledger,
        [SettleTarget(severity="MUST_FIX", file=identity.file, summary=GAP_SUMMARY)],
        actor="operator",
        reason="ran the check by hand",
        now=NOW,
    )
    assert [entry.identity for entry in report.settled] == [identity]
    return settled


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

    def test_applying_settle_prunes_prior_memory(self) -> None:
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
        settled = {gap_identity(): settled_entry}
        assert carried_held(prior, classified, settled) == []
        settled_ledger = ledger.model_copy(update={"settled": [settled_entry]})
        assert outstanding_held(settled_ledger) == []

    def test_non_applying_settle_leaves_a_coverage_gap_outstanding(self) -> None:
        identity = _coverage_gap_identity()
        prior = [
            HeldEntry(
                identity=identity, rationale=GAP_RATIONALE, rule_id=_COVERAGE_RULE
            )
        ]
        classified = classify_findings(fresh_ledger(), [], 2)
        deferred = SettledEntry(
            identity=identity,
            outcome="defer",
            rationale="operator deferred",
            round=1,
            origin="operator",
        )
        assert carried_held(prior, classified, {identity: deferred}) == prior
        ledger = fresh_ledger().model_copy(
            update={"held": prior, "settled": [deferred]}
        )
        assert outstanding_held(ledger) == prior

    def test_applying_defer_discharges_a_non_coverage_engine_gap(self) -> None:
        identity = _coverage_gap_identity()
        prior = [HeldEntry(identity=identity, rationale=GAP_RATIONALE, rule_id="x.y")]
        classified = classify_findings(fresh_ledger(), [], 2)
        deferred = SettledEntry(
            identity=identity,
            outcome="defer",
            rationale="operator deferred",
            round=1,
            origin="operator",
        )
        assert carried_held(prior, classified, {identity: deferred}) == []


@pytest.mark.parametrize("kind", _KINDS)
class TestHeldCarriedAcrossRounds:
    def test_held_only_round_stops_for_the_operator(
        self, kind: RecordableRoundKind
    ) -> None:
        ledger, report = _round(_open_ledger(kind), kind, held_gap(), sha=_SHA)
        assert report.decision == "stop_operator_action"
        assert ledger.status == "stopped"
        assert ledger.stop_reason == "operator_action"
        assert ledger.stop_identities == [gap_identity()]
        assert ledger.held == [
            HeldEntry(identity=gap_identity(), rationale=GAP_RATIONALE)
        ]
        row = ledger.rounds[-1]
        assert row.kind == kind
        assert row.clean is False
        assert row.new_accepted == 0
        assert row.new_held == 1
        assert ledger.last_clean_sha is None

    def test_mixed_round_repairs_first_and_keeps_the_gap(
        self, kind: RecordableRoundKind
    ) -> None:
        ledger, report = _round(
            _open_ledger(kind), kind, held_gap(), real_fix(), sha=_SHA
        )
        assert report.decision == "continue"
        assert ledger.status == "in_progress"
        assert ledger.fix_cycles_used == 1
        assert len(ledger.fixed) == 1
        assert [entry.identity for entry in ledger.held] == [gap_identity()]

    def test_empty_following_round_still_stops(self, kind: RecordableRoundKind) -> None:
        ledger, _ = _round(_open_ledger(kind), kind, held_gap(), real_fix(), sha=_SHA)
        ledger, report = _round(ledger, "loop", sha="sha-1")
        assert report.decision == "stop_operator_action"
        assert ledger.status == "stopped"
        assert ledger.last_clean_sha is None
        assert ledger.awaiting_confirm_sha is None
        assert ledger.stop_identities == [gap_identity()]

    def test_settled_gap_lets_the_next_empty_round_converge(
        self, kind: RecordableRoundKind
    ) -> None:
        ledger, _ = _round(_open_ledger(kind), kind, held_gap(), real_fix(), sha=_SHA)
        ledger = _settle_gap(ledger)
        assert outstanding_held(ledger) == []
        ledger, report = _round(ledger, "loop", sha="sha-1")
        if kind == "loop":
            assert report.decision == "clean"
        else:
            assert report.decision == "confirm_required"
            assert ledger.status == "awaiting_confirm"
            ledger, report = _round(ledger, "confirm", sha="sha-1")
            assert report.decision == "clean"
        assert ledger.status == "converged"
        assert ledger.last_clean_sha == "sha-1"
        assert ledger.rounds[-1].clean is True

    def test_failed_role_on_held_only_round_does_not_raise(
        self, kind: RecordableRoundKind
    ) -> None:
        unread = UnreadFindings(
            rejected_must_fix=0,
            discarded_run_failures=0,
            failed_roles=("Data Safety Reviewer",),
        )
        ledger, report = _round(
            _open_ledger(kind), kind, held_gap(), sha=_SHA, unread=unread
        )
        assert report.decision == "stop_operator_action"
        assert ledger.rounds[-1].clean is False


class TestDeferOfANonDeferrableGap:
    """Operator item 4: a defer that does not apply cannot discharge a gap."""

    def test_defer_of_a_fingerprinted_coverage_gap_is_inert(self) -> None:
        ledger, report = record(fresh_ledger(), _coverage_gap(), real_fix(), sha="s-1")
        assert report.decision == "continue"
        ledger, settle = operator_settle(
            ledger,
            [
                SettleTarget(
                    severity="MUST_FIX",
                    file="N/A",
                    summary=GAP_SUMMARY,
                    fingerprint=_FINGERPRINT,
                )
            ],
            actor="operator",
            reason="accept it",
            now=NOW,
            outcome="defer",
        )
        assert [entry.identity for entry in settle.settled] == [
            _coverage_gap_identity()
        ]
        assert [entry.identity for entry in outstanding_held(ledger)] == [
            _coverage_gap_identity()
        ]
        ledger, follow = record(ledger, _coverage_gap(), sha="s-2")
        assert follow.decision == "stop_operator_action"
        assert ledger.status == "stopped"
        assert ledger.rounds[-1].clean is False
        assert ledger.last_clean_sha is None

    def test_a_quiet_round_after_the_inert_defer_still_stops(self) -> None:
        ledger, _ = record(fresh_ledger(), _coverage_gap(), real_fix(), sha="s-1")
        ledger, _ = operator_settle(
            ledger,
            [
                SettleTarget(
                    severity="MUST_FIX",
                    file="N/A",
                    summary=GAP_SUMMARY,
                    fingerprint=_FINGERPRINT,
                )
            ],
            actor="operator",
            reason="accept it",
            now=NOW,
            outcome="defer",
        )
        ledger, follow = record(ledger, sha="s-2")
        assert follow.decision == "stop_operator_action"
        assert ledger.rounds[-1].clean is False
        assert ledger.last_clean_sha is None


class TestHeldBesideOtherRoundTypes:
    def test_contest_plus_held_stops_on_the_contest_identities_only(self) -> None:
        contest = operator_finding("Contested")
        ledger, report = record(fresh_ledger(), contest, held_gap(), sha=_SHA)
        assert report.decision == "stop_operator_action"
        contest_identity = finding_identity("MUST_FIX", "N/A", "Contested")
        assert ledger.stop_identities == [contest_identity]
        assert [entry.identity for entry in ledger.held] == [gap_identity()]

    def test_park_route_confirm_round_keeps_the_held_memory(self) -> None:
        carried = [HeldEntry(identity=gap_identity(), rationale=GAP_RATIONALE)]
        ledger = engine_pass_ledger().model_copy(update={"held": carried})
        parked, report = record(
            ledger, round_finding("MUST_FIX"), sha="sha-0001", kind="confirm"
        )
        assert report.decision == "stop_confirm_must_fix"
        assert parked.held == carried

    def test_delta_round_over_a_converged_ledger_stops_on_a_held_gap(self) -> None:
        converged = confirmed_ledger(sha="S", confirm="codex")
        ledger, report = record_delta_round(
            converged,
            backend="codex",
            reviewed_sha="merge-1",
            findings=[held_gap()],
            roster=ROSTER,
            now=NOW,
        )
        assert report.decision == "stop_operator_action"
        assert ledger.status == "stopped"
        assert ledger.stop_identities == [gap_identity()]
        row = ledger.rounds[-1]
        assert (row.kind, row.clean, row.new_held) == ("delta", False, 1)
        assert ledger.last_clean_sha == "S"


class TestHeldRelease:
    """Operator item 6: a release clears only what the stop named."""

    def test_operator_action_release_clears_exactly_the_named_gaps(self) -> None:
        ledger, _ = record(fresh_ledger(), held_gap(), sha="sha-1")
        other = HeldEntry(
            identity=finding_identity("MUST_FIX", "N/A", "Another gap"),
            rationale="run the linter",
        )
        ledger = ledger.model_copy(update={"held": [*ledger.held, other]})
        released = reconcile_ledger(ledger, head="sha-1")
        assert released.ledger.status == "in_progress"
        assert released.released is not None
        assert released.released.cause == "operator_action_done"
        assert released.ledger.held == [other]
        assert released.released.cleared_held == [
            HeldEntry(identity=gap_identity(), rationale=GAP_RATIONALE)
        ]
        assert released.ledger.releases[-1].cleared_held == (
            released.released.cleared_held
        )

    def test_max_cycles_raised_release_keeps_the_held_memory(self) -> None:
        ledger = fresh_ledger(max_cycles=1)
        ledger, _ = record(ledger, held_gap(), real_fix(), sha="sha-1")
        ledger, report = record(
            ledger, round_finding("MUST_FIX", "Second bug"), sha="sha-2"
        )
        assert report.decision == "stop_max_cycles"
        released = reconcile_ledger(ledger, head="sha-2", max_cycles=4)
        assert released.released is not None
        assert released.released.cause == "max_cycles_raised"
        assert released.released.cleared_held == []
        assert "cleared_held" not in released.released.model_dump(mode="json")
        assert [entry.identity for entry in released.ledger.held] == [gap_identity()]
        reopened, follow = record(released.ledger, sha="sha-2")
        assert follow.decision == "stop_operator_action"
        assert reopened.stop_identities == [gap_identity()]
        assert reopened.last_clean_sha is None

    def test_contest_only_stop_release_keeps_an_unnamed_gap(self) -> None:
        ledger, _ = record(fresh_ledger(), held_gap(), real_fix(), sha="sha-1")
        ledger, report = record(ledger, operator_finding("Contested"), sha="sha-2")
        assert report.decision == "stop_operator_action"
        assert gap_identity() not in ledger.stop_identities
        released = reconcile_ledger(ledger, head="sha-2")
        assert released.ledger.status == "in_progress"
        assert [entry.identity for entry in released.ledger.held] == [gap_identity()]
        _, follow = record(released.ledger, sha="sha-2")
        assert follow.decision == "stop_operator_action"

    def test_new_commits_clear_all_held_memory(self) -> None:
        """Operator item 6(c): the epoch, not the stop, clears an unnamed gap.

        The stop names only the contest, so the ``operator_action`` arm would
        keep the gap; only the new-epoch arm can clear it. The same-head half
        (the gap survives) is ``test_contest_only_stop_release_keeps_an_unnamed_gap``.
        """
        ledger, _ = record(fresh_ledger(), held_gap(), real_fix(), sha="sha-1")
        ledger, report = record(ledger, operator_finding("Contested"), sha="sha-2")
        assert report.decision == "stop_operator_action"
        assert gap_identity() not in ledger.stop_identities
        other = HeldEntry(
            identity=finding_identity("MUST_FIX", "N/A", "Another gap"),
            rationale="run the linter",
        )
        ledger = ledger.model_copy(update={"held": [*ledger.held, other]})
        released = reconcile_ledger(ledger, head="sha-3")
        assert released.released is not None
        assert released.released.cause == "new_commits"
        assert released.ledger.held == []
        assert released.released.cleared_held == [
            HeldEntry(identity=gap_identity(), rationale=GAP_RATIONALE),
            other,
        ]

    def test_in_progress_held_ledger_resumes_with_held_intact(self) -> None:
        ledger, _ = record(fresh_ledger(), held_gap(), real_fix(), sha="sha-1")
        resumed = reconcile_ledger(ledger, head="sha-1")
        assert [entry.identity for entry in resumed.ledger.held] == [gap_identity()]


class TestOverrideOverHeld:
    """Operator item 6b: an override cannot converge past an outstanding gap."""

    @staticmethod
    def _dirty_confirm() -> ConvergenceLedger:
        ledger, report = _round(
            _open_ledger("confirm"), "confirm", held_gap(), real_fix(), sha=_SHA
        )
        assert report.decision == "continue"
        return ledger

    @staticmethod
    def _override(ledger: ConvergenceLedger) -> ConvergenceLedger:
        fix = (finding_identity("MUST_FIX", "src/cw/foo.py", "Real bug"),)
        updated, _ = override_confirm_round(
            ledger,
            fix,
            reviewed_sha=_SHA,
            round_identities=fix,
            actor="operator",
            reason="accepted",
            now=NOW,
        )
        return updated

    def test_refuses_naming_the_gap_and_both_routes(self) -> None:
        ledger = self._dirty_confirm()
        with pytest.raises(ConvergenceError) as raised:
            self._override(ledger)
        message = str(raised.value)
        assert "N/A" in message
        assert GAP_SUMMARY in message
        assert "cw review converge operator-settle" in message
        assert "cw review settle" not in message
        assert "requeue" in message
        assert [entry.identity for entry in ledger.held] == [gap_identity()]
        assert ledger.status == "in_progress"

    def test_a_settled_gap_no_longer_blocks_the_override(self) -> None:
        ledger = _settle_gap(self._dirty_confirm())
        converged = self._override(ledger)
        assert converged.status == "converged"
        assert converged.last_clean_sha == _SHA


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

    def test_a_pre_change_ledger_loads_with_no_held_and_dumps_without_it(self) -> None:
        ledger = ConvergenceLedger.model_validate_json(old_ledger_json())
        assert ledger.held == []
        assert ledger.schema_version == 1
        dumped = json.loads(ledger.model_dump_json())
        assert "held" not in dumped
        assert all("new_held" not in row for row in dumped["rounds"])
