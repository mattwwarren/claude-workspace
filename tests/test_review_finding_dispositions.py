"""Tests for the cross-round adjudication-memory seam (GitHub #1838).

1:1 with ``src/cw/review_finding_dispositions.py`` per the CLAUDE.md Testing
convention. Reuses ``tests/conftest.py``'s ``_make_finding`` fixture rather
than re-declaring an equivalent; ``_accepted``/``_verdict`` are declared
file-local here for the same reason ``tests/test_review_adjudication.py``
declares its own — they are thin, module-specific construction helpers, not
generically reusable builders.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

import cw
from cw.auto_dev_result import Review
from cw.events import read_events
from cw.gh import AGENT_COMMENT_MARKER
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
    log_refused_dispositions,
    merge_finding_dispositions,
    parse_finding_disposition_block,
    partition_enforceable_dispositions,
    render_finding_disposition_block,
    split_disposition_key,
    suppress_adjudicated_findings,
)
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict
from cw.review_markers import DISPOSITION_SENTINEL, RefusedDisposition
from tests._cli_review_helpers import CLAIM_ROW1_CANDIDATE, CLAIM_ROW1_RECORDED

from .conftest import _make_finding

_LOGGER = "cw.review_finding_dispositions"
_TICKET = "T-1838"
#: The gh login ``cw review settle`` would record as having settled a finding.
_OPERATOR = "mattwwarren"
#: What ``render_finding_disposition_block`` puts in front of the sentinel
#: block. A hand-built body needs it since #2210 round 4: the reader honours a
#: block only where the writer emits it, so a body that opens with a bare
#: ``<!--`` is not a record at all and would make the degrade-path tests below
#: pass for the wrong reason.
_MARKER_TITLE = "## Review Finding Dispositions\n\n"


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

    def test_key_is_file_normalized_summary_and_verbatim_digest(self) -> None:
        # #2210 round 3: the key extends fingerprint_v1 with a SHA-256 of the
        # EXACT summary text, so a finding whose summary differs by even one
        # byte is a different finding.
        summary = "Bug at line 42"
        fingerprint = fingerprint_v1("src/cw/foo.py", summary)
        assert fingerprint is not None
        digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        assert len(digest) == 64
        assert (
            _key("src/cw/foo.py", summary)
            == f"{fingerprint[0]}::{fingerprint[1]}::{digest}"
        )

    def test_summaries_that_normalize_alike_no_longer_share_a_key(self) -> None:
        # Round 2 keyed on the LOSSY normalized form, so a finding could drift
        # onto a different finding's record. The verbatim digest ends that.
        first = _key("src/cw/foo.py", "3 call sites at line 10")
        second = _key("src/cw/foo.py", "4 call sites at line 99")
        assert first != second
        assert split_disposition_key(first) == split_disposition_key(second)

    def test_the_digest_is_verbatim_not_normalized_or_stripped(self) -> None:
        assert _key(summary="Bug here") != _key(summary="Bug here ")
        assert _key(summary="Bug here") != _key(summary="bug here")

    def test_a_lone_surrogate_in_the_summary_does_not_raise(self) -> None:
        # The reader recomputes the key from a hand-pasteable JSON string, and
        # json.loads accepts a lone surrogate escape.
        assert _key(summary="Bug \ud800 here").endswith(
            hashlib.sha256(
                "Bug \ud800 here".encode("utf-8", errors="surrogatepass")
            ).hexdigest()
        )

    def test_no_diff_anchor_file_is_never_keyed(self) -> None:
        assert _disposition_key("N/A", "Nothing to anchor on") is None

    def test_split_round_trips_a_key(self) -> None:
        assert split_disposition_key(_key()) == ("src/cw/foo.py", "bug here")

    def test_split_keeps_a_double_colon_inside_the_summary(self) -> None:
        # The normalized summary may itself contain the separator, so the
        # digest is recognised by its fixed shape, never by rpartition alone.
        key = _key("src/cw/foo.py", "foo::bar Baz")
        assert split_disposition_key(key) == ("src/cw/foo.py", "foo::bar baz")

    @pytest.mark.parametrize(
        "legacy",
        [
            "src/cw/foo.py::bug here",
            "src/cw/foo.py::foo::bar",
            f"src/cw/foo.py::bug here::{'a' * 63}",
            f"src/cw/foo.py::bug here::{'A' * 64}",
        ],
    )
    def test_split_treats_a_key_without_a_digest_as_digestless(
        self, legacy: str
    ) -> None:
        file, _, rest = legacy.partition("::")
        assert split_disposition_key(legacy) == (file, rest)


# ---------------------------------------------------------------------------
# render / parse round trip
# ---------------------------------------------------------------------------


class TestRenderParseFindingDispositionBlockRoundTrip:
    def test_round_trips_through_the_marker(self) -> None:
        ledger = {_key(): _entry()}
        rendered = render_finding_disposition_block(ledger)
        assert parse_finding_disposition_block([rendered]) == (ledger, [])

    def test_embeds_its_own_header(self) -> None:
        rendered = render_finding_disposition_block({_key(): _entry()})
        assert rendered.startswith("## Review Finding Dispositions")

    def test_empty_ledger_renders_nothing(self) -> None:
        assert render_finding_disposition_block({}) == ""

    def test_malformed_json_body_degrades_to_empty(self) -> None:
        body = (
            _MARKER_TITLE + "<!-- REVIEW-FINDING-DISPOSITIONS\n{not json\n"
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == ({}, [])

    def test_malformed_entry_is_skipped_without_discarding_siblings(self) -> None:
        key = _key()
        payload = {
            "schema_version": 1,
            "dispositions": {
                key: _entry().model_dump(mode="json"),
                "bad::entry": {"outcome": "NOT_A_VALID_OUTCOME"},
            },
        }
        body = (
            _MARKER_TITLE + "<!-- REVIEW-FINDING-DISPOSITIONS\n"
            f"{json.dumps(payload)}\n"
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == ({key: _entry()}, [])

    def test_non_object_payload_degrades_to_empty(self) -> None:
        body = (
            _MARKER_TITLE + "<!-- REVIEW-FINDING-DISPOSITIONS\n[1, 2, 3]\n"
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == ({}, [])

    def test_non_object_dispositions_value_degrades_to_empty(self) -> None:
        body = (
            _MARKER_TITLE + "<!-- REVIEW-FINDING-DISPOSITIONS\n"
            '{"schema_version": 1, "dispositions": ["not", "a", "map"]}\n'
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == ({}, [])

    def test_missing_dispositions_key_degrades_to_empty(self) -> None:
        body = (
            _MARKER_TITLE + "<!-- REVIEW-FINDING-DISPOSITIONS\n"
            '{"schema_version": 1}\n'
            "REVIEW-FINDING-DISPOSITIONS -->"
        )
        assert parse_finding_disposition_block([body]) == ({}, [])

    def test_missing_marker_yields_empty(self) -> None:
        assert parse_finding_disposition_block(["just prose", ""]) == ({}, [])

    def test_unions_across_comments_newest_recorded_at_wins(self) -> None:
        key_a = _key("src/cw/foo.py", "Bug here")
        key_b = _key("src/cw/bar.py", "Other bug")
        older = render_finding_disposition_block(
            {
                key_a: _entry(recorded_at="2026-08-01T00:00:00Z", rationale="old"),
            }
        )
        newer = render_finding_disposition_block(
            {
                key_a: _entry(recorded_at="2026-08-15T00:00:00Z", rationale="new"),
                key_b: _entry(
                    outcome="ACCEPTED", rationale="accepted", summary="Other bug"
                ),
            }
        )
        parsed, refused = parse_finding_disposition_block([older, newer])
        assert parsed[key_a].rationale == "new"
        assert parsed[key_b].outcome == "ACCEPTED"
        assert refused == []

    def test_an_invalid_record_is_reported_and_never_returned(self) -> None:
        # Validate first, write second: the parse result is what reaches the
        # ledger, so a refused record must not be in it.
        bad_key = _key("src/cw/bar.py", "Other bug")
        rendered = render_finding_disposition_block(
            {_key(): _entry(), bad_key: _entry(actor="", summary="Other bug")}
        )
        parsed, refused = parse_finding_disposition_block([rendered])
        assert list(parsed) == [_key()]
        assert refused == [RefusedDisposition(key=bad_key, missing=["actor"])]

    def test_a_later_invalid_comment_does_not_displace_an_earlier_valid_one(
        self,
    ) -> None:
        # The same eviction path as merge_finding_dispositions, one hop
        # earlier: two comments on ONE thread carrying the same key.
        key = _key()
        valid = render_finding_disposition_block(
            {key: _entry(rationale="the settled one")}
        )
        hijack = render_finding_disposition_block(
            {
                key: _entry(
                    actor="", rationale="hijack", recorded_at="2099-01-01T00:00:00Z"
                )
            }
        )
        parsed, refused = parse_finding_disposition_block([valid, hijack])
        assert parsed[key].rationale == "the settled one"
        assert refused == [RefusedDisposition(key=key, missing=["actor"])]

    def test_a_refused_key_is_reported_once_however_often_it_is_posted(self) -> None:
        bad = render_finding_disposition_block({_key(): _entry(actor="")})
        _, refused = parse_finding_disposition_block([bad, bad])
        assert [r.key for r in refused] == [_key()]


# ---------------------------------------------------------------------------
# Positional parse: a record is made by WHERE it is, not only what it says
# ---------------------------------------------------------------------------


class TestOnlyAMarkerCommentCarriesARecord:
    """#2210 round 4: shape alone is not provenance.

    The pipeline renders model-authored text into the comments this parser
    reads on the next round, so a sentinel block a REVIEWER wrote into its own
    finding used to be indistinguishable from one an operator minted with ``cw
    review settle``. The block is now honoured only where the writer emits it:
    opening the comment body, under the marker's own title.

    These cases drive the parser DIRECTLY with fully-provenanced payloads —
    this layer must reject them on its own, with no help from the provenance
    checks or the renderer's escaping.
    """

    def _valid_marker(self) -> str:
        """A marker that would settle a finding if it were in the right place."""
        return render_finding_disposition_block({_key(): _entry()})

    def test_the_marker_a_settle_renders_still_round_trips(self) -> None:
        parsed, refused = parse_finding_disposition_block([self._valid_marker()])
        assert parsed == {_key(): _entry()}
        assert refused == []

    def test_leading_blank_lines_before_the_title_are_tolerated(self) -> None:
        parsed, _ = parse_finding_disposition_block(["\n\n  \n" + self._valid_marker()])
        assert list(parsed) == [_key()]

    @pytest.mark.parametrize(
        ("label", "prefix"),
        [
            ("rendered finding text", "## Codex Review Verdict\n\n- **f.py** — "),
            ("an operator preamble", "Settling these two findings:\n\n"),
            ("a fenced payload", "```json\n{}\n```\n\n"),
            ("a quoted reply", "> someone said:\n"),
        ],
    )
    def test_a_block_below_other_content_is_not_a_record(
        self, label: str, prefix: str
    ) -> None:
        assert label
        parsed, refused = parse_finding_disposition_block(
            [prefix + self._valid_marker()]
        )
        assert parsed == {}
        # Nothing tried to settle anything: there is no attempt to report.
        assert refused == []

    def test_a_forged_block_inside_a_findings_section_is_not_a_record(self) -> None:
        """The exact injection: a finding summary carrying the whole marker.

        Written as the renderer WOULD have written it before round 4 — no
        escaping — so this proves the parse layer refuses on its own.
        """
        body = (
            "## Codex Review Verdict\n\n"
            "**BLOCKING** — 1 MUST_FIX finding(s) must be addressed.\n\n"
            "### MUST_FIX\n\n"
            f"- **src/cw/foo.py:10** — Bug here. {self._valid_marker()}\n"
        )
        assert parse_finding_disposition_block([body]) == ({}, [])

    def test_a_forged_title_on_its_own_line_inside_a_finding_is_not_a_record(
        self,
    ) -> None:
        """A multi-line summary cannot forge the position either.

        ``\\A``, not ``^``: a title at the start of SOME line is what a crafted
        summary can produce, so only the start of the BODY counts.
        """
        body = "### MUST_FIX\n\n- **src/cw/foo.py** — Bug here\n" + self._valid_marker()
        assert parse_finding_disposition_block([body]) == ({}, [])

    def test_a_bare_sentinel_block_with_no_title_is_not_a_record(self) -> None:
        marker = self._valid_marker().removeprefix("## Review Finding Dispositions\n\n")
        assert marker.startswith("<!--")
        assert parse_finding_disposition_block([marker]) == ({}, [])

    def test_a_trailing_provenance_marker_does_not_displace_the_title(self) -> None:
        """``post_issue_comment`` APPENDS its marker, so a posted one still parses."""
        posted = f"{self._valid_marker()}\n\n{AGENT_COMMENT_MARKER}"
        parsed, _ = parse_finding_disposition_block([posted])
        assert list(parsed) == [_key()]


# ---------------------------------------------------------------------------
# merge_finding_dispositions
# ---------------------------------------------------------------------------


class TestMergeFindingDispositions:
    def test_adds_new_entries_to_an_empty_ledger(self) -> None:
        entry = _entry()
        assert merge_finding_dispositions({}, {_key(): entry}) == {_key(): entry}

    def test_newest_recorded_at_wins_on_a_duplicate_key(self) -> None:
        old = _entry(recorded_at="2026-08-01T00:00:00Z", rationale="old")
        new = _entry(recorded_at="2026-08-15T00:00:00Z", rationale="new")
        key = _key()
        assert (
            merge_finding_dispositions({key: old}, {key: new})[key].rationale == "new"
        )

    def test_an_older_parsed_entry_does_not_overwrite_a_newer_stored_one(self) -> None:
        old = _entry(recorded_at="2026-08-01T00:00:00Z", rationale="old")
        new = _entry(recorded_at="2026-08-15T00:00:00Z", rationale="new")
        key = _key()
        merged = merge_finding_dispositions({key: new}, {key: old})
        assert merged[key].rationale == "new"

    def test_existing_entry_absent_from_the_parsed_set_is_preserved(self) -> None:
        # Forward-only (R3): the ledger is additive and durable. A pass whose
        # comment thread no longer carries the marker must not forget it.
        kept = _entry(rationale="settled long ago")
        kept_key = _key("src/cw/kept.py", "Bug here")
        fresh_key = _key("src/cw/fresh.py", "Bug here")
        merged = merge_finding_dispositions({kept_key: kept}, {fresh_key: _entry()})
        assert merged[kept_key] == kept
        assert set(merged) == {kept_key, fresh_key}

    def test_does_not_mutate_either_input(self) -> None:
        key = _key()
        existing = {key: _entry(rationale="old")}
        parsed = {key: _entry(recorded_at="2026-09-01T00:00:00Z", rationale="new")}
        merge_finding_dispositions(existing, parsed)
        assert existing[key].rationale == "old"
        assert set(parsed) == {key}


class TestInvalidRecordNeverEvictsAValidEntry:
    """Validate first, write second (#2210 round 3, MUST_FIX).

    Round 2 made the READER ignore an under-provenanced record for suppression.
    That is not enough if the same record can still REPLACE a valid entry on
    write: a pasted or malformed block would destroy the provenance of a
    legitimately settled finding on the ledger that decides what stays
    suppressed. The invariant lives at the one write chokepoint.
    """

    def test_an_invalid_record_with_a_later_recorded_at_does_not_evict(self) -> None:
        # The real eviction path: `>=` on recorded_at let ANY later record win.
        key = _key()
        valid = _entry(rationale="the settled one")
        hijack = _entry(
            actor="", rationale="hijack", recorded_at="2099-01-01T00:00:00Z"
        )
        merged = merge_finding_dispositions({key: valid}, {key: hijack})
        assert merged == {key: valid}

    @pytest.mark.parametrize(
        "overrides",
        [
            {"actor": ""},
            {"reviewed_sha": ""},
            {"rationale": "  "},
            {"summary": ""},
            {"recorded_at": "not a timestamp"},
            {"summary": "A different finding entirely"},
        ],
    )
    def test_every_provenance_gap_is_kept_out_of_the_ledger(
        self, overrides: dict[str, object]
    ) -> None:
        key = _key()
        valid = _entry()
        merged = merge_finding_dispositions(
            {key: valid},
            {key: _entry(**{"recorded_at": "2099-01-01T00:00:00Z", **overrides})},
        )
        assert merged == {key: valid}

    def test_an_invalid_record_for_an_unknown_key_is_not_added(self) -> None:
        merged = merge_finding_dispositions({}, {_key(): _entry(actor="")})
        assert merged == {}

    def test_a_digestless_legacy_key_is_not_added(self) -> None:
        merged = merge_finding_dispositions({}, {"src/cw/foo.py::bug here": _entry()})
        assert merged == {}

    def test_the_valid_entry_still_suppresses_after_a_rejected_overwrite(
        self,
    ) -> None:
        finding = _make_finding(severity="MUST_FIX")
        key = _key(finding.file, finding.summary)
        existing = _ledger(finding, rationale="the settled one")
        hijack = {
            key: _entry(
                outcome="ACCEPTED",
                actor="",
                rationale="hijack",
                recorded_at="2099-01-01T00:00:00Z",
                summary=finding.summary,
            )
        }
        merged = merge_finding_dispositions(existing, hijack)

        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)), merged, ticket_id=_TICKET
        )

        assert result.blocking is False
        assert result.accepted[0].disposition == "rejected"
        assert "the settled one" in result.accepted[0].disposition_detail

    def test_a_valid_newer_replacement_is_applied(self) -> None:
        key = _key()
        older = _entry(recorded_at="2026-01-01T00:00:00Z", rationale="older")
        newer = _entry(recorded_at="2026-09-01T00:00:00Z", rationale="newer")
        assert merge_finding_dispositions({key: older}, {key: newer}) == {key: newer}

    def test_a_valid_entry_replaces_an_invalid_one_already_in_the_ledger(
        self,
    ) -> None:
        # Legacy history may hold an under-provenanced row for this key. It
        # applies nothing, so a valid record for the same key heals it
        # whatever the two timestamps say.
        key = _key()
        stale = _entry(actor="", recorded_at="2099-01-01T00:00:00Z")
        healed = _entry(rationale="healed", recorded_at="2026-01-01T00:00:00Z")
        assert merge_finding_dispositions({key: stale}, {key: healed}) == {key: healed}

    def test_entries_already_in_the_ledger_pass_through_untouched(self) -> None:
        # Legacy history is the reader's to keep refusing and reporting; the
        # writer neither drops nor rewrites it.
        legacy = {"src/cw/foo.py::bug here": _entry(actor="")}
        merged = merge_finding_dispositions(legacy, {_key("src/cw/bar.py"): _entry()})
        assert merged["src/cw/foo.py::bug here"] == legacy["src/cw/foo.py::bug here"]
        assert len(merged) == 2

    def test_neither_argument_is_mutated_by_a_rejected_write(self) -> None:
        key = _key()
        existing = {key: _entry(rationale="the settled one")}
        parsed = {key: _entry(actor="", rationale="hijack")}
        merge_finding_dispositions(existing, parsed)
        assert existing[key].rationale == "the settled one"
        assert parsed[key].rationale == "hijack"


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

    def test_a_same_file_entry_below_the_thresholds_is_not_a_candidate(self) -> None:
        # The entry shares the file but not the claim, so it never enters the
        # nearest-decision contest at all.
        finding = self._reworded()
        verdict = _verdict(_accepted(finding))
        suppressed = suppress_adjudicated_findings(
            verdict,
            _ledger_for("src/cw/foo.py", "the retry loop is slow"),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )
        assert suppressed is verdict

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
        assert parse_finding_disposition_block([rendered]) == (ledger, [])

    def test_a_minted_record_carries_the_digest_its_key_binds(self) -> None:
        # The settle -> marker -> reader round trip must keep the binding: the
        # minted record's key ends in the digest of the very summary it stores,
        # and it survives the reader's provenance check (so it is applied).
        ledger = build_finding_disposition_ledger(
            [("src/cw/foo.py", "Bug here", _entry())]
        )
        ((key, entry),) = ledger.items()
        assert key.endswith(hashlib.sha256(entry.summary.encode()).hexdigest())
        enforceable, refused = partition_enforceable_dispositions(
            parse_finding_disposition_block([render_finding_disposition_block(ledger)])[
                0
            ]
        )
        assert enforceable == ledger
        assert refused == []


# ---------------------------------------------------------------------------
# Reader-enforced provenance (#2210 round 2)
# ---------------------------------------------------------------------------


class TestReaderEnforcedProvenance:
    """The READER refuses a record that cannot say who/when/against what/why.

    Every guard #2210 added — the mandatory ``--reason``, the recorded actor,
    the CLI-stamped timestamp, the reviewed sha, the refusal inside a worker —
    lives in ``cw review settle``, the WRITER. A block pasted by hand, or one a
    worker writes into a ticket comment itself, never passes through it, so
    enforcing there enforces nothing. These tests pin the enforcement to the
    reader instead: a record short of the full provenance set is ignored,
    logged, and reported, never applied.
    """

    def _blocking(self, finding: Finding) -> ReviewVerdict:
        return _verdict(_accepted(finding))

    def test_full_provenance_record_is_applied(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        result = suppress_adjudicated_findings(
            self._blocking(finding), _ledger(finding), ticket_id=_TICKET
        )

        assert result.blocking is False
        assert result.accepted[0].disposition == "rejected"
        assert result.refused_dispositions == []

    @pytest.mark.parametrize(
        ("gap", "overrides"),
        [
            ("actor", {"actor": ""}),
            ("actor", {"actor": "   "}),
            ("rationale", {"rationale": ""}),
            ("reviewed_sha", {"reviewed_sha": ""}),
            ("summary", {"summary": ""}),
            ("recorded_at", {"recorded_at": ""}),
            ("recorded_at", {"recorded_at": "whenever"}),
            ("recorded_at", {"recorded_at": "2026-08-16T00:00:00+02:00"}),
        ],
    )
    def test_record_missing_provenance_is_never_applied(
        self, gap: str, overrides: dict[str, object]
    ) -> None:
        finding = _make_finding(severity="MUST_FIX")
        result = suppress_adjudicated_findings(
            self._blocking(finding), _ledger(finding, **overrides), ticket_id=_TICKET
        )

        assert result.blocking is True
        assert result.must_fix == [finding]
        assert result.accepted[0].disposition == "fixed"
        assert [r.key for r in result.refused_dispositions] == [
            _disposition_key(finding.file, finding.summary)
        ]
        assert gap in result.refused_dispositions[0].missing
        assert read_events() == []

    def test_a_pre_provenance_record_is_refused_rather_than_honoured(self) -> None:
        """A marker or queue row written before #2210 carries no provenance.

        Those fields are optional and defaulted so such a record still LOADS —
        but loading is not applying, and an entry that cannot name an actor,
        a sha or a verbatim summary is exactly the unaudited suppression the
        reader now refuses.
        """
        finding = _make_finding(severity="MUST_FIX")
        key = _disposition_key(finding.file, finding.summary)
        assert key is not None
        legacy = {
            key: FindingDisposition.model_validate(
                {
                    "outcome": "REJECTED",
                    "rationale": "settled in round 1",
                    "recorded_at": "2026-08-16T00:00:00Z",
                }
            )
        }
        result = suppress_adjudicated_findings(
            self._blocking(finding), legacy, ticket_id=_TICKET
        )

        assert result.blocking is True
        assert sorted(result.refused_dispositions[0].missing) == [
            "actor",
            "reviewed_sha",
            "summary",
        ]

    def test_refusal_is_logged_once_at_warning_naming_ticket_and_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        finding = _make_finding(severity="MUST_FIX")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            suppress_adjudicated_findings(
                self._blocking(finding),
                _ledger(finding, actor=""),
                ticket_id=_TICKET,
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert _TICKET in message
        assert finding.file in message
        assert "actor" in message

    def test_a_refused_record_does_not_stop_a_well_formed_sibling(self) -> None:
        good = _make_finding(severity="MUST_FIX")
        bad = _make_finding(
            severity="MUST_FIX", file="src/cw/bar.py", summary="Other bug"
        )
        ledger = {**_ledger(good), **_ledger(bad, actor="")}

        result = suppress_adjudicated_findings(
            _verdict(_accepted(good), _accepted(bad)), ledger, ticket_id=_TICKET
        )

        assert [af.disposition for af in result.accepted] == ["rejected", "fixed"]
        assert result.blocking is True
        assert [r.key for r in result.refused_dispositions] == [
            _disposition_key(bad.file, bad.summary)
        ]

    def test_an_under_provenanced_accepted_record_is_refused_too(self) -> None:
        """An ``ACCEPTED`` entry is binding on the reviewer's prompt.

        It changes no gate mechanically, but it reaches the model as a decided
        finding — so an unaudited one is a suppression channel of its own and
        gets the same treatment.
        """
        finding = _make_finding(severity="MUST_FIX")
        result = suppress_adjudicated_findings(
            self._blocking(finding),
            _ledger(finding, outcome="ACCEPTED", reviewed_sha=""),
            ticket_id=_TICKET,
        )

        assert [r.missing for r in result.refused_dispositions] == [["reviewed_sha"]]

    def test_partition_splits_the_ledger_and_names_every_gap(self) -> None:
        good = _make_finding(severity="MUST_FIX")
        bad = _make_finding(
            severity="MUST_FIX", file="src/cw/bar.py", summary="Other bug"
        )
        ledger = {**_ledger(good), **_ledger(bad, actor="", reviewed_sha="")}

        enforceable, refused = partition_enforceable_dispositions(ledger)

        assert list(enforceable) == [_disposition_key(good.file, good.summary)]
        assert [r.missing for r in refused] == [["actor", "reviewed_sha"]]


# ---------------------------------------------------------------------------
# The key binds the verbatim summary (#2210 round 3)
# ---------------------------------------------------------------------------

#: Two findings the normalizer cannot tell apart ("at line N" is stripped) whose
#: verbatim text differs, and whose claim tokens clear the claim tier's anchored
#: floor: the pair that USED to share one ledger key.
_SETTLED_WORDING = "`foo_bar` drops the follow-up task at line 10"
_DRIFTED_WORDING = "`foo_bar` drops the follow-up task at line 99"


class TestKeyBindsTheVerbatimSummary:
    """A record may only ever apply to the finding it was settled for.

    The round-2 key held only the LOSSY normalized summary, so every rewording
    that normalised alike shared one record and a finding could drift onto a
    DIFFERENT finding's suppression. The verbatim digest closes that.
    """

    def test_two_findings_differing_only_in_summary_do_not_share_a_record(
        self,
    ) -> None:
        settled = _make_finding(severity="MUST_FIX", summary=_SETTLED_WORDING)
        drifted = _make_finding(severity="MUST_FIX", summary=_DRIFTED_WORDING)
        assert split_disposition_key(_key(settled.file, settled.summary)) == (
            split_disposition_key(_key(drifted.file, drifted.summary))
        )
        verdict = _verdict(_accepted(drifted))

        result = suppress_adjudicated_findings(
            verdict, _ledger(settled), ticket_id=_TICKET
        )

        assert result is verdict
        assert result.blocking is True
        assert not read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED]
        )

    def test_identical_summaries_do_share_a_record(self) -> None:
        settled = _make_finding(severity="MUST_FIX", summary=_SETTLED_WORDING)
        result = suppress_adjudicated_findings(
            _verdict(_accepted(settled)), _ledger(settled), ticket_id=_TICKET
        )
        assert result.blocking is False

    def test_a_same_normalized_different_verbatim_finding_falls_to_the_claim_tier(
        self,
    ) -> None:
        # Gate off: shadowed, never applied. The exact tier cannot see it.
        settled = _make_finding(severity="MUST_FIX", summary=_SETTLED_WORDING)
        drifted = _make_finding(severity="MUST_FIX", summary=_DRIFTED_WORDING)
        verdict = _verdict(_accepted(drifted))

        result = suppress_adjudicated_findings(
            verdict, _ledger(settled), ticket_id=_TICKET, reviewed_sha="abc1234"
        )

        assert result is verdict
        shadowed = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_CLAIM_SHADOWED]
        )
        assert len(shadowed) == 1
        assert shadowed[0].payload["matched_key"] == _key(settled.file, settled.summary)

    def test_the_claim_tier_is_the_only_path_that_applies_it_and_says_so(
        self,
    ) -> None:
        settled = _make_finding(severity="MUST_FIX", summary=_SETTLED_WORDING)
        drifted = _make_finding(severity="MUST_FIX", summary=_DRIFTED_WORDING)

        result = suppress_adjudicated_findings(
            _verdict(_accepted(drifted)),
            _ledger(settled),
            ticket_id=_TICKET,
            claim_tier_enabled=True,
        )

        assert result.blocking is False
        assert "claim similarity" in result.accepted[0].disposition_detail
        suppressed = read_events(
            event_types=[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_SUPPRESSED]
        )
        assert [e.payload["match_kind"] for e in suppressed] == ["claim"]

    def test_the_exact_tier_accepted_veto_still_holds_for_a_byte_identical_twin(
        self,
    ) -> None:
        settled = _make_finding(severity="MUST_FIX", summary=_SETTLED_WORDING)
        drifted = _make_finding(severity="MUST_FIX", summary=_DRIFTED_WORDING)
        ledger = {
            **_ledger(drifted, outcome="ACCEPTED"),
            **_ledger(settled),
        }
        verdict = _verdict(_accepted(drifted))

        result = suppress_adjudicated_findings(
            verdict, ledger, ticket_id=_TICKET, claim_tier_enabled=True
        )

        assert result is verdict

    def test_a_record_whose_summary_is_not_the_one_its_key_was_minted_from_is_refused(
        self,
    ) -> None:
        # The key says "Bug here"; the payload says something else entirely.
        finding = _make_finding(severity="MUST_FIX")
        ledger = _ledger(finding, summary="A different finding altogether")

        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)), ledger, ticket_id=_TICKET
        )

        assert result.blocking is True
        assert [r.missing for r in result.refused_dispositions] == [["identity"]]

    def test_a_key_whose_digest_belongs_to_another_summary_is_refused(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        other_digest = _key(finding.file, "Some other summary").rsplit("::", 1)[1]
        forged = {f"{finding.file}::bug here::{other_digest}": _entry()}

        _, refused = partition_enforceable_dispositions(forged)

        assert [r.missing for r in refused] == [["identity"]]

    def test_a_digestless_legacy_key_is_refused_as_an_identity_gap(self) -> None:
        # Every record minted before round 3 has this shape. It must be
        # re-settled with `cw review settle`; it is never silently honoured.
        finding = _make_finding(severity="MUST_FIX")
        fingerprint = fingerprint_v1(finding.file, finding.summary)
        assert fingerprint is not None
        legacy = {"::".join(fingerprint): _entry()}

        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)), legacy, ticket_id=_TICKET
        )

        assert result.blocking is True
        assert [r.missing for r in result.refused_dispositions] == [["identity"]]

    @pytest.mark.parametrize("key", ["", "::", "src/cw/foo.py", "::bug here"])
    def test_a_key_with_no_file_or_no_normalized_summary_is_an_identity_gap(
        self, key: str
    ) -> None:
        _, refused = partition_enforceable_dispositions({key: _entry()})
        assert [r.missing for r in refused] == [["identity"]]

    def test_a_blank_summary_reports_the_summary_gap_alone(self) -> None:
        # The binding cannot be checked without a summary, and "summary" is
        # already the gap that names it: one problem, one entry.
        finding = _make_finding(severity="MUST_FIX")
        _, refused = partition_enforceable_dispositions(_ledger(finding, summary=""))
        assert [r.missing for r in refused] == [["summary"]]


# ---------------------------------------------------------------------------
# Refusals from the write path reach the verdict, once (#2210 round 3)
# ---------------------------------------------------------------------------


class TestRefusedRecordsFromTheWritePath:
    def _refused(self, finding: Finding) -> RefusedDisposition:
        return RefusedDisposition(
            key=_key(finding.file, finding.summary), missing=["actor"]
        )

    def test_records_refused_at_parse_time_are_stamped_on_the_verdict(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            {},
            ticket_id=_TICKET,
            refused=[self._refused(finding)],
        )
        assert result.refused_dispositions == [self._refused(finding)]

    def test_they_are_merged_with_refusals_from_legacy_ledger_rows(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        legacy_key = "src/cw/old.py::old bug"
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            {legacy_key: _entry(summary="old bug")},
            ticket_id=_TICKET,
            refused=[self._refused(finding)],
        )
        assert sorted(r.key for r in result.refused_dispositions) == sorted(
            [legacy_key, self._refused(finding).key]
        )

    def test_a_record_refused_twice_is_reported_once_and_warned_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The same key can be refused at parse time AND sit as a legacy row in
        # the durable ledger. One pass must not WARN for it twice: the parse
        # side already logged, so only the ledger-derived remainder logs here.
        finding = _make_finding(severity="MUST_FIX")
        legacy = {self._refused(finding).key: _entry(actor="")}
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = suppress_adjudicated_findings(
                _verdict(_accepted(finding)),
                legacy,
                ticket_id=_TICKET,
                refused=[self._refused(finding)],
            )
        assert [r.key for r in result.refused_dispositions] == [
            self._refused(finding).key
        ]
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_log_refused_dispositions_names_ticket_key_and_gaps_once_each(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        refused = [
            RefusedDisposition(key="k1", missing=["actor", "recorded_at"]),
            RefusedDisposition(key="k2", missing=["identity"]),
        ]
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            log_refused_dispositions(refused, _TICKET)
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 2
        assert all(_TICKET in m for m in messages)
        assert "key=k1" in messages[0]
        assert "actor, recorded_at" in messages[0]
        assert "NOT" in messages[0]

    def test_refusals_are_reported_in_key_order(self) -> None:
        finding = _make_finding(severity="MUST_FIX")
        result = suppress_adjudicated_findings(
            _verdict(_accepted(finding)),
            {"z::legacy": _entry(), "a::legacy": _entry()},
            ticket_id=_TICKET,
            refused=[RefusedDisposition(key="m::parsed", missing=["actor"])],
        )
        assert [r.key for r in result.refused_dispositions] == [
            "a::legacy",
            "m::parsed",
            "z::legacy",
        ]

    def test_nothing_refused_leaves_the_verdict_untouched(self) -> None:
        verdict = _verdict(_accepted(_make_finding(severity="MUST_FIX")))
        assert (
            suppress_adjudicated_findings(verdict, {}, ticket_id=_TICKET, refused=[])
            is verdict
        )


class TestBuildLedgerRefusesAnInvalidEntry:
    """``cw review settle`` always supplies full provenance; if an entry were
    refused it must raise, exactly like an un-keyable file, never drop it."""

    def test_an_entry_with_a_provenance_gap_raises_naming_the_gaps(self) -> None:
        with pytest.raises(ValueError, match="actor"):
            build_finding_disposition_ledger(
                [("src/cw/foo.py", "Bug here", _entry(actor=""))]
            )

    def test_an_entry_whose_summary_is_not_the_keyed_summary_raises(self) -> None:
        with pytest.raises(ValueError, match="identity"):
            build_finding_disposition_ledger(
                [("src/cw/foo.py", "Bug here", _entry(summary="Something else"))]
            )


def _sentinel_literals_outside_docstrings(tree: ast.AST) -> list[int]:
    """Line numbers of string literals naming the sentinel, docstrings aside."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and DISPOSITION_SENTINEL in node.value
        and id(node) not in docstrings
    ]


