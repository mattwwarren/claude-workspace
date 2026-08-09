"""Tests for cw.review_findings — executor-neutral structured finding contract.

Covers the model group (Finding/EscalationMetadata/ReviewerFindingsDocument/
ReviewVerdict and friends), the validation/dedup/aggregation functions, the
escalation strip-on-invalid-evidence rule, and the #1108 artifact writer.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from cw.review_findings import (
    AcceptedFinding,
    CapturedDiff,
    Finding,
    RejectedFinding,
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    ReviewerRunRecord,
    ReviewVerdict,
    StrippedEscalation,
    _anchor_in_enclosing_def,
    _classify_finding,
    _enclosing_def_span,
    _line_reference_valid,
    _select_rejected_must_fix,
    consolidate_verdict,
    dedupe_findings,
    derive_review_counts,
    validate_reviewer_document,
    write_review_verdict,
)
from tests.conftest import (
    _finding_kwargs,
    _make_diff,
    _make_escalation,
    _make_finding,
    _make_reviewer_doc,
)


class TestSeverityAndDispositionLiterals:
    def test_valid_severities_round_trip(self) -> None:
        for sev in ("MUST_FIX", "SHOULD_FIX", "NIT", "PRINCIPLE"):
            f = _make_finding(severity=sev)
            assert f.severity == sev

    def test_invalid_severity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_finding(severity="CRITICAL")

    def test_valid_dispositions_round_trip(self) -> None:
        for disp in ("fixed", "rejected", "deferred"):
            af = AcceptedFinding(
                finding=_make_finding(), reviewers=["r"], disposition=disp
            )
            assert af.disposition == disp

    def test_invalid_disposition_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AcceptedFinding(
                finding=_make_finding(), reviewers=["r"], disposition="wontfix"
            )


class TestRejectionReasonLiteral:
    def test_escalation_reason_is_valid_rejection_reason(self) -> None:
        # StrippedEscalation.reason accepts the escalation-only value.
        se = StrippedEscalation(
            reviewer_role="Security Reviewer",
            finding_index=0,
            target_reviewer="Performance Reviewer",
            reason="escalation_evidence_not_in_diff",
        )
        assert se.reason == "escalation_evidence_not_in_diff"

    def test_rejected_finding_never_uses_escalation_reason(self) -> None:
        # The escalation-only value is never produced on a RejectedFinding by
        # validate_reviewer_document — every rejected finding uses one of the
        # five core reasons.
        bad = Finding.model_construct(**_finding_kwargs(severity="BOGUS"))
        diff = _make_diff()
        _accepted, rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(bad), diff
        )
        assert rejected
        assert all(r.reason != "escalation_evidence_not_in_diff" for r in rejected)

    def test_rejected_finding_reason_rejects_escalation_value(self) -> None:
        # R6 (#1236): RejectedFinding.reason uses the split RejectedFindingReason
        # Literal, which excludes the escalation-only value.
        with pytest.raises(ValidationError):
            RejectedFinding(
                raw={},
                reviewer_role="R",
                reason="escalation_evidence_not_in_diff",
            )

    def test_stripped_escalation_reason_rejects_core_value(self) -> None:
        # R6 (#1236): StrippedEscalation.reason uses EscalationStripReason, which
        # excludes every core rejection reason.
        with pytest.raises(ValidationError):
            StrippedEscalation(
                reviewer_role="R",
                finding_index=0,
                target_reviewer="Perf Reviewer",
                reason="evidence_not_in_diff",
            )

    def test_unanchored_is_valid_rejection_reason_literal(self) -> None:
        # #1632: "unanchored" is a 6th RejectedFindingReason value. Normal
        # operation never constructs a RejectedFinding with it (validate_
        # reviewer_document routes it to accepted instead) — this only pins
        # the Literal itself accepts direct construction.
        rf = RejectedFinding(raw={}, reviewer_role="R", reason="unanchored")
        assert rf.reason == "unanchored"


class TestFindingValidation:
    def test_required_fields(self) -> None:
        f = _make_finding()
        assert f.file == "src/cw/foo.py"
        assert f.escalation is None

    def test_blank_evidence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_finding(evidence="   ")

    def test_blank_summary_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_finding(summary="")

    def test_line_end_before_line_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_finding(line_start=10, line_end=5)

    def test_line_end_equal_line_start_ok(self) -> None:
        f = _make_finding(line_start=10, line_end=10)
        assert f.line_end == 10

    def test_file_level_finding_null_lines_ok(self) -> None:
        f = _make_finding(line_start=None, line_end=None)
        assert f.line_start is None


class TestEscalationMetadata:
    def test_required_fields_round_trip(self) -> None:
        e = _make_escalation()
        assert e.target_reviewer
        assert e.evidence_quote

    def test_blank_target_reviewer_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_escalation(target_reviewer="  ")

    def test_blank_evidence_quote_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_escalation(evidence_quote="")

    def test_finding_escalation_defaults_none(self) -> None:
        assert _make_finding().escalation is None

    def test_finding_round_trips_escalation(self) -> None:
        esc = _make_escalation(target_reviewer="Perf Reviewer")
        f = _make_finding(escalation=esc)
        assert f.escalation is not None
        assert f.escalation.target_reviewer == "Perf Reviewer"


class TestReviewerFindingsDocument:
    def test_failed_status_with_findings_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument(
                reviewer_role="R",
                status="failed",
                detail="crashed",
                findings=[_make_finding()],
            )

    def test_degraded_status_may_carry_findings(self) -> None:
        doc = ReviewerFindingsDocument(
            reviewer_role="R",
            status="degraded",
            detail="partial",
            findings=[_make_finding()],
        )
        assert len(doc.findings) == 1

    def test_ok_clean_review_round_trips(self) -> None:
        doc = ReviewerFindingsDocument(
            reviewer_role="R", status="ok", detail="checked X; no issues.", findings=[]
        )
        assert doc.findings == []

    def test_make_reviewer_doc_default_still_valid(self) -> None:
        # Tripwire for the conftest fixture-default fix (#1544): the zero-arg
        # _make_reviewer_doc() call (status="ok", findings=[]) must keep
        # validating cleanly once the new ok/empty-findings justification
        # validator lands — if the conftest default regresses to a blank
        # detail, this fails immediately instead of scattering failures
        # across the ~56 other call sites that rely on it.
        doc = _make_reviewer_doc()
        assert doc.status == "ok"
        assert doc.findings == []


class TestReviewerFindingsDocumentOkJustification:
    """R6 (#1544): status='ok' + empty findings requires a non-blank detail."""

    def test_ok_empty_findings_blank_detail_rejected(self) -> None:
        with pytest.raises(ValidationError, match="degraded"):
            ReviewerFindingsDocument(
                reviewer_role="R", status="ok", detail="", findings=[]
            )

    def test_ok_empty_findings_whitespace_detail_rejected(self) -> None:
        # Proves _is_blank's .strip() semantics are actually invoked, not a
        # naive falsy/empty-string check.
        with pytest.raises(ValidationError, match="degraded"):
            ReviewerFindingsDocument(
                reviewer_role="R", status="ok", detail="   ", findings=[]
            )

    def test_ok_empty_findings_nonblank_detail_passes(self) -> None:
        doc = ReviewerFindingsDocument(
            reviewer_role="R",
            status="ok",
            detail="Checked X, Y, Z; no issues.",
            findings=[],
        )
        assert doc.detail == "Checked X, Y, Z; no issues."

    def test_ok_nonempty_findings_blank_detail_passes(self) -> None:
        # The justification rule only applies when findings is empty.
        doc = ReviewerFindingsDocument(
            reviewer_role="R", status="ok", detail="", findings=[_make_finding()]
        )
        assert doc.detail == ""

    def test_degraded_empty_findings_blank_detail_passes(self) -> None:
        # Regression lock for R2: degraded is exempt from the justification
        # requirement entirely, even with empty findings and blank detail.
        doc = ReviewerFindingsDocument(
            reviewer_role="R", status="degraded", detail="", findings=[]
        )
        assert doc.detail == ""

    def test_failed_status_unaffected_by_justification_check(self) -> None:
        # The new validator doesn't newly constrain "failed" — existing
        # _check_failed_has_no_findings behavior (failed + findings rejected)
        # is covered separately by test_failed_status_with_findings_rejected.
        doc = ReviewerFindingsDocument(
            reviewer_role="R", status="failed", detail="", findings=[]
        )
        assert doc.detail == ""


class TestReviewerFindingsDocumentNullNormalization:
    """A ``None`` detail/findings (from an OpenAI strict-schema nullable-wrapped
    field, #1364) normalizes to the same default a caller omitting the key
    would get, rather than failing type validation.
    """

    def test_null_detail_normalizes_to_empty_string(self) -> None:
        # Decoupled from status="ok" (#1544): the new ok/empty-findings
        # justification validator rejects a blank detail on that combination,
        # but the null->"" coercion this test verifies is a field-level
        # concern orthogonal to that cross-field rule, so it moves off
        # status="ok" to status="degraded" (exempt from the justification
        # rule) to keep testing exactly what it intends.
        doc = _make_reviewer_doc(detail=None, status="degraded")
        assert doc.detail == ""

    def test_null_findings_normalizes_to_empty_list(self) -> None:
        doc = _make_reviewer_doc(findings=None)
        assert doc.findings == []

    def test_status_failed_with_null_findings_still_passes_no_findings_check(
        self,
    ) -> None:
        doc = _make_reviewer_doc(status="failed", findings=None)
        assert doc.findings == []


class TestValidateReviewerDocument:
    def test_invalid_severity_rejected(self) -> None:
        bad = Finding.model_construct(**_finding_kwargs(severity="BOGUS"))
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(bad), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "invalid_severity"
        assert rejected[0].raw["severity"] == "BOGUS"

    def test_missing_evidence_rejected(self) -> None:
        bad = Finding.model_construct(**_finding_kwargs(evidence="   "))
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(bad), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "missing_evidence"

    def test_evidence_not_in_diff_rejected(self) -> None:
        f = _make_finding(evidence="not present anywhere")
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_unknown_file_rejected_without_worktree(self) -> None:
        # Exercises the worktree=None back-compat path (#1632): with no
        # worktree opted in, a non-diff file is always "unknown_file",
        # regardless of whether it exists on disk anywhere.
        f = _make_finding(file="src/cw/other.py")
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "unknown_file"

    def test_invalid_line_reference_rejected(self) -> None:
        f = _make_finding(line_start=999, line_end=999)
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "invalid_line_reference"

    def test_file_level_finding_skips_line_check(self) -> None:
        f = _make_finding(line_start=None, line_end=None)
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff()
        )
        assert len(accepted) == 1
        assert not rejected

    def test_rejected_preserves_raw_payload(self) -> None:
        # worktree=None explicitly: stays on the no-worktree fallback path
        # (#1632) — the tree-existence relaxation never engages here.
        f = _make_finding(file="src/cw/other.py", summary="raw kept")
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff(), worktree=None
        )
        assert rejected[0].raw["summary"] == "raw kept"
        assert rejected[0].reviewer_role == "Test Reviewer"

    def test_evidence_from_removed_line_rejected_by_default_claimed_line(self) -> None:
        # R6 (#1236): supersedes A3's file-full-diff matching claim for
        # Finding.evidence. The default finding claims line_start=line_end=10
        # ("def broken():"); evidence quoting a removed/context line elsewhere in
        # the diff is NOT the content of the claimed line, so true line-position
        # validation now rejects it (evidence_not_in_diff), where the old
        # whole-diff substring check accepted it. Escalation quotes are
        # unaffected — see test_quote_matches_full_diff_not_only_added_lines.
        diff = _make_diff(extra_text="-removed_context_line = 1")
        f = _make_finding(evidence="removed_context_line = 1")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_evidence_from_other_line_same_file_rejected(self) -> None:
        # R6 (#1236), one level more precise than the cross-file gap: a quote
        # that IS the verbatim content of a *different added line in the same
        # file* — outside the finding's claimed line window — is rejected
        # (evidence_not_in_diff), even though it's real, in-file, in that file's
        # hunk. The claimed window (line 10) must contain the evidence.
        diff = _make_diff(
            "def broken():",
            "sneaky = elsewhere()",
            files={"src/cw/foo.py": [10, 11]},
        )
        f = _make_finding(evidence="sneaky = elsewhere()", line_start=10, line_end=10)
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_evidence_cross_file_rejected(self) -> None:
        # R6 (#1236): a quote copied verbatim from a *different changed file's*
        # hunk is rejected even though it appears in the full diff text — the
        # claimed file/line window governs, not whole-diff substring presence.
        diff = _make_diff(
            "def broken():",
            "other_file_line = 2",
            files={"src/cw/foo.py": [10], "src/cw/bar.py": [10]},
        )
        # MUST_FIX 3 (#1236): confirm the "stolen" evidence is genuinely
        # present in bar.py's OWN hunk — not just absent everywhere, which
        # would make the rejection below tautological rather than proving
        # file-scoping.
        assert diff.file_line_text["src/cw/bar.py"][10] == "other_file_line = 2"
        assert diff.file_line_text["src/cw/foo.py"][10] == "def broken():"
        f = _make_finding(
            file="src/cw/foo.py",
            evidence="other_file_line = 2",
            line_start=10,
            line_end=10,
        )
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_single_endpoint_finding_checks_that_line(self) -> None:
        # A finding with only line_start set (line_end None) checks evidence
        # against exactly that one line.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(evidence="def broken():", line_start=10, line_end=None)
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    def test_single_endpoint_line_end_only_checks_that_line(self) -> None:
        # Symmetric: only line_end set (line_start None).
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(evidence="def broken():", line_start=None, line_end=10)
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    def test_file_level_evidence_matches_file_hunk(self) -> None:
        # A file-level finding (both endpoints None) has no line anchor and
        # falls back to matching against that file's full hunk text
        # (file_diffs), the same fallback _line_reference_valid grants today.
        diff = _make_diff(extra_text="-context_only = 3")
        f = _make_finding(evidence="context_only = 3", line_start=None, line_end=None)
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    # -- #1715: near-line anchor tolerance -----------------------------

    def test_near_line_anchor_within_tolerance_retained(self) -> None:
        # Anchor is 2 lines off the real added line (10) — within the
        # +/-3 tolerance bound. Evidence text is correct, so the finding
        # should be retained rather than rejected invalid_line_reference.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(line_start=12, line_end=12, evidence="def broken():")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected
        # The accepted finding's anchor is snapped to the real added line
        # (10), not left at the reviewer's raw off-by-2 claim (12) — a
        # downstream renderer showing this location must point at real
        # source, not the reviewer's drift (#1715).
        assert accepted[0].line_start == 10
        assert accepted[0].line_end == 10

    def test_near_line_range_anchor_retained(self) -> None:
        # line_start=8 is 2 lines off added line 10; line_end=13 is 2 lines
        # off added line 11 — each endpoint independently within tolerance.
        diff = _make_diff("line one", "line two", files={"src/cw/foo.py": [10, 11]})
        f = _make_finding(line_start=8, line_end=13, evidence="line one\nline two")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected
        assert accepted[0].line_start == 10
        assert accepted[0].line_end == 11

    # -- #1715: multiline evidence prefix normalization -----------------

    def test_file_level_multiline_evidence_matches_after_prefix_normalization(
        self,
    ) -> None:
        # File-level finding (no line anchor) falls back to file_diffs, which
        # stores raw hunk text with a "+" marker on every line. A reviewer's
        # genuine multiline quote carries no such markers at all (that's the
        # real-world shape of Bug B: a plain source-code quote, not a
        # diff-rendered one) — the *second* line's missing "+" breaks
        # contiguous substring matching against "+line one\n+line two" even
        # though the content is identical. MUST fail red pre-fix (verified:
        # "line one\nline two" is NOT a substring of the raw
        # "+++ b/...\n+line one\n+line two\n" hunk text).
        diff = _make_diff("line one", "line two", files={"src/cw/foo.py": [10, 11]})
        f = _make_finding(line_start=None, line_end=None, evidence="line one\nline two")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    def test_windowed_multiline_evidence_with_prefix_still_matches(self) -> None:
        # Windowed finding (explicit line_start/line_end) builds its window
        # from file_line_text, which is already prefix-free. Here the
        # REVIEWER's evidence itself carries diff-style "+" markers (plausible
        # if copied from a rendered diff view) — the latent exposure noted in
        # Bug B's second half. MUST fail red pre-fix: "+line one\n+line two"
        # is not a substring of the prefix-free window "line one\nline two".
        diff = _make_diff("line one", "line two", files={"src/cw/foo.py": [10, 11]})
        f = _make_finding(line_start=10, line_end=11, evidence="+line one\n+line two")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    # -- #1715: regression guards (mutation-proof) -----------------------

    def test_anchor_outside_tolerance_still_rejected(self) -> None:
        # Distance 4 from the only added line (10) — outside the +/-3 bound.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(line_start=14, line_end=14, evidence="def broken():")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "invalid_line_reference"

    def test_near_line_content_mismatch_still_rejected(self) -> None:
        # line_start=12 resolves to added line 10 (distance 2, within
        # tolerance), but the evidence text is not that line's real content —
        # the loosened anchor bound must not loosen the evidence check.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(line_start=12, line_end=12, evidence="totally unrelated text")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_widened_range_window_does_not_admit_third_unrelated_line(self) -> None:
        # line_start=8 snaps to 10 (distance 2); line_end=15 snaps to 16
        # (distance 1) -> resolved window is 10-16 inclusive, wider than a
        # single +/-3 span (the deliberate, tested compounding effect from
        # independently-snapped endpoints). Evidence genuinely inside that
        # window (line 13's real content) is accepted.
        diff = _make_diff(
            "first line content",
            "second line content",
            "third line content",
            "fourth line content",
            files={"src/cw/foo.py": [10, 13, 16, 20]},
        )
        f = _make_finding(line_start=8, line_end=15, evidence="second line content")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected
        assert accepted[0].line_start == 10
        assert accepted[0].line_end == 16

    def test_widened_range_window_rejects_evidence_outside_resolved_window(
        self,
    ) -> None:
        # Same widened window (10-16) as above, but the evidence is the real
        # content of line 20 — a genuine added line, just outside the
        # resolved window. Proves the widened window is still bounded, not an
        # unbounded escape hatch.
        diff = _make_diff(
            "first line content",
            "second line content",
            "third line content",
            "fourth line content",
            files={"src/cw/foo.py": [10, 13, 16, 20]},
        )
        f = _make_finding(line_start=8, line_end=15, evidence="fourth line content")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"


