"""Tests for :mod:`cw.dev_queue.review_engine_reconcile` (#3625). Pure: no I/O."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from cw.dev_queue.review_engine_reconcile import (
    engine_settled_fingerprints,
    reconcile_deferred_summary,
)
from cw.models import QueueItemStatus, Stage
from cw.models.review_engine_settlement import ReviewEngineSettlement
from cw.models.review_engine_summary import ReviewEngineSummary
from cw.review_convergence import (
    ConvergenceLedger,
    GatingSeverity,
    SettledEntry,
    finding_identity,
)
from tests._review_convergence_helpers import fresh_ledger
from tests._review_engine_helpers import SHA, make_finding, make_summary
from tests._shared.session_seeding import _make_ticket_task

_TICKET = "3625"
_WHEN = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _summary(
    findings: list[dict[str, object]], **overrides: object
) -> ReviewEngineSummary:
    return ReviewEngineSummary.model_validate(make_summary(findings, **overrides))


def _shouldfix(fp: str, **overrides: object) -> dict[str, object]:
    return make_finding(fingerprint=fp, severity="SHOULD_FIX", **overrides)


def _entry(
    fp: str,
    *,
    severity: GatingSeverity = "SHOULD_FIX",
    outcome: Literal["reject", "defer"] = "defer",
) -> SettledEntry:
    return SettledEntry(
        identity=finding_identity(
            severity, "src/a.py", f"finding {fp}", fingerprint=fp
        ),
        outcome=outcome,
        rationale="deferred",
        round=1,
    )


def _ledger(*fps: str, extra: tuple[SettledEntry, ...] = ()) -> ConvergenceLedger:
    base = fresh_ledger(confirm="none", ticket_id=_TICKET)
    return base.model_copy(update={"settled": [*(_entry(fp) for fp in fps), *extra]})


_NIT = make_finding(fingerprint="fp-000000nit", severity="NIT", rule_id="style.naming")


def test_happy_path_reconciles_to_pass_with_debt() -> None:
    summary = _summary([_shouldfix("fp-0000001"), _shouldfix("fp-0000002"), _NIT])
    result = reconcile_deferred_summary(
        summary,
        ledger=_ledger("fp-0000001", "fp-0000002"),
        settled_fingerprints={"fp-0000001", "fp-0000002"},
    )
    assert result is not None
    assert result.verdict == "PASS"
    assert result.counts.should_fix == 0
    assert result.counts.debt == 2
    assert result.counts.nit == 1
    assert [f.severity for f in result.findings] == ["DEBT", "DEBT", "NIT"]
    again = ReviewEngineSummary.model_validate_json(result.model_dump_json())
    assert again == result
    assert summary.verdict == "FAIL"  # input never mutated


def test_any_must_fix_refuses_even_when_operator_deferred() -> None:
    summary = _summary(
        [
            _shouldfix("fp-0000001"),
            make_finding(fingerprint="fp-000000m", severity="MUST_FIX"),
        ]
    )
    extra = (_entry("fp-000000m", severity="MUST_FIX"),)
    assert (
        reconcile_deferred_summary(
            summary,
            ledger=_ledger("fp-0000001", extra=extra),
            settled_fingerprints={"fp-0000001", "fp-000000m"},
        )
        is None
    )


def test_coverage_and_acceptance_rules_refuse() -> None:
    for rule in ("coverage.missing", "acceptance.criterion-1"):
        summary = _summary([_shouldfix("fp-0000001", rule_id=rule)])
        assert (
            reconcile_deferred_summary(
                summary,
                ledger=_ledger("fp-0000001"),
                settled_fingerprints={"fp-0000001"},
            )
            is None
        )


def test_fingerprint_missing_from_row_set_refuses() -> None:
    summary = _summary([_shouldfix("fp-0000001")])
    assert (
        reconcile_deferred_summary(
            summary, ledger=_ledger("fp-0000001"), settled_fingerprints=set()
        )
        is None
    )


def test_row_record_for_another_head_refuses() -> None:
    task = _make_ticket_task(
        ticket_id=_TICKET,
        review_engine_settlements=[
            ReviewEngineSettlement(
                head_sha="c" * 40, fingerprints=["fp-0000001"], recorded_at=_WHEN
            )
        ],
    )
    assert engine_settled_fingerprints(task, SHA) == frozenset()


def test_ledger_missing_or_rejecting_the_defer_refuses() -> None:
    summary = _summary([_shouldfix("fp-0000001")])
    reject = fresh_ledger(confirm="none", ticket_id=_TICKET).model_copy(
        update={"settled": [_entry("fp-0000001", outcome="reject")]}
    )
    for ledger in (_ledger(), reject):
        assert (
            reconcile_deferred_summary(
                summary, ledger=ledger, settled_fingerprints={"fp-0000001"}
            )
            is None
        )


def test_partial_or_failed_coverage_refuses() -> None:
    for coverage in ("PARTIAL", "FAILED"):
        summary = _summary([_shouldfix("fp-0000001")], coverage=coverage)
        assert (
            reconcile_deferred_summary(
                summary,
                ledger=_ledger("fp-0000001"),
                settled_fingerprints={"fp-0000001"},
            )
            is None
        )


def test_fail_without_gating_findings_refuses() -> None:
    summary = _summary([_NIT], verdict="FAIL")
    assert (
        reconcile_deferred_summary(
            summary, ledger=_ledger(), settled_fingerprints=set()
        )
        is None
    )


def test_summary_already_pass_is_not_applicable() -> None:
    summary = _summary([_NIT])
    assert (
        reconcile_deferred_summary(
            summary, ledger=_ledger(), settled_fingerprints=set()
        )
        is None
    )


def test_pass_through_entries_do_not_change_the_decision() -> None:
    summary = _summary([_shouldfix("fp-0000001")])
    extra = (
        _entry("fp-000000m", severity="MUST_FIX"),
        _entry("fp-000000cov", outcome="defer"),
    )
    plain = reconcile_deferred_summary(
        summary, ledger=_ledger("fp-0000001"), settled_fingerprints={"fp-0000001"}
    )
    padded = reconcile_deferred_summary(
        summary,
        ledger=_ledger("fp-0000001", extra=extra),
        settled_fingerprints={"fp-0000001"},
    )
    assert plain is not None
    assert padded == plain


def test_must_fix_in_current_summary_refuses_despite_ledger_and_row() -> None:
    summary = _summary(
        [
            _shouldfix("fp-0000001"),
            make_finding(fingerprint="fp-000000m", severity="MUST_FIX"),
        ]
    )
    ledger = _ledger("fp-0000001", "fp-000000m")
    assert (
        reconcile_deferred_summary(
            summary, ledger=ledger, settled_fingerprints={"fp-0000001", "fp-000000m"}
        )
        is None
    )


def test_settled_fingerprints_reads_only_the_matching_head() -> None:
    task = _make_ticket_task(
        ticket_id=_TICKET,
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
        review_engine_settlements=[
            ReviewEngineSettlement(
                head_sha=SHA, fingerprints=["fp-0000001"], recorded_at=_WHEN
            )
        ],
    )
    assert engine_settled_fingerprints(task, SHA) == frozenset({"fp-0000001"})
