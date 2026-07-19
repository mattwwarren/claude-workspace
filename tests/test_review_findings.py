"""Tests for cw.review_findings — executor-neutral structured finding contract.

Covers the model group (Finding/EscalationMetadata/ReviewerFindingsDocument/
ReviewVerdict and friends), the validation/dedup/aggregation functions, the
escalation strip-on-invalid-evidence rule, and the #1108 artifact writer.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from pathlib import Path


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
            reviewer_role="R", status="ok", detail="", findings=[]
        )
        assert doc.findings == []


class TestReviewerFindingsDocumentNullNormalization:
    """A ``None`` detail/findings (from an OpenAI strict-schema nullable-wrapped
    field, #1364) normalizes to the same default a caller omitting the key
    would get, rather than failing type validation.
    """

    def test_null_detail_normalizes_to_empty_string(self) -> None:
        doc = _make_reviewer_doc(detail=None)
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

    def test_unknown_file_rejected(self) -> None:
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
        f = _make_finding(file="src/cw/other.py", summary="raw kept")
        _, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), _make_diff())
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
