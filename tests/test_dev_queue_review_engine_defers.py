"""Tests for :mod:`cw.dev_queue.review_engine_defers` (#3636).

The shared decision core both lanes call: which fingerprints settle, and the
best-effort row write that never raises.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue.review_engine_defers import (
    deferred_fingerprints,
    record_settlement_best_effort,
)
from cw.dev_queue.storage import load_dev_queue
from cw.exceptions import CwError
from cw.models import QueueItemStatus
from tests._cli_review_helpers import _seed_review_row
from tests._review_attestation_helpers import engine_round_finding

if TYPE_CHECKING:
    from cw.review_convergence import RoundFinding

_TICKET = "T-DEFERS"
_CLIENT = "acme"
_HEAD = f"{1:040x}"
_LOGGER = "cw.dev_queue.review_engine_defers"
_WARNING_TEXT = "review engine settlement not recorded"
_RECORDED_AT = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _row_settlements() -> list[tuple[str, list[str]]]:
    rows = [t for t in load_dev_queue().tasks if t.ticket_id == _TICKET]
    return [(s.head_sha, s.fingerprints) for s in rows[0].review_engine_settlements]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING
    ]


class TestDeferredFingerprints:
    def test_all_deferred_should_fix_returns_the_fingerprints_in_order(self) -> None:
        findings = [
            engine_round_finding("fp-a"),
            engine_round_finding("fp-b", rule_id="style.other"),
        ]
        assert deferred_fingerprints(findings) == ["fp-a", "fp-b"]

    @pytest.mark.parametrize(
        "finding",
        [
            engine_round_finding("fp-x", severity="MUST_FIX"),
            engine_round_finding("fp-x", outcome="fix"),
            engine_round_finding("fp-x", outcome="reject"),
            engine_round_finding("fp-x", outcome="operator_action"),
            engine_round_finding(None),
            engine_round_finding("fp-x", rule_id="coverage.untested-branch"),
            engine_round_finding("fp-x", rule_id="acceptance.ac-1"),
        ],
        ids=[
            "must-fix",
            "fix",
            "reject",
            "operator-action",
            "no-fingerprint",
            "coverage-rule",
            "acceptance-rule",
        ],
    )
    def test_one_non_deferrable_finding_records_nothing(
        self, finding: RoundFinding
    ) -> None:
        assert deferred_fingerprints([engine_round_finding("fp-ok"), finding]) == []

    def test_an_empty_findings_list_returns_nothing(self) -> None:
        assert deferred_fingerprints([]) == []


class TestRecordSettlementBestEffort:
    def test_writes_the_row_at_the_head(self) -> None:
        _seed_review_row(_TICKET, _CLIENT, status=QueueItemStatus.RUNNING)
        record_settlement_best_effort(
            _TICKET, _CLIENT, _HEAD, ["fp-1", "fp-2"], recorded_at=_RECORDED_AT
        )
        assert _row_settlements() == [(_HEAD, ["fp-1", "fp-2"])]

    def test_same_head_twice_is_idempotent(self) -> None:
        _seed_review_row(_TICKET, _CLIENT, status=QueueItemStatus.RUNNING)
        for _ in range(2):
            record_settlement_best_effort(
                _TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_RECORDED_AT
            )
        assert _row_settlements() == [(_HEAD, ["fp-1"])]

    def test_no_row_warns_once_and_never_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            record_settlement_best_effort(
                _TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_RECORDED_AT
            )
        records = _warnings(caplog)
        assert len(records) == 1
        assert _WARNING_TEXT in records[0].getMessage()

    def test_a_cwerror_from_the_writer_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def refuse(*_args: object, **_kwargs: object) -> None:
            msg = "refused"
            raise CwError(msg)

        monkeypatch.setattr(
            "cw.dev_queue.review_engine_defers.record_engine_settlement", refuse
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            record_settlement_best_effort(
                _TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_RECORDED_AT
            )
        assert len(_warnings(caplog)) == 1

    def test_an_oserror_warns_once_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def fail(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(
            "cw.dev_queue.review_engine_defers.record_engine_settlement", fail
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            record_settlement_best_effort(
                _TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_RECORDED_AT
            )
        assert len(_warnings(caplog)) == 1

    def test_a_non_review_row_leaves_the_row_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        _seed_review_row(_TICKET, _CLIENT, status=QueueItemStatus.PENDING)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            record_settlement_best_effort(
                _TICKET, _CLIENT, _HEAD, ["fp-1"], recorded_at=_RECORDED_AT
            )
        assert _row_settlements() == []
        assert len(_warnings(caplog)) == 1
