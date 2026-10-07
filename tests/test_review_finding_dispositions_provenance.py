"""Tests for ``cw.review_finding_dispositions.provenance`` (GitHub #1838, #2498).

The provenance gate: the newest-wins, validate-first ledger merge, the
reader-enforced provenance partition, the key's binding to the verbatim
summary, and refusal reporting. Relocated verbatim from the flat
``tests/test_review_finding_dispositions.py`` when the module became a package
(#2498), 1:1 with the ``provenance`` submodule per the CLAUDE.md Testing
convention. Reuses ``tests/conftest.py``'s ``_make_finding`` fixture rather
than re-declaring an equivalent; ``_accepted``/``_verdict`` are declared
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
from cw.review_debt import fingerprint_v1
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
    log_refused_dispositions,
    merge_finding_dispositions,
    partition_enforceable_dispositions,
    split_disposition_key,
    suppress_adjudicated_findings,
)
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict
from cw.review_markers import RefusedDisposition

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
