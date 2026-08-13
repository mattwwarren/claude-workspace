"""Tests for the Claude-native adjudication seam (GitHub #1805).

1:1 with ``src/cw/review_adjudication.py`` per the CLAUDE.md Testing
convention. Reuses ``tests/conftest.py``'s ``_make_finding``/``_make_diff``
fixtures rather than re-declaring equivalents.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from cw.auto_dev_result import Review
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_adjudication import (
    Adjudication,
    VoidedFinding,
    apply_adjudication,
    apply_voided_suppression,
    find_voided_matches,
    parse_voided_findings_block,
    render_deferred_findings_md,
    render_voided_findings_block,
    verify_fixed_dispositions,
)
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

from .conftest import _make_diff, _make_finding

_LOGGER = "cw.review_adjudication"
_TICKET = "T-1814"


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


def _make_voided_finding(**overrides: object) -> VoidedFinding:
    """Minimal-but-valid :class:`VoidedFinding` with keyword overrides (#1814).

    Defaults line up with ``conftest._finding_kwargs`` so
    ``_make_voided_finding()`` matches ``_make_finding()`` under
    :func:`find_voided_matches` with no argument juggling at the call site.

    Deliberately defined here rather than in ``tests/conftest.py``: unlike
    ``_make_finding``/``_make_reviewer_doc``/``_make_diff``, ``VoidedFinding``
    is one ticket's machinery, not a source type every test file needs. The
    consumer test files import it from this module, the same cross-file helper
    idiom ``tests/_codex_review_helpers.py`` already establishes.
    """
    kwargs: dict[str, object] = {
        "severity": "MUST_FIX",
        "file": "src/cw/foo.py",
        "summary": "Bug here",
        "evidence": "def broken():",
        "operator_comment_id": "mattwwarren@2026-08-11T02:43:30Z",
        "operator_comment_excerpt": "this is intentional, do not re-raise it",
        "voided_at": "2026-08-11T02:43:30Z",
        "original_rationale": "deliberate design choice, documented in the ADR",
    }
    kwargs.update(overrides)
    return VoidedFinding.model_validate(kwargs)


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

    def test_downgrade_of_a_must_fix_leaves_blocking_and_must_fix_stale(self) -> None:
        """Record-only (#1805 R1): a downgrade never re-opens the gate.

        Realistic pipeline shape: apply_adjudication stamps a MUST_FIX
        'fixed' (blocking becomes False), then verify_fixed_dispositions
        downgrades it to 'dropped' because the fix-cycle diff never touched
        it. blocking/must_fix are deliberately left as apply_adjudication set
        them -- verify_fixed_dispositions relabels the record, it does not
        recompute the gate. The caller (auto-dev-review.md Step 3c) is
        responsible for surfacing the downgrade via friction_highlights.
        """
        finding = _make_finding(
            severity="MUST_FIX", file="src/cw/other.py", line_start=10, line_end=10
        )
        adjudicated = apply_adjudication(
            _verdict(_accepted(finding)), [_adjudication(finding, outcome="fix")]
        )
        assert adjudicated.blocking is False
        assert adjudicated.must_fix == []

        result = verify_fixed_dispositions(
            adjudicated, _make_diff(files={"src/cw/unrelated.py": [1]})
        )

        assert result.accepted[0].disposition == "dropped"
        assert result.blocking is False
        assert result.must_fix == []


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


def _no_diff_anchor_finding(**overrides: object) -> Finding:
    """A #1817 marker finding: no diff artifact, fixed ``"N/A"`` file literal."""
    kwargs: dict[str, object] = {
        "severity": "MUST_FIX",
        "no_diff_anchor": True,
        "file": "N/A",
        "line_start": None,
        "line_end": None,
        "summary": "AC3's follow-up ticket was never filed",
        "evidence": "AC3: a follow-up ticket must exist before this ships",
    }
    kwargs.update(overrides)
    return _make_finding(**kwargs)


_OPERATOR_RATIONALE = (
    "acceptance criterion 3 requires a follow-up ticket for the deferred "
    "migration; none exists — operator must file it (or link the one that "
    "discharges it) before this ticket can ship"
)


class TestOperatorActionOutcome:
    """#1817: the OPERATOR ACTIONABLE route for a non-diff-anchorable MUST_FIX."""

    def test_operator_action_outcome_requires_rationale(self) -> None:
        finding = _no_diff_anchor_finding()
        with pytest.raises(ValidationError):
            _adjudication(finding, outcome="operator_action", rationale="   ")
        # Non-blank passes — the same `outcome != "fix"` gate, no new code.
        entry = _adjudication(
            finding, outcome="operator_action", rationale=_OPERATOR_RATIONALE
        )
        assert entry.outcome == "operator_action"

    def test_operator_action_outcome_requires_must_fix_severity(self) -> None:
        """Decision C2: OPERATOR ACTIONABLE is MUST_FIX-scoped at the model.

        A SHOULD_FIX ``no_diff_anchor`` finding routes through ordinary DEFER;
        it can never legally serialize into an ``operator_action`` entry.
        """
        should_fix = _no_diff_anchor_finding(severity="SHOULD_FIX")
        with pytest.raises(ValidationError):
            _adjudication(
                should_fix, outcome="operator_action", rationale=_OPERATOR_RATIONALE
            )
        for severity in ("NIT", "PRINCIPLE"):
            with pytest.raises(ValidationError):
                _adjudication(
                    _no_diff_anchor_finding(severity=severity),
                    outcome="operator_action",
                    rationale=_OPERATOR_RATIONALE,
                )

    def test_operator_action_outcome_requires_na_file_and_no_line_anchor(
        self,
    ) -> None:
        """A coordinating-session typo on file/line must fail fast (#1817 review).

        Mirrors ``Finding``'s own ``_check_no_diff_anchor_file_is_na``: an
        ``operator_action`` entry is copied straight off a ``no_diff_anchor``
        finding, which is itself model-pinned to ``file="N/A"`` with no line
        anchor — a mismatched entry here would fail the location-key match
        against its ``Finding`` and silently fall back to ``"dropped"``.
        """
        finding = _no_diff_anchor_finding()
        with pytest.raises(ValidationError):
            _adjudication(
                finding,
                outcome="operator_action",
                rationale=_OPERATOR_RATIONALE,
                file="src/cw/review_findings.py",
            )
        with pytest.raises(ValidationError):
            _adjudication(
                finding,
                outcome="operator_action",
                rationale=_OPERATOR_RATIONALE,
                line_start=5,
            )
        with pytest.raises(ValidationError):
            _adjudication(
                finding,
                outcome="operator_action",
                rationale=_OPERATOR_RATIONALE,
                line_end=5,
            )
        # The correct shape still passes — the same one every other test here
        # already constructs via _no_diff_anchor_finding()'s defaults.
        entry = _adjudication(
            finding, outcome="operator_action", rationale=_OPERATOR_RATIONALE
        )
        assert entry.file == "N/A"
        assert entry.line_start is None
        assert entry.line_end is None

    def test_apply_adjudication_operator_action_does_not_block(self) -> None:
        finding = _no_diff_anchor_finding()
        verdict = _verdict(_accepted(finding))
        assert verdict.blocking is True

        result = apply_adjudication(
            verdict,
            [
                _adjudication(
                    finding, outcome="operator_action", rationale=_OPERATOR_RATIONALE
                )
            ],
        )

        assert result.accepted[0].disposition == "operator_actionable"
        assert result.accepted[0].disposition_detail == _OPERATOR_RATIONALE
        assert result.blocking is False
        assert result.must_fix == []
        assert result.unmatched_adjudication_count == 0

    def test_unadjudicated_no_diff_anchor_must_fix_still_blocks_as_dropped(
        self,
    ) -> None:
        """AC4: #1714's no-silent-drop guarantee composes with the new marker."""
        finding = _no_diff_anchor_finding()
        verdict = _verdict(_accepted(finding))

        result = apply_adjudication(verdict, [])

        assert result.accepted[0].disposition == "dropped"
        assert result.accepted[0].disposition_detail == _NO_ENTRY_DETAIL
        assert result.blocking is True
        assert result.must_fix == [finding]

    def test_apply_adjudication_composes_all_four_outcomes_in_one_pass(self) -> None:
        fixed = _make_finding(severity="MUST_FIX", line_start=10, line_end=10)
        rejected = _make_finding(
            severity="SHOULD_FIX",
            file="src/cw/bar.py",
            line_start=20,
            line_end=20,
            summary="Rejected one",
        )
        deferred = _make_finding(
            severity="SHOULD_FIX",
            file="src/cw/baz.py",
            line_start=30,
            line_end=30,
            summary="Deferred one",
        )
        operator = _no_diff_anchor_finding()
        dropped = _make_finding(
            severity="MUST_FIX",
            file="src/cw/qux.py",
            line_start=40,
            line_end=40,
            summary="Nobody decided this one",
        )
        verdict = _verdict(
            _accepted(fixed),
            _accepted(rejected),
            _accepted(deferred),
            _accepted(operator),
            _accepted(dropped),
        )
        stale = _adjudication(
            _make_finding(
                severity="MUST_FIX",
                file="src/cw/gone.py",
                line_start=99,
                line_end=99,
            ),
            outcome="reject",
            rationale="stale anchor, matches nothing",
        )

        result = apply_adjudication(
            verdict,
            [
                _adjudication(fixed),
                _adjudication(rejected, outcome="reject", rationale="deliberate"),
                _adjudication(deferred, outcome="defer", rationale="out of scope"),
                _adjudication(
                    operator,
                    outcome="operator_action",
                    rationale=_OPERATOR_RATIONALE,
                ),
                stale,
            ],
        )

        assert [af.disposition for af in result.accepted] == [
            "fixed",
            "rejected",
            "deferred",
            "operator_actionable",
            "dropped",
        ]
        assert result.unmatched_adjudication_count == 1
        # Only the genuinely undecided MUST_FIX still blocks.
        assert result.must_fix == [dropped]
        assert result.blocking is True

    def test_render_deferred_findings_md_ignores_operator_action_entries(self) -> None:
        """``operator_action`` posts to the ticket, never to the PR body."""
        entry = Adjudication(
            severity="MUST_FIX",
            file="N/A",
            summary="AC3's follow-up ticket was never filed",
            outcome="operator_action",
            rationale=_OPERATOR_RATIONALE,
        )
        assert render_deferred_findings_md([entry]) == ""

        rendered = render_deferred_findings_md([entry, _deferred_adj()])
        assert "DEFERRED-REVIEW-FINDINGS" in rendered
        assert "AC3's follow-up ticket was never filed" not in rendered


class TestCoexistsWithTerminalSnapshot:
    """#1763's ``is_terminal_snapshot`` must survive this seam untouched.

    Both tickets add fields to ``ReviewVerdict``; they are additive and
    independent (#1763 marks which persisted snapshot is authoritative, #1805
    records adjudication disposition). Neither function here has any business
    re-deciding a snapshot's terminality, so both must pass whatever value
    they were handed straight through. ``model_copy(update=...)`` gives that
    for free — these tests exist so a future refactor to a direct
    ``ReviewVerdict(...)`` constructor (which would silently take the
    ``True`` default) fails loudly instead of mislabelling a cycle snapshot.
    """

    @pytest.mark.parametrize("terminal", [True, False])
    def test_apply_adjudication_preserves_is_terminal_snapshot(
        self, terminal: bool
    ) -> None:
        finding = _make_finding(severity="SHOULD_FIX")
        verdict = _verdict(_accepted(finding), is_terminal_snapshot=terminal)
        assert verdict.is_terminal_snapshot is terminal

        result = apply_adjudication(
            verdict,
            [_adjudication(finding, outcome="defer", rationale="out of scope")],
        )

        assert result.is_terminal_snapshot is terminal
        # The #1805 field is stamped on the same object, not instead of it.
        assert result.accepted[0].disposition == "deferred"
        assert result.unmatched_adjudication_count == 0

    @pytest.mark.parametrize("terminal", [True, False])
    def test_verify_fixed_dispositions_preserves_is_terminal_snapshot(
        self, terminal: bool
    ) -> None:
        finding = _make_finding(file="src/cw/other.py", line_start=10, line_end=10)
        verdict = _verdict(
            _accepted(finding, disposition="fixed"), is_terminal_snapshot=terminal
        )

        result = verify_fixed_dispositions(
            verdict, _make_diff(files={"src/cw/foo.py": [10]})
        )

        assert result.is_terminal_snapshot is terminal
        assert result.accepted[0].disposition == "dropped"

    def test_consolidate_shaped_verdict_defaults_to_terminal_and_zero_unmatched(
        self,
    ) -> None:
        """The one direct ``ReviewVerdict(...)`` construction site's shape.

        ``consolidate_verdict`` passes neither field, so both take their
        defaults: ``is_terminal_snapshot=True`` (a freshly consolidated verdict
        IS its pass's outcome, per #1763's docstring) and
        ``unmatched_adjudication_count=0`` (nothing has been adjudicated yet).
        """
        verdict = _verdict(_accepted(_make_finding(severity="SHOULD_FIX")))

        assert verdict.is_terminal_snapshot is True
        assert verdict.unmatched_adjudication_count == 0


class TestVoidedFingerprintIsContentAnchored:
    """#1814: identity is (severity, file, summary, evidence) — never lines."""

    def test_same_location_different_content_is_not_suppressed(self) -> None:
        """Acceptance criterion (a): a genuinely NEW finding at a voided
        finding's file+line is never suppressed."""
        fresh = _make_finding(
            summary="A different bug entirely",
            evidence="def also_broken():",
            line_start=10,
            line_end=10,
        )
        # VoidedFinding carries no line anchor at all — that absence IS the
        # zero-false-positive guarantee, not an omission.
        assert find_voided_matches([_accepted(fresh)], [_make_voided_finding()]) == {}

    def test_same_content_different_file_is_not_suppressed(self) -> None:
        moved_file = _make_finding(file="src/cw/bar.py")

        assert (
            find_voided_matches([_accepted(moved_file)], [_make_voided_finding()]) == {}
        )

    def test_same_content_different_lines_is_suppressed(self) -> None:
        """Line drift (#1715-style code motion) must not break the anchor."""
        drifted = _make_finding(line_start=417, line_end=419)

        matches = find_voided_matches([_accepted(drifted)], [_make_voided_finding()])

        assert list(matches) == [0]

    def test_evidence_whitespace_is_normalized(self) -> None:
        matches = find_voided_matches(
            [_accepted(_make_finding())],
            [_make_voided_finding(evidence="  def  broken():  ")],
        )

        assert list(matches) == [0]

    def test_evidence_leading_marker_is_not_stripped(self) -> None:
        """A leading ``+``/``-`` in evidence is real content, not stripped.

        Evidence is arbitrary reviewed source text (Markdown, YAML, diffs of
        diffs), not exclusively diff-hunk lines — a leading ``-``/``+`` can be
        a genuine part of the code (a YAML list item, a negative literal), not
        a diff marker. Stripping it risked collapsing two different findings
        onto the same fingerprint, which is exactly the false positive this
        ticket's zero-tolerance decision forbids (#1814 MUST_FIX). Same
        principle ``test_summary_whitespace_is_normalized_but_markers_are_not_stripped``
        already applies to ``summary``, now applied identically to ``evidence``.
        """
        assert (
            find_voided_matches(
                [_accepted(_make_finding(evidence="+def broken():"))],
                [_make_voided_finding(evidence="def broken():")],
            )
            == {}
        )
        assert (
            find_voided_matches(
                [_accepted(_make_finding(evidence="-def broken():"))],
                [_make_voided_finding(evidence="def broken():")],
            )
            == {}
        )

    def test_summary_whitespace_is_normalized_but_markers_are_not_stripped(
        self,
    ) -> None:
        """Prose keeps its leading ``+``/``-``: only whitespace is collapsed."""
        assert list(
            find_voided_matches(
                [_accepted(_make_finding(summary="Bug  here"))],
                [_make_voided_finding(summary=" Bug here ")],
            )
        ) == [0]
        assert (
            find_voided_matches(
                [_accepted(_make_finding(summary="-Bug here"))],
                [_make_voided_finding(summary="Bug here")],
            )
            == {}
        )

    def test_different_severity_is_not_suppressed(self) -> None:
        should_fix = _make_finding(severity="SHOULD_FIX")

        assert (
            find_voided_matches([_accepted(should_fix)], [_make_voided_finding()]) == {}
        )


class TestApplyVoidedSuppression:
    """#1814: the shared suppression seam both backends consult."""

    def test_matched_must_fix_is_rejected_and_stops_blocking(self) -> None:
        voided_finding = _make_finding()
        live_finding = _make_finding(
            summary="A real, unadjudicated bug", evidence="def also_broken():"
        )
        verdict = _verdict(_accepted(voided_finding), _accepted(live_finding))

        suppressed, adjudications = apply_voided_suppression(
            verdict, [_make_voided_finding()], ticket_id=_TICKET
        )

        assert suppressed.blocking is True
        assert suppressed.must_fix == [live_finding]
        assert suppressed.accepted[0].disposition == "rejected"
        assert "mattwwarren@2026-08-11T02:43:30Z" in (
            suppressed.accepted[0].disposition_detail
        )
        assert suppressed.accepted[1].disposition == "fixed"
        assert len(adjudications) == 1
        assert adjudications[0].outcome == "reject"
        assert adjudications[0].rationale.strip()
        assert adjudications[0].summary == voided_finding.summary

    def test_sole_must_fix_voided_clears_blocking(self) -> None:
        verdict = _verdict(_accepted(_make_finding()))

        suppressed, adjudications = apply_voided_suppression(
            verdict, [_make_voided_finding()], ticket_id=_TICKET
        )

        assert suppressed.blocking is False
        assert suppressed.must_fix == []
        assert len(adjudications) == 1

    def test_no_matches_returns_verdict_unchanged(self) -> None:
        verdict = _verdict(_accepted(_make_finding()))

        suppressed, adjudications = apply_voided_suppression(
            verdict,
            [_make_voided_finding(file="src/cw/unrelated.py")],
            ticket_id=_TICKET,
        )

        assert adjudications == []
        assert suppressed == verdict
        assert suppressed.accepted[0].disposition == "fixed"

    def test_empty_voided_list_returns_verdict_unchanged(self) -> None:
        verdict = _verdict(_accepted(_make_finding()))

        suppressed, adjudications = apply_voided_suppression(
            verdict, [], ticket_id=_TICKET
        )

        assert adjudications == []
        assert suppressed == verdict

    def test_frozen_baseline_fields_preserved_verbatim(self) -> None:
        """Mirrors ``apply_adjudication``'s own frozen-cycle-0 contract (R3)."""
        verdict = _verdict(
            _accepted(_make_finding()),
            _accepted(_make_finding(severity="SHOULD_FIX", summary="Style nit")),
        )

        suppressed, _adjudications = apply_voided_suppression(
            verdict, [_make_voided_finding()], ticket_id=_TICKET
        )

        assert suppressed.review.must_fix_initial == verdict.review.must_fix_initial
        assert suppressed.review.should_fix == verdict.review.should_fix
        assert suppressed.review.agents_run == verdict.review.agents_run
        assert suppressed.review.deferred == verdict.review.deferred
        assert suppressed.is_terminal_snapshot == verdict.is_terminal_snapshot

    def test_suppression_emits_one_event_per_match(self) -> None:
        verdict = _verdict(_accepted(_make_finding()))

        apply_voided_suppression(verdict, [_make_voided_finding()], ticket_id=_TICKET)

        events = read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED])
        assert len(events) == 1
        assert events[0].correlation_id == _TICKET
        assert events[0].payload["file"] == "src/cw/foo.py"
        assert events[0].payload["severity"] == "MUST_FIX"
        assert (
            events[0].payload["operator_comment_id"]
            == "mattwwarren@2026-08-11T02:43:30Z"
        )
        assert events[0].payload["voided_at"] == "2026-08-11T02:43:30Z"
        assert events[0].payload["original_rationale"] == (
            "deliberate design choice, documented in the ADR"
        )

    def test_no_match_emits_no_event(self) -> None:
        verdict = _verdict(_accepted(_make_finding()))

        apply_voided_suppression(
            verdict, [_make_voided_finding(file="src/cw/other.py")], ticket_id=_TICKET
        )

        assert (
            read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED]) == []
        )


