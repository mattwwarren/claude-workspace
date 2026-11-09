"""Engine finding identity through the deferred-findings artifact (#3108).

An engine deferral carries its ``fingerprint`` (and ``rule_id``) through
``matched_adjudications``, the artifact's optional ``fingerprint:`` and
``rule_id:`` lines, the merge identity and the ticket write-back, so a reworded
re-raise dedupes onto one review-debt ticket.
"""

from __future__ import annotations

import pytest

from cw.review_adjudication import (
    Adjudication,
    apply_adjudication,
    matched_adjudications,
    merge_deferred_adjudications,
    parse_deferred_findings_md,
    render_deferred_findings_md,
    with_tickets,
)
from cw.review_findings import AcceptedFinding
from tests.conftest import _make_finding

_HEAD = (
    "# Deferred Review Findings\n"
    "<!-- written by Stage 3 (auto-dev-review.md), consumed by Stage 4 Step 4d"
    " (auto-dev-finalize.md) -->\n\n## Review adjudication\n\n"
    "<!-- DEFERRED-REVIEW-FINDINGS\n"
)
_TAIL = "\nDEFERRED-REVIEW-FINDINGS -->\n"
_ENTRY = (
    "- severity: SHOULD_FIX\n"
    '  summary: "Helper swallows the error"\n'
    "  file: src/cw/foo.py\n"
    '  rationale: "out of scope"'
)
_IDENT = "\n  fingerprint: fp-1\n  rule_id: py.swallow"
_STAMP = "\n  round: 2\n  recorded_at: 2026-11-08T12:00:00Z"
_TICKET = "\n  ticket: #5"
_URL = "https://github.com/owner/repo/issues/101"


def _doc(entry: str) -> str:
    return f"{_HEAD}{entry}{_TAIL}"


def _defer(**overrides: object) -> Adjudication:
    fields: dict[str, object] = {
        "severity": "SHOULD_FIX",
        "file": "src/cw/foo.py",
        "summary": "Helper swallows the error",
        "outcome": "defer",
        "rationale": "out of scope",
    }
    fields.update(overrides)
    return Adjudication.model_validate(fields)


def _accepted(**finding_overrides: object) -> AcceptedFinding:
    finding = _make_finding(severity="SHOULD_FIX", **finding_overrides)
    return AcceptedFinding.model_validate(
        {"finding": finding, "reviewers": ["Repo Review Engine"]}
    )


def _engine_accepted() -> AcceptedFinding:
    return _accepted(fingerprint="fp-1", rule_id="py.swallow", source="engine")


def _engine_entry(**overrides: object) -> Adjudication:
    fields: dict[str, object] = {
        "file": "src/cw/foo.py",
        "line_start": 10,
        "line_end": 10,
        "evidence": "def broken():",
        "summary": "Bug here",
        "fingerprint": "fp-1",
    }
    fields.update(overrides)
    return _defer(**fields)


class TestMatchedAdjudicationsEnrichment:
    def test_a_matched_engine_entry_gains_the_findings_rule_id(self) -> None:
        (matched,) = matched_adjudications([_engine_accepted()], [_engine_entry()])
        assert matched.fingerprint == "fp-1"
        assert matched.rule_id == "py.swallow"

    def test_an_entry_keeps_a_rule_id_it_already_carries(self) -> None:
        entry = _engine_entry(rule_id="py.own")
        (matched,) = matched_adjudications([_engine_accepted()], [entry])
        assert matched.rule_id == "py.own"

    def test_a_builtin_entry_is_untouched(self) -> None:
        accepted = _accepted(rule_id="py.swallow")
        entry = _defer(
            line_start=10, line_end=10, evidence="def broken():", summary="Bug here"
        )
        (matched,) = matched_adjudications([accepted], [entry])
        assert matched == entry
        assert matched.rule_id is None

    def test_a_finding_without_a_rule_id_leaves_the_entry_alone(self) -> None:
        accepted = _accepted(fingerprint="fp-1", source="engine")
        (matched,) = matched_adjudications([accepted], [_engine_entry()])
        assert matched.rule_id is None

    def test_apply_adjudication_is_unchanged_by_the_rule_id(self) -> None:
        accepted = [_engine_accepted()]
        plain = apply_adjudication(accepted, [_engine_entry()])
        pre_enriched = apply_adjudication(accepted, [_engine_entry(rule_id="py.own")])
        assert plain == pre_enriched


