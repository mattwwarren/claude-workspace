"""Tests for src/cw/unavailability.py — classify_unavailability unit tests (#1156).

Mirrors tests/test_review_strategy.py / tests/test_collision.py in shape: a
parametrized fixture table covers one case per shipped signature-family
member, plus structural tests pinning down matching semantics and guarding
against a ``tool_denied`` (#636) regression per RFC 0011 A2 R4.

The drift-guard test (``test_unavailability_signatures_mirrored_in_prose``)
reads the command prose through the shared ``_cmd()`` helper in
``tests/conftest.py`` (#1787), the same reader used by
``tests/test_auto_dev_model_pins.py`` and
``tests/test_auto_dev_preflight_resolutions.py``.
"""

from __future__ import annotations

import pytest

from cw.unavailability import (
    FAMILY_AUTH_FAILURE,
    FAMILY_GITHUB_5XX_OR_RATE_LIMIT,
    FAMILY_NETWORK_UNREACHABLE,
    FAMILY_PROVIDER_OVERLOAD,
    PROVIDER_UNAVAILABILITY_SIGNATURES,
    UNAVAILABILITY_SIGNATURES,
    classify_provider_unavailability,
    classify_unavailability,
)
from tests.conftest import _appendix, _cmd

# Verbatim capture, dev-1751 impl worker, session
# 286032f7-47ee-4985-a45d-e7a946aa1d9d, 2026-08-18T17:27:09.071Z (#1923).
_CAPTURED_529_TEXT = (
    'Agent "Implement plan for ticket #1751" failed: Agent terminated early '
    "due to an API error: API Error: 529 Overloaded. This is a server-side "
    "issue, usually temporary — try again in a moment. If it persists, check "
    "https://status.claude.com."
)


