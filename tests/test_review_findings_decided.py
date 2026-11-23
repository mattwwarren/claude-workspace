"""The held-for-operator markers (contest #2984, evidence gap #3793) and their stamp.

1:1 with ``src/cw/review_findings/_decided.py``. A reviewer that believes a
recorded decision is itself unsafe files the finding with
``contests_decision`` set; one that rests on a check it could not run sets
``evidence_gap`` (#3793). :func:`stamp_held_findings` holds a MUST_FIX one for
the operator (``operator_actionable``) and records a SHOULD_FIX one as a
non-blocking note (``deferred``), so neither ever reaches the fix loop.
"""

from __future__ import annotations

import pytest

from cw.auto_dev_result import Review
from cw.review_findings import (
    AcceptedFinding,
    Finding,
    ReviewVerdict,
    has_held_findings,
    held_disposition,
    held_findings,
    held_kind,
    is_gap_hold,
    stamp_held_findings,
)
from tests._shared.review_builders import (
    _finding_kwargs,
    _make_finding,
    _make_gap_finding,
)

_QUOTE = "Non-Goal 2 'no retraction path' leaves a stale entry binding forever."
_HELD = (
    "contests a decided constraint; held for the operator (`cw review settle`"
    " dismisses it)"
)
_NOTE = "contests a decided constraint; recorded as a non-blocking note"
_GAP_HELD = (
    "evidence gap: the reviewer could not verify this; held for the operator"
    " (supply the missing check and requeue the review)"
)
_GAP_NOTE = (
    "evidence gap: the reviewer could not verify this; recorded as a non-blocking note"
)


def _accepted(finding: Finding, **overrides: object) -> AcceptedFinding:
    kwargs: dict[str, object] = {"finding": finding, "reviewers": ["Test Reviewer"]}
    kwargs.update(overrides)
    return AcceptedFinding.model_validate(kwargs)


def _verdict(*accepted: AcceptedFinding) -> ReviewVerdict:
    must_fix = [
        af.finding
        for af in accepted
        if af.finding.severity == "MUST_FIX" and af.disposition == "fixed"
    ]
    review = Review(
        must_fix_initial=len(must_fix),
        should_fix=sum(1 for af in accepted if af.finding.severity == "SHOULD_FIX"),
        fix_cycles_used=0,
        deferred=0,
        agents_run=1,
    )
    return ReviewVerdict(
        blocking=bool(must_fix),
        must_fix=must_fix,
        reviewed_sha="abc1234",
        accepted=list(accepted),
        review=review,
    )


def _marked(severity: str = "MUST_FIX", **overrides: object) -> Finding:
    return _make_finding(severity=severity, contests_decision=_QUOTE, **overrides)


def _gapped(severity: str = "MUST_FIX", **overrides: object) -> Finding:
    return _make_gap_finding(severity, **overrides)


class TestField:
    def test_defaults_blank(self) -> None:
        assert _make_finding().contests_decision == ""
        assert _make_finding().evidence_gap == ""

    def test_null_normalizes_to_blank(self) -> None:
        finding = Finding.model_validate(
            {**_finding_kwargs(), "contests_decision": None}
        )
        assert finding.contests_decision == ""

    def test_null_evidence_gap_normalizes_to_blank(self) -> None:
        finding = Finding.model_validate({**_finding_kwargs(), "evidence_gap": None})
        assert finding.evidence_gap == ""


class TestHeldKind:
    def test_unmarked_finding_has_no_kind(self) -> None:
        assert held_kind(_make_finding()) is None

    @pytest.mark.parametrize("marker", ["", "   ", "\n\t"])
    def test_blank_gap_is_no_kind(self, marker: str) -> None:
        assert held_kind(_make_finding(evidence_gap=marker)) is None

    def test_gap_only_is_an_evidence_gap(self) -> None:
        assert held_kind(_gapped()) == "evidence_gap"

    def test_contest_only_is_a_decision_contest(self) -> None:
        assert held_kind(_marked()) == "decision_contest"

    def test_a_contest_wins_when_both_markers_are_set(self) -> None:
        assert held_kind(_gapped(contests_decision=_QUOTE)) == "decision_contest"