class TestArtifactLines:
    @pytest.mark.parametrize("stamp", ["", _STAMP])
    @pytest.mark.parametrize("ticket", ["", _TICKET])
    def test_the_lines_round_trip_byte_stably(self, stamp: str, ticket: str) -> None:
        text = _doc(f"{_ENTRY}{_IDENT}{stamp}{ticket}")
        (entry,) = parse_deferred_findings_md(text)
        assert (entry.fingerprint, entry.rule_id) == ("fp-1", "py.swallow")
        assert render_deferred_findings_md([entry]) == text

    def test_a_fingerprint_alone_round_trips(self) -> None:
        text = _doc(f"{_ENTRY}\n  fingerprint: fp-1")
        (entry,) = parse_deferred_findings_md(text)
        assert (entry.fingerprint, entry.rule_id) == ("fp-1", None)
        assert render_deferred_findings_md([entry]) == text

    def test_a_pre_change_artifact_parses_without_them(self) -> None:
        text = _doc(_ENTRY)
        (entry,) = parse_deferred_findings_md(text)
        assert (entry.fingerprint, entry.rule_id) == (None, None)
        assert render_deferred_findings_md([entry]) == text

    @pytest.mark.parametrize(
        "entry",
        [
            f"{_ENTRY}\n  fingerprint: a b",
            f"{_ENTRY}\n  fingerprint: ",
            f"{_ENTRY}\n  fingerprint:",
            f"{_ENTRY}\n  rule_id: py.x\n  fingerprint: fp-1",
            f"{_ENTRY}{_STAMP}\n  fingerprint: fp-1",
            f"{_ENTRY}\n  fingerprint: fp-1\n  rule_id: two words",
        ],
        ids=["space", "blank", "bare", "swapped", "after-round", "rule-space"],
    )
    def test_a_malformed_line_is_a_hard_error(self, entry: str) -> None:
        with pytest.raises(ValueError, match="unrecognized deferred-findings entry"):
            parse_deferred_findings_md(_doc(entry))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("fingerprint", "a b"),
            ("fingerprint", "a-->b"),
            ("rule_id", "Bad Rule"),
            ("rule_id", "a-->b"),
        ],
    )
    def test_a_value_outside_the_schema_charset_is_not_rendered(
        self, field: str, value: str
    ) -> None:
        entry = _defer(**{field: value})
        text = render_deferred_findings_md([entry])
        assert f"  {field}:" not in text
        (parsed,) = parse_deferred_findings_md(text)
        assert parsed.summary == entry.summary


class TestMergeIdentity:
    def test_defer_entries_with_different_fingerprints_do_not_collapse(self) -> None:
        merged = merge_deferred_adjudications(
            [_defer(fingerprint="fp-1")], [_defer(fingerprint="fp-2")]
        )
        assert [entry.fingerprint for entry in merged] == ["fp-1", "fp-2"]

    def test_the_same_fingerprint_and_text_collapses(self) -> None:
        merged = merge_deferred_adjudications(
            [_defer(fingerprint="fp-1")], [_defer(fingerprint="fp-1", round=2)]
        )
        assert len(merged) == 1

    def test_a_reworded_entry_with_the_same_fingerprint_appends(self) -> None:
        merged = merge_deferred_adjudications(
            [_defer(fingerprint="fp-1")],
            [_defer(fingerprint="fp-1", summary="Reworded finding")],
        )
        assert len(merged) == 2

    def test_a_rule_id_is_not_part_of_the_identity(self) -> None:
        merged = merge_deferred_adjudications(
            [_defer(fingerprint="fp-1")], [_defer(fingerprint="fp-1", rule_id="py.x")]
        )
        assert len(merged) == 1

    def test_a_reject_collapses_despite_a_fingerprint_only_on_one_side(self) -> None:
        prior = parse_deferred_findings_md(
            render_deferred_findings_md([_defer(outcome="reject")])
        )
        new = [_defer(outcome="reject", fingerprint="fp-1")]
        assert merge_deferred_adjudications(prior, new) == prior


class TestWithTicketsKey:
    def test_same_text_different_fingerprints_each_get_their_own_ticket(self) -> None:
        entries = [_defer(fingerprint="fp-1"), _defer(fingerprint="fp-2")]
        tickets = {
            ("src/cw/foo.py", "Helper swallows the error", "fp-1"): "#1",
            ("src/cw/foo.py", "Helper swallows the error", "fp-2"): "#2",
        }
        filled = with_tickets(entries, tickets)
        assert [entry.ticket for entry in filled] == ["#1", "#2"]

    def test_a_fingerprintless_entry_keys_on_an_empty_fingerprint(self) -> None:
        tickets = {("src/cw/foo.py", "Helper swallows the error", ""): _URL}
        assert with_tickets([_defer()], tickets)[0].ticket == _URL
