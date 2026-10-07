"""Tests for ``cw.review_finding_dispositions.suppress`` (GitHub #1838, #2498).

The mechanical suppression backstop: the exact tier, the per-lane claim-tier
gate and its shadow record, and the ``REVERSED`` outcome. Relocated verbatim
from the flat ``tests/test_review_finding_dispositions.py`` when the module
became a package (#2498), 1:1 with the ``suppress`` submodule per the CLAUDE.md
Testing convention. Reuses ``tests/conftest.py``'s ``_make_finding`` fixture
rather than re-declaring an equivalent; ``_accepted``/``_verdict`` are declared
file-local here for the same reason ``tests/test_review_adjudication.py``
declares its own — they are thin, module-specific construction helpers, not
generically reusable builders.
"""

from __future__ import annotations

import logging

import pytest

from cw.auto_dev_result import Review
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
    build_finding_disposition_ledger,
    merge_finding_dispositions,
    suppress_adjudicated_findings,
)
from cw.review_finding_dispositions.suppress import _render_suppression_signal
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict
from tests._cli_review_helpers import CLAIM_ROW1_CANDIDATE, CLAIM_ROW1_RECORDED

from .conftest import _make_finding

_LOGGER = "cw.review_finding_dispositions"
_TICKET = "T-1838"
#: The gh login ``cw review settle`` would record as having settled a finding.
_OPERATOR = "mattwwarren"


def _accepted(finding: Finding, **overrides: object) -> AcceptedFinding:
    """An AcceptedFinding at its post-consolidate default disposition."""
    kwargs: dict[str, object] = {"finding": finding, "reviewers": ["Test Reviewer"]}
    kwargs.update(overrides)
    return AcceptedFinding.model_validate(kwargs)


def _verdict(*accepted: AcceptedFinding, **overrides: object) -> ReviewVerdict:
    """A ReviewVerdict shaped the way ``consolidate_verdict`` builds one."""
    must_fix = [af.finding for af in accepted if af.finding.severity == "MUST_FIX"]
    review = Review(
        must_fix_initial=len(must_fix),
        should_fix=sum(1 for af in accepted if af.finding.severity == "SHOULD_FIX"),
        fix_cycles_used=0,
        deferred=0,
        agents_run=len(accepted) or 1,
    )
    kwargs: dict[str, object] = {
        "blocking": bool(must_fix),
        "must_fix": must_fix,
        "reviewed_sha": "abc1234",
        "accepted": list(accepted),
        "review": review,
    }
    kwargs.update(overrides)
    return ReviewVerdict.model_validate(kwargs)


def _entry(**overrides: object) -> FindingDisposition:
    """A FindingDisposition shaped the way ``cw review settle`` mints one.

    Carries the full provenance set by default (#2210 round 2): the reader
    applies a record only when it can say which finding, who settled it, when,
    against what code, and why — so a fixture missing any of those would
    silently stop testing suppression at all.
    """
    kwargs: dict[str, object] = {
        "outcome": "REJECTED",
        "rationale": "intentional tradeoff, settled in round 1",
        "recorded_at": "2026-08-16T00:00:00Z",
        "actor": _OPERATOR,
        "reviewed_sha": "abc1234",
        "summary": "Bug here",
    }
    kwargs.update(overrides)
    return FindingDisposition.model_validate(kwargs)


def _ledger_for(
    file: str, summary: str, /, **overrides: object
) -> dict[str, FindingDisposition]:
    """A single-entry ledger keyed on ``(file, summary)`` (#2210).

    Module-scope so the claim/contest/gate classes added by #2210 can build
    multi-entry ledgers as dict unions of it rather than each growing a
    near-identical class-local builder.

    Both parameters are positional-only so ``**overrides`` can still carry a
    ``summary=`` of its own — the entry's VERBATIM summary is a provenance
    field a test may want to blank independently of the key it is filed under.
    """
    key = _disposition_key(file, summary)
    assert key is not None
    overrides.setdefault("summary", summary)
    return {key: _entry(**overrides)}


def _ledger(finding: Finding, **overrides: object) -> dict[str, FindingDisposition]:
    """A single-entry ledger keyed on *finding*'s own identity."""
    return _ledger_for(finding.file, finding.summary, **overrides)


