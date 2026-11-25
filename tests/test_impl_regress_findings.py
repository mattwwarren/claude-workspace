"""The REVIEW-to-IMPL regress findings rule (#3694), hermetic on tmp worktrees.

Each case builds a real ``ReviewVerdictEnvelope`` through the shared
``write_review_verdict_envelope`` helper, so the artifact has production's
on-disk shape. The rule never consults git, so no repository is needed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from cw.models import Stage, TicketTask
from cw.models.review_feedback import MAX_REVIEW_REPORT_BYTES, ReviewFeedback
from cw.models.tasks.records import MustFixOverride
from cw.models.tasks.schema import REGRESSED_OWN_BRANCH_HIGH_WATER
from cw.review_park_findings import (
    REVIEW_PARK_FINDINGS_RELATIVE_PATH,
    write_park_findings,
)
from cw.stage_verification.impl_regress_findings import (
    RegressFindings,
    RegressOutcome,
    regress_review_findings,
    with_regress_findings,
)
from cw.stage_verification.posted_text import TRUNCATION_MARKER
from tests._review_history_helpers import write_held_ledger, write_round_verdict
from tests._shared.review_builders import (
    _make_finding,
    _make_gap_finding,
    build_held_verdict,
    regressed_review_row,
    write_review_verdict_envelope,
)

if TYPE_CHECKING:
    from pathlib import Path

_TICKET = "T-3694"
_SHA = "a" * 40
_PARK_TEXT = "1. [HIGH] src/a.py\n   summary: parked\n   evidence: line 3"


def _task(**updates: Any) -> TicketTask:
    """An IMPL row regressed from REVIEW unless *updates* say otherwise."""
    return regressed_review_row(_TICKET, **updates)


def _feedback() -> ReviewFeedback:
    return ReviewFeedback(
        repo="o/r",
        pr_number=1,
        reviewed_sha=_SHA,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        report="red",
    )


def _override() -> MustFixOverride:
    return MustFixOverride(
        actor="operator",
        reason="accepted",
        reviewed_sha=_SHA,
        finding_ids=[],
        recorded_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


def _verdict(worktree: Path, *, must_fix: list[Any] | None = None, **kw: Any) -> None:
    findings = must_fix if must_fix is not None else [_make_finding()]
    write_review_verdict_envelope(
        worktree,
        ticket_id=_TICKET,
        reviewed_sha=_SHA,
        must_fix=findings,
        **kw,
    )


def _delivered(outcome: RegressOutcome | None) -> RegressFindings:
    assert outcome is not None
    assert outcome.findings is not None, outcome.reason
    return outcome.findings


class TestDelivery:
    def test_delivers_must_fix_fields_and_reviewed_sha(self, tmp_path: Path) -> None:
        _verdict(tmp_path, must_fix=[_make_finding(summary="Bug here")])
        findings = _delivered(regress_review_findings(_task(), tmp_path))
        assert findings.reviewed_sha == _SHA
        assert "[MUST_FIX] src/cw/foo.py" in findings.text
        assert "summary: Bug here" in findings.text
        assert "evidence: def broken():" in findings.text

    def test_only_must_fix_entries_render(self, tmp_path: Path) -> None:
        _verdict(
            tmp_path,
            must_fix=[_make_finding(summary="Only this one")],
            blocking=True,
        )
        text = _delivered(regress_review_findings(_task(), tmp_path)).text
        assert text.count("summary:") == 1
        assert "Only this one" in text

    def test_finalize_high_water_delivers(self, tmp_path: Path) -> None:
        _verdict(tmp_path)
        task = _task(stage_high_water=Stage.FINALIZE)
        assert _delivered(regress_review_findings(task, tmp_path)).text

    def test_head_is_not_consulted(self, tmp_path: Path) -> None:
        # tmp_path is not a git repo and reviewed_sha is unrelated to any HEAD:
        # delivery still happens, because the rule never compares SHAs.
        _verdict(tmp_path)
        assert regress_review_findings(_task(), tmp_path) is not None

    def test_retry_still_delivers(self, tmp_path: Path) -> None:
        _verdict(tmp_path)
        task = _task(regressed_into_stage=None, blocked_reason=None, pr_url=None)
        first = _delivered(regress_review_findings(task, tmp_path))
        second = _delivered(regress_review_findings(task, tmp_path))
        assert first == second


class TestNotApplicable:
    def test_non_impl_stage_is_none(self, tmp_path: Path) -> None:
        _verdict(tmp_path)
        assert regress_review_findings(_task(stage=Stage.REVIEW), tmp_path) is None

    def test_non_regressed_high_water_is_none(self, tmp_path: Path) -> None:
        _verdict(tmp_path)
        for high_water in (Stage.PLAN, Stage.IMPL, None):
            task = _task(stage_high_water=high_water)
            assert regress_review_findings(task, tmp_path) is None


class TestWithheld:
    def test_review_feedback_present(self, tmp_path: Path) -> None:
        _verdict(tmp_path)
        task = _task(review_feedback=_feedback())
        outcome = regress_review_findings(task, tmp_path)
        assert outcome is not None
        assert outcome.reason == "review_feedback_present"
        assert outcome.friction == (
            "regress_review_findings: withheld (review_feedback_present)"
        )

    def test_override_present(self, tmp_path: Path) -> None:
        _verdict(tmp_path)
        outcome = regress_review_findings(
            _task(must_fix_override=_override()), tmp_path
        )
        assert outcome is not None
        assert outcome.reason == "override_present"

    def test_artifact_missing_when_absent(self, tmp_path: Path) -> None:
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.reason == "artifact_missing"

    def test_artifact_missing_when_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude" / "review-verdict.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.reason == "artifact_missing"

    def test_artifact_missing_when_unreadable(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude" / "review-verdict.json"
        path.mkdir(parents=True)  # a directory: read_text raises OSError
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.reason == "artifact_missing"

    def test_not_owned_when_another_ticket_wrote_it(self, tmp_path: Path) -> None:
        write_review_verdict_envelope(
            tmp_path,
            ticket_id="OTHER-1",
            reviewed_sha=_SHA,
            must_fix=[_make_finding()],
        )
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.reason == "not_owned"

    def test_not_blocking_when_verdict_is_non_blocking(self, tmp_path: Path) -> None:
        _verdict(tmp_path, blocking=False)
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.reason == "not_blocking"

    def test_not_blocking_when_blocking_but_empty_must_fix(
        self, tmp_path: Path
    ) -> None:
        _verdict(tmp_path, must_fix=[], blocking=True)
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.reason == "not_blocking"

    def test_rewrite_to_non_blocking_ends_delivery(self, tmp_path: Path) -> None:
        _verdict(tmp_path)
        assert _delivered(regress_review_findings(_task(), tmp_path))
        _verdict(tmp_path, blocking=False)
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.reason == "not_blocking"


class TestCapAndRedaction:
    def test_oversized_evidence_is_capped_in_bytes_with_marker(
        self, tmp_path: Path
    ) -> None:
        huge = _make_finding(evidence="é" * (MAX_REVIEW_REPORT_BYTES))
        _verdict(tmp_path, must_fix=[huge])
        text = _delivered(regress_review_findings(_task(), tmp_path)).text
        assert len(text.encode("utf-8")) <= MAX_REVIEW_REPORT_BYTES
        assert text.endswith(TRUNCATION_MARKER)

    def test_long_line_is_not_cut_at_posted_text_default(self, tmp_path: Path) -> None:
        long_summary = "s" * 400
        _verdict(tmp_path, must_fix=[_make_finding(summary=long_summary)])
        text = _delivered(regress_review_findings(_task(), tmp_path)).text
        assert long_summary in text
        assert not text.endswith(TRUNCATION_MARKER)

    def test_secret_token_in_evidence_is_redacted(self, tmp_path: Path) -> None:
        secret = "sk-" + "A" * 24
        _verdict(tmp_path, must_fix=[_make_finding(evidence=f"token {secret}")])
        text = _delivered(regress_review_findings(_task(), tmp_path)).text
        assert secret not in text


class TestWire:
    def test_with_regress_findings_sets_key_when_delivered(
        self, tmp_path: Path
    ) -> None:
        _verdict(tmp_path)
        metadata: dict[str, object] = {"branch": "dev/x", "review_feedback": None}
        out = with_regress_findings(metadata, _task(), tmp_path)
        assert out["branch"] == "dev/x"
        assert out["review_feedback"] is None
        value = out["regress_review_findings"]
        assert isinstance(value, dict)
        assert value["reviewed_sha"] == _SHA
        assert "summary:" in str(value["text"])

    def test_with_regress_findings_null_when_withheld(self, tmp_path: Path) -> None:
        out = with_regress_findings({}, _task(), tmp_path)
        assert out == {"regress_review_findings": None}

    def test_with_regress_findings_null_when_not_applicable(
        self, tmp_path: Path
    ) -> None:
        _verdict(tmp_path)
        out = with_regress_findings({}, _task(stage=Stage.PLAN), tmp_path)
        assert out == {"regress_review_findings": None}


class TestSharedHighWater:
    def test_constant_is_review_and_finalize(self) -> None:
        assert frozenset({Stage.REVIEW, Stage.FINALIZE}) == (
            REGRESSED_OWN_BRANCH_HIGH_WATER
        )

    def test_pr_gate_uses_the_same_object(self) -> None:
        from cw.dispatch import pr_gate

        assert (
            pr_gate.REGRESSED_OWN_BRANCH_HIGH_WATER is REGRESSED_OWN_BRANCH_HIGH_WATER
        )


class TestFriction:
    def test_delivered_friction_line(self, tmp_path: Path) -> None:
        _verdict(tmp_path)
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.friction == "regress_review_findings: delivered"


def _gap(summary: str = "gap") -> Any:
    return _make_gap_finding(summary=summary)


class TestHeldEvidenceGap:
    """The codex lane: a held evidence-gap finding in the saved envelope (#3862)."""

    def test_pre_pr_gap_hold_delivers(self, tmp_path: Path) -> None:
        write_review_verdict_envelope(
            tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, held=[_gap("B")]
        )
        outcome = regress_review_findings(
            _task(pr_url=None, blocked_reason="review_operator_actionable"), tmp_path
        )
        assert outcome is not None
        assert outcome.findings is not None
        assert outcome.findings.reviewed_sha == _SHA
        assert "[MUST_FIX]" in outcome.findings.text
        assert "summary: B" in outcome.findings.text
        assert "evidence gap (not verified by the reviewer):" in outcome.findings.text

    def test_pre_pr_gap_hold_wire_shape(self, tmp_path: Path) -> None:
        write_review_verdict_envelope(
            tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, held=[_gap("B")]
        )
        out = with_regress_findings({}, _task(), tmp_path)
        value = out["regress_review_findings"]
        assert isinstance(value, dict)
        assert set(value) == {"reviewed_sha", "text"}
        assert value["reviewed_sha"] == _SHA

    def test_blocking_verdict_delivers_must_fix_and_carried_gap_once(
        self, tmp_path: Path
    ) -> None:
        write_review_verdict_envelope(
            tmp_path,
            ticket_id=_TICKET,
            reviewed_sha=_SHA,
            must_fix=[_make_finding(summary="A", severity="MUST_FIX")],
            held=[_gap("B")],
        )
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.findings is not None
        text = outcome.findings.text
        assert text.count("summary: A") == 1
        assert text.count("summary: B") == 1
        assert text.index("summary: A") < text.index("summary: B")

    def test_decision_contest_hold_stays_withheld(self, tmp_path: Path) -> None:
        contest = _make_gap_finding(summary="C", contests_decision="we decided X")
        write_review_verdict_envelope(
            tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, held=[contest]
        )
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome == RegressOutcome(None, "not_blocking")

    def test_contest_beside_gap_withholds_the_gap(self, tmp_path: Path) -> None:
        contest = _make_gap_finding(summary="C", contests_decision="we decided X")
        write_review_verdict_envelope(
            tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, held=[contest, _gap("B")]
        )
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome == RegressOutcome(None, "not_blocking")

    def test_contest_beside_survivors_delivers_survivors_only(
        self, tmp_path: Path
    ) -> None:
        contest = _make_gap_finding(summary="C", contests_decision="we decided X")
        write_review_verdict_envelope(
            tmp_path,
            ticket_id=_TICKET,
            reviewed_sha=_SHA,
            must_fix=[_make_finding(summary="S", severity="MUST_FIX")],
            held=[contest, _gap("B")],
        )
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.findings is not None
        assert "summary: S" in outcome.findings.text
        assert "summary: B" not in outcome.findings.text

    def test_held_non_must_fix_not_delivered(self, tmp_path: Path) -> None:
        note = _make_gap_finding(severity="SHOULD_FIX", summary="note")
        write_review_verdict_envelope(
            tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, held=[note]
        )
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_blocking"
        )

    def test_gap_finding_text_is_capped(self, tmp_path: Path) -> None:
        big = "x" * (MAX_REVIEW_REPORT_BYTES + 100)
        write_review_verdict_envelope(
            tmp_path,
            ticket_id=_TICKET,
            reviewed_sha=_SHA,
            held=[_make_finding(severity="MUST_FIX", summary="B", evidence_gap=big)],
        )
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.findings is not None
        assert outcome.findings.text.endswith(TRUNCATION_MARKER)

    def test_review_feedback_present_still_wins_over_gap_hold(
        self, tmp_path: Path
    ) -> None:
        write_review_verdict_envelope(
            tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, held=[_gap("B")]
        )
        outcome = regress_review_findings(
            _task(pr_url="https://example/pr/1", review_feedback=_feedback()),
            tmp_path,
        )
        assert outcome == RegressOutcome(None, "review_feedback_present")

    def test_non_blocking_without_held_findings_still_not_blocking(
        self, tmp_path: Path
    ) -> None:
        write_review_verdict_envelope(
            tmp_path,
            ticket_id=_TICKET,
            reviewed_sha=_SHA,
            must_fix=[_make_finding(summary="A", severity="MUST_FIX")],
            blocking=False,
        )
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_blocking"
        )