class TestUnanchoredFindings:
    """#1632: a finding whose file is not in the diff but does resolve to a
    real path under an opted-in ``worktree`` is routed to adjudication
    (``"unanchored"``) instead of being silently discarded as
    ``"unknown_file"``. Tree-existence proves the *path* is real, never the
    evidence *quote* — the escalation-quote check still runs against the
    diff for these findings (see
    ``test_unanchored_finding_escalation_still_validated_against_diff``).
    """

    def test_unanchored_file_in_tree_is_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "plan.md").write_text("hello")
        finding = _make_finding(file="docs/plan.md", line_start=None, line_end=None)
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff(), worktree=tmp_path
        )
        assert accepted == [finding]
        assert rejected == []

    def test_unanchored_file_not_in_tree_still_unknown_file(
        self, tmp_path: Path
    ) -> None:
        # worktree is opted in but the cited file does not exist on disk —
        # the tree check fails, so this falls back to unknown_file exactly
        # like the no-worktree case.
        finding = _make_finding(file="docs/plan.md", line_start=None, line_end=None)
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff(), worktree=tmp_path
        )
        assert rejected[0].reason == "unknown_file"

    def test_unanchored_path_traversal_outside_worktree_rejected(
        self, tmp_path: Path
    ) -> None:
        # Proves the containment guard, not just existence: the cited path
        # DOES exist on the real filesystem (a tmp_path sibling), but escapes
        # the worktree root via "../" — must still be unknown_file.
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (tmp_path / "sibling.txt").write_text("secret")
        finding = _make_finding(file="../sibling.txt", line_start=None, line_end=None)
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff(), worktree=worktree
        )
        assert rejected[0].reason == "unknown_file"

    def test_unanchored_finding_preserves_reviewer_text(self, tmp_path: Path) -> None:
        (tmp_path / "docs.md").write_text("x")
        finding = _make_finding(
            file="docs.md",
            line_start=None,
            line_end=None,
            summary="custom summary",
            consequence="custom consequence",
            suggested_fix="custom fix",
            evidence="custom evidence",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff(), worktree=tmp_path
        )
        assert not rejected
        assert accepted[0].summary == "custom summary"
        assert accepted[0].consequence == "custom consequence"
        assert accepted[0].suggested_fix == "custom fix"
        assert accepted[0].evidence == "custom evidence"

    def test_unanchored_finding_escalation_still_validated_against_diff(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "docs.md").write_text("x")
        good_esc = _make_escalation(evidence_quote="def broken():")
        good_finding = _make_finding(
            file="docs.md", line_start=None, line_end=None, escalation=good_esc
        )
        accepted, rejected, stripped = validate_reviewer_document(
            _make_reviewer_doc(good_finding), _make_diff(), worktree=tmp_path
        )
        assert not rejected
        assert accepted[0].escalation is not None
        assert not stripped

        bad_esc = _make_escalation(evidence_quote="ghost quote")
        bad_finding = _make_finding(
            file="docs.md", line_start=None, line_end=None, escalation=bad_esc
        )
        accepted2, rejected2, stripped2 = validate_reviewer_document(
            _make_reviewer_doc(bad_finding), _make_diff(), worktree=tmp_path
        )
        assert not rejected2
        assert accepted2[0].escalation is None
        assert len(stripped2) == 1