class TestGapDisposition:
    def test_must_fix_gap_is_held_for_the_operator(self) -> None:
        assert held_disposition(_gapped()) == ("operator_actionable", _GAP_HELD)

    def test_should_fix_gap_is_a_deferred_note(self) -> None:
        assert held_disposition(_gapped("SHOULD_FIX")) == ("deferred", _GAP_NOTE)

    @pytest.mark.parametrize("severity", ["NIT", "PRINCIPLE", "DEBT"])
    def test_lower_severities_are_untouched(self, severity: str) -> None:
        assert held_disposition(_gapped(severity)) is None

    def test_both_markers_keep_the_contest_detail(self) -> None:
        forced = held_disposition(_gapped(contests_decision=_QUOTE))
        assert forced == ("operator_actionable", _HELD)

    @pytest.mark.parametrize("rule_id", ["coverage.tests", "acceptance.criteria"])
    def test_must_fix_non_deferrable_gap_is_held_for_the_operator(
        self, rule_id: str
    ) -> None:
        assert held_disposition(_gapped(rule_id=rule_id)) == (
            "operator_actionable",
            _GAP_HELD,
        )

    @pytest.mark.parametrize("rule_id", ["coverage.tests", "acceptance.criteria"])
    def test_should_fix_non_deferrable_gap_is_not_held(self, rule_id: str) -> None:
        assert held_disposition(_gapped("SHOULD_FIX", rule_id=rule_id)) is None

    def test_should_fix_non_deferrable_contest_is_unchanged(self) -> None:
        forced = held_disposition(
            _gapped("SHOULD_FIX", rule_id="coverage.tests", contests_decision=_QUOTE)
        )
        assert forced == ("deferred", _NOTE)


class TestContestedDisposition:
    def test_must_fix_is_held_for_the_operator(self) -> None:
        assert held_disposition(_marked()) == ("operator_actionable", _HELD)

    def test_should_fix_is_a_deferred_note(self) -> None:
        assert held_disposition(_marked("SHOULD_FIX")) == ("deferred", _NOTE)

    @pytest.mark.parametrize("severity", ["NIT", "PRINCIPLE", "DEBT"])
    def test_lower_severities_are_untouched(self, severity: str) -> None:
        assert held_disposition(_marked(severity)) is None

    @pytest.mark.parametrize("marker", ["", "   ", "\n\t"])
    def test_blank_marker_is_no_contest(self, marker: str) -> None:
        assert held_disposition(_make_finding(contests_decision=marker)) is None


class TestStamp:
    def test_must_fix_is_stamped_and_leaves_must_fix_and_blocking(self) -> None:
        verdict = stamp_held_findings(_verdict(_accepted(_marked())))
        (af,) = verdict.accepted
        assert af.disposition == "operator_actionable"
        assert af.disposition_detail == _HELD
        assert verdict.must_fix == []
        assert verdict.blocking is False

    def test_quote_is_not_copied_into_the_detail(self) -> None:
        (af,) = stamp_held_findings(_verdict(_accepted(_marked()))).accepted
        assert _QUOTE not in af.disposition_detail

    def test_should_fix_is_stamped_deferred(self) -> None:
        (af,) = stamp_held_findings(_verdict(_accepted(_marked("SHOULD_FIX")))).accepted
        assert (af.disposition, af.disposition_detail) == ("deferred", _NOTE)

    @pytest.mark.parametrize("severity", ["NIT", "PRINCIPLE", "DEBT"])
    def test_lower_severities_keep_their_disposition(self, severity: str) -> None:
        verdict = _verdict(_accepted(_marked(severity)))
        assert stamp_held_findings(verdict) == verdict

    @pytest.mark.parametrize("disposition", ["rejected", "deferred", "dropped"])
    def test_already_decided_findings_are_not_re_stamped(
        self, disposition: str
    ) -> None:
        settled = _accepted(
            _marked(), disposition=disposition, disposition_detail="settled"
        )
        verdict = _verdict(settled)
        stamped = stamp_held_findings(verdict)
        assert stamped.accepted == [settled]

    def test_unmarked_must_fix_stays_blocking_beside_a_contest(self) -> None:
        plain = _make_finding(summary="Real bug", file="src/cw/bar.py")
        verdict = stamp_held_findings(_verdict(_accepted(_marked()), _accepted(plain)))
        assert verdict.must_fix == [plain]
        assert verdict.blocking is True
        assert [af.disposition for af in verdict.accepted] == [
            "operator_actionable",
            "fixed",
        ]

    def test_review_counts_are_untouched(self) -> None:
        verdict = _verdict(_accepted(_marked()))
        assert stamp_held_findings(verdict).review == verdict.review

    def test_no_contest_returns_the_verdict_unchanged(self) -> None:
        verdict = _verdict(_accepted(_make_finding()))
        assert stamp_held_findings(verdict) == verdict