def _key(file: str = "src/cw/foo.py", summary: str = "Bug here") -> str:
    """The real ledger key for ``(file, summary)``.

    Goes through :func:`_disposition_key` rather than hard-coding the shape:
    the key binds a digest of the verbatim summary (#2210 round 3), so a
    hand-typed key is either a legacy digest-less one or a wrong one.
    """
    key = _disposition_key(file, summary)
    assert key is not None
    return key


# ---------------------------------------------------------------------------
# suppress_adjudicated_findings
# ---------------------------------------------------------------------------


class TestSuppressAdjudicatedFindings:
    def test_rejected_entry_suppresses_the_matching_must_fix(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(_accepted(finding))
        assert verdict.blocking is True

        suppressed = suppress_adjudicated_findings(
            verdict, _ledger(finding), ticket_id=_TICKET
        )

        assert suppressed.blocking is False
        assert suppressed.must_fix == []
        assert suppressed.accepted[0].disposition == "rejected"
        assert "settled in round 1" in suppressed.accepted[0].disposition_detail

    def test_visibility_signal_is_stamped_on_disposition_detail(self) -> None:
        # Operator-mandated acceptance criterion (#1838 Decisions): a
        # suppression must be VISIBLE, not merely effective. The detail is what
        # `_disposition_annotation`/`_render_findings` surface on the posted
        # comment, so asserting it here is asserting the operator-facing signal.
        finding = _make_finding(severity="MUST_FIX")
        entry = _entry(recorded_at="2026-08-16T12:00:00Z")
        suppressed = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            _ledger(finding, recorded_at="2026-08-16T12:00:00Z"),
            ticket_id=_TICKET,
        )

        detail = suppressed.accepted[0].disposition_detail
        assert detail == _render_suppression_signal(
            finding.file, finding.summary, entry
        )
        assert finding.file in detail
        assert "suppressed by prior REJECTED adjudication" in detail
        assert "2026-08-16T12:00:00Z" in detail
        assert "re-adjudicate if the code at this location has changed" in detail

    def test_accepted_outcome_does_not_change_blocking(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict, _ledger(finding, outcome="ACCEPTED"), ticket_id=_TICKET
        )

        assert suppressed.blocking is True
        assert [f.summary for f in suppressed.must_fix] == [finding.summary]
        assert suppressed.accepted[0].disposition == "fixed"

    def test_unmatched_finding_passes_through_unchanged(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        other = _make_finding(
            severity="MUST_FIX", file="src/cw/other.py", summary="Different bug"
        )
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict, _ledger(other), ticket_id=_TICKET
        )

        assert suppressed == verdict

    def test_empty_ledger_is_a_no_op(self) -> None:
        verdict = _verdict(_accepted(_make_finding(severity="MUST_FIX")))
        assert suppress_adjudicated_findings(verdict, {}, ticket_id=_TICKET) is verdict

    def test_already_rejected_sibling_is_not_resurrected_into_must_fix(self) -> None:
        # This function runs AFTER apply_voided_suppression, so a MUST_FIX the
        # void path already stamped "rejected" must stay out of must_fix even
        # though this pass's own ledger never matched it.
        voided = _accepted(
            _make_finding(severity="MUST_FIX", file="src/cw/voided.py"),
            disposition="rejected",
            disposition_detail="voided by operator",
        )
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(
            voided, _accepted(finding), blocking=True, must_fix=[finding]
        )

        suppressed = suppress_adjudicated_findings(
            verdict, _ledger(finding), ticket_id=_TICKET
        )

        assert suppressed.blocking is False
        assert suppressed.must_fix == []

    def test_no_diff_anchor_finding_is_never_suppressed(self) -> None:
        finding = _make_finding(
            severity="MUST_FIX",
            file="N/A",
            line_start=None,
            line_end=None,
            no_diff_anchor=True,
        )
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict, {"N/A::whatever": _entry()}, ticket_id=_TICKET
        )
        assert suppressed.blocking is True

    def test_suppression_emits_exactly_one_audit_event(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        suppress_adjudicated_findings(
            _verdict(_accepted(finding)), _ledger(finding), ticket_id=_TICKET
        )

        events = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED]
        )
        assert len(events) == 1
        assert events[0].correlation_id == _TICKET
        assert events[0].payload["file"] == finding.file
        assert events[0].payload["summary"] == finding.summary
        assert events[0].payload["outcome"] == "REJECTED"
        assert events[0].payload["recorded_at"] == "2026-08-16T00:00:00Z"

    def test_suppression_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        finding = _make_finding(severity="MUST_FIX")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            suppress_adjudicated_findings(
                _verdict(_accepted(finding)), _ledger(finding), ticket_id=_TICKET
            )
        assert any(_TICKET in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Claim tier: the per-lane gate and its shadow record (#2210)
# ---------------------------------------------------------------------------


class TestClaimTierGate:
    def _armed_inputs(self) -> tuple[Finding, dict[str, FindingDisposition]]:
        finding = _make_finding(severity="MUST_FIX", summary=CLAIM_ROW1_CANDIDATE)
        return finding, _ledger_for("src/cw/foo.py", CLAIM_ROW1_RECORDED)

    def test_gate_off_claim_match_is_not_suppressed(self) -> None:
        finding, ledger = self._armed_inputs()
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(verdict, ledger, ticket_id=_TICKET)
        assert suppressed is verdict
        assert suppressed.blocking is True
        assert [f.summary for f in suppressed.must_fix] == [finding.summary]

    def test_gate_off_emits_a_shadow_event_and_no_suppression_event(self) -> None:
        finding, ledger = self._armed_inputs()
        suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            reviewed_sha="deadbee",
        )

        shadows = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_CLAIM_SHADOWED]
        )
        assert len(shadows) == 1
        payload = shadows[0].payload
        assert shadows[0].correlation_id == _TICKET
        assert payload["file"] == finding.file
        assert payload["summary"] == finding.summary
        assert payload["severity"] == "MUST_FIX"
        assert float(payload["similarity"]) == pytest.approx(0.86, abs=0.005)
        assert payload["matched_key"] == next(iter(ledger))
        assert payload["matched_recorded_at"] == "2026-08-16T00:00:00Z"
        assert payload["matched_rationale"] == (
            "intentional tradeoff, settled in round 1"
        )
        assert payload["reviewed_sha"] == "deadbee"
        assert (
            read_events(
                event_types=[
                    OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED
                ]
            )
            == []
        )

    def test_shadow_recording_failure_never_alters_the_verdict(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_kw: object) -> None:
            raise OSError(2, "no such file")

        monkeypatch.setattr("cw.events.record_event", _boom)
        finding, ledger = self._armed_inputs()
        verdict = _verdict(_accepted(finding))
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            suppressed = suppress_adjudicated_findings(
                verdict, ledger, ticket_id=_TICKET
            )
        assert suppressed is verdict
        assert suppressed.blocking is True
        assert any(_TICKET in record.getMessage() for record in caplog.records)

    def test_shadow_event_is_distinct_per_reviewed_sha(self) -> None:
        finding, ledger = self._armed_inputs()
        for sha in ("sha-one", "sha-two"):
            suppress_adjudicated_findings(
                _verdict(_accepted(finding)),
                ledger,
                ticket_id=_TICKET,
                reviewed_sha=sha,
            )
        shadows = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_CLAIM_SHADOWED]
        )
        assert [s.payload["reviewed_sha"] for s in shadows] == ["sha-one", "sha-two"]
        assert {s.payload["summary"] for s in shadows} == {finding.summary}

    def test_gate_off_shadow_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        finding, ledger = self._armed_inputs()
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            suppress_adjudicated_findings(
                _verdict(_accepted(finding)), ledger, ticket_id=_TICKET
            )
        messages = [record.getMessage() for record in caplog.records]
        assert any(_TICKET in m and "NOT suppressed" in m for m in messages)

    def test_gate_on_claim_match_emits_the_suppression_event_with_claim_metadata(
        self,
    ) -> None:
        finding, ledger = self._armed_inputs()
        suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        events = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED]
        )
        assert len(events) == 1
        assert events[0].payload["match_kind"] == "claim"
        assert isinstance(events[0].payload["similarity"], float)
        assert events[0].payload["matched_key"] == next(iter(ledger))
        assert events[0].payload["severity"] == "MUST_FIX"
        assert (
            read_events(
                event_types=[OrchestratorEventType.REVIEW_FINDING_CLAIM_SHADOWED]
            )
            == []
        )

    @pytest.mark.parametrize("claim_tier_enabled", [True, False])
    def test_exact_tier_is_byte_identical_whichever_way_the_gate_is_set(
        self, claim_tier_enabled: bool
    ) -> None:
        finding = _make_finding(severity="MUST_FIX")
        suppressed = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            _ledger(finding),
            ticket_id=_TICKET,
            claim_tier_enabled=claim_tier_enabled,
        )
        assert suppressed.blocking is False
        assert suppressed.accepted[0].disposition_detail == _render_suppression_signal(
            finding.file, finding.summary, _entry()
        )
        events = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED]
        )
        assert events[0].payload["match_kind"] == "exact"
        assert events[0].payload["similarity"] == 1.0

    def test_shadow_is_independent_of_gate_state_elsewhere(self) -> None:
        verdict = _verdict(_accepted(_make_finding(severity="MUST_FIX")))
        assert suppress_adjudicated_findings(verdict, {}, ticket_id=_TICKET) is verdict
        assert read_events() == []


