"""Tests for :func:`cw.review_debt_filing.file_review_debt` (#3041, R17)."""

from __future__ import annotations

import pytest

from cw.gh import GhFailure
from cw.models import OrchestratorEventType
from cw.review_debt_filing import FilingFailureKind, file_review_debt
from cw.review_debt_filing._marker import debt_marker, scope_prefix
from cw.review_debt_filing._render import render_body
from tests._fake_gh import FAKE_ISSUE_URL_BASE, FakeGh
from tests._review_debt_helpers import (
    DEBT_FILED_AT,
    FakeClock,
    _clock_stepper,
    _debt_ctx,
    _debt_entry,
    _debt_record,
    _events_of,
)

_A = _debt_entry(summary="first finding")
_B = _debt_entry(summary="second finding", file="src/cw/bar.py")


def _create_calls(gh: FakeGh) -> list[str]:
    return [call for call in gh.calls if call.startswith("create_issue")]


def test_files_each_entry_after_ensuring_the_label_once() -> None:
    gh = FakeGh()
    result = file_review_debt([_A, _B], _debt_ctx(), gh)
    assert gh.calls[0] == "ensure_label:review-debt"
    assert gh.calls.count("ensure_label:review-debt") == 1
    assert gh.calls[1] == "list_open_issues_by_label:review-debt"
    assert len(_create_calls(gh)) == 2
    assert gh.issue_labels[f"review-debt: SHOULD_FIX {_A.summary}"] == ["review-debt"]
    assert [(r.key, r.mode, r.ticket_id) for r in result.records] == [
        (_A.key, "filed", "100"),
        (_B.key, "filed", "101"),
    ]
    assert result.records[0].ticket_url == f"{FAKE_ISSUE_URL_BASE}100"
    assert result.records[0].filed_at == DEBT_FILED_AT
    assert result.failures == []
    filed = _events_of(OrchestratorEventType.REVIEW_DEBT_FILED)
    assert [e["key"] for e in filed] == [_A.key, _B.key]
    assert filed[0] == {
        "ticket_id": "3041",
        "client": "acme",
        "branch": "dev/3041",
        "key": _A.key,
        "tracker": "github-issues",
        "tracker_id": "100",
        "url": f"{FAKE_ISSUE_URL_BASE}100",
        "mode": "filed",
    }


def test_a_failed_event_write_after_create_still_returns_and_keeps_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.review_debt_filing._events.record_event", _boom)
    gh = FakeGh()
    result = file_review_debt([_A], _debt_ctx(), gh)
    assert len(_create_calls(gh)) == 1
    assert [(r.key, r.ticket_id, r.mode) for r in result.records] == [
        (_A.key, "100", "filed")
    ]
    assert result.failures == []


def test_no_gh_call_runs_under_a_lock() -> None:
    gh = FakeGh()
    file_review_debt([_A, _B], _debt_ctx(), gh)
    assert gh.lock_snapshots
    assert all(snapshot == () for snapshot in gh.lock_snapshots)


def test_dedup_by_row_record_makes_no_gh_call() -> None:
    gh = FakeGh()
    ctx = _debt_ctx(existing=(_debt_record(key=_A.key, ticket_id="55"),))
    result = file_review_debt([_A], ctx, gh)
    assert gh.calls == []
    assert result.deduped_keys == (_A.key,)
    assert result.records == []
    (event,) = _events_of(OrchestratorEventType.REVIEW_DEBT_DEDUPED)
    assert (event["via"], event["tracker_id"]) == ("row", "55")


def test_dedup_by_marker_on_a_fresh_row() -> None:
    gh = FakeGh()
    gh.open_issues[77] = render_body(_A, _debt_ctx())
    result = file_review_debt([_A], _debt_ctx(), gh)
    assert _create_calls(gh) == []
    assert [(r.key, r.ticket_id, r.ticket_url) for r in result.records] == [
        (_A.key, "77", "#77")
    ]
    (event,) = _events_of(OrchestratorEventType.REVIEW_DEBT_DEDUPED)
    assert (event["via"], event["tracker_id"]) == ("marker", "77")


def test_marker_dedup_is_scoped_to_the_same_queue_ticket() -> None:
    gh = FakeGh()
    gh.open_issues[77] = render_body(_A, _debt_ctx(ticket_ref="#9999"))
    result = file_review_debt([_A], _debt_ctx(), gh)
    assert len(_create_calls(gh)) == 1
    assert result.deduped_keys == ()


def test_an_anchorless_entry_dedupes_by_its_key() -> None:
    entry = _debt_entry(file="N/A", line=None, summary="3 docs are stale")
    rearrived = _debt_entry(file="N/A", line=None, summary="4 docs  are stale")
    assert entry.key == rearrived.key
    gh = FakeGh()
    gh.open_issues[5] = (
        f"{scope_prefix('#3041')} `dev/3041`.\n\n{debt_marker(entry.key)}\n"
    )
    result = file_review_debt([rearrived], _debt_ctx(), gh)
    assert result.deduped_keys == (entry.key,)


def test_a_closed_match_is_a_miss() -> None:
    """``--state open`` excludes it, so only open issues ever reach the match."""
    gh = FakeGh()
    gh.bodies[77] = render_body(_A, _debt_ctx())  # closed: not in open_issues
    file_review_debt([_A], _debt_ctx(), gh)
    assert len(_create_calls(gh)) == 1


