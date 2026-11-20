"""Evidence-gap holds through the verdict, dedup, carry, seed and render (#3793)."""

from __future__ import annotations

from cw.codex_fix_loop.convergence import _carry_held_findings, _open_finding_key
from cw.codex_fix_loop.seed import _should_seed
from cw.codex_review import make_codex_blocked
from cw.codex_review._verdict._render.core import render_verdict_comment
from cw.codex_review._verdict._synthesis import _verdict_block_reason
from cw.review_findings import (
    AcceptedFinding,
    Finding,
    ReviewVerdict,
    consolidate_verdict,
    stamp_held_findings,
)
from cw.review_findings._dedup import _pick_representative
from tests._shared.review_builders import (
    _make_diff,
    _make_finding,
    _make_gap_finding,
    _make_reviewer_doc,
)

_QUOTE = "the approved plan says keep the old API"
_CODEX_MUST_FIX = "codex_must_fix_findings"
_OPERATOR_ACTIONABLE = "review_operator_actionable"


def _stamped(*findings: Finding) -> ReviewVerdict:
    diff = _make_diff("def broken():", "def other():", "def third():")
    verdict = consolidate_verdict(
        [_make_reviewer_doc(*findings)], diff, reviewed_sha="sha"
    )
    return stamp_held_findings(verdict)


class TestBlockReason:
    def test_pure_gap_parks_as_operator_actionable(self) -> None:
        assert (
            _verdict_block_reason(_stamped(_make_gap_finding())) == _OPERATOR_ACTIONABLE
        )

    def test_mixed_gap_and_unmarked_routes_to_repair(self) -> None:
        verdict = _stamped(
            _make_gap_finding(evidence="def broken():"),
            _make_finding(evidence="def other():", summary="reproduced bug"),
        )
        assert verdict.blocking is True
        assert _verdict_block_reason(verdict) == _CODEX_MUST_FIX

    def test_contest_beside_gap_parks_whole_verdict(self) -> None:
        verdict = _stamped(
            _make_gap_finding(evidence="def broken():"),
            _make_gap_finding(
                evidence="def other():", summary="c", contests_decision=_QUOTE
            ),
        )
        assert _verdict_block_reason(verdict) == _OPERATOR_ACTIONABLE

    def test_unmarked_alone_is_unchanged(self) -> None:
        verdict = _stamped(_make_finding())
        assert _verdict_block_reason(verdict) == _CODEX_MUST_FIX


class TestRenderGapHeadline:
    def test_pure_gap_renders_evidence_block_not_clean(self) -> None:
        body = render_verdict_comment(
            _stamped(_make_gap_finding()), fix_loop_enabled=True
        )
        assert "OPERATOR EVIDENCE REQUIRED — 1 finding(s)" in body
        assert "**BLOCKING**" not in body
        assert "evidence gap" in body

    def test_mixed_renders_blocking_and_evidence_block(self) -> None:
        verdict = _stamped(
            _make_gap_finding(evidence="def broken():"),
            _make_finding(evidence="def other():", summary="reproduced bug"),
        )
        body = render_verdict_comment(verdict, fix_loop_enabled=True)
        assert "**BLOCKING**" in body
        assert "OPERATOR EVIDENCE REQUIRED" in body
        assert "the fix loop's to repair" in body


class TestDedupPrefersUnmarked:
    def test_mixed_group_keeps_unmarked_representative(self) -> None:
        marked = _make_gap_finding()
        unmarked = _make_finding()
        rep = _pick_representative([("a_reviewer", marked), ("z_reviewer", unmarked)])
        assert rep.evidence_gap == ""

    def test_all_marked_group_keeps_marked(self) -> None:
        rep = _pick_representative(
            [("a", _make_gap_finding()), ("b", _make_gap_finding())]
        )
        assert rep.evidence_gap != ""


class TestCarry:
    def test_gap_carried_when_not_reraised(self) -> None:
        gap = stamp_held_findings(_stamped(_make_gap_finding())).accepted[0]
        carried, verdict = _carry_held_findings({}, _empty_with(gap))
        assert _open_finding_key(gap.finding) in carried
        assert any(af.finding == gap.finding for af in verdict.accepted)

    def test_carry_returns_same_verdict_when_nothing_carried(self) -> None:
        verdict = _stamped(_make_finding())
        carried, out = _carry_held_findings({}, verdict)
        assert carried == {}
        assert out is verdict

    def test_settled_gap_is_dropped(self) -> None:
        gap = stamp_held_findings(_stamped(_make_gap_finding())).accepted[0]
        carried, _ = _carry_held_findings({}, _empty_with(gap))
        settled = gap.model_copy(update={"disposition": "rejected"})
        carried, _ = _carry_held_findings(carried, _empty_with(settled))
        assert carried == {}


def _empty_with(af: AcceptedFinding) -> ReviewVerdict:
    return ReviewVerdict.model_construct(
        accepted=[af],
        must_fix=[],
        blocking=False,
        reviewed_sha="sha",
        review=_stamped(_make_finding()).review,
        rejected_must_fix=[],
        rejected_count=0,
        rejected_count_by_severity={},
        run_failures_with_should_fix_discards=False,
    )


class TestSeedGate:
    def test_gap_only_parked_cycle0_still_seeds(self) -> None:
        verdict = _stamped(_make_gap_finding())
        result = make_codex_blocked(
            ticket_id="T", worktree=None, reason=_OPERATOR_ACTIONABLE, details="x"
        )
        seed = AcceptedFinding(finding=_make_finding(summary="seed"), reviewers=["r"])
        assert _should_seed(verdict, result, [seed], True) is True

    def test_contest_parked_cycle0_does_not_seed(self) -> None:
        verdict = _stamped(_make_gap_finding(contests_decision=_QUOTE))
        result = make_codex_blocked(
            ticket_id="T", worktree=None, reason=_OPERATOR_ACTIONABLE, details="x"
        )
        seed = AcceptedFinding(finding=_make_finding(summary="seed"), reviewers=["r"])
        assert _should_seed(verdict, result, [seed], True) is False
