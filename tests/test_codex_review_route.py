"""Tests for the codex disposition router (#2464, ``cw.codex_review._verdict._route``).

Every accepted MUST_FIX/SHOULD_FIX the codex path produces must end in a
terminal disposition: fixed (the fix loop's input), deferred (ticketed on
merge), or handed to the operator. These pin each route, the recompute of the
gate-feeding fields, and the deferral record FINALIZE consumes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw.codex_review._verdict._route import (
    AUTO_DEFERRED_DETAIL_PREFIX,
    NO_DIFF_ANCHOR_DETAIL_PREFIX,
    PLAN_DEVIATION_DETAIL_PREFIX,
    codex_deferrals,
    operator_actions,
    plan_deviations,
    record_codex_deferrals,
    route_codex_dispositions,
    verified_plan_quote,
)
from cw.exceptions import CwError
from cw.review_adjudication import (
    DEFERRED_FINDINGS_RELATIVE_PATH,
    parse_deferred_findings_md,
)
from tests.conftest import _make_finding
from tests.test_review_adjudication import _accepted, _verdict

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

_PLAN = (
    "# Plan\n\n## Requirements\n\n"
    "- The writer must use atomic_write_text and do NOT\n"
    "  write the file in place.\n"
)
_QUOTE = "do NOT write the file in place."


def _no_anchor(severity: str = "MUST_FIX") -> Finding:
    return _make_finding(
        severity=severity,
        file="N/A",
        line_start=None,
        line_end=None,
        no_diff_anchor=True,
        summary="follow-up ticket never filed",
    )


def _route_one(
    finding: Finding, plan: str | None = _PLAN
) -> tuple[ReviewVerdict, AcceptedFinding]:
    routed = route_codex_dispositions(_verdict(_accepted(finding)), plan)
    (accepted,) = routed.accepted
    return routed, accepted


class TestRouteCodexDispositions:
    def test_ordinary_must_fix_stays_fixed_and_blocking(self) -> None:
        routed, accepted = _route_one(_make_finding())

        assert accepted.disposition == "fixed"
        assert accepted.disposition_detail == ""
        assert routed.blocking is True
        assert routed.must_fix == [accepted.finding]

    def test_no_diff_anchor_must_fix_is_operator_actionable(self) -> None:
        routed, accepted = _route_one(_no_anchor())

        assert accepted.disposition == "operator_actionable"
        assert accepted.disposition_detail.startswith(NO_DIFF_ANCHOR_DETAIL_PREFIX)
        assert routed.must_fix == []
        assert routed.blocking is False
        assert operator_actions(routed) == [accepted]

    def test_should_fix_is_deferred_and_counted(self) -> None:
        routed, accepted = _route_one(_make_finding(severity="SHOULD_FIX"))

        assert accepted.disposition == "deferred"
        assert accepted.disposition_detail.startswith(AUTO_DEFERRED_DETAIL_PREFIX)
        assert routed.review.deferred == 1
        # The frozen cycle-0 counts are preserved, not recomputed.
        assert routed.review.should_fix == 1
        assert routed.blocking is False
        assert codex_deferrals(routed) == [accepted]

    def test_no_diff_anchor_should_fix_is_deferred_not_operator(self) -> None:
        """The Claude contract routes it through ordinary DEFER (#1817 C2)."""
        routed, accepted = _route_one(_no_anchor("SHOULD_FIX"))

        assert accepted.disposition == "deferred"
        assert operator_actions(routed) == []

    @pytest.mark.parametrize("severity", ["MUST_FIX", "SHOULD_FIX"])
    def test_verified_plan_quote_is_a_plan_deviation(self, severity: str) -> None:
        routed, accepted = _route_one(
            _make_finding(severity=severity, plan_requirement_quote=_QUOTE)
        )

        assert accepted.disposition == "operator_actionable"
        assert accepted.disposition_detail.startswith(PLAN_DEVIATION_DETAIL_PREFIX)
        assert _QUOTE in accepted.disposition_detail
        assert routed.must_fix == []
        assert plan_deviations(routed) == [accepted]
        assert operator_actions(routed) == []

    def test_plan_deviation_outranks_no_diff_anchor(self) -> None:
        finding = _no_anchor().model_copy(update={"plan_requirement_quote": _QUOTE})

        routed, _accepted_finding = _route_one(finding)

        assert len(plan_deviations(routed)) == 1
        assert operator_actions(routed) == []

    @pytest.mark.parametrize(
        ("quote", "plan"),
        [
            pytest.param("never write anything to disk", _PLAN, id="not-in-plan"),
            pytest.param("do NOT", _PLAN, id="too-short"),
            pytest.param(_QUOTE, None, id="no-plan"),
        ],
    )
    def test_unverified_quote_routes_as_ordinary(
        self, quote: str, plan: str | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING)

        routed, accepted = _route_one(_make_finding(plan_requirement_quote=quote), plan)

        assert accepted.disposition == "fixed"
        assert routed.blocking is True
        assert plan_deviations(routed) == []
        if plan is not None:
            assert "codex_plan_quote_unverified" in caplog.text

    def test_already_decided_finding_is_not_rerouted(self) -> None:
        voided = _accepted(
            _make_finding(severity="SHOULD_FIX"),
            disposition="rejected",
            disposition_detail="operator voided it",
        )

        routed = route_codex_dispositions(_verdict(voided), _PLAN)

        assert routed.accepted == [voided]

    @pytest.mark.parametrize("severity", ["NIT", "DEBT", "PRINCIPLE"])
    def test_non_gating_severities_are_untouched(self, severity: str) -> None:
        _routed, accepted = _route_one(_make_finding(severity=severity))

        assert accepted.disposition == "fixed"
        assert accepted.disposition_detail == ""

    def test_mixed_pass_keeps_only_fixable_must_fix_blocking(self) -> None:
        fixable = _make_finding(summary="fixable")
        verdict = _verdict(
            _accepted(fixable),
            _accepted(_no_anchor()),
            _accepted(_make_finding(severity="SHOULD_FIX", summary="sf")),
        )

        routed = route_codex_dispositions(verdict, _PLAN)

        assert routed.must_fix == [fixable]
        assert routed.blocking is True
        assert [af.disposition for af in routed.accepted] == [
            "fixed",
            "operator_actionable",
            "deferred",
        ]


class TestVerifiedPlanQuote:
    def test_whitespace_is_collapsed_on_both_sides(self) -> None:
        """A quote of a wrapped plan line still verifies."""
        finding = _make_finding(plan_requirement_quote="do NOT   write the file")

        assert verified_plan_quote(finding, _PLAN) == "do NOT write the file"

    def test_blank_quote_verifies_nothing(self) -> None:
        assert verified_plan_quote(_make_finding(), _PLAN) is None


class TestRecordCodexDeferrals:
    def _sf_verdict(self, summary: str = "sf one") -> ReviewVerdict:
        return route_codex_dispositions(
            _verdict(_accepted(_make_finding(severity="SHOULD_FIX", summary=summary))),
            None,
        )

    def test_none_verdict_records_nothing(self, tmp_path: Path) -> None:
        assert record_codex_deferrals(tmp_path, None) == 0
        assert not (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).exists()

    def test_no_deferrals_writes_no_file(self, tmp_path: Path) -> None:
        verdict = route_codex_dispositions(_verdict(_accepted(_make_finding())), None)

        assert record_codex_deferrals(tmp_path, verdict) == 0
        assert not (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).exists()

    def test_writes_a_parseable_defer_entry(self, tmp_path: Path) -> None:
        assert record_codex_deferrals(tmp_path, self._sf_verdict()) == 1

        entries = parse_deferred_findings_md(
            (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        (entry,) = entries
        assert entry.outcome == "defer"
        assert entry.severity == "SHOULD_FIX"
        assert entry.summary == "sf one"
        assert entry.rationale.startswith(AUTO_DEFERRED_DETAIL_PREFIX)

    def test_recording_the_same_pass_twice_is_idempotent(self, tmp_path: Path) -> None:
        record_codex_deferrals(tmp_path, self._sf_verdict())
        record_codex_deferrals(tmp_path, self._sf_verdict())
        record_codex_deferrals(tmp_path, self._sf_verdict("sf two"))

        entries = parse_deferred_findings_md(
            (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        assert sorted(e.summary for e in entries) == ["sf one", "sf two"]

    def test_unparseable_prior_file_is_refused_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_text("not a deferred-findings artifact\n", encoding="utf-8")

        with pytest.raises(CwError, match="Refusing to overwrite"):
            record_codex_deferrals(tmp_path, self._sf_verdict())

        assert path.read_text(encoding="utf-8") == "not a deferred-findings artifact\n"
