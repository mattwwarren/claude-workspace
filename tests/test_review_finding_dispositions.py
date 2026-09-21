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
    _claim_similarity,
    _claim_symbols,
    _claim_tokens,
    _disposition_key,
    _render_suppression_signal,
    build_finding_disposition_ledger,
    merge_finding_dispositions,
    parse_finding_disposition_block,
    render_finding_disposition_block,
    split_disposition_key,
    suppress_adjudicated_findings,
)
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict
from tests._cli_review_helpers import CLAIM_ROW1_CANDIDATE, CLAIM_ROW1_RECORDED

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


def _ledger_for(
    file: str, summary: str, **overrides: object
) -> dict[str, FindingDisposition]:
    """A single-entry ledger keyed on ``(file, summary)`` (#2210).

    Module-scope so the claim/contest/gate classes added by #2210 can build
    multi-entry ledgers as dict unions of it rather than each growing a
    near-identical class-local builder.
    """
    key = _disposition_key(file, summary)
    assert key is not None
    return {key: _entry(**overrides)}


def _ledger(finding: Finding, **overrides: object) -> dict[str, FindingDisposition]:
    """A single-entry ledger keyed on *finding*'s own identity."""
    return _ledger_for(finding.file, finding.summary, **overrides)


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
# Claim tier: tokenizer, symbol extraction, similarity (#2210)
# ---------------------------------------------------------------------------


class TestClaimTokensAndSymbols:
    def test_tokens_split_hyphens_and_drop_stopwords_and_short_tokens(self) -> None:
        assert _claim_tokens("the early-return branch drops a follow-up task") == {
            "early",
            "return",
            "branch",
            "drops",
            "follow",
            "task",
        }

    def test_bare_dotted_filenames_and_abbreviations_are_never_symbols(self) -> None:
        text = (
            "e.g. foo.py and i.e. baz.md drop `Foo.bar()` and `x + y` "
            "plus cw.review_debt"
        )
        assert _claim_symbols(text) == {"foo.bar", "review_debt"}

    def test_backticked_identifiers_and_snake_case_tokens_are_symbols(self) -> None:
        assert _claim_symbols(
            "the `_track_open_findings` helper and self.parse_config drop `a b`"
        ) == {"_track_open_findings", "parse_config"}

    def test_a_backticked_span_shorter_than_three_characters_is_not_a_symbol(
        self,
    ) -> None:
        assert _claim_symbols("the `x` value") == set()

    def test_digit_placeholder_survives_inside_an_identifier(self) -> None:
        # review_debt masks digit runs to an uppercase "N"; the tokenizer
        # lowercases first, so `parse_vN` stays ONE token rather than splitting.
        assert "parse_vn" in _claim_tokens("`parse_vN` fails")


@pytest.mark.parametrize(
    ("recorded", "candidate", "expected"),
    [
        (CLAIM_ROW1_RECORDED, CLAIM_ROW1_CANDIDATE, 0.86),
        (
            "retry loop swallows timeout errors silently",
            "timeout errors are silently swallowed by the retry loop",
            0.83,
        ),
        ("retry loop swallows timeout errors silently", "retry loop is slow", None),
        ("`parse_config` missing null check", "`load_config` missing null check", None),
        ("`load` crashes on empty input", "`load` leaks file handle on error", None),
        (
            "`load` crashes on empty input",
            "`load` crashes when config is missing",
            None,
        ),
        (
            "`foo` returns none when list is empty",
            "`foo` returns none when list contains duplicates",
            0.73,
        ),
        (
            "parsing the config ignores null check for missing values",
            "`parse_config` ignores null check for missing values",
            0.77,
        ),
        (
            "parsing the config ignores null check",
            "`parse_config` ignores null check for missing values",
            None,
        ),
        ("it is not the", "`foo` drops the task", None),
        ("", "", None),
    ],
)
def test_claim_similarity_table(
    recorded: str, candidate: str, expected: float | None
) -> None:
    """The matcher's contract, pinned case by case (#2210, ADR-0016).

    Rows 4 and 6 are the vetoes (disjoint symbols; shared symbol but too few
    shared tokens). Row 7 is the ACCEPTED false-match class ADR-0016 records.
    """
    score = _claim_similarity(recorded, candidate)
    if expected is None:
        assert score is None
    else:
        assert score == pytest.approx(expected, abs=0.005)


