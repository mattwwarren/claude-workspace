"""Evidence-gap holds: the `held_*` helpers and their stamp (#3793)."""

from __future__ import annotations

from cw.review_findings import (
    Finding,
    ReviewVerdict,
    consolidate_verdict,
    has_held_findings,
    held_disposition,
    held_findings,
    held_kind,
    stamp_held_findings,
)
from tests._shared.review_builders import (
    _EVIDENCE_GAP,
    _make_diff,
    _make_finding,
    _make_gap_finding,
    _make_reviewer_doc,
)

_QUOTE = "the approved plan says keep the old API"


def _verdict(*findings: Finding) -> ReviewVerdict:
    """Real consolidation of *findings*, each with its own diff evidence line."""
    diff = _make_diff("def broken():", "def other():", "def third():")
    doc = _make_reviewer_doc(*findings)
    return consolidate_verdict([doc], diff, reviewed_sha="sha")


class TestHeldKind:
    def test_blank_markers_give_none(self) -> None:
        assert held_kind(_make_finding()) is None
        assert held_kind(_make_finding(evidence_gap="   ")) is None

    def test_gap_only(self) -> None:
        assert held_kind(_make_gap_finding()) == "evidence_gap"

    def test_contest_wins_over_gap(self) -> None:
        finding = _make_gap_finding(contests_decision=_QUOTE)
        assert held_kind(finding) == "decision_contest"


class TestHeldDisposition:
    def test_must_fix_gap_is_held(self) -> None:
        forced = held_disposition(_make_gap_finding())
        assert forced is not None
        assert forced[0] == "operator_actionable"
        assert "evidence gap" in forced[1]

    def test_should_fix_gap_is_deferred_note(self) -> None:
        forced = held_disposition(_make_gap_finding(severity="SHOULD_FIX"))
        assert forced is not None
        assert forced[0] == "deferred"

    def test_nit_gap_gives_none(self) -> None:
        assert held_disposition(_make_gap_finding(severity="NIT")) is None

    def test_contest_keeps_contest_text_when_both_set(self) -> None:
        finding = _make_gap_finding(contests_decision=_QUOTE)
        forced = held_disposition(finding)
        assert forced is not None
        assert "contests a decided constraint" in forced[1]


class TestStampAndFilters:
    def test_stamp_flips_fixed_gap_and_recomputes_gate(self) -> None:
        stamped = stamp_held_findings(_verdict(_make_gap_finding()))
        assert stamped.blocking is False
        assert stamped.must_fix == []
        assert stamped.accepted[0].disposition == "operator_actionable"

    def test_unmarked_must_fix_beside_gap_keeps_blocking(self) -> None:
        verdict = _verdict(_make_gap_finding(), _make_finding(summary="other bug"))
        stamped = stamp_held_findings(verdict)
        assert stamped.blocking is True
        assert [f.summary for f in stamped.must_fix] == ["other bug"]

    def test_settled_gap_is_not_restamped(self) -> None:
        verdict = _verdict(_make_gap_finding())
        af = verdict.accepted[0].model_copy(update={"disposition": "rejected"})
        settled = verdict.model_copy(update={"accepted": [af], "must_fix": []})
        assert stamp_held_findings(settled) is settled

    def test_kind_filters(self) -> None:
        verdict = stamp_held_findings(
            _verdict(
                _make_gap_finding(summary="gap", evidence="def broken():"),
                _make_gap_finding(
                    summary="contest",
                    evidence="def other():",
                    contests_decision=_QUOTE,
                ),
            )
        )
        assert len(held_findings(verdict)) == 2
        assert len(held_findings(verdict, "evidence_gap")) == 1
        assert len(held_findings(verdict, "decision_contest")) == 1
        assert has_held_findings(verdict, "evidence_gap")
        assert has_held_findings(verdict, "decision_contest")

    def test_gap_text_is_exported_constant(self) -> None:
        assert _EVIDENCE_GAP