@pytest.mark.parametrize(
    ("gh_failure", "kind"),
    [
        (GhFailure.NETWORK, FilingFailureKind.NETWORK),
        (GhFailure.AUTH, FilingFailureKind.AUTH),
        (GhFailure.LABEL, FilingFailureKind.LABEL),
        (GhFailure.MILESTONE, FilingFailureKind.OTHER),
    ],
)
def test_a_create_failure_degrades_per_entry(
    gh_failure: GhFailure, kind: FilingFailureKind
) -> None:
    gh = FakeGh()
    gh.fail_create_kind = gh_failure
    result = file_review_debt([_A, _B], _debt_ctx(), gh)
    assert result.records == []
    assert [(f.key, f.kind) for f in result.failures] == [
        (_A.key, kind),
        (_B.key, kind),
    ]
    failed = _events_of(OrchestratorEventType.REVIEW_DEBT_FILING_FAILED)
    assert [e["failure"] for e in failed] == [kind.value, kind.value]
    assert failed[0]["tracker_id"] is None


def test_a_label_failure_files_nothing() -> None:
    gh = FakeGh()
    gh.fail_ensure_label = GhFailure.AUTH
    result = file_review_debt([_A, _B], _debt_ctx(), gh)
    assert _create_calls(gh) == []
    assert {f.kind for f in result.failures} == {FilingFailureKind.AUTH}


def test_a_lookup_failure_files_nothing() -> None:
    gh = FakeGh()
    gh.fail_list = GhFailure.NETWORK
    result = file_review_debt([_A], _debt_ctx(), gh)
    assert _create_calls(gh) == []
    assert result.failures[0].kind is FilingFailureKind.LOOKUP_FAILED


def test_a_truncated_lookup_without_a_match_fails_closed() -> None:
    gh = FakeGh()
    gh.list_truncated = True
    gh.open_issues[77] = render_body(_A, _debt_ctx())
    result = file_review_debt([_A, _B], _debt_ctx(), gh)
    assert _create_calls(gh) == []
    assert result.deduped_keys == (_A.key,)
    assert [(f.key, f.kind) for f in result.failures] == [
        (_B.key, FilingFailureKind.LOOKUP_FAILED)
    ]


def test_a_missing_row_creates_nothing() -> None:
    gh = FakeGh()
    result = file_review_debt([_A, _B], _debt_ctx(existing=None), gh)
    assert gh.calls == []
    assert [f.kind for f in result.failures] == [FilingFailureKind.OTHER] * 2


def test_budget_exhaustion_leaves_the_rest_unticketed() -> None:
    clock = FakeClock()
    gh = FakeGh()
    gh.on_call = _clock_stepper(clock, 40.0)
    ctx = _debt_ctx(clock=clock, budget_s=90.0)
    entries = [_A, _B, _debt_entry(summary="third")]
    result = file_review_debt(entries, ctx, gh)
    # label (t=0->40), list (40->80), create A at 80 (10s left), then spent.
    assert [r.key for r in result.records] == [_A.key]
    assert [f.kind for f in result.failures] == [
        FilingFailureKind.BUDGET_EXHAUSTED,
        FilingFailureKind.BUDGET_EXHAUSTED,
    ]


def test_each_call_gets_min_of_its_cap_and_the_remaining_budget() -> None:
    clock = FakeClock()
    gh = FakeGh()
    gh.on_call = _clock_stepper(clock, 35.0)
    file_review_debt([_A], _debt_ctx(clock=clock, budget_s=90.0), gh)
    assert gh.timeouts == [
        ("ensure_label", 20),
        ("list_open_issues_by_label", 30),
        ("create_issue", 20),
    ]


def test_budget_spent_before_the_label_fails_everything() -> None:
    result = file_review_debt([_A], _debt_ctx(budget_s=0.0), FakeGh())
    assert result.failures[0].kind is FilingFailureKind.BUDGET_EXHAUSTED


def test_budget_spent_before_the_lookup_fails_everything() -> None:
    clock = FakeClock()
    gh = FakeGh()
    gh.on_call = _clock_stepper(clock, 100.0)
    result = file_review_debt([_A], _debt_ctx(clock=clock, budget_s=90.0), gh)
    assert gh.calls == ["ensure_label:review-debt"]
    assert result.failures[0].kind is FilingFailureKind.BUDGET_EXHAUSTED


def test_an_entry_already_carrying_a_ticket_is_skipped() -> None:
    gh = FakeGh()
    result = file_review_debt([_A._replace(ticket="#12")], _debt_ctx(), gh)
    assert gh.calls == []
    assert (result.records, result.failures, result.deduped_keys) == ([], [], ())


def test_duplicate_keys_file_once() -> None:
    gh = FakeGh()
    file_review_debt([_A, _A], _debt_ctx(), gh)
    assert len(_create_calls(gh)) == 1


def test_a_retry_after_a_failure_files_only_the_failed_entries() -> None:
    gh = FakeGh()
    gh.fail_create_issue = {f"review-debt: SHOULD_FIX {_B.summary}"}
    first = file_review_debt([_A, _B], _debt_ctx(), gh)
    assert [r.key for r in first.records] == [_A.key]
    gh.fail_create_issue = set()
    second = file_review_debt([_A, _B], _debt_ctx(existing=tuple(first.records)), gh)
    assert [r.key for r in second.records] == [_B.key]
    assert second.deduped_keys == (_A.key,)


def test_a_failure_detail_is_redacted_and_capped() -> None:
    gh = FakeGh()
    gh.fail_create_kind = GhFailure.OTHER
    result = file_review_debt(
        [_debt_entry(summary="s " * 100)], _debt_ctx(home="/home/alice"), gh
    )
    failure = result.failures[0]
    assert len(failure.summary) <= 80
    assert failure.detail == "other failure"