def test_claim_similarity_table_row_one_pair_clears_the_thresholds() -> None:
    # The shared wording pair every reworded-finding test imports must keep
    # matching; a threshold or tokenizer edit fails HERE, at the source.
    assert _claim_similarity(CLAIM_ROW1_RECORDED, CLAIM_ROW1_CANDIDATE) is not None


# ---------------------------------------------------------------------------
# Claim tier: matching through suppress_adjudicated_findings (#2210)
# ---------------------------------------------------------------------------


class TestClaimMatching:
    """The armed claim tier, exercised at the seam it ships in."""

    def _reworded(self, **overrides: object) -> Finding:
        kwargs: dict[str, object] = {
            "severity": "MUST_FIX",
            "summary": CLAIM_ROW1_CANDIDATE,
        }
        kwargs.update(overrides)
        return _make_finding(**kwargs)

    def _recorded_ledger(self, **overrides: object) -> dict[str, FindingDisposition]:
        return _ledger_for("src/cw/foo.py", CLAIM_ROW1_RECORDED, **overrides)

    def test_reworded_must_fix_with_shared_symbol_is_suppressed(self) -> None:
        finding = self._reworded()
        suppressed = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            self._recorded_ledger(),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )

        assert suppressed.blocking is False
        assert suppressed.accepted[0].disposition == "rejected"
        detail = suppressed.accepted[0].disposition_detail
        assert "claim similarity" in detail
        assert "re-adjudicate if the code at this location has changed" in detail
        assert "drops the follow-up task" in detail

    def test_same_words_in_a_different_file_never_match(self) -> None:
        finding = self._reworded(file="src/cw/other.py")
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict,
            self._recorded_ledger(),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert suppressed is verdict

    def test_claim_tier_only_considers_must_fix(self) -> None:
        should_fix = self._reworded(severity="SHOULD_FIX")
        verdict = _verdict(_accepted(should_fix))
        suppressed = suppress_adjudicated_findings(
            verdict,
            self._recorded_ledger(),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert suppressed is verdict
        assert read_events() == []

    def test_exact_tier_still_suppresses_a_should_fix(self) -> None:
        # The exact tier is severity-blind and unchanged by this ticket.
        should_fix = _make_finding(severity="SHOULD_FIX")
        suppressed = suppress_adjudicated_findings(
            _verdict(_accepted(should_fix)),
            _ledger(should_fix),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert suppressed.accepted[0].disposition == "rejected"

    def test_claim_tier_skips_an_already_stamped_finding(self) -> None:
        finding = self._reworded()
        verdict = _verdict(
            _accepted(
                finding, disposition="rejected", disposition_detail="voided by operator"
            ),
            blocking=False,
            must_fix=[],
        )
        suppressed = suppress_adjudicated_findings(
            verdict,
            self._recorded_ledger(),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert suppressed is verdict
        assert read_events() == []

    def test_accepted_entry_never_suppresses_even_fuzzily(self) -> None:
        finding = self._reworded()
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict,
            self._recorded_ledger(outcome="ACCEPTED"),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert suppressed is verdict

    def test_exact_accepted_entry_vetoes_a_fuzzy_rejected_sibling(self) -> None:
        finding = self._reworded()
        ledger = {
            **_ledger(finding, outcome="ACCEPTED"),
            **self._recorded_ledger(),
        }
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict, ledger, ticket_id=_TICKET, claim_tier_enabled=True
        )
        assert suppressed is verdict

    def test_nearer_accepted_entry_vetoes_a_fuzzy_rejected_entry(self) -> None:
        finding = self._reworded()
        ledger = {
            # An exact-wording ACCEPTED twin of the candidate scores 1.0 and
            # therefore wins the nearest-decision contest against the REJECTED
            # rewording below.
            **_ledger_for("src/cw/foo.py", CLAIM_ROW1_CANDIDATE, outcome="ACCEPTED"),
            **self._recorded_ledger(),
        }
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict, ledger, ticket_id=_TICKET, claim_tier_enabled=True
        )
        assert suppressed is verdict

    def test_best_similarity_wins_among_multiple_rejected_entries(self) -> None:
        finding = self._reworded()
        ledger = {
            **self._recorded_ledger(rationale="the near one"),
            **_ledger_for(
                "src/cw/foo.py",
                "`_track_open_findings` drops the follow-up task entirely",
                rationale="the far one",
            ),
        }
        suppressed = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert "the near one" in suppressed.accepted[0].disposition_detail

    def test_full_tie_resolves_to_the_first_key_in_sort_order(self) -> None:
        # Two REJECTED entries scoring identically against the candidate, with
        # identical recorded_at: `max` keeps the first maximal element it meets
        # and candidates are built over sorted(ledger.items()), so the
        # alphabetically first key wins.
        finding = self._reworded(summary="`alpha_helper` drops the follow-up task")
        ledger = {
            **_ledger_for(
                "src/cw/foo.py",
                "`alpha_helper` drops a follow-up task",
                rationale="AAA first key",
            ),
            **_ledger_for(
                "src/cw/foo.py",
                "`alpha_helper` drops that follow-up task",
                rationale="ZZZ later key",
            ),
        }
        suppressed = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert "AAA first key" in suppressed.accepted[0].disposition_detail

    def test_later_recorded_at_wins_at_equal_similarity(self) -> None:
        finding = self._reworded(summary="`alpha_helper` drops the follow-up task")
        ledger = {
            **_ledger_for(
                "src/cw/foo.py",
                "`alpha_helper` drops a follow-up task",
                rationale="AAA first key",
                recorded_at="2026-01-01T00:00:00Z",
            ),
            **_ledger_for(
                "src/cw/foo.py",
                "`alpha_helper` drops that follow-up task",
                rationale="ZZZ later key",
                recorded_at="2026-09-01T00:00:00Z",
            ),
        }
        suppressed = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            ledger,
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert "ZZZ later key" in suppressed.accepted[0].disposition_detail

    def test_no_diff_anchor_file_is_never_claim_matched(self) -> None:
        finding = _make_finding(
            severity="MUST_FIX",
            file="N/A",
            line_start=None,
            line_end=None,
            no_diff_anchor=True,
            summary=CLAIM_ROW1_CANDIDATE,
        )
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict,
            self._recorded_ledger(),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert suppressed is verdict


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
        suppressed = suppress_adjudicated_findings(
            verdict, ledger, ticket_id=_TICKET
        )
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

    def test_gate_off_shadow_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
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
# Finding.contests_adjudication — the typed escape hatch (#2210)
# ---------------------------------------------------------------------------


class TestContest:
    def test_contested_exact_match_is_not_suppressed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        finding = _make_finding(
            severity="MUST_FIX",
            contests_adjudication="the guard was deleted in commit abc123",
        )
        verdict = _verdict(_accepted(finding))
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            suppressed = suppress_adjudicated_findings(
                verdict, _ledger(finding), ticket_id=_TICKET
            )
        assert suppressed is verdict
        assert suppressed.blocking is True
        assert suppressed.accepted[0].disposition == "fixed"
        assert (
            read_events(
                event_types=[
                    OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED
                ]
            )
            == []
        )
        assert any(
            _TICKET in record.getMessage() and "admitted contest" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.parametrize("claim_tier_enabled", [True, False])
    def test_contested_claim_match_is_not_suppressed_and_not_shadowed(
        self, claim_tier_enabled: bool
    ) -> None:
        finding = _make_finding(
            severity="MUST_FIX",
            summary=CLAIM_ROW1_CANDIDATE,
            contests_adjudication="the early-return branch is now unreachable",
        )
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict,
            _ledger_for("src/cw/foo.py", CLAIM_ROW1_RECORDED),
            ticket_id=_TICKET,
            claim_tier_enabled=claim_tier_enabled,
        )
        assert suppressed is verdict
        assert read_events() == []

    def test_whitespace_only_contest_is_treated_as_bare_and_suppressed(self) -> None:
        finding = _make_finding(severity="MUST_FIX", contests_adjudication="   \n ")
        suppressed = suppress_adjudicated_findings(
            _verdict(_accepted(finding)), _ledger(finding), ticket_id=_TICKET
        )
        assert suppressed.blocking is False
        assert suppressed.accepted[0].disposition == "rejected"

    def test_contest_on_an_unmatched_finding_is_a_no_op(self) -> None:
        finding = _make_finding(
            severity="MUST_FIX", contests_adjudication="something changed"
        )
        other = _make_finding(severity="MUST_FIX", file="src/cw/other.py")
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict, _ledger(other), ticket_id=_TICKET
        )
        assert suppressed is verdict

    def test_contest_admission_emits_no_event(self) -> None:
        # Admission is intentionally log-only (ADR-0016, follow-up F9).
        finding = _make_finding(
            severity="MUST_FIX", contests_adjudication="the code moved"
        )
        suppress_adjudicated_findings(
            _verdict(_accepted(finding)), _ledger(finding), ticket_id=_TICKET
        )
        assert read_events() == []