class TestLedgerLane:
    """The Claude-native clean-mode lane: the saved round verdict (#3862)."""

    def test_pre_pr_clean_mode_gap_hold_delivers(self, tmp_path: Path) -> None:
        write_held_ledger(tmp_path, [[_gap("A")]], ticket_id=_TICKET)
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.findings is not None
        assert "summary: A" in outcome.findings.text

    def test_held_in_round_one_rounds_two_to_four_clean_is_delivered(
        self, tmp_path: Path
    ) -> None:
        write_held_ledger(tmp_path, [[_gap("A")], [], [], []], ticket_id=_TICKET)
        out = with_regress_findings({}, _task(), tmp_path)
        value = out["regress_review_findings"]
        assert isinstance(value, dict)
        assert value["reviewed_sha"] == "sha-0001"

    def test_contest_in_selected_round_withholds_gap(self, tmp_path: Path) -> None:
        contest = _make_gap_finding(summary="C", contests_decision="we decided X")
        write_held_ledger(tmp_path, [[contest, _gap("B")]], ticket_id=_TICKET)
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_blocking"
        )

    def test_ledger_no_round_holds_finding_reports_not_blocking(
        self, tmp_path: Path
    ) -> None:
        write_held_ledger(tmp_path, [[], []], ticket_id=_TICKET)
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_blocking"
        )

    def test_ledger_head_mismatch_reports_not_owned(self, tmp_path: Path) -> None:
        write_held_ledger(tmp_path, [[_gap("A")]], ticket_id=_TICKET)
        write_round_verdict(
            tmp_path,
            1,
            build_held_verdict([_gap("A")], reviewed_sha="sha-other"),
        )
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_owned"
        )

    def test_ledger_foreign_ticket_reports_not_owned(self, tmp_path: Path) -> None:
        write_held_ledger(tmp_path, [[_gap("A")]], ticket_id="OTHER-1")
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_owned"
        )

    def test_ledger_lane_does_not_deliver_ledger_must_fix(self, tmp_path: Path) -> None:
        write_held_ledger(tmp_path, [[]], ticket_id=_TICKET)
        write_round_verdict(
            tmp_path,
            1,
            build_held_verdict(
                [], reviewed_sha="sha-0001", must_fix=[_make_finding(summary="A")]
            ),
        )
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_blocking"
        )

    def test_envelope_present_wins_over_ledger(self, tmp_path: Path) -> None:
        write_review_verdict_envelope(
            tmp_path,
            ticket_id=_TICKET,
            reviewed_sha=_SHA,
            must_fix=[_make_finding(summary="E", severity="MUST_FIX")],
        )
        write_held_ledger(tmp_path, [[_gap("L")]], ticket_id=_TICKET)
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.findings is not None
        assert "summary: E" in outcome.findings.text
        assert "summary: L" not in outcome.findings.text

    def test_foreign_envelope_does_not_fall_back_to_ledger(
        self, tmp_path: Path
    ) -> None:
        write_review_verdict_envelope(
            tmp_path,
            ticket_id="OTHER-1",
            reviewed_sha=_SHA,
            held=[_gap("B")],
        )
        write_held_ledger(tmp_path, [[_gap("L")]], ticket_id=_TICKET)
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_owned"
        )

    def test_blocking_mode_claude_native_still_withheld(self, tmp_path: Path) -> None:
        """With no saved park-findings record (#3762) there is nothing to deliver."""
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "artifact_missing"
        )


