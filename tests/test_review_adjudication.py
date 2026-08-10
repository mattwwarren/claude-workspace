"""Tests for the Claude-native adjudication seam (GitHub #1805).

1:1 with ``src/cw/review_adjudication.py`` per the CLAUDE.md Testing
convention. Reuses ``tests/conftest.py``'s ``_make_finding``/``_make_diff``
fixtures rather than re-declaring equivalents.
"""

from __future__ import annotations

import logging

import pytest
from cw.review_adjudication import (
    Adjudication,
    apply_adjudication,
    render_deferred_findings_md,
    verify_fixed_dispositions,
)
from pydantic import ValidationError

from cw.auto_dev_result import Review
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

from .conftest import _make_diff, _make_finding

_LOGGER = "cw.review_adjudication"


def _accepted(finding: Finding, **overrides: object) -> AcceptedFinding:
    """An AcceptedFinding at its post-consolidate default disposition."""
    kwargs: dict[str, object] = {"finding": finding, "reviewers": ["Test Reviewer"]}
    kwargs.update(overrides)
    return AcceptedFinding.model_validate(kwargs)


def _verdict(*accepted: AcceptedFinding, **overrides: object) -> ReviewVerdict:
    """A ReviewVerdict shaped exactly the way ``consolidate_verdict`` builds one.

    ``blocking``/``must_fix``/``review`` are derived from *accepted* with every
    disposition still at its ``"fixed"`` default — the genuine cycle-0 baseline
    ``apply_adjudication`` must preserve (R3).
    """
    must_fix = [af.finding for af in accepted if af.finding.severity == "MUST_FIX"]
    review = Review(
        must_fix_initial=len(must_fix),
        should_fix=sum(1 for af in accepted if af.finding.severity == "SHOULD_FIX"),
        fix_cycles_used=0,
        deferred=0,
        agents_run=len(accepted) or 1,
    )
    kwargs: dict[str, object] = {
        "blocking": bool(must_fix),
        "must_fix": must_fix,
        "reviewed_sha": "abc1234",
        "accepted": list(accepted),
        "review": review,
    }
    kwargs.update(overrides)
    return ReviewVerdict.model_validate(kwargs)


def _adjudication(finding: Finding, **overrides: object) -> Adjudication:
    """An Adjudication keyed to *finding* (defaults to ``outcome="fix"``)."""
    kwargs: dict[str, object] = {
        "severity": finding.severity,
        "file": finding.file,
        "line_start": finding.line_start,
        "line_end": finding.line_end,
        "evidence": finding.evidence,
        "summary": finding.summary,
        "outcome": "fix",
    }
    kwargs.update(overrides)
    return Adjudication.model_validate(kwargs)


_NO_ENTRY_DETAIL = "no adjudication entry recorded for this finding"


