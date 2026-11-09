"""The optional ``ticket:`` line in DEFERRED-REVIEW-FINDINGS entries (#3041, R14)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.models.review_debt_record import ReviewDebtRecord
from cw.review_adjudication import (
    Adjudication,
    merge_deferred_adjudications,
    parse_deferred_findings_md,
    render_deferred_findings_md,
    with_tickets,
)
from cw.review_debt_filing import (
    artifact_debt_entries,
    debt_entry,
    ticket_value,
    write_artifact_tickets,
)

if TYPE_CHECKING:
    from pathlib import Path

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
_STAMP = "\n  round: 2\n  recorded_at: 2026-11-08T12:00:00Z"
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


@pytest.mark.parametrize("stamp", ["", _STAMP])
def test_a_ticket_line_is_accepted(stamp: str) -> None:
    (entry,) = parse_deferred_findings_md(_doc(f"{_ENTRY}{stamp}\n  ticket: {_URL}"))
    assert entry.ticket == _URL


@pytest.mark.parametrize("stamp", ["", _STAMP])
def test_a_ticketed_entry_renders_byte_stably(stamp: str) -> None:
    text = _doc(f"{_ENTRY}{stamp}\n  ticket: {_URL}")
    assert render_deferred_findings_md(parse_deferred_findings_md(text)) == text


@pytest.mark.parametrize(
    "entry",
    [
        f"{_ENTRY}\n  ticket: {_URL}{_STAMP}",
        f"{_ENTRY}\n  ticket: ",
        f"{_ENTRY}\n  ticket:",
        f"{_ENTRY}\n  ticket: two words",
    ],
    ids=["before-round", "blank", "bare", "space"],
)
def test_a_malformed_ticket_line_is_refused(entry: str) -> None:
    with pytest.raises(ValueError, match="unrecognized deferred-findings entry"):
        parse_deferred_findings_md(_doc(entry))


def test_an_unticketed_legacy_file_still_round_trips() -> None:
    text = _doc(_ENTRY)
    (entry,) = parse_deferred_findings_md(text)
    assert entry.ticket == ""
    assert render_deferred_findings_md([entry]) == text


def test_the_merge_keeps_a_prior_ticket_when_the_entry_re_arrives() -> None:
    prior = [_defer(ticket=_URL, round=1, recorded_at="2026-11-01T00:00:00Z")]
    merged = merge_deferred_adjudications(prior, [_defer(round=2)])
    assert [entry.ticket for entry in merged] == [_URL]


def test_with_tickets_fills_only_unticketed_defer_entries() -> None:
    entries = [
        _defer(),
        _defer(file="src/cw/bar.py", ticket="#5"),
        _defer(outcome="reject"),
        _defer(summary="other"),
    ]
    tickets = {("src/cw/foo.py", "Helper swallows the error", ""): _URL}
    filled = with_tickets(entries, tickets)
    assert [entry.ticket for entry in filled] == [_URL, "#5", "", ""]


def test_a_ticket_with_whitespace_is_not_written() -> None:
    tickets = {("src/cw/foo.py", "Helper swallows the error", ""): "has space"}
    assert with_tickets([_defer()], tickets)[0].ticket == ""


class TestArtifactBridge:
    """``cw.review_debt_filing``'s artifact read and ticket write-back."""

    def test_reads_defer_entries_and_writes_tickets_back(self, tmp_path: Path) -> None:
        path = tmp_path / "deferred-findings.md"
        rejected = _defer(outcome="reject", summary="nope", file="src/cw/x.py")
        path.write_text(render_deferred_findings_md([rejected, _defer()]))
        (entry,) = artifact_debt_entries(path)
        record = ReviewDebtRecord(
            key=entry.key,
            tracker="github-issues",
            ticket_id="101",
            ticket_url=_URL,
            filed_at=datetime(2026, 11, 8, tzinfo=UTC),
            mode="filed",
        )
        assert write_artifact_tickets(path, [record]) is True
        assert f"  ticket: {_URL}\n" in path.read_text(encoding="utf-8")
        assert write_artifact_tickets(path, [record]) is False
        assert artifact_debt_entries(path)[0].ticket == _URL

    def test_a_missing_artifact_reads_empty_and_stays_missing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "absent.md"
        assert artifact_debt_entries(path) == []
        assert write_artifact_tickets(path, []) is False
        assert not path.exists()

    def test_an_unparseable_artifact_is_a_cw_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        path.write_text("not the artifact\n")
        with pytest.raises(CwError, match="DEFERRED-REVIEW-FINDINGS"):
            artifact_debt_entries(path)

    def test_debt_entry_carries_evidence_line_and_ticket(self) -> None:
        adj = _defer(line_start=4, evidence="x = 1", ticket="#9")
        entry = debt_entry(adj)
        assert (entry.line, entry.evidence, entry.ticket) == (4, "x = 1", "#9")

    def test_ticket_value_prefers_the_url(self) -> None:
        record = ReviewDebtRecord(
            key="k",
            tracker="linear",
            ticket_id="ENG-1",
            ticket_url=None,
            filed_at=datetime(2026, 11, 8, tzinfo=UTC),
            mode="emitted",
        )
        assert ticket_value(record) == "ENG-1"
        assert ticket_value(record.model_copy(update={"ticket_url": "u"})) == "u"

    @pytest.mark.parametrize(
        ("fingerprint", "prefix"),
        [("fp-1", "engine:fp-1"), ("fp_1", "engine:h:")],
        ids=["raw", "hashed"],
    )
    def test_an_engine_entry_is_keyed_and_ticketed_by_its_fingerprint(
        self, tmp_path: Path, fingerprint: str, prefix: str
    ) -> None:
        path = tmp_path / "deferred-findings.md"
        path.write_text(render_deferred_findings_md([_defer(fingerprint=fingerprint)]))
        (entry,) = artifact_debt_entries(path)
        assert entry.fingerprint == fingerprint
        assert entry.key.startswith(prefix)
        record = ReviewDebtRecord(
            key=entry.key,
            tracker="github-issues",
            ticket_id="101",
            ticket_url=_URL,
            filed_at=datetime(2026, 11, 8, tzinfo=UTC),
            mode="filed",
        )
        assert write_artifact_tickets(path, [record]) is True
        assert artifact_debt_entries(path)[0].ticket == _URL

    def test_two_engine_entries_with_one_text_get_separate_tickets(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "deferred-findings.md"
        entries = [_defer(fingerprint="fp-1"), _defer(fingerprint="fp-2")]
        path.write_text(render_deferred_findings_md(entries))
        records = [
            ReviewDebtRecord(
                key=entry.key,
                tracker="github-issues",
                ticket_id=str(number),
                ticket_url=None,
                filed_at=datetime(2026, 11, 8, tzinfo=UTC),
                mode="filed",
            )
            for number, entry in enumerate(artifact_debt_entries(path), start=1)
        ]
        assert write_artifact_tickets(path, records) is True
        assert [e.ticket for e in artifact_debt_entries(path)] == ["1", "2"]

    def test_debt_entry_carries_the_engine_identity(self) -> None:
        adj = _defer(fingerprint="fp-1", rule_id="py.x")
        entry = debt_entry(adj)
        assert (entry.key, entry.rule_id, entry.fingerprint) == (
            "engine:fp-1",
            "py.x",
            "fp-1",
        )

    def test_an_unsafe_fingerprint_gets_the_hashed_key_without_raising(self) -> None:
        entry = debt_entry(_defer(fingerprint="a b"))
        assert entry.key.startswith("engine:h:")

    def test_a_fingerprintless_entry_keeps_the_text_key(self) -> None:
        entry = debt_entry(_defer())
        assert (entry.fingerprint, entry.rule_id) == (None, "")
        assert not entry.key.startswith("engine:")