class TestVoidedFindingsBlockRoundTrip:
    """#1814: the ticket-comment JSON sentinel is machine-parsed, not prose."""

    def test_render_parse_round_trips(self) -> None:
        entries = [
            _make_voided_finding(),
            _make_voided_finding(
                severity="SHOULD_FIX",
                file="src/cw/bar.py",
                summary="Second void",
                evidence="def other():",
            ),
        ]

        assert parse_voided_findings_block([render_voided_findings_block(entries)]) == (
            entries
        )

    def test_render_embeds_its_own_header(self) -> None:
        rendered = render_voided_findings_block([_make_voided_finding()])

        assert rendered.startswith("## Voided Review Findings")
        assert "VOIDED-REVIEW-FINDINGS" in rendered

    def test_render_empty_list_writes_nothing(self) -> None:
        assert render_voided_findings_block([]) == ""

    def test_distinct_entries_across_comments_are_unioned(self) -> None:
        first = _make_voided_finding()
        second = _make_voided_finding(summary="Second void", evidence="def other():")

        parsed = parse_voided_findings_block(
            [
                f"prose\n\n{render_voided_findings_block([first])}",
                f"other prose\n\n{render_voided_findings_block([second])}",
            ]
        )

        assert parsed == [first, second]

    def test_repeated_entry_across_comments_is_deduplicated(self) -> None:
        entry = _make_voided_finding()
        body = render_voided_findings_block([entry])

        assert parse_voided_findings_block([body, body]) == [entry]

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "no sentinel here at all",
            "<!-- VOIDED-REVIEW-FINDINGS\n{not json\nVOIDED-REVIEW-FINDINGS -->",
            "<!-- VOIDED-REVIEW-FINDINGS\n{}\nVOIDED-REVIEW-FINDINGS -->",
            # Valid JSON, wrong top-level shape (a bare list, not the
            # {"schema_version", "voided"} envelope).
            '<!-- VOIDED-REVIEW-FINDINGS\n["not", "an", "envelope"]\n'
            "VOIDED-REVIEW-FINDINGS -->",
            '<!-- VOIDED-REVIEW-FINDINGS\n{"voided": "not a list"}\n'
            "VOIDED-REVIEW-FINDINGS -->",
            '<!-- VOIDED-REVIEW-FINDINGS\n{"voided": [{"file": ""}]}\n'
            "VOIDED-REVIEW-FINDINGS -->",
            "<!-- VOIDED-REVIEW-FINDINGS\ntruncated with no closing",
        ],
    )
    def test_malformed_or_absent_sentinel_degrades_to_empty(self, body: str) -> None:
        assert parse_voided_findings_block([body]) == []

    def test_one_malformed_block_does_not_discard_a_valid_sibling(self) -> None:
        entry = _make_voided_finding()
        parsed = parse_voided_findings_block(
            [
                "<!-- VOIDED-REVIEW-FINDINGS\n{not json\nVOIDED-REVIEW-FINDINGS -->",
                render_voided_findings_block([entry]),
            ]
        )

        assert parsed == [entry]