class TestApplyAdjudicationDispositions:
    def test_deferred_adjudication_never_serializes_as_fixed(self) -> None:
        """#1805 Round-2 fixture: a DEFERred finding must not read 'fixed'."""
        finding = _make_finding(severity="SHOULD_FIX")
        verdict = _verdict(_accepted(finding))
        adj = _adjudication(
            finding,
            outcome="defer",
            rationale="out of scope, handle in follow-up sweep",
        )

        result = apply_adjudication(verdict, [adj])

        assert result.accepted[0].disposition == "deferred"
        assert result.accepted[0].disposition != "fixed"
        assert (
            result.accepted[0].disposition_detail
            == "out of scope, handle in follow-up sweep"
        )

    def test_no_action_adjudication_never_serializes_as_fixed(self) -> None:
        """#1805 Round-1 fixture: a never-actioned PRINCIPLE finding."""
        finding = _make_finding(severity="PRINCIPLE")
        verdict = _verdict(_accepted(finding))

        result = apply_adjudication(verdict, [])

        assert result.accepted[0].disposition == "dropped"
        assert result.accepted[0].disposition != "fixed"
        assert result.accepted[0].disposition_detail == _NO_ENTRY_DETAIL

    def test_rejected_adjudication_stamps_rejected(self) -> None:
        finding = _make_finding(severity="SHOULD_FIX")
        verdict = _verdict(_accepted(finding))
        adj = _adjudication(
            finding, outcome="reject", rationale="deliberate tradeoff, documented"
        )

        result = apply_adjudication(verdict, [adj])

        assert result.accepted[0].disposition == "rejected"
        assert (
            result.accepted[0].disposition_detail == "deliberate tradeoff, documented"
        )

    def test_fix_adjudication_stamps_fixed(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(_accepted(finding))

        result = apply_adjudication(verdict, [_adjudication(finding)])

        assert result.accepted[0].disposition == "fixed"

    def test_non_fix_outcome_requires_rationale(self) -> None:
        finding = _make_finding()
        with pytest.raises(ValidationError):
            _adjudication(finding, outcome="defer", rationale="   ")


class TestApplyAdjudicationUnmatchedEntries:
    def test_unmatched_adjudication_entry_does_not_raise_and_is_counted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R4: an orphaned entry degrades + is counted, never raises."""
        finding = _make_finding(severity="SHOULD_FIX", line_start=10, line_end=10)
        verdict = _verdict(_accepted(finding))
        # line_end off by one -> matches no accepted finding's location key.
        stale = _adjudication(
            finding, line_end=11, outcome="reject", rationale="stale anchor"
        )

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = apply_adjudication(verdict, [stale])

        assert result.unmatched_adjudication_count == 1
        assert result.accepted[0].disposition == "dropped"
        assert result.accepted[0].disposition_detail == _NO_ENTRY_DETAIL
        assert any(
            "adjudication entry did not match" in rec.getMessage()
            and finding.file in rec.getMessage()
            for rec in caplog.records
        )

    def test_duplicate_entries_for_one_finding_first_wins_and_second_counted(
        self,
    ) -> None:
        finding = _make_finding(severity="SHOULD_FIX")
        verdict = _verdict(_accepted(finding))
        first = _adjudication(finding, outcome="reject", rationale="first wins")
        second = _adjudication(finding, outcome="defer", rationale="shadowed")

        result = apply_adjudication(verdict, [first, second])

        assert result.accepted[0].disposition == "rejected"
        assert result.accepted[0].disposition_detail == "first wins"
        assert result.unmatched_adjudication_count == 1


class TestApplyAdjudicationRecomputesVerdict:
    def test_must_fix_rejected_recomputes_blocking_false(self) -> None:
        """R3: stale pre-adjudication blocking/must_fix must be recomputed."""
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(_accepted(finding))
        assert verdict.blocking is True
        assert verdict.must_fix == [finding]

        result = apply_adjudication(
            verdict,
            [_adjudication(finding, outcome="reject", rationale="reviewer is wrong")],
        )

        assert result.blocking is False
        assert result.must_fix == []

    def test_must_fix_deferred_recomputes_blocking_false(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(_accepted(finding))

        result = apply_adjudication(
            verdict,
            [_adjudication(finding, outcome="defer", rationale="out of scope")],
        )

        assert result.blocking is False
        assert result.must_fix == []

    def test_must_fix_dropped_keeps_blocking_true(self) -> None:
        """R3/R4 fail-safe: an undecided MUST_FIX must never stop blocking."""
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(_accepted(finding))

        result = apply_adjudication(verdict, [])

        assert result.accepted[0].disposition == "dropped"
        assert result.blocking is True
        assert result.must_fix == [finding]

    def test_review_deferred_recomputed(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(_accepted(finding))
        assert verdict.review.deferred == 0

        result = apply_adjudication(
            verdict,
            [_adjudication(finding, outcome="defer", rationale="out of scope")],
        )

        assert result.review.deferred == 1

    def test_review_baseline_fields_preserved_verbatim(self) -> None:
        """R3: the cycle-0 baseline is preserved, never recomputed."""
        must_fix_finding = _make_finding(severity="MUST_FIX")
        should_fix_finding = _make_finding(
            severity="SHOULD_FIX", line_start=20, line_end=20, evidence="slow = True"
        )
        rejected_finding = _make_finding(
            severity="SHOULD_FIX", line_start=30, line_end=30, evidence="x = 1"
        )
        untouched_finding = _make_finding(
            severity="NIT", line_start=40, line_end=40, evidence="y = 2"
        )
        baseline = Review(
            must_fix_initial=7,
            should_fix=5,
            fix_cycles_used=3,
            deferred=0,
            agents_run=4,
            had_real_commit=True,
        )
        verdict = _verdict(
            _accepted(must_fix_finding),
            _accepted(should_fix_finding),
            _accepted(rejected_finding),
            _accepted(untouched_finding),
            review=baseline,
        )

        result = apply_adjudication(
            verdict,
            [
                _adjudication(must_fix_finding, outcome="fix"),
                _adjudication(
                    should_fix_finding, outcome="defer", rationale="scale-demand item"
                ),
                _adjudication(
                    rejected_finding, outcome="reject", rationale="deliberate choice"
                ),
            ],
        )

        assert result.review.must_fix_initial == 7
        assert result.review.should_fix == 5
        assert result.review.fix_cycles_used == 3
        assert result.review.agents_run == 4
        assert result.review.had_real_commit is True
        # Only `deferred` may change.
        assert result.review.deferred == 1


class TestApplyAdjudicationCollisionMatching:
    """R8: the collision that needs handling is 2+ findings, 1 entry."""

    @staticmethod
    def _colliding_pair() -> tuple[Finding, Finding]:
        one = _make_finding(
            severity="MUST_FIX",
            line_start=10,
            line_end=10,
            evidence="def broken():",
            summary="First finding here",
        )
        two = _make_finding(
            severity="MUST_FIX",
            line_start=10,
            line_end=10,
            evidence="    pass",
            summary="Second finding here",
        )
        return one, two

    def test_colliding_findings_share_one_entry_and_fall_back_to_unmatched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        one, two = self._colliding_pair()
        verdict = _verdict(_accepted(one), _accepted(two))
        entry = _adjudication(
            one,
            evidence="something neither finding quoted",
            outcome="reject",
            rationale="ambiguous anchor",
        )

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = apply_adjudication(verdict, [entry])

        assert [af.disposition for af in result.accepted] == ["dropped", "dropped"]
        assert all(af.disposition_detail == _NO_ENTRY_DETAIL for af in result.accepted)
        assert result.unmatched_adjudication_count == 1
        assert any(
            "adjudication entry did not match" in rec.getMessage()
            for rec in caplog.records
        )

    def test_one_entry_resolved_by_evidence_tiebreak_among_colliding_findings(
        self,
    ) -> None:
        one, two = self._colliding_pair()
        verdict = _verdict(_accepted(one), _accepted(two))
        entry = _adjudication(two, outcome="defer", rationale="out of scope for now")

        result = apply_adjudication(verdict, [entry])

        assert result.accepted[0].disposition == "dropped"
        assert result.accepted[0].disposition_detail == _NO_ENTRY_DETAIL
        assert result.accepted[1].disposition == "deferred"
        assert result.accepted[1].disposition_detail == "out of scope for now"
        assert result.unmatched_adjudication_count == 0


class TestVerifyFixedDispositions:
    def test_verify_fixed_dispositions_downgrades_untouched_fixed_finding(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        finding = _make_finding(file="src/cw/other.py", line_start=10, line_end=10)
        verdict = _verdict(_accepted(finding, disposition="fixed"))
        fix_diff = _make_diff(files={"src/cw/foo.py": [10]})

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = verify_fixed_dispositions(verdict, fix_diff)

        assert result.accepted[0].disposition == "dropped"
        assert "src/cw/other.py" in result.accepted[0].disposition_detail
        assert any(
            "downgraded 'fixed' disposition" in rec.getMessage()
            for rec in caplog.records
        )

    def test_verify_fixed_dispositions_retains_touched_fixed_finding(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        finding = _make_finding(file="src/cw/foo.py", line_start=10, line_end=10)
        verdict = _verdict(_accepted(finding, disposition="fixed"))
        fix_diff = _make_diff(files={"src/cw/foo.py": [10]})

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = verify_fixed_dispositions(verdict, fix_diff)

        assert result.accepted[0].disposition == "fixed"
        assert result.accepted[0].disposition_detail == ""
        assert caplog.records == []

    def test_verify_fixed_dispositions_retains_touched_file_level_finding(self) -> None:
        finding = _make_finding(file="src/cw/foo.py", line_start=None, line_end=None)
        verdict = _verdict(_accepted(finding, disposition="fixed"))

        result = verify_fixed_dispositions(
            verdict, _make_diff(files={"src/cw/foo.py": [10]})
        )

        assert result.accepted[0].disposition == "fixed"

    def test_verify_fixed_dispositions_downgrades_untouched_file_level_finding(
        self,
    ) -> None:
        finding = _make_finding(file="src/cw/other.py", line_start=None, line_end=None)
        verdict = _verdict(_accepted(finding, disposition="fixed"))

        result = verify_fixed_dispositions(
            verdict, _make_diff(files={"src/cw/foo.py": [10]})
        )

        assert result.accepted[0].disposition == "dropped"

    @pytest.mark.parametrize("disposition", ["deferred", "rejected", "dropped"])
    def test_verify_fixed_dispositions_ignores_non_fixed_dispositions(
        self, disposition: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        finding = _make_finding(file="src/cw/other.py", line_start=10, line_end=10)
        verdict = _verdict(
            _accepted(finding, disposition=disposition, disposition_detail="kept")
        )
        fix_diff = _make_diff(files={"src/cw/foo.py": [10]})

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = verify_fixed_dispositions(verdict, fix_diff)

        assert result.accepted[0].disposition == disposition
        assert result.accepted[0].disposition_detail == "kept"
        assert caplog.records == []


# Split across two source lines only to stay under the 88-column ruff limit;
# the rendered artifact is a single line (asserted byte-for-byte below).
_MD_PROVENANCE_COMMENT = (
    "<!-- written by Stage 3 (auto-dev-review.md), consumed by "
    "Stage 4 Step 4d (auto-dev-finalize.md) -->"
)

_RENDERED_BOTH = f"""# Deferred Review Findings
{_MD_PROVENANCE_COMMENT}

## Review adjudication

Rejected (intentional / documented tradeoff):
- src/cw/foo.py — "Bug here" — deliberate tradeoff, documented inline

<!-- DEFERRED-REVIEW-FINDINGS
- severity: SHOULD_FIX
  summary: "Slow loop"
  file: src/cw/bar.py
  rationale: "handle when scale demands"
DEFERRED-REVIEW-FINDINGS -->
"""


def _rejected_adj() -> Adjudication:
    return Adjudication(
        severity="MUST_FIX",
        file="src/cw/foo.py",
        line_start=10,
        line_end=10,
        summary="Bug here",
        outcome="reject",
        rationale="deliberate tradeoff, documented inline",
    )


def _deferred_adj() -> Adjudication:
    return Adjudication(
        severity="SHOULD_FIX",
        file="src/cw/bar.py",
        line_start=20,
        line_end=20,
        summary="Slow loop",
        outcome="defer",
        rationale="handle when scale demands",
    )


class TestRenderDeferredFindingsMd:
    def test_render_deferred_findings_md_matches_documented_shape(self) -> None:
        rendered = render_deferred_findings_md([_rejected_adj(), _deferred_adj()])
        assert rendered == _RENDERED_BOTH

    def test_rejected_section_omitted_when_no_rejections(self) -> None:
        rendered = render_deferred_findings_md([_deferred_adj()])
        assert "Rejected (intentional / documented tradeoff):" not in rendered
        assert "DEFERRED-REVIEW-FINDINGS" in rendered

    def test_deferred_block_omitted_when_no_deferrals(self) -> None:
        rendered = render_deferred_findings_md([_rejected_adj()])
        assert "DEFERRED-REVIEW-FINDINGS" not in rendered
        assert "Rejected (intentional / documented tradeoff):" in rendered

    def test_all_fixed_renders_nothing(self) -> None:
        adj = Adjudication(
            severity="MUST_FIX",
            file="src/cw/foo.py",
            line_start=10,
            line_end=10,
            summary="Bug here",
            outcome="fix",
        )
        assert render_deferred_findings_md([adj]) == ""
        assert render_deferred_findings_md([]) == ""

    def test_render_deferred_findings_md_reconciles_with_stamped_verdict(self) -> None:
        """AC2: the rendered markdown and the stamped verdict agree."""
        rejected_finding = _make_finding(
            severity="MUST_FIX",
            file="src/cw/foo.py",
            line_start=10,
            line_end=10,
            summary="Bug here",
        )
        deferred_finding = _make_finding(
            severity="SHOULD_FIX",
            file="src/cw/bar.py",
            line_start=20,
            line_end=20,
            summary="Slow loop",
            evidence="slow = True",
        )
        adjudications = [
            _adjudication(
                rejected_finding,
                outcome="reject",
                rationale="deliberate tradeoff, documented inline",
            ),
            _adjudication(
                deferred_finding,
                outcome="defer",
                rationale="handle when scale demands",
            ),
        ]
        verdict = _verdict(_accepted(rejected_finding), _accepted(deferred_finding))

        stamped = apply_adjudication(verdict, adjudications)
        rendered = render_deferred_findings_md(adjudications)

        expected_disposition = {"reject": "rejected", "defer": "deferred"}
        by_location = {
            (af.finding.file, af.finding.summary): af for af in stamped.accepted
        }
        for adj in adjudications:
            assert adj.rationale in rendered
            assert adj.file in rendered
            assert adj.summary in rendered
            matched = by_location[(adj.file, adj.summary)]
            assert matched.disposition == expected_disposition[adj.outcome]
            assert matched.disposition_detail == adj.rationale