def _park(worktree: Path, text: str = _PARK_TEXT, ticket_id: str = _TICKET) -> None:
    assert write_park_findings(
        worktree, ticket_id=ticket_id, reviewed_sha=_SHA, text=text
    )


class TestParkFindingsLane:
    """A claude-native park's saved blocking-findings comment (#3762)."""

    @pytest.mark.parametrize("high_water", [Stage.REVIEW, Stage.FINALIZE])
    def test_delivers_text_and_reviewed_sha(
        self, tmp_path: Path, high_water: Stage
    ) -> None:
        _park(tmp_path)
        task = _task(stage_high_water=high_water)
        findings = _delivered(regress_review_findings(task, tmp_path))
        assert (findings.reviewed_sha, findings.text) == (_SHA, _PARK_TEXT)

    def test_head_is_not_consulted_and_retry_still_delivers(
        self, tmp_path: Path
    ) -> None:
        _park(tmp_path)
        first = _delivered(regress_review_findings(_task(), tmp_path))
        second = _delivered(regress_review_findings(_task(), tmp_path))
        assert first == second

    def test_beats_a_codex_envelope_and_a_ledger_round(self, tmp_path: Path) -> None:
        _verdict(tmp_path, must_fix=[_make_finding(summary="from the envelope")])
        write_held_ledger(tmp_path, [[_gap("L")]], ticket_id=_TICKET)
        _park(tmp_path)
        text = _delivered(regress_review_findings(_task(), tmp_path)).text
        assert text == _PARK_TEXT

    def test_foreign_ticket_is_not_owned_without_falling_through(
        self, tmp_path: Path
    ) -> None:
        _verdict(tmp_path)
        _park(tmp_path, ticket_id="OTHER-1")
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "not_owned"
        )

    @pytest.mark.parametrize("raw", ["{not json", '{"ticket_id": "T-3694"}'])
    def test_invalid_artifact_falls_through_to_artifact_missing(
        self, tmp_path: Path, raw: str
    ) -> None:
        (tmp_path / ".cw").mkdir()
        (tmp_path / REVIEW_PARK_FINDINGS_RELATIVE_PATH).write_text(
            raw, encoding="utf-8"
        )
        assert regress_review_findings(_task(), tmp_path) == RegressOutcome(
            None, "artifact_missing"
        )

    def test_blank_artifact_falls_through_to_the_verdict_readers(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".cw").mkdir()
        (tmp_path / REVIEW_PARK_FINDINGS_RELATIVE_PATH).write_text(
            json.dumps({"ticket_id": _TICKET, "reviewed_sha": _SHA, "text": " "}),
            encoding="utf-8",
        )
        _verdict(tmp_path, must_fix=[_make_finding(summary="from the envelope")])
        text = _delivered(regress_review_findings(_task(), tmp_path)).text
        assert "from the envelope" in text

    def test_review_feedback_still_wins(self, tmp_path: Path) -> None:
        _park(tmp_path)
        outcome = regress_review_findings(_task(review_feedback=_feedback()), tmp_path)
        assert outcome == RegressOutcome(None, "review_feedback_present")

    def test_override_still_wins(self, tmp_path: Path) -> None:
        _park(tmp_path)
        outcome = regress_review_findings(
            _task(must_fix_override=_override()), tmp_path
        )
        assert outcome == RegressOutcome(None, "override_present")

    def test_oversized_text_is_capped_and_a_secret_redacted_on_delivery(
        self, tmp_path: Path
    ) -> None:
        secret = "ghp_" + "a" * 36
        (tmp_path / ".cw").mkdir()
        # Written by hand: the writer itself caps, so this is a record that
        # predates (or bypasses) it, and delivery must still bound it.
        (tmp_path / REVIEW_PARK_FINDINGS_RELATIVE_PATH).write_text(
            json.dumps(
                {
                    "ticket_id": _TICKET,
                    "reviewed_sha": _SHA,
                    "text": f"{secret}\n" + "x" * (MAX_REVIEW_REPORT_BYTES + 100),
                }
            ),
            encoding="utf-8",
        )
        text = _delivered(regress_review_findings(_task(), tmp_path)).text
        assert secret not in text
        assert text.endswith(TRUNCATION_MARKER)
        assert len(text.encode("utf-8")) <= MAX_REVIEW_REPORT_BYTES

    def test_wire_shape_and_friction_line(self, tmp_path: Path) -> None:
        _park(tmp_path)
        metadata = with_regress_findings({"attempt": 1}, _task(), tmp_path)
        assert metadata == {
            "attempt": 1,
            "regress_review_findings": {"reviewed_sha": _SHA, "text": _PARK_TEXT},
        }
        outcome = regress_review_findings(_task(), tmp_path)
        assert outcome is not None
        assert outcome.friction == "regress_review_findings: delivered"


class TestReadOwnedVerdictBlockingFlag:
    """With require_blocking off, the caller owns the blocking decision (#3862)."""

    def test_default_still_refuses_non_blocking(self, tmp_path: Path) -> None:
        from cw.dev_queue.must_fix_override import read_owned_verdict

        path = write_review_verdict_envelope(
            tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, blocking=False
        )
        read = read_owned_verdict(path, _TICKET)
        assert read.verdict is None
        assert read.refusal == "not_blocking"

    def test_non_blocking_returned_when_caller_owns_the_decision(
        self, tmp_path: Path
    ) -> None:
        from cw.dev_queue.must_fix_override import read_owned_verdict

        path = write_review_verdict_envelope(
            tmp_path, ticket_id=_TICKET, reviewed_sha=_SHA, blocking=False
        )
        read = read_owned_verdict(path, _TICKET, require_blocking=False)
        assert read.refusal is None
        assert read.verdict is not None
        assert read.verdict.blocking is False