class TestClassifyUnavailabilityFixtures:
    """One case per shipped family member, plus a negative (non-matching) case."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "...git@github.com: Permission denied (publickey).",
                FAMILY_AUTH_FAILURE,
            ),
            (
                "remote: Invalid username or password.\n"
                "fatal: Authentication failed for 'https://...'",
                FAMILY_AUTH_FAILURE,
            ),
            (
                "fatal: could not read Username for 'https://github.com': "
                "terminal prompts disabled",
                FAMILY_AUTH_FAILURE,
            ),
            (
                "Host key verification failed.\n"
                "fatal: Could not read from remote repository.",
                FAMILY_AUTH_FAILURE,
            ),
            (
                "ssh: connect to host github.com port 22: Network is unreachable",
                FAMILY_NETWORK_UNREACHABLE,
            ),
            (
                "fatal: unable to access '...': Failed to connect to "
                "github.com port 443: Could not connect to server",
                FAMILY_NETWORK_UNREACHABLE,
            ),
            (
                "gh: You have exceeded a secondary rate limit. Please wait...",
                FAMILY_GITHUB_5XX_OR_RATE_LIMIT,
            ),
            (
                "gh: Something Went Wrong (HTTP 500)",
                FAMILY_GITHUB_5XX_OR_RATE_LIMIT,
            ),
            (
                "error: pathspec 'foo' did not match any file(s) known to git",
                None,
            ),
        ],
    )
    def test_fixture(self, text: str, expected: str | None) -> None:
        assert classify_unavailability(text) == expected


class TestClassifyUnavailabilityStructural:
    def test_classify_unavailability_is_case_sensitive(
        self,
    ) -> None:
        """Matching is exact substring (case-sensitive) -- mirrors #1049's

        "match verbatim" prose semantics. A case change must NOT match, so a
        future change to matching semantics fails loudly here rather than
        silently.
        """
        assert classify_unavailability("permission denied (publickey)") is None
        assert (
            classify_unavailability("Permission denied (publickey)")
            == FAMILY_AUTH_FAILURE
        )

    def test_classify_unavailability_empty_string_returns_none(self) -> None:
        assert classify_unavailability("") is None

    def test_family_constants_are_frozen_and_disjoint(self) -> None:
        """Guards against a copy-paste collision when a 4th/5th family

        (e.g. MCP-github-unreachable or tool_denied) is added later.
        """
        constants = (
            FAMILY_NETWORK_UNREACHABLE,
            FAMILY_GITHUB_5XX_OR_RATE_LIMIT,
            FAMILY_AUTH_FAILURE,
        )
        assert len(set(constants)) == len(constants)
        assert isinstance(UNAVAILABILITY_SIGNATURES, tuple)

    def test_signature_table_excludes_tool_denied(self) -> None:
        """RFC 0011 A2 R4: tool_denied (#636) must never be folded in here.

        It carries its own retry_eligible/auto-redispatch semantics and would
        misclassify a classifier-flaky hiccup as an overnight-locked key.
        """
        for signature, family in UNAVAILABILITY_SIGNATURES:
            assert "tool_denied" not in signature
            assert family != "tool_denied"

    def test_unavailability_signatures_mirrored_in_prose(self) -> None:
        """Drift guard: every shipped signature must appear verbatim in both

        prose mirrors. Adapted from test_plan_spec_marker_matches_gh_marker
        since there's no Python constant on the prose side to compare directly.

        #1879 moved the intake-side mirror out of ``auto-dev-intake.md`` and
        into its companion appendix: the table is consulted only when a ticket
        fetch has already failed, so it is rare-path content the worker should
        not load on every run. The assertion follows the content -- it is
        re-pointed at the appendix, not weakened or dropped.
        """
        finalize = _appendix("finalize")
        intake_appendix = _appendix("intake")
        for signature, _family in UNAVAILABILITY_SIGNATURES:
            assert signature in finalize, (
                f"{signature!r} missing from auto-dev-finalize-appendix.md prose mirror"
            )
            assert signature in intake_appendix, (
                f"{signature!r} missing from auto-dev-intake-appendix.md prose mirror"
            )

    def test_classify_unavailability_does_not_match_529_signature(self) -> None:
        """The existing classifier must stay blind to the 529 signature (#1923).

        Proves the two tables don't silently collide -- classify_unavailability
        (the pre-existing 3-family classifier) must return None for the
        captured provider-overload text, which lives only in the new sibling
        table.
        """
        assert classify_unavailability(_CAPTURED_529_TEXT) is None


class TestClassifyProviderUnavailability:
    """Sibling classifier for the provider-overload (API 529) family (#1923).

    Deliberately separate from TestClassifyUnavailabilityFixtures /
    UNAVAILABILITY_SIGNATURES -- see PROVIDER_UNAVAILABILITY_SIGNATURES'
    module-level comment for why this is a new table, not a new member of
    the existing one.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (_CAPTURED_529_TEXT, FAMILY_PROVIDER_OVERLOAD),
            (
                "error: pathspec 'foo' did not match any file(s) known to git",
                None,
            ),
        ],
    )
    def test_fixture(self, text: str, expected: str | None) -> None:
        assert classify_provider_unavailability(text) == expected

    def test_classify_provider_unavailability_is_case_sensitive(self) -> None:
        assert classify_provider_unavailability("api error: 529 overloaded") is None
        assert (
            classify_provider_unavailability("API Error: 529 Overloaded")
            == FAMILY_PROVIDER_OVERLOAD
        )

    def test_classify_provider_unavailability_does_not_match_existing_families(
        self,
    ) -> None:
        """Genuine disjointness, not accidental non-overlap (#1923)."""
        assert (
            classify_provider_unavailability(
                "...git@github.com: Permission denied (publickey)."
            )
            is None
        )
        assert (
            classify_provider_unavailability("gh: Something Went Wrong (HTTP 500)")
            is None
        )

    def test_provider_unavailability_signatures_table_is_frozen(self) -> None:
        assert isinstance(PROVIDER_UNAVAILABILITY_SIGNATURES, tuple)