class TestEnclosingDefSpan:
    """Pure unit tests of ``_enclosing_def_span`` (#1743): resolve a source
    line to the ``(start, end)`` line span of its innermost enclosing
    function/class, or ``None`` if no such enclosing definition exists.
    """

    _BASIC_SOURCE = (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def target_function(a, b, c, d, e):\n"
        "    x = a + b\n"
        "    y = c + d\n"
        "    return x + y + e\n"
    )

    def test_line_at_def_itself_returns_span(self) -> None:
        assert _enclosing_def_span(self._BASIC_SOURCE, 4) == (4, 7)

    def test_line_inside_body_returns_same_span(self) -> None:
        assert _enclosing_def_span(self._BASIC_SOURCE, 6) == (4, 7)

    def test_module_scope_line_returns_none(self) -> None:
        # Line 3 is the blank line between the two top-level defs — module
        # scope, no enclosing function/class.
        assert _enclosing_def_span(self._BASIC_SOURCE, 3) is None

    def test_nested_function_innermost_span_wins(self) -> None:
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        return 1\n"
            "    return inner()\n"
        )
        # Line 3 is inside both outer (1-4) and inner (2-3) — inner must win.
        assert _enclosing_def_span(source, 3) == (2, 3)

    def test_class_definition_span_covers_whole_body(self) -> None:
        source = "class Foo:\n    def bar(self):\n        return 1\n"
        assert _enclosing_def_span(source, 1) == (1, 3)

    def test_decorator_line_has_no_enclosing_span(self) -> None:
        source = "@staticmethod\ndef foo():\n    return 1\n"
        assert _enclosing_def_span(source, 1) is None
        assert _enclosing_def_span(source, 2) == (2, 3)

    def test_syntax_error_source_returns_none(self) -> None:
        assert _enclosing_def_span("def foo(:\n    pass\n", 1) is None

    def test_line_past_eof_returns_none(self) -> None:
        assert _enclosing_def_span(self._BASIC_SOURCE, 999) is None


