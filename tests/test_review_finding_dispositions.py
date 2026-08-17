"""Tests for the cross-round adjudication-memory seam (GitHub #1838).

1:1 with ``src/cw/review_finding_dispositions.py`` per the CLAUDE.md Testing
convention. Reuses ``tests/conftest.py``'s ``_make_finding`` fixture rather
than re-declaring an equivalent; ``_accepted``/``_verdict`` are declared
file-local here for the same reason ``tests/test_review_adjudication.py``
declares its own — they are thin, module-specific construction helpers, not
generically reusable builders.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pytest

from cw.auto_dev_result import Review
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_debt import fingerprint_v1
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
    _render_suppression_signal,
    merge_finding_dispositions,
    parse_finding_disposition_block,
    render_finding_disposition_block,
    split_disposition_key,
    suppress_adjudicated_findings,
)
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict

from .conftest import _make_finding

_LOGGER = "cw.review_finding_dispositions"
_TICKET = "T-1838"


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
    kwargs: dict[str, object] = {
        "outcome": "REJECTED",
        "rationale": "intentional tradeoff, settled in round 1",
        "recorded_at": "2026-08-16T00:00:00Z",
    }
    kwargs.update(overrides)
    return FindingDisposition.model_validate(kwargs)


# ---------------------------------------------------------------------------
# _disposition_key / split_disposition_key
# ---------------------------------------------------------------------------


class TestDispositionKey:
    def test_is_deterministic_for_identical_inputs(self) -> None:
        first = _disposition_key("src/cw/foo.py", "Bug here")
        second = _disposition_key("src/cw/foo.py", "Bug here")
        assert first is not None
        assert first == second

    def test_matches_fingerprint_v1_normalization(self) -> None:
        fingerprint = fingerprint_v1("src/cw/foo.py", "Bug at line 42")
        assert fingerprint is not None
        key = _disposition_key("src/cw/foo.py", "Bug at line 42")
        assert key is not None
        assert split_disposition_key(key) == fingerprint

    def test_position_and_count_drift_collapse_onto_one_key(self) -> None:
        # Mirrors review_debt's documented false-merge acceptance: two summaries
        # that normalize alike deliberately share one identity.
        assert _disposition_key("src/cw/foo.py", "3 call sites at line 10") == (
            _disposition_key("src/cw/foo.py", "4 call sites at line 99")
        )

    def test_no_diff_anchor_file_is_never_keyed(self) -> None:
        assert _disposition_key("N/A", "Nothing to anchor on") is None

    def test_split_round_trips_a_key(self) -> None:
        key = _disposition_key("src/cw/foo.py", "Bug here")
        assert key is not None
        assert split_disposition_key(key) == ("src/cw/foo.py", "bug here")


# ---------------------------------------------------------------------------
# render / parse round trip
# ---------------------------------------------------------------------------


class TestRenderParseFindingDispositionBlockRoundTrip:
    def test_round_trips_through_the_marker(self) -> None:
        key = _disposition_key("src/cw/foo.py", "Bug here")
        assert key is not None
        ledger = {key: _entry()}
        rendered = render_finding_disposition_block(ledger)
        assert parse_finding_disposition_block([rendered]) == ledger

    def test_embeds_its_own_header(self) -> None:
        key = _disposition_key("src/cw/foo.py", "Bug here")
        assert key is not None
        rendered = render_finding_disposition_block({key: _entry()})
        assert rendered.startswith("## Review Finding Dispositions")

    def test_empty_ledger_renders_nothing(self) -> None:
        assert render_finding_disposition_block({}) == ""

    def test_malformed_json_body_degrades_to_empty(self) -> None:
        body = (
            "<!-- REVIEW-FINDING-DISPOSITIONS\n{not json\n"
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == {}

    def test_malformed_entry_is_skipped_without_discarding_siblings(self) -> None:
        key = _disposition_key("src/cw/foo.py", "Bug here")
        assert key is not None
        payload = {
            "schema_version": 1,
            "dispositions": {
                key: _entry().model_dump(mode="json"),
                "bad::entry": {"outcome": "NOT_A_VALID_OUTCOME"},
            },
        }
        body = (
            "<!-- REVIEW-FINDING-DISPOSITIONS\n"
            f"{json.dumps(payload)}\n"
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == {key: _entry()}

    def test_non_object_payload_degrades_to_empty(self) -> None:
        body = (
            "<!-- REVIEW-FINDING-DISPOSITIONS\n[1, 2, 3]\n"
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == {}

    def test_non_object_dispositions_value_degrades_to_empty(self) -> None:
        body = (
            "<!-- REVIEW-FINDING-DISPOSITIONS\n"
            '{"schema_version": 1, "dispositions": ["not", "a", "map"]}\n'
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == {}

    def test_missing_dispositions_key_degrades_to_empty(self) -> None:
        body = (
            "<!-- REVIEW-FINDING-DISPOSITIONS\n"
            '{"schema_version": 1}\n'
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == {}

    def test_missing_marker_yields_empty(self) -> None:
        assert parse_finding_disposition_block(["just prose", ""]) == {}

    def test_unions_across_comments_newest_recorded_at_wins(self) -> None:
        key_a = _disposition_key("src/cw/foo.py", "Bug here")
        key_b = _disposition_key("src/cw/bar.py", "Other bug")
        assert key_a is not None
        assert key_b is not None
        older = render_finding_disposition_block(
            {key_a: _entry(recorded_at="2026-08-01T00:00:00Z", rationale="old")}
        )
        newer = render_finding_disposition_block(
            {
                key_a: _entry(recorded_at="2026-08-15T00:00:00Z", rationale="new"),
                key_b: _entry(outcome="ACCEPTED", rationale="accepted"),
            }
        )
        parsed = parse_finding_disposition_block([older, newer])
        assert parsed[key_a].rationale == "new"
        assert parsed[key_b].outcome == "ACCEPTED"


# ---------------------------------------------------------------------------
# merge_finding_dispositions
# ---------------------------------------------------------------------------


class TestMergeFindingDispositions:
    def test_adds_new_entries_to_an_empty_ledger(self) -> None:
        entry = _entry()
        assert merge_finding_dispositions({}, {"k": entry}) == {"k": entry}

    def test_newest_recorded_at_wins_on_a_duplicate_key(self) -> None:
        old = _entry(recorded_at="2026-08-01T00:00:00Z", rationale="old")
        new = _entry(recorded_at="2026-08-15T00:00:00Z", rationale="new")
        assert (
            merge_finding_dispositions({"k": old}, {"k": new})["k"].rationale == "new"
        )

    def test_an_older_parsed_entry_does_not_overwrite_a_newer_stored_one(self) -> None:
        old = _entry(recorded_at="2026-08-01T00:00:00Z", rationale="old")
        new = _entry(recorded_at="2026-08-15T00:00:00Z", rationale="new")
        merged = merge_finding_dispositions({"k": new}, {"k": old})
        assert merged["k"].rationale == "new"

    def test_existing_entry_absent_from_the_parsed_set_is_preserved(self) -> None:
        # Forward-only (R3): the ledger is additive and durable. A pass whose
        # comment thread no longer carries the marker must not forget it.
        kept = _entry(rationale="settled long ago")
        merged = merge_finding_dispositions({"kept": kept}, {"fresh": _entry()})
        assert merged["kept"] == kept
        assert set(merged) == {"kept", "fresh"}

    def test_does_not_mutate_either_input(self) -> None:
        existing = {"k": _entry(rationale="old")}
        parsed = {"k": _entry(recorded_at="2026-09-01T00:00:00Z", rationale="new")}
        merge_finding_dispositions(existing, parsed)
        assert existing["k"].rationale == "old"
        assert set(parsed) == {"k"}


# ---------------------------------------------------------------------------
# suppress_adjudicated_findings
# ---------------------------------------------------------------------------


class TestSuppressAdjudicatedFindings:
    def _ledger(
        self, finding: Finding, **overrides: object
    ) -> dict[str, FindingDisposition]:
        key = _disposition_key(finding.file, finding.summary)
        assert key is not None
        return {key: _entry(**overrides)}

    def test_rejected_entry_suppresses_the_matching_must_fix(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        verdict = _verdict(_accepted(finding))
        assert verdict.blocking is True

        suppressed = suppress_adjudicated_findings(
            verdict, self._ledger(finding), ticket_id=_TICKET
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
            self._ledger(finding, recorded_at="2026-08-16T12:00:00Z"),
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
            verdict, self._ledger(finding, outcome="ACCEPTED"), ticket_id=_TICKET
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
            verdict, self._ledger(other), ticket_id=_TICKET
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
            verdict, self._ledger(finding), ticket_id=_TICKET
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
            _verdict(_accepted(finding)), self._ledger(finding), ticket_id=_TICKET
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
                _verdict(_accepted(finding)), self._ledger(finding), ticket_id=_TICKET
            )
        assert any(_TICKET in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Import-cycle lock (#1838)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        "cw.review_finding_dispositions",
        "cw.review_findings",
        "cw.review_debt",
        "cw.models",
        "cw.events",
    ],
)
def test_module_imports_cleanly_whichever_module_loads_first(first: str) -> None:
    """``cw.models.tasks`` imports this module, so a module-scope ``cw`` import
    here would close a cycle that only fails in ONE import order — invisible to
    a test suite whose conftest always warms ``cw.models`` first.

    Each interpreter below starts cold and imports one module, then the rest,
    which is what makes the ordering genuinely exercised. Uses
    ``sys.executable`` (not a bare ``python3``) per PYTHON-PATTERNS' compiled-
    dependency isolation rule — ``pydantic_core`` is ABI-bound to this venv.
    """
    script = (
        f"import {first}\n"
        "import cw.review_finding_dispositions, cw.models, cw.events\n"
        "from cw.models import TicketTask\n"
        "assert TicketTask(ticket_id='T-1', client='c').finding_dispositions == {}\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True)
