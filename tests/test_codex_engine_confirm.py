"""Tests for ``cw.codex_engine_confirm``: the codex engine-confirm park (#3216).

1:1 with ``src/cw/codex_engine_confirm.py``. ``engine_confirm_park`` renames a
codex confirm park to ``review_confirm_must_fix`` and records the same
``review.confirm_escalated`` audit event claude-native's ``announce_confirm_park``
records, best-effort. ``pending_lines`` renders the Claude findings kept
reachable when ledger bookkeeping fails. The decided-verdict persistence that
rides on the same gate is covered in ``tests/test_codex_clean_confirm.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.auto_dev_result import (
    REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON,
    AutoDevResult,
)
from cw.codex_engine_confirm import engine_confirm_park, pending_lines
from cw.codex_review import CODEX_MUST_FIX_FINDINGS, make_codex_blocked
from cw.events import read_events
from cw.models import ClientConfig, OrchestratorEvent, OrchestratorEventType
from cw.review_convergence import (
    DECISION_STOP_CONFIRM_MUST_FIX,
    ConvergenceLedger,
    RoundReport,
    confirm_escalated_event_payload,
)
from tests._codex_review_helpers import _task
from tests._review_convergence_helpers import (
    DEFAULT_SHA,
    consolidated_verdict,
    engine_pass_ledger,
    record,
    round_finding,
)

if TYPE_CHECKING:
    from pathlib import Path

_EVENT = OrchestratorEventType.REVIEW_CONFIRM_ESCALATED
_SIGNALS_LOGGER = "cw.codex_review_signals"
_LOGGER = "cw.codex_engine_confirm"
_DETAILS = (
    "## Claude Review Verdict\n\nconvergence ledger stopped (confirm); release: x"
)
_PAYLOAD_KEYS = ["ticket_id", "client", "reason", "round", "reviewed_sha", "identities"]


def _events() -> list[OrchestratorEvent]:
    return list(read_events(consumer="test-3216-confirm", event_types=[_EVENT]))


def _parked_result(tmp_path: Path) -> AutoDevResult:
    """The codex ``codex_must_fix_findings`` park a dirty confirm round returns."""
    result = make_codex_blocked(
        ticket_id="T-1",
        worktree=tmp_path,
        reason=CODEX_MUST_FIX_FINDINGS,
        details=_DETAILS,
    )
    assert result.blocker is not None
    return result


def _client(tmp_path: Path) -> ClientConfig:
    return ClientConfig(name="test", workspace_path=tmp_path)


def _stopped_confirm() -> tuple[ConvergenceLedger, RoundReport]:
    """The post-record ledger and report of an engine confirm that parks."""
    ledger, report = record(
        engine_pass_ledger(), round_finding("MUST_FIX"), sha=DEFAULT_SHA, kind="confirm"
    )
    assert report.decision == DECISION_STOP_CONFIRM_MUST_FIX
    return ledger, report


class TestParkOnConfirmMustFix:
    def test_the_blocker_is_renamed_and_made_non_retryable(
        self, tmp_path: Path
    ) -> None:
        ledger, report = _stopped_confirm()
        result = _parked_result(tmp_path)
        before = result.blocker
        assert before is not None
        assert before.reason == CODEX_MUST_FIX_FINDINGS

        parked = engine_confirm_park(result, report, ledger, _task(), _client(tmp_path))

        assert parked.blocker is not None
        assert parked.blocker.reason == REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON
        assert parked.blocker.retry_eligible is False
        assert parked.blocker.details == _DETAILS
        assert parked.blocker.stage == before.stage
        assert parked.status == result.status
        # the input result is not mutated
        assert result.blocker is before
        assert before.reason == CODEX_MUST_FIX_FINDINGS

    def test_the_event_is_recorded_once_with_the_six_key_payload(
        self, tmp_path: Path
    ) -> None:
        ledger, report = _stopped_confirm()
        task = _task()
        client = _client(tmp_path)

        engine_confirm_park(_parked_result(tmp_path), report, ledger, task, client)

        (event,) = _events()
        assert event.correlation_id == task.ticket_id
        payload = event.payload
        assert list(payload) == _PAYLOAD_KEYS
        assert payload == confirm_escalated_event_payload(
            ledger, ticket_id=task.ticket_id, client=client.name
        )
        assert payload["ticket_id"] == "T-1"
        assert payload["client"] == "test"
        assert payload["round"] == report.round
        assert payload["reviewed_sha"] == DEFAULT_SHA

    def test_one_warning_names_the_ticket_client_reason_and_round(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        ledger, report = _stopped_confirm()

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            engine_confirm_park(
                _parked_result(tmp_path), report, ledger, _task(), _client(tmp_path)
            )

        (record_,) = [r for r in caplog.records if r.name == _LOGGER]
        assert record_.getMessage() == (
            f"codex confirm escalated: ticket=T-1 client=test "
            f"reason={report.reason} round={report.round}"
        )

    def test_an_event_oserror_is_swallowed_and_the_park_is_still_returned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        ledger, report = _stopped_confirm()

        with (
            patch("cw.codex_review_signals.record_event", side_effect=OSError("disk")),
            caplog.at_level(logging.WARNING, logger=_SIGNALS_LOGGER),
        ):
            parked = engine_confirm_park(
                _parked_result(tmp_path), report, ledger, _task(), _client(tmp_path)
            )

        assert parked.blocker is not None
        assert parked.blocker.reason == REVIEW_CONFIRM_MUST_FIX_BLOCKER_REASON
        (failed,) = [r for r in caplog.records if r.name == _SIGNALS_LOGGER]
        assert failed.getMessage().startswith("codex_review_signal_failed")
        assert "review.confirm_escalated" in failed.getMessage()
        assert _events() == []


class TestOtherDecisionsAreUntouched:
    @staticmethod
    def _reports() -> dict[str, tuple[ConvergenceLedger, RoundReport]]:
        clean = record(engine_pass_ledger(), sha=DEFAULT_SHA, kind="confirm")
        # A deferred-and-filed confirm converges like a clean one.
        deferred = record(
            engine_pass_ledger(),
            round_finding("SHOULD_FIX", outcome="defer", rationale="debt"),
            sha=DEFAULT_SHA,
            kind="confirm",
        )
        # A confirm after a non-engine loop round is the legacy decision.
        legacy = record(
            engine_pass_ledger(engine=False),
            round_finding("MUST_FIX"),
            sha=DEFAULT_SHA,
            kind="confirm",
        )
        return {"clean": clean, "deferred": deferred, "non_engine": legacy}

    @pytest.mark.parametrize("case", ["clean", "deferred", "non_engine"])
    def test_the_result_is_returned_unchanged_and_no_event_is_recorded(
        self, tmp_path: Path, case: str
    ) -> None:
        ledger, report = self._reports()[case]
        assert report.decision != DECISION_STOP_CONFIRM_MUST_FIX
        result = _parked_result(tmp_path)

        returned = engine_confirm_park(
            result, report, ledger, _task(), _client(tmp_path)
        )

        assert returned is result
        assert returned.blocker is not None
        assert returned.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert _events() == []

    def test_a_stop_report_over_a_result_without_a_blocker_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        ledger, report = _stopped_confirm()
        clean = _parked_result(tmp_path).model_copy(update={"blocker": None})

        returned = engine_confirm_park(
            clean, report, ledger, _task(), _client(tmp_path)
        )

        assert returned is clean
        assert _events() == []


class TestPendingLines:
    def test_each_gating_finding_renders_severity_location_and_summary(self) -> None:
        verdict = consolidated_verdict(
            accepted=[("MUST_FIX", "broken"), ("SHOULD_FIX", "needs a name")]
        )

        assert pending_lines(verdict) == [
            "MUST_FIX src/cw/foo.py:10 broken",
            "SHOULD_FIX src/cw/foo.py:11 needs a name",
        ]

    def test_non_gating_severities_are_left_out(self) -> None:
        verdict = consolidated_verdict(
            accepted=[("NIT", "style"), ("DEBT", "later"), ("MUST_FIX", "broken")]
        )

        assert pending_lines(verdict) == ["MUST_FIX src/cw/foo.py:12 broken"]

    def test_a_finding_without_a_line_renders_the_bare_file(self) -> None:
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "broken")])
        unanchored = verdict.model_copy(
            update={
                "accepted": [
                    item.model_copy(
                        update={
                            "finding": item.finding.model_copy(
                                update={"line_start": None}
                            )
                        }
                    )
                    for item in verdict.accepted
                ]
            }
        )

        assert pending_lines(unanchored) == ["MUST_FIX src/cw/foo.py broken"]

    def test_a_multiline_summary_collapses_to_one_line(self) -> None:
        verdict = consolidated_verdict(accepted=[("MUST_FIX", "first\n   second")])

        assert pending_lines(verdict) == ["MUST_FIX src/cw/foo.py:10 first second"]

    def test_an_empty_verdict_has_no_lines(self) -> None:
        assert pending_lines(consolidated_verdict()) == []