class TestGapStamp:
    def test_must_fix_gap_is_stamped_and_leaves_must_fix_and_blocking(self) -> None:
        verdict = stamp_held_findings(_verdict(_accepted(_gapped())))

        (af,) = verdict.accepted
        assert (af.disposition, af.disposition_detail) == (
            "operator_actionable",
            _GAP_HELD,
        )
        assert verdict.must_fix == []
        assert verdict.blocking is False

    def test_should_fix_gap_is_stamped_deferred(self) -> None:
        (af,) = stamp_held_findings(_verdict(_accepted(_gapped("SHOULD_FIX")))).accepted

        assert (af.disposition, af.disposition_detail) == ("deferred", _GAP_NOTE)

    def test_a_settled_gap_is_not_re_stamped(self) -> None:
        settled = _accepted(
            _gapped(), disposition="rejected", disposition_detail="settled"
        )

        stamped = stamp_held_findings(_verdict(settled))

        assert stamped.accepted == [settled]

    def test_must_fix_non_deferrable_gap_is_stamped_and_not_blocking(self) -> None:
        verdict = stamp_held_findings(
            _verdict(_accepted(_gapped(rule_id="coverage.tests")))
        )

        (af,) = verdict.accepted
        assert (af.disposition, af.disposition_detail) == (
            "operator_actionable",
            _GAP_HELD,
        )
        assert verdict.must_fix == []
        assert verdict.blocking is False

    def test_should_fix_non_deferrable_gap_stays_fixed(self) -> None:
        (af,) = stamp_held_findings(
            _verdict(_accepted(_gapped("SHOULD_FIX", rule_id="coverage.tests")))
        ).accepted

        assert af.disposition == "fixed"

    def test_unmarked_must_fix_stays_blocking_beside_a_gap(self) -> None:
        plain = _make_finding(summary="Real bug", file="src/cw/bar.py")

        verdict = stamp_held_findings(_verdict(_accepted(_gapped()), _accepted(plain)))

        assert verdict.must_fix == [plain]
        assert verdict.blocking is True
        assert [af.disposition for af in verdict.accepted] == [
            "operator_actionable",
            "fixed",
        ]


class TestHeldFindingsByKind:
    def _both_kinds(self) -> ReviewVerdict:
        return stamp_held_findings(
            _verdict(
                _accepted(_gapped(summary="gap", file="src/cw/a.py")),
                _accepted(_marked(summary="contest", file="src/cw/b.py")),
            )
        )

    def test_no_kind_covers_both(self) -> None:
        verdict = self._both_kinds()

        assert {af.finding.summary for af in held_findings(verdict)} == {
            "gap",
            "contest",
        }
        assert has_held_findings(verdict) is True

    def test_kind_filters_to_that_kind(self) -> None:
        verdict = self._both_kinds()

        (gap,) = held_findings(verdict, "evidence_gap")
        (contest,) = held_findings(verdict, "decision_contest")
        assert (gap.finding.summary, contest.finding.summary) == ("gap", "contest")

    def test_has_held_follows_the_kind(self) -> None:
        gap_only = stamp_held_findings(_verdict(_accepted(_gapped())))

        assert has_held_findings(gap_only, "evidence_gap") is True
        assert has_held_findings(gap_only, "decision_contest") is False


class TestContestedFindings:
    def test_returns_exactly_the_stamped_must_fix_findings(self) -> None:
        plain = _accepted(_make_finding(summary="Real bug"))
        should = _accepted(_marked("SHOULD_FIX", summary="Should"))
        verdict = stamp_held_findings(_verdict(_accepted(_marked()), plain, should))
        (contested,) = held_findings(verdict)
        assert contested.finding.contests_decision == _QUOTE
        assert contested.disposition == "operator_actionable"
        assert has_held_findings(verdict) is True

    def test_needs_a_non_blank_marker(self) -> None:
        for marker in ("", "   "):
            held = _accepted(
                _make_finding(contests_decision=marker),
                disposition="operator_actionable",
            )
            verdict = _verdict(held)
            assert held_findings(verdict) == []
            assert has_held_findings(verdict) is False

    def test_needs_the_operator_actionable_disposition(self) -> None:
        verdict = _verdict(_accepted(_marked()))
        assert has_held_findings(verdict) is False

    def test_needs_must_fix_severity(self) -> None:
        verdict = _verdict(
            _accepted(_marked("SHOULD_FIX"), disposition="operator_actionable")
        )
        assert has_held_findings(verdict) is False


class TestIsGapHold:
    @pytest.mark.parametrize(
        ("severity", "disposition", "marker", "expected"),
        [
            ("MUST_FIX", "operator_actionable", "gap", True),
            ("MUST_FIX", "fixed", "gap", False),
            ("MUST_FIX", "operator_actionable", "", False),
            ("MUST_FIX", "operator_actionable", "contest", False),
            ("SHOULD_FIX", "operator_actionable", "gap", False),
            ("SHOULD_FIX", "deferred", "gap", False),
        ],
    )
    def test_truth_table(
        self, severity: str, disposition: str, marker: str, expected: bool
    ) -> None:
        if marker == "gap":
            finding = _gapped(severity)
        elif marker == "contest":
            finding = _marked(severity)
        else:
            finding = _make_finding(severity=severity)
        af = _accepted(finding, disposition=disposition)

        assert is_gap_hold(af) is expected
