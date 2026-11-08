"""Tests for ``cw.review_finding_dispositions.model`` (GitHub #1838, #2498).

The ledger key and its inverse: ``_disposition_key`` and
``split_disposition_key``. Relocated verbatim from the flat
``tests/test_review_finding_dispositions.py`` when the module became a package
(#2498), 1:1 with the ``model`` submodule per the CLAUDE.md Testing convention.
"""

from __future__ import annotations

import hashlib

import pytest

from cw.review_debt import fingerprint_v1, normalize_summary
from cw.review_finding_dispositions import (
    _disposition_key,
    debt_key,
    split_disposition_key,
)


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


class TestDebtKey:
    """``debt_key`` (#3041): the review-debt dedup key."""

    def test_an_ordinary_finding_hashes_its_disposition_key(self) -> None:
        key = debt_key("src/cw/foo.py", "Bug here")
        expected = hashlib.sha256(_key().encode("utf-8")).hexdigest()
        assert key == expected
        assert debt_key("src/cw/foo.py", "Bug here") == key

    def test_an_anchorless_finding_hashes_the_normalized_summary(self) -> None:
        summary = "Missing  CHECK at line 4"
        normalized = normalize_summary(summary)
        expected = hashlib.sha256(f"N/A|{normalized}".encode()).hexdigest()
        assert debt_key("N/A", summary) == expected

    def test_anchorless_summaries_that_normalize_alike_collide(self) -> None:
        assert debt_key("N/A", "3 call sites miss it") == debt_key(
            "N/A", "4  call sites miss it"
        )

    def test_anchorless_summaries_that_differ_do_not_collide(self) -> None:
        assert debt_key("N/A", "missing guard") != debt_key("N/A", "missing test")

    def test_an_engine_fingerprint_wins(self) -> None:
        key = debt_key("src/cw/foo.py", "Bug", fingerprint="r1:abc.def-9")
        assert key == "engine:r1:abc.def-9"

    @pytest.mark.parametrize("bad", ["a-->b", "a b", "a\nb", ""])
    def test_an_unsafe_fingerprint_raises(self, bad: str) -> None:
        with pytest.raises(ValueError, match="fingerprint"):
            debt_key("src/cw/foo.py", "Bug", fingerprint=bad)

    def test_the_same_summary_under_different_files_differs(self) -> None:
        assert debt_key("a.py", "Bug here") != debt_key("b.py", "Bug here")

    def test_the_key_can_never_close_an_html_comment(self) -> None:
        assert "-->" not in debt_key("x-->y.py", "a --> b")

    def test_an_underscore_fingerprint_raises(self) -> None:
        """``_`` is reserved for the emit file naming's ``:`` substitution."""
        with pytest.raises(ValueError, match="fingerprint"):
            debt_key("a.py", "Bug", fingerprint="a_b")
