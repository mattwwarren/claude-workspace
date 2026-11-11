"""The decided-constraint contest marker and its stamp (#2984).

1:1 with ``src/cw/review_findings/_decided.py``. A reviewer that believes a
recorded decision is itself unsafe files the finding with
``contests_decision`` set; :func:`stamp_decision_contests` holds a MUST_FIX
one for the operator (``operator_actionable``) and records a SHOULD_FIX one as
a non-blocking note (``deferred``), so neither ever reaches the fix loop.
"""

from __future__ import annotations

import pytest

from cw.auto_dev_result import Review
from cw.review_findings import (
    AcceptedFinding,
    Finding,
    ReviewVerdict,
    contested_disposition,
    contested_findings,
    has_contested_decision,
    stamp_decision_contests,
)
from tests._shared.review_builders import _finding_kwargs, _make_finding

_QUOTE = "Non-Goal 2 'no retraction path' leaves a stale entry binding forever."
_HELD = (
    "contests a decided constraint; held for the operator (`cw review settle`"
    " dismisses it)"
)
_NOTE = "contests a decided constraint; recorded as a non-blocking note"


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


class TestField:
    def test_defaults_blank(self) -> None:
        assert _make_finding().contests_decision == ""

    def test_null_normalizes_to_blank(self) -> None:
        finding = Finding.model_validate(
            {**_finding_kwargs(), "contests_decision": None}
        )
        assert finding.contests_decision == ""


class TestContestedDisposition:
    def test_must_fix_is_held_for_the_operator(self) -> None:
        assert contested_disposition(_marked()) == ("operator_actionable", _HELD)

    def test_should_fix_is_a_deferred_note(self) -> None:
        assert contested_disposition(_marked("SHOULD_FIX")) == ("deferred", _NOTE)

    @pytest.mark.parametrize("severity", ["NIT", "PRINCIPLE", "DEBT"])
    def test_lower_severities_are_untouched(self, severity: str) -> None:
        assert contested_disposition(_marked(severity)) is None

    @pytest.mark.parametrize("marker", ["", "   ", "\n\t"])
    def test_blank_marker_is_no_contest(self, marker: str) -> None:
        assert contested_disposition(_make_finding(contests_decision=marker)) is None


class TestStamp:
    def test_must_fix_is_stamped_and_leaves_must_fix_and_blocking(self) -> None:
        verdict = stamp_decision_contests(_verdict(_accepted(_marked())))
        (af,) = verdict.accepted
        assert af.disposition == "operator_actionable"
        assert af.disposition_detail == _HELD
        assert verdict.must_fix == []
        assert verdict.blocking is False

    def test_quote_is_not_copied_into_the_detail(self) -> None:
        (af,) = stamp_decision_contests(_verdict(_accepted(_marked()))).accepted
        assert _QUOTE not in af.disposition_detail

    def test_should_fix_is_stamped_deferred(self) -> None:
        (af,) = stamp_decision_contests(
            _verdict(_accepted(_marked("SHOULD_FIX")))
        ).accepted
        assert (af.disposition, af.disposition_detail) == ("deferred", _NOTE)

    @pytest.mark.parametrize("severity", ["NIT", "PRINCIPLE", "DEBT"])
    def test_lower_severities_keep_their_disposition(self, severity: str) -> None:
        verdict = _verdict(_accepted(_marked(severity)))
        assert stamp_decision_contests(verdict) == verdict

    @pytest.mark.parametrize("disposition", ["rejected", "deferred", "dropped"])
    def test_already_decided_findings_are_not_re_stamped(
        self, disposition: str
    ) -> None:
        settled = _accepted(
            _marked(), disposition=disposition, disposition_detail="settled"
        )
        verdict = _verdict(settled)
        stamped = stamp_decision_contests(verdict)
        assert stamped.accepted == [settled]

    def test_unmarked_must_fix_stays_blocking_beside_a_contest(self) -> None:
        plain = _make_finding(summary="Real bug", file="src/cw/bar.py")
        verdict = stamp_decision_contests(
            _verdict(_accepted(_marked()), _accepted(plain))
        )
        assert verdict.must_fix == [plain]
        assert verdict.blocking is True
        assert [af.disposition for af in verdict.accepted] == [
            "operator_actionable",
            "fixed",
        ]

    def test_review_counts_are_untouched(self) -> None:
        verdict = _verdict(_accepted(_marked()))
        assert stamp_decision_contests(verdict).review == verdict.review

    def test_no_contest_returns_the_verdict_unchanged(self) -> None:
        verdict = _verdict(_accepted(_make_finding()))
        assert stamp_decision_contests(verdict) == verdict


class TestContestedFindings:
    def test_returns_exactly_the_stamped_must_fix_findings(self) -> None:
        plain = _accepted(_make_finding(summary="Real bug"))
        should = _accepted(_marked("SHOULD_FIX", summary="Should"))
        verdict = stamp_decision_contests(_verdict(_accepted(_marked()), plain, should))
        (contested,) = contested_findings(verdict)
        assert contested.finding.contests_decision == _QUOTE
        assert contested.disposition == "operator_actionable"
        assert has_contested_decision(verdict) is True

    def test_needs_a_non_blank_marker(self) -> None:
        for marker in ("", "   "):
            held = _accepted(
                _make_finding(contests_decision=marker),
                disposition="operator_actionable",
            )
            verdict = _verdict(held)
            assert contested_findings(verdict) == []
            assert has_contested_decision(verdict) is False

    def test_needs_the_operator_actionable_disposition(self) -> None:
        verdict = _verdict(_accepted(_marked()))
        assert has_contested_decision(verdict) is False

    def test_needs_must_fix_severity(self) -> None:
        verdict = _verdict(
            _accepted(_marked("SHOULD_FIX"), disposition="operator_actionable")
        )
        assert has_contested_decision(verdict) is False
