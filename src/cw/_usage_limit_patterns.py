"""Table of Claude usage-limit wordings joined into ``USAGE_LIMIT_RE`` (#2220).

Every string here comes from an external binary (the ``claude`` CLI and its
docs) and can change without notice. A wording change is therefore a data edit
to :data:`USAGE_LIMIT_PATTERNS`, not a code change: each row records its own
provenance so a future reader can tell which release a wording was read from.

:mod:`cw.exceptions` joins the rows into one alternation and compiles it as
``USAGE_LIMIT_RE``, the single detection seam the spawn path, ``queue_peek`` and
the reconcile detectors share. This is a leaf module: it imports nothing from
``cw`` so :mod:`cw.exceptions` (itself dependency-light) can import it without a
cycle (precedent: :mod:`cw._text`).

Known, accepted exposure: the detector scans agent prose as well as tool
output, so an unquoted mention of a wording in a worker's own text can match.
The pre-existing ``hit_your`` row had the same exposure.
"""

from __future__ import annotations

from typing import NamedTuple


class UsageLimitPattern(NamedTuple):
    """One usage-limit wording: where it came from and the regex source."""

    name: str
    source: str
    pattern: str


# Non-capturing form of the reset-time grammar in
# ``cw.exceptions.USAGE_LIMIT_RESET_RE``, used only inside lookaheads so a
# reset-bearing row requires a parseable clock time without consuming it. Named
# groups cannot repeat inside one alternation, hence the separate fragment.
_RESET_WHEN = (
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]{0,6}\s{1,4}"
    r"|[a-z]{3,9}\.?\s{1,4}[0-9]{1,2},?\s{1,4})?"
    r"[0-9]{1,2}(?::[0-9]{2})?\s?[ap]\.?m\b"
)

# Rows are compiled together with re.IGNORECASE. Reset-bearing rows use a
# lookahead so ``match.end()`` lands right after "limit": the companion
# ``USAGE_LIMIT_RESET_RE`` is applied at the LAST match's end (R5), so the
# anchor must sit immediately before " resets <time>". Bounded quantifiers only,
# none nested over an unbounded one.
USAGE_LIMIT_PATTERNS: tuple[UsageLimitPattern, ...] = (
    # "You've hit your session/weekly/Opus limit". ``\S+`` is deliberate so
    # future limit names ("5-hour") match. Byte-identical to the pre-#2220
    # pattern.
    UsageLimitPattern(
        name="hit_your",
        source=(
            "errors.md 'You've hit your session/weekly/Opus limit'; "
            'claude binary 2.1.278 / 2.1.310 "You\'ve hit your "'
        ),
        pattern=r"hit (?:your )?\S+ limit",
    ),
    # The "Usage limit reached" title and banner ("Usage limit reached again",
    # "Usage limit reached · continuing automatically ..."). Case-sensitive
    # and skipped when quoted or backticked, so prose citing the banner does not
    # match. Deliberately not line-start anchored: that would miss
    # "Error: Usage limit reached" and ANSI-wrapped stderr.
    UsageLimitPattern(
        name="usage_limit_reached",
        source=(
            "claude binary 2.1.278 / 2.1.310 'Usage limit reached · "
            "continuing automatically {when} · esc to cancel'"
        ),
        pattern=r"(?<![\"'`])(?-i:Usage limit reached)\b(?![\"'`])",
    ),
    # Workflow failureNote: "usage limit; resets <time>, too far out to wait
    # for" / ", and this run already waited for N resets".
    UsageLimitPattern(
        name="usage_limit_semicolon_resets",
        source=(
            "claude binary 2.1.278 / 2.1.310 workflow failureNote "
            "'usage limit; resets ${time}, too far out to wait for'"
        ),
        pattern=r"\busage limit(?=;\s{1,3}resets\s{1,4}" + _RESET_WHEN + ")",
    ),
    # "Usage limit reached · limit resets <time>" (the helper that follows the
    # banner). Needs BOTH a middot (U+00B7) / en dash (U+2013) / em dash (U+2014)
    # opener directly before "limit" and a following clock time. ASCII "-" and
    # "|" openers are excluded on purpose so "rate-limit resets 3pm" and
    # table/list rows do not match.
    #
    # Deliberately NOT matched (#2220 operator disposition): the non-blocking
    # notices "Continuing now at lower priority until your limit resets at
    # <time>" and "Now using usage credits for X · Your X limit resets <time>".
    # The worker keeps running after them, and the transcript consumers
    # (reconcile, queue_peek) would otherwise arm backoff and a per-client
    # lockout for a healthy session. The "· Your <model> limit" form fails the
    # opener below because "Your <model>" sits between the middot and "limit".
    UsageLimitPattern(
        name="limit_resets_time",
        source=(
            "claude binary 2.1.278 / 2.1.310 helper "
            "'Usage limit reached · limit resets ${time}'"
        ),
        pattern=(
            r"[\u00b7\u2013\u2014]\s{0,3}limit"
            r"(?=\s{1,4}resets\s{1,4}" + _RESET_WHEN + ")"
        ),
    ),
)
