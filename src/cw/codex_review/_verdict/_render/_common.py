"""Escaping helpers shared by every verdict-comment section.

**Everything a model wrote is escaped on its way in** (#2210 round 4). A review
finding is not trusted input: it is text a model produced from the diff, and
the comment this package renders is read back on the NEXT round by the
disposition ledger's parser. A summary, file path, quoted evidence or contest
claim carrying a well-formed ``REVIEW-FINDING-DISPOSITIONS`` block would
otherwise mint a durable suppression no operator authored — the reviewer
silencing its own finding. Every interpolation of model-authored text in this
package therefore goes through :func:`~cw.review_markers.neutralise_marker_syntax`
(aliased ``_safe``), which escapes the sentinels and the HTML comment
delimiters visibly rather than dropping them. The settle payload is the one
exception and has its own, lossless treatment — see
:func:`~cw.codex_review._verdict._render._settle._settle_payload_block`. The
reader enforces the same contract independently, by position; see
``review_finding_dispositions.parse``' ``_DISPOSITION_BLOCK_RE``.
"""

from __future__ import annotations

from cw.review_markers import neutralise_marker_syntax

# Local alias for the neutraliser (#2210 round 4). This package interpolates
# model-authored text at roughly twenty points and every one of them must be
# wrapped; the full name does not fit inside the surrounding f-strings.
_safe = neutralise_marker_syntax


def _truncate(text: str, limit: int) -> str:
    """Whitespace-collapse *text* and cap it at *limit* characters (#2210).

    Shared by the places a comment line embeds model-authored free text — a
    contest claim, an over-budget settle row, a refused record's summary — so
    one verbose model cannot dominate the comment from any direction.

    Neutralisation happens AFTER the cut (#2210 round 4), so the result is
    always fully escaped: truncating an escaped token could otherwise leave a
    fragment, and the cut is the cheap operation to make approximate. *limit*
    is therefore a limit on the reviewer's own text, not on the rendered
    bytes, which an escape can push a few characters past.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return _safe(collapsed)
    return _safe(collapsed[:limit].rstrip()) + "…"