class TestVoidedAndOrdinaryAdjudicationsCompose:
    """#1814/#1816/#1817 compose check — whichever of the three lands last.

    Re-verified at implementation time: #1816 landed as a test-only change to
    ``tests/test_review_findings.py`` and #1817 is still open, so neither
    sibling has contributed a compose test here and #1814 owns it.

    The specific coexistence risk: ``apply_voided_suppression`` auto-generates
    ``reject`` entries that the coordinating session then appends to its OWN
    ``ADJUDICATIONS`` array alongside its ordinary ``fix``/``defer`` entries.
    If the two erased each other, one finding's outcome would silently take
    the other's — the exact "two records disagree" defect this whole sprint
    has been closing.
    """

    def test_auto_generated_reject_and_ordinary_fix_coexist(self) -> None:
        voided_finding = _make_finding()
        fixed_finding = _make_finding(
            summary="A real bug the session fixed",
            evidence="def also_broken():",
            line_start=20,
            line_end=20,
        )
        verdict = _verdict(_accepted(voided_finding), _accepted(fixed_finding))

        suppressed, auto = apply_voided_suppression(
            verdict, [_make_voided_finding()], ticket_id=_TICKET
        )
        adjudications = [*auto, _adjudication(fixed_finding, outcome="fix")]
        stamped = apply_adjudication(suppressed, adjudications)

        assert stamped.accepted[0].disposition == "rejected"
        assert stamped.accepted[1].disposition == "fixed"
        assert stamped.blocking is False
        assert stamped.must_fix == []
        assert stamped.unmatched_adjudication_count == 0
