"""The confirm round after a repo review engine pass (#3042, RFC 0017 I3).

Pure tests of :func:`cw.review_convergence.route_engine_confirm`,
:func:`engine_pass_precedes` and the engine wiring in ``record_round``: a new
MUST_FIX (or a never-deferrable SHOULD_FIX) parks above every other stop, a
SHOULD_FIX is deferred and the round is clean, nothing new is clean, and the
codex-loop, opencode, non-engine and delta lanes are unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.models import OPENCODE_BACKEND
from cw.review_convergence import (
    CONFIRM_ENGINE_DEFER_RATIONALE,
    ConfirmAction,
    ConvergenceError,
    ConvergenceLedger,
    RoundFinding,
    SettledEntry,
    UnreadFindings,
    engine_pass_precedes,
    finding_identity,
    record_delta_round,
    route_engine_confirm,
)
from cw.review_finding_dispositions import debt_key
from tests._review_convergence_helpers import (
    DEFAULT_SHA,
    NOW,
    attestation_digest,
    engine_pass_ledger,
    fresh_ledger,
    record,
    round_finding,
    settled_deferrals,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.review_adjudication import AdjudicationOutcome
    from cw.review_convergence import GatingSeverity, RoundReport

_COVERAGE = "coverage.patch"
_ACCEPTANCE = "acceptance.criteria"
_CONFIRM_SHA = DEFAULT_SHA


def _ruled(severity: GatingSeverity, summary: str, rule_id: str | None) -> RoundFinding:
    return round_finding(severity, summary).model_copy(update={"rule_id": rule_id})


def _confirm(
    ledger: ConvergenceLedger,
    *findings: RoundFinding,
    unread: UnreadFindings | None = None,
) -> tuple[ConvergenceLedger, RoundReport]:
    return record(ledger, *findings, sha=_CONFIRM_SHA, kind="confirm", unread=unread)


class TestRouter:
    def test_a_new_must_fix_parks(self) -> None:
        route = route_engine_confirm(engine_pass_ledger(), [round_finding("MUST_FIX")])
        assert route.action is ConfirmAction.PARK
        assert route.parks
        assert route.park_identities == (
            finding_identity("MUST_FIX", "src/cw/foo.py", "Bug here"),
        )
        assert route.classified.new_fix == []
        assert route.classified.new_accepted == 1

    def test_should_fix_only_defers(self) -> None:
        findings = [
            round_finding("SHOULD_FIX", "Style"),
            _ruled("SHOULD_FIX", "Naming", "style.naming"),
        ]
        route = route_engine_confirm(engine_pass_ledger(), findings)
        assert route.action is ConfirmAction.DEFER
        assert not route.parks
        assert [f.summary for f in route.deferred] == ["Style", "Naming"]
        assert route.classified.new_accepted == 0
        assert all(
            entry.outcome == "defer"
            and entry.rationale == CONFIRM_ENGINE_DEFER_RATIONALE
            and entry.round == 2
            for entry in route.classified.newly_settled
        )

    def test_nothing_is_clean(self) -> None:
        route = route_engine_confirm(engine_pass_ledger(), [])
        assert route.action is ConfirmAction.CLEAN
        assert route.deferred == ()
        assert route.park_identities == ()

    def test_a_mixed_round_parks_and_still_defers_the_should_fix(self) -> None:
        route = route_engine_confirm(
            engine_pass_ledger(),
            [round_finding("MUST_FIX"), round_finding("SHOULD_FIX", "Style")],
        )
        assert route.action is ConfirmAction.PARK
        assert [f.summary for f in route.deferred] == ["Style"]
        assert len(route.classified.newly_settled) == 1

    @pytest.mark.parametrize("outcome", ["fix", "defer", "reject", "operator_action"])
    def test_a_should_fix_is_deferred_whatever_its_stamped_outcome(
        self, outcome: AdjudicationOutcome
    ) -> None:
        finding = round_finding("SHOULD_FIX", outcome=outcome, rationale="stamped")
        route = route_engine_confirm(engine_pass_ledger(), [finding])
        assert route.action is ConfirmAction.DEFER

    def test_a_rejected_must_fix_still_parks(self) -> None:
        finding = round_finding("MUST_FIX", outcome="reject", rationale="cw says no")
        route = route_engine_confirm(engine_pass_ledger(), [finding])
        assert route.action is ConfirmAction.PARK

    def test_a_resurfaced_settled_identity_is_not_new(self) -> None:
        identity = finding_identity("MUST_FIX", "src/cw/foo.py", "Bug here")
        ledger = engine_pass_ledger()
        ledger = ledger.model_copy(
            update={
                "settled": [
                    SettledEntry(
                        identity=identity,
                        outcome="defer",
                        rationale="operator override",
                        round=1,
                        origin="operator",
                    )
                ]
            }
        )
        route = route_engine_confirm(ledger, [round_finding("MUST_FIX")])
        assert route.action is ConfirmAction.CLEAN
        assert route.classified.resurfaced == 1

    def test_duplicate_identities_collapse(self) -> None:
        route = route_engine_confirm(
            engine_pass_ledger(),
            [
                round_finding("MUST_FIX"),
                round_finding("MUST_FIX"),
                round_finding("SHOULD_FIX", "Style"),
                round_finding("SHOULD_FIX", "Style"),
            ],
        )
        assert len(route.park_identities) == 1
        assert len(route.deferred) == 1
        assert len(route.classified.newly_settled) == 1

    def test_an_engine_fingerprint_at_two_severities_parks(self) -> None:
        must = round_finding("MUST_FIX", "A").model_copy(update={"fingerprint": "fp1"})
        should = round_finding("SHOULD_FIX", "A").model_copy(
            update={"fingerprint": "fp1"}
        )
        route = route_engine_confirm(engine_pass_ledger(), [should, must])
        assert route.action is ConfirmAction.PARK
        assert route.deferred == ()


class TestNeverDeferrableShouldFix:
    @pytest.mark.parametrize("rule", [_COVERAGE, _ACCEPTANCE])
    def test_alone_it_parks_at_should_fix_severity(self, rule: str) -> None:
        route = route_engine_confirm(
            engine_pass_ledger(), [_ruled("SHOULD_FIX", "Untested", rule)]
        )
        assert route.action is ConfirmAction.PARK
        assert route.park_identities == (
            finding_identity("SHOULD_FIX", "src/cw/foo.py", "Untested"),
        )
        assert route.deferred == ()

    def test_mixed_with_an_ordinary_should_fix(self) -> None:
        route = route_engine_confirm(
            engine_pass_ledger(),
            [
                _ruled("SHOULD_FIX", "Untested", _COVERAGE),
                round_finding("SHOULD_FIX", "Style"),
            ],
        )
        assert route.action is ConfirmAction.PARK
        assert [f.summary for f in route.deferred] == ["Style"]

    def test_an_operator_defer_of_its_fingerprint_does_not_resurface_it(
        self,
    ) -> None:
        finding = _ruled("SHOULD_FIX", "Untested", _COVERAGE).model_copy(
            update={"fingerprint": "fp-cov"}
        )
        identity = finding_identity(
            "SHOULD_FIX", finding.file, finding.summary, fingerprint="fp-cov"
        )
        ledger = engine_pass_ledger().model_copy(
            update={
                "settled": [
                    SettledEntry(
                        identity=identity,
                        outcome="defer",
                        rationale="operator",
                        round=1,
                        origin="operator",
                    )
                ]
            }
        )
        route = route_engine_confirm(ledger, [finding])
        assert route.action is ConfirmAction.PARK
        assert route.classified.resurfaced == 0


class TestEnginePassPrecedes:
    def test_true_after_a_clean_engine_loop_round(self) -> None:
        assert engine_pass_precedes(engine_pass_ledger())

    def test_false_on_a_fresh_ledger(self) -> None:
        assert not engine_pass_precedes(fresh_ledger(confirm="codex"))

    def test_false_when_the_loop_round_did_not_run_the_engine(self) -> None:
        assert not engine_pass_precedes(engine_pass_ledger(engine=False))

    def test_false_when_not_awaiting_confirm(self) -> None:
        ledger = engine_pass_ledger().model_copy(update={"status": "in_progress"})
        assert not engine_pass_precedes(ledger)

    def test_false_when_the_sha_moved(self) -> None:
        ledger = engine_pass_ledger().model_copy(
            update={"awaiting_confirm_sha": "sha-other"}
        )
        assert not engine_pass_precedes(ledger)

    def test_false_when_the_last_round_is_dirty(self) -> None:
        ledger = engine_pass_ledger()
        last = ledger.rounds[-1].model_copy(update={"clean": False})
        assert not engine_pass_precedes(ledger.model_copy(update={"rounds": [last]}))

    def test_false_when_the_last_round_is_not_a_loop_round(self) -> None:
        ledger = engine_pass_ledger()
        last = ledger.rounds[-1].model_copy(update={"kind": "confirm"})
        assert not engine_pass_precedes(ledger.model_copy(update={"rounds": [last]}))


def _hostile_engine_ledger() -> ConvergenceLedger:
    """An engine-pass ledger with max_cycles spent, fixed memory and a stall."""
    ledger = fresh_ledger(confirm="codex", max_cycles=2)
    ledger, report = record(
        ledger, round_finding("MUST_FIX"), round_finding("MUST_FIX", "B"), sha="s1"
    )
    assert report.decision == "continue"
    ledger, report = record(
        ledger, round_finding("MUST_FIX", "C"), round_finding("MUST_FIX", "D"), sha="s2"
    )
    assert report.decision == "continue"
    assert ledger.fix_cycles_used == ledger.max_cycles
    return engine_pass_ledger(ledger, sha=_CONFIRM_SHA)


class TestPrecedence:
    def test_the_park_outranks_every_other_stop(self) -> None:
        ledger = _hostile_engine_ledger()
        fixed_before = list(ledger.fixed)
        updated, report = _confirm(ledger, round_finding("MUST_FIX"))
        assert report.decision == "stop_confirm_must_fix"
        assert report.reason == "confirm_must_fix"
        assert updated.status == "stopped"
        assert updated.stop_reason == "confirm_must_fix"
        assert updated.fix_cycles_used == ledger.fix_cycles_used
        assert updated.fixed == fixed_before
        assert updated.stop_identities == [
            finding_identity("MUST_FIX", "src/cw/foo.py", "Bug here")
        ]
        last = updated.rounds[-1]
        assert (last.clean, last.new_fix, last.new_operator_action) == (
            False,
            0,
            1,
        )
        assert last.new_accepted == 1
        assert last.engine is False

    def test_the_unread_guard_still_wins_on_a_would_be_clean_round(self) -> None:
        with pytest.raises(ConvergenceError, match="unread_findings"):
            _confirm(
                engine_pass_ledger(),
                round_finding("SHOULD_FIX", "Style"),
                unread=UnreadFindings(rejected_must_fix=1),
            )

    def test_a_park_with_unread_findings_proceeds(self) -> None:
        updated, report = _confirm(
            engine_pass_ledger(),
            round_finding("MUST_FIX"),
            unread=UnreadFindings(rejected_must_fix=1),
        )
        assert report.decision == "stop_confirm_must_fix"
        assert updated.status == "stopped"

    def test_should_fix_only_converges_with_settled_deferrals(self) -> None:
        updated, report = _confirm(
            engine_pass_ledger(), round_finding("SHOULD_FIX", "Style")
        )
        assert report.decision == "clean"
        assert updated.status == "converged"
        assert updated.last_clean_sha == _CONFIRM_SHA
        deferrals = settled_deferrals(updated)
        assert len(deferrals) == 1
        assert deferrals[0].rationale == CONFIRM_ENGINE_DEFER_RATIONALE
        assert deferrals[0].origin == "adjudication"

    def test_a_mixed_park_keeps_the_deferral_in_settled(self) -> None:
        updated, _ = _confirm(
            engine_pass_ledger(),
            round_finding("MUST_FIX"),
            round_finding("SHOULD_FIX", "Style"),
        )
        assert updated.status == "stopped"
        assert len(settled_deferrals(updated)) == 1
        assert updated.rounds[-1].new_settled == 1


class TestAttestationRule:
    def test_a_clean_confirm_leaves_the_digest_unchanged(self) -> None:
        before = engine_pass_ledger()
        updated, report = _confirm(before)
        assert report.decision == "clean"
        assert attestation_digest(updated) == attestation_digest(before)
        assert updated.settled == before.settled
        assert updated.fixed == before.fixed

    def test_a_deferred_should_fix_adds_a_deferral(self) -> None:
        before = engine_pass_ledger()
        updated, _ = _confirm(before, round_finding("SHOULD_FIX", "Style"))
        _, deferrals = attestation_digest(updated)
        _, prior = attestation_digest(before)
        assert len(deferrals) == len(prior) + 1

    def test_a_park_leaves_the_digest_unchanged(self) -> None:
        before = engine_pass_ledger()
        updated, _ = _confirm(before, round_finding("MUST_FIX"))
        assert attestation_digest(updated) == attestation_digest(before)


def _non_engine_claude_native() -> ConvergenceLedger:
    ledger, report = record(fresh_ledger(confirm="codex"), sha=_CONFIRM_SHA)
    assert report.decision == "confirm_required"
    return ledger


def _codex_loop() -> ConvergenceLedger:
    ledger, report = record(
        fresh_ledger(confirm="claude", loop_backend="codex"),
        sha=_CONFIRM_SHA,
        backend="codex",
    )
    assert report.decision == "confirm_required"
    return ledger


def _opencode() -> ConvergenceLedger:
    ledger, report = record(
        fresh_ledger(confirm="claude", loop_backend=OPENCODE_BACKEND),
        sha=_CONFIRM_SHA,
        backend=OPENCODE_BACKEND,
    )
    assert report.decision == "confirm_required"
    return ledger


def _engine_role_without_flag() -> ConvergenceLedger:
    return engine_pass_ledger(engine=False)


class TestUnchangedLanes:
    @pytest.mark.parametrize(
        "build",
        [_non_engine_claude_native, _codex_loop, _opencode, _engine_role_without_flag],
    )
    def test_a_confirm_must_fix_continues(
        self, build: Callable[[], ConvergenceLedger]
    ) -> None:
        ledger = build()
        updated, report = _confirm(ledger, round_finding("MUST_FIX"))
        assert report.decision == "continue"
        assert updated.fix_cycles_used == ledger.fix_cycles_used + 1

    def test_a_delta_round_must_fix_continues_on_a_converged_engine_ledger(
        self,
    ) -> None:
        converged, report = _confirm(engine_pass_ledger())
        assert report.decision == "clean"
        updated, delta = record_delta_round(
            converged,
            backend="codex",
            reviewed_sha="sha-delta",
            findings=[round_finding("MUST_FIX")],
            roster=["codex"],
            now=NOW,
        )
        assert delta.decision == "continue"
        assert updated.rounds[-1].engine is False


class TestConfirmDeferDebtKey:
    """#3094: each SHOULD_FIX the confirm round defers carries its debt key."""

    def test_deferred_should_fix_settles_with_its_debt_key(self) -> None:
        deferred = round_finding("SHOULD_FIX", "Trailing  ", file="src/a.py")
        route = route_engine_confirm(fresh_ledger(), [deferred])
        (entry,) = route.classified.newly_settled
        assert entry.outcome == "defer"
        assert entry.debt_key == debt_key("src/a.py", "Trailing  ")
        assert entry.last_seen_round == len(fresh_ledger().rounds) + 1

    def test_park_class_findings_settle_nothing(self) -> None:
        park = round_finding("MUST_FIX", "Blocker", file="src/a.py")
        route = route_engine_confirm(fresh_ledger(), [park])
        assert route.classified.newly_settled == []
