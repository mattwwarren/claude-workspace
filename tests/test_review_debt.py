"""Tests for cw.review_debt — debt fingerprinting, promotion, and dedup (#1837)."""

from __future__ import annotations

import logging

import pytest

from cw.review_debt import (
    FINGERPRINT_VERSION,
    _normalize_summary,
    dedupe_debt,
    fingerprint_v1,
    promote_debt_finding,
    record_debt,
)
from cw.review_findings import AcceptedFinding, DebtRecord
from tests.conftest import _make_debt_record, _make_finding


def _accepted(**overrides: object) -> AcceptedFinding:
    return AcceptedFinding(
        finding=_make_finding(**overrides), reviewers=["Code Quality Reviewer"]
    )


# ---------------------------------------------------------------------------
# _normalize_summary (R1)
# ---------------------------------------------------------------------------


class TestNormalizeSummary:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # Position patterns are stripped, in all three spellings.
            ("Update code at line 42", "Update code at lines 42-47"),
            ("Update code at line 42", "Update code:42"),
            # Bare digit runs collapse to a single placeholder.
            ("3 call sites", "4 call sites"),
            # A backticked identifier is kept verbatim apart from the shared
            # lowercase step -- it is not exempted from case folding, just not
            # fuzzy-matched any further.
            ("the `_row_badge` helper", "the `_ROW_BADGE` helper"),
        ],
    )
    def test_equivalent_summaries_normalize_identically(
        self, left: str, right: str
    ) -> None:
        assert _normalize_summary(left) == _normalize_summary(right)

    def test_whitespace_runs_collapse(self) -> None:
        assert _normalize_summary("too   many   returns") == "too many returns"

    def test_no_fuzzy_matching_beyond_documented_transforms(self) -> None:
        """R4: normalization does no semantic merging.

        Same number, different noun -- these are genuinely different findings
        and must keep different fingerprints.
        """
        assert _normalize_summary("the helper is 50 lines long") != _normalize_summary(
            "the function is 50 lines long"
        )


# ---------------------------------------------------------------------------
# fingerprint_v1 (R2/R3/R5)
# ---------------------------------------------------------------------------


class TestFingerprintV1:
    def test_na_file_is_not_fingerprinted(self) -> None:
        assert fingerprint_v1("N/A", "anything at all") is None

    def test_real_file_fingerprints_to_file_and_normalized_summary(self) -> None:
        assert fingerprint_v1("src/cw/foo.py", "Update code at line 42") == (
            "src/cw/foo.py",
            "update code",
        )

    def test_severity_is_not_a_component(self) -> None:
        """R2: severity is deliberately absent from the fingerprint.

        Asserted at the signature level (``fingerprint_v1`` takes no severity
        argument at all), not just behaviorally -- so a later edit cannot
        reintroduce severity without breaking this test.
        """
        import inspect

        params = list(inspect.signature(fingerprint_v1).parameters)
        assert params == ["file", "summary"]

        must_fix = _make_finding(severity="MUST_FIX")
        debt = _make_finding(severity="DEBT")
        assert fingerprint_v1(must_fix.file, must_fix.summary) == fingerprint_v1(
            debt.file, debt.summary
        )

    def test_version_constant_is_stamped_on_every_record(self) -> None:
        assert FINGERPRINT_VERSION == "FINGERPRINT_V1"
        record = promote_debt_finding(_accepted(), discovery_sha="abc1234")
        assert record is not None
        assert record.fingerprint_version == FINGERPRINT_VERSION


# ---------------------------------------------------------------------------
# promote_debt_finding (R6)
# ---------------------------------------------------------------------------


class TestPromoteDebtFinding:
    def test_builds_record_from_debt_finding(self) -> None:
        af = _accepted(severity="DEBT", summary="Duplicated at line 12")
        record = promote_debt_finding(af, discovery_sha="cafe123")

        assert record is not None
        assert record.fingerprint == ("src/cw/foo.py", "duplicated")
        assert record.discovery_sha == "cafe123"
        assert record.tracking_disposition == "NEEDS_FILING"
        assert record.file == af.finding.file
        assert record.evidence == af.finding.evidence
        assert record.summary == af.finding.summary
        assert record.suggested_follow_up == af.finding.suggested_fix
        assert record.reviewer_role == "Code Quality Reviewer"
        # `Finding` carries no source field for either, so both stay blank.
        assert record.rule_id == ""
        assert record.symbol == ""

    def test_na_file_finding_is_not_persisted(self) -> None:
        af = _accepted(
            severity="DEBT",
            file="N/A",
            no_diff_anchor=True,
            line_start=None,
            line_end=None,
        )
        assert promote_debt_finding(af, discovery_sha="cafe123") is None


# ---------------------------------------------------------------------------
# dedupe_debt / record_debt (R4)
# ---------------------------------------------------------------------------


class TestDedupeDebt:
    def test_same_fingerprint_collapses_first_discovered_wins(self) -> None:
        first = _make_debt_record(summary="Update code at line 42", discovery_sha="aaa")
        second = _make_debt_record(
            summary="Update code at line 99", discovery_sha="bbb"
        )

        merged = dedupe_debt([first, second])

        assert len(merged) == 1
        assert merged[0].discovery_sha == "aaa"

    def test_distinct_fingerprints_are_both_kept(self) -> None:
        first = _make_debt_record(fingerprint=("a.py", "one"))
        second = _make_debt_record(fingerprint=("b.py", "two"))
        assert len(dedupe_debt([first, second])) == 2

    def test_false_merge_logs_one_warning_naming_both_summaries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        first = _make_debt_record(summary="Update code at line 42")
        second = _make_debt_record(summary="Update code at line 99")

        with caplog.at_level(logging.WARNING, logger="cw.review_debt"):
            dedupe_debt([first, second])

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "Update code at line 42" in message
        assert "Update code at line 99" in message

    def test_identical_summaries_do_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        record = _make_debt_record()
        with caplog.at_level(logging.WARNING, logger="cw.review_debt"):
            dedupe_debt([record, record])
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_record_debt_is_first_wins_in_place(self) -> None:
        ledger: dict[tuple[str, str], DebtRecord] = {}
        record_debt(ledger, _make_debt_record(discovery_sha="aaa"))
        record_debt(ledger, _make_debt_record(discovery_sha="bbb"))
        assert [r.discovery_sha for r in ledger.values()] == ["aaa"]