class TestDispositionSentinelConstant:
    def test_the_sentinel_is_the_wire_grammar_the_marker_uses(self) -> None:
        rendered = render_finding_disposition_block({_key(): _entry()})
        assert f"<!-- {DISPOSITION_SENTINEL}\n" in rendered
        assert rendered.rstrip().endswith(f"{DISPOSITION_SENTINEL} -->")

    def test_no_other_module_spells_the_sentinel_as_a_string_literal(self) -> None:
        """#2210 round 3: a parser keys on this string, so it has ONE spelling.

        The reviewer prompt and the blocking comment both named it inline, a
        second copy the parser would never notice drifting. Asserts on the
        SOURCE — behaviour is identical either way, which is the problem — in
        the style of ``test_cli_hook_io``'s constant-drift guard. Docstrings and
        comments naming it in prose are fine; a literal that reaches a prompt
        or a comment body is not, so the only one left is the definition.
        """
        offenders: dict[str, list[int]] = {}
        for path in sorted(Path(cw.__file__).parent.rglob("*.py")):
            lines = _sentinel_literals_outside_docstrings(
                ast.parse(path.read_text(encoding="utf-8"))
            )
            if lines:
                offenders[path.name] = lines
        assert list(offenders) == ["review_markers.py"]
        assert len(offenders["review_markers.py"]) == 1


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
