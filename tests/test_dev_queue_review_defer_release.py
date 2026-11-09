"""Regress releases unticketed review defers, keeps ticketed ones (#3137)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from cw.dev_queue.lifecycle import _stage_regress
from cw.dev_queue.review_defer_release import release_unticketed_defers
from cw.models import Stage
from cw.review_adjudication import DEFERRED_FINDINGS_RELATIVE_PATH, Adjudication
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    SettledEntry,
    finding_identity,
    load_ledger,
)
from tests._review_convergence_helpers import fresh_ledger, write_ledger
from tests._review_debt_helpers import _debt_record, write_deferred_findings
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger

_LOGGER = "cw.dev_queue.review_defer_release"
_FILED_AT = "2026-11-01T00:00:00Z"


def _defer_entry(summary: str, *, ticket: str = "") -> Adjudication:
    return Adjudication(
        severity="SHOULD_FIX",
        file="src/cw/foo.py",
        line_start=None,
        line_end=None,
        evidence="",
        summary=summary,
        outcome="defer",
        rationale="non-gating",
        round=1,
        recorded_at=_FILED_AT,
        ticket=ticket,
    )


def _settled(
    summary: str,
    *,
    outcome: Literal["reject", "defer"] = "defer",
    origin: Literal["adjudication", "operator"] = "adjudication",
    fingerprint: str | None = None,
) -> SettledEntry:
    return SettledEntry(
        identity=finding_identity(
            "SHOULD_FIX", "src/cw/foo.py", summary, fingerprint=fingerprint
        ),
        outcome=outcome,
        rationale="non-gating",
        round=1,
        origin=origin,
    )


def _ledger_with(*settled: SettledEntry) -> ConvergenceLedger:
    return fresh_ledger().model_copy(update={"settled": list(settled)})


def _task(worktree: Path, *, review_debt: list[object] | None = None) -> TicketTask:
    return _make_ticket_task(
        ticket_id="101",
        client="acme",
        stage=Stage.REVIEW,
        worktree_path=str(worktree),
        review_debt=review_debt or [],
    )


def _settled_summaries(worktree: Path) -> set[str]:
    ledger = load_ledger(worktree / LEDGER_RELATIVE_PATH)
    assert ledger is not None
    return {entry.identity.summary_normalized for entry in ledger.settled}


def test_unticketed_adjudicated_defer_is_released(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_ledger(tmp_path, _ledger_with(_settled("Stale claim")))
    write_deferred_findings(tmp_path, _defer_entry("Stale claim"))
    caplog.set_level(logging.INFO, logger=_LOGGER)

    released = release_unticketed_defers(_task(tmp_path), None)

    assert released == 2
    assert _settled_summaries(tmp_path) == set()
    assert not (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).exists()
    assert "released unticketed review defers on regress ticket=101" in caplog.text


def test_artifact_ticketed_defer_keeps_its_ticket(tmp_path: Path) -> None:
    write_ledger(tmp_path, _ledger_with(_settled("Filed claim")))
    write_deferred_findings(tmp_path, _defer_entry("Filed claim", ticket="#55"))

    released = release_unticketed_defers(_task(tmp_path), None)

    assert released == 0
    assert _settled_summaries(tmp_path) == {"Filed claim"}
    assert (tmp_path / DEFERRED_FINDINGS_RELATIVE_PATH).exists()


def test_engine_defer_ticketed_only_by_row_record_is_kept(tmp_path: Path) -> None:
    write_ledger(tmp_path, _ledger_with(_settled("Engine claim", fingerprint="fp1")))
    row = _debt_record(key="engine:fp1", ticket_id="101")
    task = _task(tmp_path, review_debt=[row])

    assert release_unticketed_defers(task, None) == 0
    assert _settled_summaries(tmp_path) == {"Engine claim"}


def test_engine_defer_without_row_record_is_released(tmp_path: Path) -> None:
    write_ledger(tmp_path, _ledger_with(_settled("Engine claim", fingerprint="fp1")))

    assert release_unticketed_defers(_task(tmp_path), None) == 1
    assert _settled_summaries(tmp_path) == set()


def test_operator_and_reject_settles_are_kept(tmp_path: Path) -> None:
    write_ledger(
        tmp_path,
        _ledger_with(
            _settled("Operator call", origin="operator"),
            _settled("Rejected claim", outcome="reject"),
        ),
    )

    assert release_unticketed_defers(_task(tmp_path), None) == 0
    assert _settled_summaries(tmp_path) == {"Operator call", "Rejected claim"}


def test_no_worktree_is_a_quiet_no_op(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    task = _make_ticket_task(ticket_id="101", client="acme", stage=Stage.REVIEW)

    assert release_unticketed_defers(task, None) == 0
    assert caplog.text == ""


def test_corrupt_ledger_is_logged_and_left_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / LEDGER_RELATIVE_PATH).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / LEDGER_RELATIVE_PATH).write_text("{not json", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    assert release_unticketed_defers(_task(tmp_path), None) == 0
    assert "review defer release skipped ticket=101 client=acme" in caplog.text


def test_regress_into_impl_releases_with_client_config(
    tmp_path: Path, sample_client_config: ClientConfig
) -> None:
    write_ledger(tmp_path, _ledger_with(_settled("Stale claim")))
    task = _task(tmp_path)

    _stage_regress(task, Stage.IMPL, client_cfg=sample_client_config)

    assert _settled_summaries(tmp_path) == set()


def test_regress_into_review_does_not_release(tmp_path: Path) -> None:
    write_ledger(tmp_path, _ledger_with(_settled("Stale claim")))
    task = _task(tmp_path)

    _stage_regress(task, Stage.REVIEW, client_cfg=None)

    assert _settled_summaries(tmp_path) == {"Stale claim"}
