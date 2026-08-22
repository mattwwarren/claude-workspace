"""Shared unavailability-failure classifier (RFC 0011 A2, #1156).

Generalizes #1049's push-site 4-signature auth-failure prose classifier into
one canonical signature table covering three members of the A1
``awaiting_operator`` failure family: network-unreachable, GitHub 5xx /
secondary-rate-limit, and auth-failure (the original #1049 four signatures).
``tool_denied`` (#636) is deliberately excluded per RFC 0011 A2 R4 — it
carries its own retry_eligible/auto-redispatch semantics and would
misclassify a classifier-flaky hiccup as an overnight-locked key if folded
in here.

``MCP-github-unreachable`` is deliberately deferred (2026-07-15 premise-3
resolution, #1156) — no MCP-github client/wrapper exists in this repo and no
captured failure transcript exists to lift a verbatim signature from; an
invented placeholder would ship a fixture and a classifier arm that never
fires. Add it in a follow-up once a real failure transcript is captured.

Runtime split (R2): push sites (``auto-dev-finalize.md``) and intake fetch
(``auto-dev-intake.md``) run inside the worker LLM session with no Python
import available, so those two skill files carry a PROSE MIRROR of the
signature list below with an explicit comment pointing back here (pattern:
``cw.dev_queue.lifecycle._PLAN_SPEC_MARKER`` mirroring ``gh._PLAN_MARKER``).
Keep the three copies in sync — see
``test_unavailability_signatures_mirrored_in_prose`` for the drift guard.

A separate, sibling ``PROVIDER_UNAVAILABILITY_SIGNATURES`` table (#1923)
covers the Anthropic-API provider-overload (HTTP 529) family. It is
deliberately NOT folded into ``UNAVAILABILITY_SIGNATURES`` above: that
table's drift guard requires every signature appear verbatim in
``auto-dev-finalize.md``/``auto-dev-intake.md``, two prose files scoped to
git/gh subprocess failures that have no business carrying an API-error
string. See ``classify_provider_unavailability`` below.
"""

from __future__ import annotations

FAMILY_NETWORK_UNREACHABLE = "network_unreachable"
FAMILY_GITHUB_5XX_OR_RATE_LIMIT = "github_5xx_or_rate_limit"
FAMILY_AUTH_FAILURE = "auth_failure"
# FAMILY_MCP_GITHUB_UNREACHABLE: deferred -- no verified signature yet;
# capture a real MCP tool-error transcript before adding (see #1156
# premise-3 resolution, 2026-07-15T23:32:55Z).

# (signature substring, family) pairs, checked in order, first match wins.
# Auth-failure entries are #1049's original four signatures, unchanged
# (auto-dev-finalize.md:229-232) -- do not reorder/reword without updating
# the prose mirror.
UNAVAILABILITY_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("Permission denied (publickey)", FAMILY_AUTH_FAILURE),
    ("could not read Username", FAMILY_AUTH_FAILURE),
    ("Host key verification failed", FAMILY_AUTH_FAILURE),
    ("Authentication failed", FAMILY_AUTH_FAILURE),
    # network-unreachable -- new for #1156. First 3 are well-known DNS/routing
    # failures; the last 2 are the dead-proxy shape live-captured in Comment 6
    # (2026-07-15T19:08:52Z) -- the un-expanded 3-signature list misses the
    # most common laptop-offline shape (connection refused/timeout).
    ("Could not resolve host", FAMILY_NETWORK_UNREACHABLE),
    ("Network is unreachable", FAMILY_NETWORK_UNREACHABLE),
    ("Temporary failure in name resolution", FAMILY_NETWORK_UNREACHABLE),
    ("Failed to connect to", FAMILY_NETWORK_UNREACHABLE),
    ("Could not connect to server", FAMILY_NETWORK_UNREACHABLE),
    # GitHub 5xx / secondary-rate-limit -- new for #1156. "HTTP 500/502/503"
    # are grounded in gh's verified `gh: <message> (HTTP <code>)` positional
    # format (round-3 resolution, 2026-07-16T03:40:41Z); the message text
    # itself ("Internal Server Error") was an unverified guess and was
    # dropped in favor of the code-position match.
    ("secondary rate limit", FAMILY_GITHUB_5XX_OR_RATE_LIMIT),
    ("HTTP 502", FAMILY_GITHUB_5XX_OR_RATE_LIMIT),
    ("HTTP 503", FAMILY_GITHUB_5XX_OR_RATE_LIMIT),
    ("HTTP 500", FAMILY_GITHUB_5XX_OR_RATE_LIMIT),
)


def classify_unavailability(text: str) -> str | None:
    """Return the matched unavailability family, or None if no signature matches.

    Exact substring match (mirrors #1049's "match verbatim" prose semantics).
    Never raises -- any failure to match is a plain None, consistent with
    cw.review_strategy/cw.collision's safe-default philosophy.
    """
    for signature, family in UNAVAILABILITY_SIGNATURES:
        if signature in text:
            return family
    return None


# Sibling family: Anthropic provider overload (HTTP 529). Deliberately a
# separate table from UNAVAILABILITY_SIGNATURES -- see the module docstring.
FAMILY_PROVIDER_OVERLOAD = "provider_overload"

# (signature substring, family) pairs, checked in order, first match wins.
# Signature lifted verbatim from a live capture: dev-1751 impl worker,
# session 286032f7-47ee-4985-a45d-e7a946aa1d9d, 8 occurrences starting
# 2026-08-18T17:27:09.071Z. A deliberate sibling table per the #1923 SPLIT
# decision, not a member of UNAVAILABILITY_SIGNATURES.
PROVIDER_UNAVAILABILITY_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("API Error: 529 Overloaded", FAMILY_PROVIDER_OVERLOAD),
)


def classify_provider_unavailability(text: str) -> str | None:
    """Return the matched provider-overload family, or None if no signature matches.

    Same substring-match/never-raises contract as classify_unavailability.
    Kept as an independent small loop rather than factored through a shared
    helper with classify_unavailability -- a 1-entry table doesn't justify
    coupling two independently-evolving tables through shared infra; revisit
    if a third table is ever added.
    """
    for signature, family in PROVIDER_UNAVAILABILITY_SIGNATURES:
        if signature in text:
            return family
    return None