# ---------------------------------------------------------------------------
# build_finding_disposition_ledger — the producer-side twin (#2210)
# ---------------------------------------------------------------------------


class TestBuildFindingDispositionLedger:
    def test_keys_equal_disposition_key(self) -> None:
        ledger = build_finding_disposition_ledger(
            [("src/cw/foo.py", "Bug here", _entry())]
        )
        assert list(ledger) == [_disposition_key("src/cw/foo.py", "Bug here")]

    def test_unkeyable_file_raises(self) -> None:
        with pytest.raises(ValueError, match="no path to key on"):
            build_finding_disposition_ledger([("N/A", "Bug here", _entry())])

    def test_duplicate_key_resolves_newest_recorded_at_first(self) -> None:
        ledger = build_finding_disposition_ledger(
            [
                (
                    "src/cw/foo.py",
                    "Bug here",
                    _entry(rationale="older", recorded_at="2026-01-01T00:00:00Z"),
                ),
                (
                    "src/cw/foo.py",
                    "Bug here",
                    _entry(rationale="newer", recorded_at="2026-09-01T00:00:00Z"),
                ),
            ]
        )
        assert len(ledger) == 1
        assert next(iter(ledger.values())).rationale == "newer"

    def test_older_duplicate_never_overwrites_a_newer_entry(self) -> None:
        ledger = build_finding_disposition_ledger(
            [
                (
                    "src/cw/foo.py",
                    "Bug here",
                    _entry(rationale="newer", recorded_at="2026-09-01T00:00:00Z"),
                ),
                (
                    "src/cw/foo.py",
                    "Bug here",
                    _entry(rationale="older", recorded_at="2026-01-01T00:00:00Z"),
                ),
            ]
        )
        assert next(iter(ledger.values())).rationale == "newer"

    def test_round_trips_through_the_marker(self) -> None:
        ledger = build_finding_disposition_ledger(
            [("src/cw/foo.py", "Bug here", _entry())]
        )
        rendered = render_finding_disposition_block(ledger)
        assert parse_finding_disposition_block([rendered]) == ledger


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
