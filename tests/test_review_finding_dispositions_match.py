"""Tests for ``cw.review_finding_dispositions.match`` (GitHub #2210, #2498).

The fuzzy claim tier: the tokenizer, symbol extraction and similarity score,
claim matching through ``suppress_adjudicated_findings``, and the typed
``contests_adjudication`` escape hatch. Relocated verbatim from the flat
``tests/test_review_finding_dispositions.py`` when the module became a package
(#2498), 1:1 with the ``match`` submodule per the CLAUDE.md Testing convention.
Reuses ``tests/conftest.py``'s ``_make_finding`` fixture rather than
re-declaring an equivalent; ``_accepted``/``_verdict`` are declared file-local
here for the same reason ``tests/test_review_adjudication.py`` declares its own
— they are thin, module-specific construction helpers, not generically
reusable builders.
"""

from __future__ import annotations

import logging

import pytest

from cw.auto_dev_result import Review
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_finding_dispositions import (
    FindingDisposition,
    _claim_similarity,
    _claim_symbols,
    _claim_tokens,
    _disposition_key,
    suppress_adjudicated_findings,
)
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