# ---------------------------------------------------------------------------
# The REVERSED outcome (#2232)
# ---------------------------------------------------------------------------


class TestReversedOutcome:
    """#2232: a third outcome that withdraws a prior settle.

    ``cw review settle`` is reused as the rollback producer, so the matchers
    need no new code — a ``REVERSED`` entry simply is not a decision, and both
    tiers already treat anything that is not ``REJECTED`` that way. These pin
    that, so a later change to either matcher cannot quietly make a withdrawn
    settle start suppressing again.
    """

    @pytest.mark.parametrize("outcome", ["ACCEPTED", "REVERSED"])
    def test_a_non_rejected_exact_entry_never_suppresses(self, outcome: str) -> None:
        finding = _make_finding(severity="MUST_FIX")
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            _ledger(finding, outcome=outcome),
            ticket_id=_TICKET,
        )

        assert result.blocking is True
        assert [f.summary for f in result.must_fix] == [finding.summary]
        assert result.accepted[0].disposition == "fixed"

    def test_a_reversed_entry_never_wins_the_claim_tier(self) -> None:
        """Armed or not, the fuzzy tier's best match must not be a reversal."""
        finding = _make_finding(severity="MUST_FIX", summary=CLAIM_ROW1_CANDIDATE)
        ledger = _ledger_for(finding.file, CLAIM_ROW1_RECORDED, outcome="REVERSED")
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )

        assert result.blocking is True
        assert read_events() == []

    def test_a_reversal_overrides_an_earlier_rejection_newest_wins(self) -> None:
        """Merge is outcome-agnostic, so the reversal is durable (#2232).

        This is the whole rollback mechanism: a second ``cw review settle``
        for the same identity with a newer ``recorded_at`` replaces the record
        rather than sitting beside it, because ``merge_finding_dispositions``
        resolves a duplicate key newest-wins without reading ``outcome``.
        """
        key = _key()
        rejected = {key: _entry(recorded_at="2026-08-16T00:00:00Z")}
        reversed_entry = {
            key: _entry(outcome="REVERSED", recorded_at="2026-09-22T00:00:00Z")
        }

        merged = merge_finding_dispositions(rejected, reversed_entry)

        assert merged[key].outcome == "REVERSED"

    def test_a_reversal_older_than_the_rejection_does_not_win(self) -> None:
        key = _key()
        rejected = {key: _entry(recorded_at="2026-09-22T00:00:00Z")}
        stale_reversal = {
            key: _entry(outcome="REVERSED", recorded_at="2026-08-16T00:00:00Z")
        }

        merged = merge_finding_dispositions(rejected, stale_reversal)

        assert merged[key].outcome == "REJECTED"

    def test_a_reversal_is_a_writable_ledger_entry(self) -> None:
        """``build_finding_disposition_ledger`` mints one like any other."""
        ledger = build_finding_disposition_ledger(
            [
                (
                    "src/cw/foo.py",
                    "Bug here",
                    _entry(outcome="REVERSED"),
                )
            ]
        )
        assert list(ledger) == [_key()]
        assert ledger[_key()].outcome == "REVERSED"