class TestAnchorInEnclosingDef:
    """Unit tests of ``_anchor_in_enclosing_def`` (#1743): the I/O-touching
    wrapper that reads *file* under *worktree*, resolves *line*'s enclosing
    def/class span, and checks whether any of *diff*'s changed lines for
    *file* fall inside that span.
    """

    _SOURCE = (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def target_function(a, b, c, d, e):\n"
        "    x = a + b\n"
        "    y = c + d\n"
        "    return x + y + e\n"
    )

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        assert (
            _anchor_in_enclosing_def(diff, tmp_path, "src/pkg/mod.py", 4) is False
        )

    def test_changed_line_inside_span_returns_true(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "mod.py").write_text(self._SOURCE)
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        assert _anchor_in_enclosing_def(diff, tmp_path, "src/pkg/mod.py", 4) is True

    def test_no_changed_line_inside_span_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "mod.py").write_text(self._SOURCE)
        # Line 2 (inside helper(), span 1-2) is changed, but the anchor is
        # target_function's def line (span 4-7) — no overlap.
        diff = _make_diff("    return 1", files={"src/pkg/mod.py": [2]})
        assert (
            _anchor_in_enclosing_def(diff, tmp_path, "src/pkg/mod.py", 4) is False
        )


class TestEnclosingDefAnchor:
    """Integration tests through ``validate_reviewer_document`` (#1743): a
    finding anchored on an enclosing ``def``/``class`` line that is not
    itself changed is no longer mechanically rejected ``invalid_line_reference``
    when a changed line falls inside that definition's span AND a worktree is
    supplied — it instead proceeds to the evidence check, which (since these
    findings don't quote the changed line's real content) currently lands on
    ``evidence_not_in_diff``. That reclassification is an intentional
    side-effect of this ticket; #1738 owns evidence-quote matching itself.
    """

    _SOURCE = (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def target_function(a, b, c, d, e):\n"
        "    x = a + b\n"
        "    y = c + d\n"
        "    return x + y + e\n"
    )

    _CLASS_SOURCE = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "    def baz(self):\n"
        "        return 2\n"
    )

    def _write_source(self, tmp_path: Path, source: str) -> None:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "mod.py").write_text(source)

    def test_def_line_anchor_accepted_with_worktree(self, tmp_path: Path) -> None:
        self._write_source(tmp_path, self._SOURCE)
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=4,
            line_end=4,
            evidence="target_function does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_def_line_anchor_rejected_without_worktree(self, tmp_path: Path) -> None:
        self._write_source(tmp_path, self._SOURCE)
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=4,
            line_end=4,
            evidence="target_function does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=None
        )
        assert rejected[0].reason == "invalid_line_reference"

    def test_def_span_with_no_changed_line_inside_still_rejected(
        self, tmp_path: Path
    ) -> None:
        self._write_source(tmp_path, self._SOURCE)
        # Changed line 2 sits inside helper()'s span (1-2), not
        # target_function's (4-7) — the anchor's own span has no changed line.
        diff = _make_diff("    return 1", files={"src/pkg/mod.py": [2]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=4,
            line_end=4,
            evidence="target_function does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "invalid_line_reference"

    def test_anchor_with_no_enclosing_def_still_rejected(self, tmp_path: Path) -> None:
        self._write_source(tmp_path, self._SOURCE)
        # Line 3 (blank line between the two top-level defs) has no enclosing
        # function/class at all, regardless of where the changed lines are.
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=3,
            line_end=3,
            evidence="module scope finding",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "invalid_line_reference"

    def test_class_def_anchor_accepted(self, tmp_path: Path) -> None:
        self._write_source(tmp_path, self._CLASS_SOURCE)
        diff = _make_diff("        return 2", files={"src/pkg/mod.py": [6]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=1,
            line_end=1,
            evidence="Foo does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_syntax_error_source_falls_back_to_invalid_line_reference(
        self, tmp_path: Path
    ) -> None:
        self._write_source(tmp_path, "def foo(:\n    pass\n")
        # Changed line (100) is far outside tolerance of the anchor (1), so
        # the fallback is actually exercised (and hits the parse failure).
        diff = _make_diff("    pass", files={"src/pkg/mod.py": [100]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=1,
            line_end=1,
            evidence="foo does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "invalid_line_reference"


class TestEnclosingDefAnchorRealFileRegression:
    """Reproduces the ticket's exact evidence: a structural finding anchored
    on ``_run_fix_and_commit``'s real ``def`` line in
    ``src/cw/codex_fix_loop.py``, which is not itself a changed line. The
    function's real span is discovered dynamically via ``ast.parse`` in this
    test's own setup (not the helper under test) so the assertion stays
    correct if the function is refactored — #1743 explicitly rejects
    hardcoding the line numbers observed at plan time.
    """

    def _discover_span(self, repo_root: Path) -> tuple[int, int]:
        source_path = repo_root / "src" / "cw" / "codex_fix_loop.py"
        tree = ast.parse(source_path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_run_fix_and_commit"
            ):
                assert node.end_lineno is not None
                return node.lineno, node.end_lineno
        msg = "_run_fix_and_commit not found in codex_fix_loop.py"
        raise AssertionError(msg)

    def test_def_line_anchor_accepted_with_worktree(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        def_line, end_line = self._discover_span(repo_root)
        changed_line = def_line + 1
        assert changed_line <= end_line
        diff = _make_diff(
            "some changed line inside the function",
            files={"src/cw/codex_fix_loop.py": [changed_line]},
        )
        finding = _make_finding(
            file="src/cw/codex_fix_loop.py",
            line_start=def_line,
            line_end=def_line,
            evidence="_run_fix_and_commit does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=repo_root
        )
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_def_line_anchor_rejected_without_worktree(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        def_line, end_line = self._discover_span(repo_root)
        changed_line = def_line + 1
        assert changed_line <= end_line
        diff = _make_diff(
            "some changed line inside the function",
            files={"src/cw/codex_fix_loop.py": [changed_line]},
        )
        finding = _make_finding(
            file="src/cw/codex_fix_loop.py",
            line_start=def_line,
            line_end=def_line,
            evidence="_run_fix_and_commit does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=None
        )
        assert rejected[0].reason == "invalid_line_reference"


class TestLineReferenceValidWorktreeParam:
    """Direct unit tests of ``_line_reference_valid``'s new ``worktree``
    parameter and ``_classify_finding``'s pass-through of it (#1743).
    """

    _SOURCE = (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def target_function(a, b, c, d, e):\n"
        "    x = a + b\n"
        "    y = c + d\n"
        "    return x + y + e\n"
    )

    def test_line_reference_valid_defaults_to_no_worktree(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "mod.py").write_text(self._SOURCE)
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        finding = _make_finding(
            file="src/pkg/mod.py", line_start=4, line_end=4, evidence="x"
        )
        assert _line_reference_valid(diff, finding) is False

    def test_line_reference_valid_with_worktree_rescues_def_anchor(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "mod.py").write_text(self._SOURCE)
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        finding = _make_finding(
            file="src/pkg/mod.py", line_start=4, line_end=4, evidence="x"
        )
        assert _line_reference_valid(diff, finding, tmp_path) is True

    def test_classify_finding_passes_worktree_through(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "mod.py").write_text(self._SOURCE)
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=4,
            line_end=4,
            evidence="target_function does too many things",
        )
        changed = frozenset(diff.files)
        # evidence doesn't match the changed line's real text, so the def-line
        # anchor is rescued and classification proceeds to the evidence check.
        assert (
            _classify_finding(finding, diff, changed, tmp_path)
            == "evidence_not_in_diff"
        )
        assert _classify_finding(finding, diff, changed, None) == (
            "invalid_line_reference"
        )


class TestEscalationStripOnInvalidEvidence:
    def test_bad_quote_stripped_finding_survives(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        esc = _make_escalation(
            target_reviewer="Perf Reviewer", evidence_quote="ghost quote"
        )
        f = _make_finding(severity="MUST_FIX", escalation=esc)
        doc = _make_reviewer_doc(f, reviewer_role="Security Reviewer")
        with caplog.at_level(logging.WARNING):
            accepted, rejected, stripped = validate_reviewer_document(doc, _make_diff())
        assert not rejected
        assert len(accepted) == 1
        assert accepted[0].escalation is None
        assert len(stripped) == 1
        se = stripped[0]
        assert se.reason == "escalation_evidence_not_in_diff"
        assert se.reviewer_role == "Security Reviewer"
        assert se.finding_index == 0
        assert se.target_reviewer == "Perf Reviewer"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "Security Reviewer" in msg
        assert "Perf Reviewer" in msg
        assert "0" in msg
        # Counted in must_fix_initial despite the strip.
        review = derive_review_counts(
            dedupe_findings([("Security Reviewer", accepted[0])])
        )
        assert review.must_fix_initial == 1

    def test_valid_quote_preserved(self, caplog: pytest.LogCaptureFixture) -> None:
        esc = _make_escalation(evidence_quote="def broken():")
        f = _make_finding(escalation=esc)
        with caplog.at_level(logging.WARNING):
            accepted, _, stripped = validate_reviewer_document(
                _make_reviewer_doc(f), _make_diff()
            )
        assert accepted[0].escalation is not None
        assert not stripped
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_no_escalation_untouched(self, caplog: pytest.LogCaptureFixture) -> None:
        f = _make_finding(escalation=None)
        with caplog.at_level(logging.WARNING):
            accepted, _, stripped = validate_reviewer_document(
                _make_reviewer_doc(f), _make_diff()
            )
        assert accepted[0].escalation is None
        assert not stripped
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_rejected_finding_never_reaches_escalation_check(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Finding is itself rejected (blank evidence via model_construct) AND
        # carries an escalation with a bad quote — no strip is produced.
        esc = _make_escalation(evidence_quote="ghost")
        bad = Finding.model_construct(**_finding_kwargs(evidence="   ", escalation=esc))
        with caplog.at_level(logging.WARNING):
            accepted, rejected, stripped = validate_reviewer_document(
                _make_reviewer_doc(bad), _make_diff()
            )
        assert not accepted
        assert rejected[0].reason == "missing_evidence"
        assert not stripped
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_quote_matches_full_diff_not_only_added_lines(self) -> None:
        # A context/removed line (no + prefix) still counts for the quote check.
        diff = _make_diff(extra_text="-removed_context_line = 1")
        esc = _make_escalation(evidence_quote="removed_context_line = 1")
        f = _make_finding(escalation=esc)
        accepted, _, stripped = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert accepted[0].escalation is not None
        assert not stripped


class TestDedupeFindings:
    def test_two_reviewers_merge_to_one(self) -> None:
        f1 = _make_finding()
        f2 = _make_finding()
        merged = dedupe_findings([("Reviewer A", f1), ("Reviewer B", f2)])
        assert len(merged) == 1
        assert merged[0].reviewers == ["Reviewer A", "Reviewer B"]

    def test_non_duplicates_not_merged(self) -> None:
        f1 = _make_finding(line_start=10, line_end=10)
        f2 = _make_finding(line_start=20, line_end=20)
        merged = dedupe_findings([("A", f1), ("B", f2)])
        assert len(merged) == 2

    def test_deterministic_order_across_permutations(self) -> None:
        f1 = _make_finding(line_start=10, line_end=10)
        f2 = _make_finding(line_start=20, line_end=20, evidence="def broken():")
        order1 = [
            af.finding.line_start for af in dedupe_findings([("A", f1), ("B", f2)])
        ]
        order2 = [
            af.finding.line_start for af in dedupe_findings([("B", f2), ("A", f1)])
        ]
        assert order1 == order2

    def test_escalation_non_null_wins_when_one_side_set(self) -> None:
        esc = _make_escalation(evidence_quote="def broken():")
        with_esc = _make_finding(escalation=esc)
        without = _make_finding(escalation=None)
        merged = dedupe_findings([("A", without), ("B", with_esc)])
        assert len(merged) == 1
        assert merged[0].finding.escalation is not None

    def test_tiebreak_lowest_role_when_neither_escalates(self) -> None:
        f_a = _make_finding(summary="from A")
        f_b = _make_finding(summary="from B")
        forward = dedupe_findings([("Alpha", f_a), ("Zeta", f_b)])
        backward = dedupe_findings([("Zeta", f_b), ("Alpha", f_a)])
        assert forward[0].finding.summary == "from A"
        assert backward[0].finding.summary == "from A"

    def test_tiebreak_lowest_role_when_both_escalate(self) -> None:
        esc = _make_escalation(evidence_quote="def broken():")
        f_a = _make_finding(summary="from A", escalation=esc)
        f_b = _make_finding(summary="from B", escalation=esc)
        merged = dedupe_findings([("Zeta", f_b), ("Alpha", f_a)])
        assert merged[0].finding.summary == "from A"


class TestDeriveReviewCounts:
    def test_counts_by_severity(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="MUST_FIX"), reviewers=["a"]
            ),
            AcceptedFinding(
                finding=_make_finding(severity="SHOULD_FIX"), reviewers=["a"]
            ),
        ]
        review = derive_review_counts(findings, fix_cycles_used=2, agents_run=3)
        assert review.must_fix_initial == 1
        assert review.should_fix == 1
        assert review.deferred == 0
        assert review.fix_cycles_used == 2
        assert review.agents_run == 3

    def test_must_fix_deferred_counts_as_deferred(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="MUST_FIX"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.must_fix_initial == 0
        assert review.deferred == 1

    def test_should_fix_deferred_counts_as_deferred(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="SHOULD_FIX"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.should_fix == 0
        assert review.deferred == 1

    def test_nit_deferred_excluded_from_deferred_count(self) -> None:
        # NIT/PRINCIPLE never touch any of the 3 gate-feeding aggregates,
        # regardless of disposition — deferred is severity-filtered too.
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="NIT"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.deferred == 0
        assert review.must_fix_initial == 0
        assert review.should_fix == 0

    def test_principle_deferred_excluded_from_deferred_count(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="PRINCIPLE"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.deferred == 0
        assert review.must_fix_initial == 0
        assert review.should_fix == 0


class TestConsolidateVerdict:
    def test_base_verdict(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX"), reviewer_role="Reviewer A"
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="abc123")
        assert verdict.blocking is True
        assert len(verdict.must_fix) == 1
        assert verdict.reviewed_sha == "abc123"
        assert len(verdict.agents_run) == 1
        assert verdict.agents_run[0].reviewer_role == "Reviewer A"
        assert verdict.review.agents_run == 1

    def test_non_blocking_when_no_must_fix(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.blocking is False
        assert verdict.must_fix == []

    def test_unanchored_must_fix_finding_blocks_via_worktree(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "docs.md").write_text("x")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict(
            [doc], _make_diff(), reviewed_sha="sha", worktree=tmp_path
        )
        assert verdict.blocking is True
        assert len(verdict.must_fix) == 1
        assert verdict.rejected == []
        assert verdict.review.must_fix_initial == 1

    def test_unanchored_without_worktree_still_rejected(self, tmp_path: Path) -> None:
        # Same finding, no worktree kwarg passed — proves the relaxation is
        # strictly opt-in even when the cited file genuinely exists on disk.
        (tmp_path / "docs.md").write_text("x")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.blocking is False
        assert verdict.rejected[0].reason == "unknown_file"

    def test_mechanically_rejected_must_fix_populates_rejected_must_fix_field(
        self, tmp_path: Path
    ) -> None:
        # #1714: the fleet reproduction. A MUST_FIX rejected for a MECHANICAL
        # reason (here unknown_file) is dropped before adjudication, so
        # `blocking` stays False by design (R4 -- an unreliable anchor must
        # never enter the autofix loop). `rejected_must_fix` is the separate
        # signal that says "something MUST_FIX-shaped was silently dropped".
        (tmp_path / "docs.md").write_text("x")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.blocking is False
        assert verdict.must_fix == []
        assert len(verdict.rejected_must_fix) == 1
        assert verdict.rejected_must_fix[0].reason == "unknown_file"
        assert verdict.rejected_must_fix[0].raw["severity"] == "MUST_FIX"

    def test_should_fix_mechanical_rejection_does_not_populate_rejected_must_fix(
        self,
    ) -> None:
        # #1714 AC#4: only MUST_FIX-severity rejections raise the new signal;
        # a mechanically-rejected SHOULD_FIX stays purely informational.
        finding = _make_finding(
            severity="SHOULD_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.rejected[0].reason == "unknown_file"
        assert verdict.rejected_must_fix == []

    def test_rejected_must_fix_keyed_on_category_not_enumerated_reason(self) -> None:
        # #1714 AC#3: the selection is keyed on the finding's SEVERITY, never on
        # an enumerated set of RejectedFindingReason values -- so a reason value
        # that does not exist today is covered by construction. model_construct
        # bypasses the Literal so a synthetic reason can be exercised at all.
        synthetic = RejectedFinding.model_construct(
            raw=_finding_kwargs(severity="MUST_FIX"),
            reviewer_role="Test Reviewer",
            reason="a_synthetic_reason_never_seen_before",
            detail="",
        )
        benign = RejectedFinding.model_construct(
            raw=_finding_kwargs(severity="NIT"),
            reviewer_role="Test Reviewer",
            reason="a_synthetic_reason_never_seen_before",
            detail="",
        )
        assert _select_rejected_must_fix([synthetic, benign]) == [synthetic]

    def test_mixed_blocking_and_rejected_must_fix(self) -> None:
        # #1714: the two signals are independent and can coexist -- an accepted
        # MUST_FIX still blocks while a mechanically-rejected one is reported.
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX"),
            _make_finding(
                severity="MUST_FIX",
                file="not/in/diff.py",
                line_start=None,
                line_end=None,
                summary="dropped one",
            ),
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.blocking is True
        assert len(verdict.must_fix) == 1
        assert len(verdict.rejected_must_fix) == 1

    def test_aggregate_near_line_and_multiline_via_consolidate_verdict(self) -> None:
        # #1715 integration: three findings through the full
        # consolidate_verdict pipeline. (1) file not in diff, no worktree ->
        # still unknown_file (#1632 mechanism untouched). (2) near-line
        # anchor (distance 2, within tolerance) with matching evidence ->
        # accepted. (3) file-level multiline evidence with no diff markers,
        # matched against raw "+"-prefixed file_diffs text via normalization
        # -> accepted. MUST fail red pre-fix (findings 2 and 3 both rejected
        # under exact-match/raw-substring behavior).
        diff = _make_diff(
            "def broken():",
            "line one",
            "line two",
            files={"src/cw/foo.py": [10], "src/cw/bar.py": [20, 21]},
        )
        findings = [
            _make_finding(
                file="src/cw/other.py",
                line_start=None,
                line_end=None,
                evidence="whatever",
            ),
            _make_finding(
                file="src/cw/foo.py",
                line_start=12,
                line_end=12,
                evidence="def broken():",
            ),
            _make_finding(
                file="src/cw/bar.py",
                line_start=None,
                line_end=None,
                evidence="line one\nline two",
            ),
        ]
        doc = _make_reviewer_doc(*findings, reviewer_role="Reviewer A")
        verdict = consolidate_verdict([doc], diff, "deadbeef")
        assert len(verdict.rejected) == 1
        assert verdict.rejected[0].reason == "unknown_file"
        assert len(verdict.accepted) == 2


class TestConsolidateVerdictFailedReviewers:
    def test_default_no_failed_reviewers(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding())
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert len(verdict.agents_run) == 1

    def test_failed_reviewer_appends_record(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer A")
        verdict = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            failed_reviewers=[
                ReviewerRunFailure(role="Perf Reviewer", reason="timeout")
            ],
        )
        # The failed reviewer is still RECORDED in the agents_run list (audit
        # trail)...
        assert len(verdict.agents_run) == 2
        failed = [r for r in verdict.agents_run if r.status == "failed"]
        assert len(failed) == 1
        assert failed[0].reviewer_role == "Perf Reviewer"
        assert failed[0].finding_count == 0
        # ...but excluded from the countable review.agents_run int — only
        # roles that actually produced a document count (standing binding
        # decision, #1236).
        assert verdict.review.agents_run == 1

    def test_failed_reviewer_without_document(self) -> None:
        diff = _make_diff()
        verdict = consolidate_verdict(
            [],
            diff,
            reviewed_sha="sha",
            failed_reviewers=[ReviewerRunFailure(role="Solo", reason="crash")],
        )
        assert len(verdict.agents_run) == 1
        assert verdict.agents_run[0].status == "failed"
        # Zero documents produced -> review.agents_run is 0, not 1.
        assert verdict.review.agents_run == 0

    def test_stripped_escalations_union_in_document_order(self) -> None:
        diff = _make_diff()
        esc = _make_escalation(evidence_quote="ghost")
        doc1 = _make_reviewer_doc(_make_finding(escalation=esc), reviewer_role="R1")
        doc2 = _make_reviewer_doc(_make_finding(escalation=esc), reviewer_role="R2")
        verdict = consolidate_verdict([doc1, doc2], diff, reviewed_sha="sha")
        assert len(verdict.stripped_escalations) == 2
        assert verdict.stripped_escalations[0].reviewer_role == "R1"
        assert verdict.stripped_escalations[1].reviewer_role == "R2"


class TestConsolidateVerdictFixCycles:
    """The #1392 fix_cycles_used threading through consolidate_verdict."""

    def test_fix_cycles_used_threads_into_review(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        verdict = consolidate_verdict(
            [doc], diff, reviewed_sha="sha", fix_cycles_used=3
        )
        assert verdict.review.fix_cycles_used == 3

    def test_fix_cycles_used_defaults_to_zero_when_omitted(self) -> None:
        # Regression guard: the pre-#1392 call shape (no fix_cycles_used) still
        # yields fix_cycles_used=0, so every existing single-pass caller is
        # byte-identical.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.review.fix_cycles_used == 0

    def test_derive_review_counts_default_unchanged(self) -> None:
        # derive_review_counts still defaults fix_cycles_used to 0 for the
        # hardcoded-zero Review(...) constructions in local_runner.
        findings = [
            AcceptedFinding(finding=_make_finding(severity="MUST_FIX"), reviewers=["a"])
        ]
        review = derive_review_counts(findings)
        assert review.fix_cycles_used == 0
        assert review.must_fix_initial == 1


class TestWriteReviewVerdictArtifact:
    def test_atomic_write_round_trips(self, tmp_path: Path) -> None:
        diff = _make_diff()
        esc = _make_escalation(evidence_quote="ghost")
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", escalation=esc),
            reviewer_role="R1",
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="deadbeef")
        path = tmp_path / "review-verdict.json"
        write_review_verdict(verdict, path)
        data = json.loads(path.read_text())
        # #1108's 3 required keys present.
        assert "blocking" in data
        assert "must_fix" in data
        assert "reviewed_sha" in data
        assert data["reviewed_sha"] == "deadbeef"
        # Superset round-trips.
        assert data["stripped_escalations"][0]["reason"] == (
            "escalation_evidence_not_in_diff"
        )

    def test_full_replace_semantics(self, tmp_path: Path) -> None:
        diff = _make_diff()
        path = tmp_path / "review-verdict.json"
        path.write_text('{"stale": true}')
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            diff,
            reviewed_sha="sha",
        )
        write_review_verdict(verdict, path)
        data = json.loads(path.read_text())
        assert "stale" not in data


class TestExecutorNeutralContract:
    def test_claude_and_codex_shapes_validate_identically(self) -> None:
        diff = _make_diff(extra_text="-context = removed()")
        good_esc = _make_escalation(evidence_quote="def broken():")
        bad_esc = _make_escalation(evidence_quote="ghost")
        # Two documents modeling the same findings from different executors.
        claude_doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", escalation=good_esc),
            _make_finding(
                severity="SHOULD_FIX",
                line_start=10,
                line_end=10,
                evidence="def broken():",
                escalation=bad_esc,
            ),
            reviewer_role="Reviewer",
        )
        codex_doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", escalation=good_esc),
            _make_finding(
                severity="SHOULD_FIX",
                line_start=10,
                line_end=10,
                evidence="def broken():",
                escalation=bad_esc,
            ),
            reviewer_role="Reviewer",
        )
        v1 = consolidate_verdict([claude_doc], diff, reviewed_sha="sha")
        v2 = consolidate_verdict([codex_doc], diff, reviewed_sha="sha")
        assert v1.model_dump() == v2.model_dump()


class TestReviewVerdictSchemaRegistration:
    def test_schema_surfaces_core_and_new_defs(self) -> None:
        schema = ReviewVerdict.model_json_schema()
        assert "blocking" in schema["properties"]
        assert "must_fix" in schema["properties"]
        assert "reviewed_sha" in schema["properties"]
        assert "stripped_escalations" in schema["properties"]
        assert "EscalationMetadata" in schema["$defs"]
        assert "StrippedEscalation" in schema["$defs"]


class TestReviewerRunRecord:
    def test_construct(self) -> None:
        r = ReviewerRunRecord(reviewer_role="R", status="ok", finding_count=3)
        assert r.finding_count == 3

    def test_audit_metrics_fields_all_default_when_unset(self) -> None:
        # #1710: every new telemetry field is optional, so pre-#1710 bare
        # construction (and consolidate_verdict's own two construction sites)
        # keep working untouched.
        r = ReviewerRunRecord(reviewer_role="R", status="ok", finding_count=3)
        assert r.thread_id is None
        assert r.effective_model is None
        assert r.duration_seconds is None
        assert r.input_tokens is None
        assert r.cached_input_tokens is None
        assert r.output_tokens is None
        assert r.reasoning_tokens is None
        assert r.terminal_event is None
        assert r.tool_call_counts == {}
        assert r.had_command_evidence is False
        assert r.unexpected_tool_attempts == []

    def test_construct_with_explicit_metrics(self) -> None:
        r = ReviewerRunRecord(
            reviewer_role="R",
            status="ok",
            finding_count=0,
            thread_id="thr-1",
            effective_model=None,
            duration_seconds=12.5,
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=5,
            reasoning_tokens=1,
            terminal_event="turn.completed",
            tool_call_counts={"command_execution": 2},
            had_command_evidence=True,
            unexpected_tool_attempts=["mcp_tool_call"],
        )
        assert r.thread_id == "thr-1"
        assert r.duration_seconds == pytest.approx(12.5)
        assert r.tool_call_counts == {"command_execution": 2}
        assert r.had_command_evidence is True
        assert r.unexpected_tool_attempts == ["mcp_tool_call"]

    def test_metrics_defaults_are_not_shared_between_instances(self) -> None:
        # Mutable defaults must come from a factory, not a shared literal.
        a = ReviewerRunRecord(reviewer_role="A", status="ok", finding_count=0)
        b = ReviewerRunRecord(reviewer_role="B", status="ok", finding_count=0)
        a.tool_call_counts["x"] = 1
        a.unexpected_tool_attempts.append("y")
        assert b.tool_call_counts == {}
        assert b.unexpected_tool_attempts == []


class TestConsolidateVerdictMetricsByRole:
    """#1710: per-role codex audit metrics land on ReviewerRunRecord."""

    def test_metrics_attach_to_matching_document_record(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer A")
        verdict = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            metrics_by_role={
                "Reviewer A": {
                    "thread_id": "thr-a",
                    "duration_seconds": 3.5,
                    "input_tokens": 42,
                    "terminal_event": "turn.completed",
                    "tool_call_counts": {"agent_message": 1},
                    "had_command_evidence": True,
                }
            },
        )
        record = verdict.agents_run[0]
        assert record.thread_id == "thr-a"
        assert record.duration_seconds == pytest.approx(3.5)
        assert record.input_tokens == 42
        assert record.terminal_event == "turn.completed"
        assert record.tool_call_counts == {"agent_message": 1}
        assert record.had_command_evidence is True

    def test_role_absent_from_metrics_gets_defaults(self) -> None:
        diff = _make_diff()
        doc_a = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer A")
        doc_b = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer B")
        verdict = consolidate_verdict(
            [doc_a, doc_b],
            diff,
            reviewed_sha="sha",
            metrics_by_role={"Reviewer A": {"thread_id": "thr-a"}},
        )
        by_role = {r.reviewer_role: r for r in verdict.agents_run}
        assert by_role["Reviewer A"].thread_id == "thr-a"
        assert by_role["Reviewer B"].thread_id is None
        assert by_role["Reviewer B"].tool_call_counts == {}

    def test_failed_reviewer_record_picks_up_its_metrics(self) -> None:
        # A role that invoked codex and failed still has audit telemetry —
        # the ticket's "runtime failures before the final document" framing.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer A")
        verdict = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            failed_reviewers=[
                ReviewerRunFailure(role="Perf Reviewer", reason="timeout")
            ],
            metrics_by_role={
                "Perf Reviewer": {
                    "thread_id": "thr-perf",
                    "terminal_event": "turn.failed",
                    "duration_seconds": 900.0,
                }
            },
        )
        failed = next(r for r in verdict.agents_run if r.status == "failed")
        assert failed.reviewer_role == "Perf Reviewer"
        assert failed.thread_id == "thr-perf"
        assert failed.terminal_event == "turn.failed"
        assert failed.duration_seconds == pytest.approx(900.0)

    def test_none_default_is_byte_identical_to_omitting_the_param(self) -> None:
        # Regression guard for the additive-default claim: the new parameter
        # must not perturb any pre-#1710 verdict.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        without = consolidate_verdict([doc], diff, reviewed_sha="sha")
        with_none = consolidate_verdict(
            [doc], diff, reviewed_sha="sha", metrics_by_role=None
        )
        assert without.model_dump() == with_none.model_dump()

    def test_metrics_never_affect_blocking_or_must_fix(self) -> None:
        # R2/R4 (#1710): metrics are purely observational.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        baseline = consolidate_verdict([doc], diff, reviewed_sha="sha")
        with_metrics = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            metrics_by_role={
                doc.reviewer_role: {
                    "terminal_event": None,
                    "unexpected_tool_attempts": ["mcp_tool_call"],
                    "had_command_evidence": False,
                }
            },
        )
        assert with_metrics.blocking == baseline.blocking
        assert with_metrics.must_fix == baseline.must_fix
        assert with_metrics.review.model_dump() == baseline.review.model_dump()


class TestCapturedDiffStructure:
    def test_file_diffs_and_line_text_round_trip(self) -> None:
        # R6 (#1236): the restructured CapturedDiff carries per-file hunk text
        # and per-file {line: content}, and both survive a JSON round-trip
        # (int line-number keys coerce back from their JSON string form).
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        assert diff.file_line_text["src/cw/foo.py"][10] == "def broken():"
        assert "def broken():" in diff.file_diffs["src/cw/foo.py"]
        reloaded = CapturedDiff.model_validate_json(diff.model_dump_json())
        assert reloaded.file_line_text == diff.file_line_text
        assert reloaded.file_diffs == diff.file_diffs

    def test_files_matches_file_line_text_keys(self) -> None:
        # The _make_diff invariant the production _capture_diff also upholds:
        # files[f] == sorted(file_line_text[f]).
        diff = _make_diff("a = 1", "b = 2", files={"src/cw/foo.py": [10, 11]})
        for path, line_nums in diff.files.items():
            assert line_nums == sorted(diff.file_line_text[path])


class TestReviewVerdictSchemaVersion:
    def test_schema_version_defaults_to_one(self) -> None:
        diff = _make_diff()
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding())], diff, reviewed_sha="sha"
        )
        assert verdict.schema_version == 1

    def test_schema_version_round_trips(self) -> None:
        diff = _make_diff()
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding())], diff, reviewed_sha="sha"
        )
        reloaded = ReviewVerdict.model_validate_json(verdict.model_dump_json())
        assert reloaded.schema_version == 1

    def test_schema_version_rejects_other_value(self) -> None:
        with pytest.raises(ValidationError):
            ReviewVerdict.model_validate(
                {
                    "schema_version": 2,
                    "blocking": False,
                    "must_fix": [],
                    "reviewed_sha": "sha",
                    "review": derive_review_counts([]).model_dump(),
                }
            )
