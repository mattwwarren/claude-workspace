"""The wire grammar of the review comment markers, and how to make it inert.

Three records travel back into this pipeline as *text* inside a ticket
comment: the cross-round disposition ledger
(:data:`DISPOSITION_SENTINEL`, :mod:`cw.review_finding_dispositions`), the
voided-findings record (:data:`VOIDED_SENTINEL`,
:mod:`cw.review_adjudication`), and the ready-to-paste settle section
(:data:`SETTLE_SECTION_HEADING`, rendered by
``cw.codex_review._verdict._render`` and elided again by
``cw.codex_review._context.core``). Each of them is a token a parser keys on,
so each is spelled exactly once — here — rather than in the writer and again
in the reader.

**Why a leaf module (#2210 round 4).** Two modules needed one constant apiece
out of :mod:`cw.review_finding_dispositions` and were importing the whole
ledger implementation to get it: ``codex_review._context._prompt_text`` (static
prompt text, which must stay dependency-free) and ``review_findings._models``
(the executor-neutral finding contract, which must not depend on one
executor's ledger). Moving the shared vocabulary down here inverts nothing and
costs nothing — it is the same shape #1409's import cycle was fixed with.

**Import discipline — load-bearing, not style.** This module MUST NOT import
anything from ``cw``, at module scope or inside a function. It is the bottom of
the dependency order for everything that touches marker text, and
:mod:`cw.review_finding_dispositions` (which itself imports nothing else from
``cw`` at module scope, because :mod:`cw.models.tasks` imports *it*) imports
this one. ``tests/test_review_markers.py`` pins that by parsing this module's
AST.

**Neutralisation (#2210 round 4).** :func:`neutralise_marker_syntax` is the
other half of the grammar: every place the pipeline renders MODEL-GENERATED
text into a comment the next round will parse — a finding summary, a file
path, quoted evidence, reviewer prose — runs it through here first. A review
finding is not trusted input: a summary carrying a full, well-formed
disposition block would otherwise mint a suppression no operator authored.
Escaping is visible and lossless to a human reader (see the function), never a
silent strip.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

#: The disposition ledger's sentinel (#1838). Owned here because the parser in
#: :mod:`cw.review_finding_dispositions`, the reviewer prompt text, and the
#: blocking review comment must all agree on it byte for byte, and a second
#: spelling of a string a parser keys on is how a writer and a reader drift
#: apart in a later change.
DISPOSITION_SENTINEL = "REVIEW-FINDING-DISPOSITIONS"

#: The voided-findings record's sentinel (#1814), owned here for the same
#: reason and so :func:`neutralise_marker_syntax` has one list to work from
#: rather than a copy of this string. :mod:`cw.review_adjudication._voided`
#: imports it.
VOIDED_SENTINEL = "VOIDED-REVIEW-FINDINGS"

#: The heading the blocking review comment's ready-to-paste settle section
#: renders under (#2210). TWO modules must agree on it byte for byte:
#: ``codex_review._verdict._render`` emits it, and
#: ``codex_review._context.core`` builds its elision regex from it so a
#: pipeline-authored payload never re-enters the next reviewer's prompt as
#: evidence.
SETTLE_SECTION_HEADING = "### Settle a finding"

#: The HTML comment delimiters every marker above is carried inside. They are
#: the real choke point: without a literal ``<!--`` no sentinel block can
#: form, and ``cw.gh.AGENT_COMMENT_MARKER`` (``<!-- cw-agent-authored -->``,
#: the provenance marker the elision path keys on) cannot form either. Escaped
#: as tokens in their own right so an injected ``-->`` cannot break out of a
#: real block that encloses it.
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"

#: Each dangerous token mapped to the text rendered in its place. The escape is
#: a single backslash inserted inside the token, chosen over a zero-width
#: character or an HTML entity on purpose: it is plainly visible in the raw
#: comment body (which is what the next round's parser, and anyone running
#: ``gh issue view``, actually reads), while CommonMark renders ``\-``/``\>``
#: as ``-``/``>``, so the comment still displays the reviewer's text exactly as
#: written. Nothing is dropped and nothing is silently rewritten.
#:
#: Where two tokens overlap (``<!-->``) the backslash goes at the END of the
#: dangerous run rather than the start, so a replacement can never re-form its
#: sibling: escaping ``<!--`` to ``<\!--`` would leave ``<\!-->`` carrying a
#: literal ``-->``.
_MARKER_ESCAPES: dict[str, str] = {
    _COMMENT_OPEN: "<!-\\-",
    _COMMENT_CLOSE: "--\\>",
    DISPOSITION_SENTINEL: "REVIEW-FINDING\\-DISPOSITIONS",
    VOIDED_SENTINEL: "VOIDED-REVIEW\\-FINDINGS",
}

#: Longest-first so ``<!--`` wins over ``-->`` where they overlap (``<!-->``),
#: which keeps the scan single-pass: every match is taken from the ORIGINAL
#: text, so a replacement is never itself rescanned and nothing double-escapes.
_MARKER_RE = re.compile(
    "|".join(
        re.escape(token) for token in sorted(_MARKER_ESCAPES, key=len, reverse=True)
    )
)

#: JSON's own escapes for the comment delimiters' two characters. A settle
#: payload must carry the finding's summary VERBATIM — it is the ledger
#: identity, and the operator pastes it unedited — so the rendered payload
#: cannot use the visible escapes above. It escapes the angle brackets the way
#: JSON itself does instead: ``json.loads`` yields the identical string, while
#: the rendered text carries no literal ``<!--`` or ``-->``.
_JSON_ANGLE_ESCAPES = {"<": "\\u003c", ">": "\\u003e"}
_JSON_ANGLE_RE = re.compile("[<>]")


def neutralise_marker_syntax(text: str) -> str:
    """Render *text* so no marker parser can read a record out of it (#2210 R4).

    Apply this at every point the pipeline interpolates MODEL-GENERATED text
    into a comment body — finding summaries, file paths, quoted evidence,
    consequences, contest claims, reviewer prose. Review text is produced by a
    model reading the diff, so it is untrusted input to the next round's
    reader: an unescaped summary carrying a well-formed
    ``REVIEW-FINDING-DISPOSITIONS`` block would mint a durable suppression
    nobody authored.

    Escaping, never stripping: each token is replaced by a visibly backslashed
    form that still reads as the original, so the comment reports what the
    reviewer actually said. Idempotent in effect but not by construction — an
    already-escaped string contains none of the tokens, so a second pass is a
    no-op.

    This is one layer of two. The reader's positional parse
    (``cw.review_finding_dispositions``) independently refuses a sentinel that
    is not at the structural position the writer emits, so an injection fails
    at parse even if some future renderer forgets to call this.
    """
    return _MARKER_RE.sub(lambda match: _MARKER_ESCAPES[match.group(0)], text)


def escape_angle_brackets_in_json(rendered: str) -> str:
    """Make a rendered JSON document carry no literal ``<!--``/``-->``.

    For the one renderer that cannot use :func:`neutralise_marker_syntax`: the
    ``### Settle a finding`` payload, whose ``file`` and ``summary`` ARE the
    ledger key and must survive ``json.loads`` byte-identically so the operator
    can paste the block unedited.

    *rendered* must be the output of :func:`json.dumps`. ``<`` and ``>`` can
    only occur inside JSON string literals — every structural character is one
    of ``{}[],:`` or whitespace — so replacing them with their ``\\uXXXX``
    escapes is semantically invisible and textually total.
    """
    return _JSON_ANGLE_RE.sub(
        lambda match: _JSON_ANGLE_ESCAPES[match.group(0)], rendered
    )


class RefusedDisposition(BaseModel):
    """One ledger record the reader refused to apply, and why (#2210 round 2).

    Carried on ``ReviewVerdict.refused_dispositions`` and rendered onto the
    posted review comment, because "ignored" must not mean "invisible": an
    operator has to be able to see that something tried to suppress a finding
    and was refused. ``key`` is the ledger key
    (``file::normalized summary::digest`` — or the digest-less legacy shape,
    which is itself one of the things refused), ``missing`` the provenance
    fields it could not produce, in the fixed order
    ``cw.review_finding_dispositions._provenance_gaps`` checks them.

    Lives here rather than in :mod:`cw.review_findings` because that package is
    the EXECUTOR-NEUTRAL finding contract and must not depend on one executor's
    ledger implementation (#2210 round 4); and not in
    :mod:`cw.review_finding_dispositions` for the same reason — importing the
    ledger to name its refusal type is what inverted that dependency.
    """

    key: str
    missing: list[str] = Field(default_factory=list)


class StaleDisposition(BaseModel):
    """One ledger record whose code moved since it was settled (#2232).

    The drift twin of :class:`RefusedDisposition`, and carried the same way:
    on ``ReviewVerdict.stale_dispositions``, rendered onto the posted review
    comment. A refusal says "this record was never applicable"; this says "it
    was, and the code underneath it has changed since". The ledger entry is
    NOT expired — ADR-0016 rejects silent expiry outright — it simply is not
    applied for this pass, and the finding keeps blocking until an operator
    re-settles it against the current code.

    ``key`` is the ledger key (``file::normalized summary::digest``),
    ``reviewed_sha`` the commit the record was settled against, and
    ``current_sha`` the commit this pass reviewed. Both shas ride along so a
    reader of the comment or the ``review.finding_disposition_stale`` event can
    run the diff themselves rather than take the pipeline's word for it.

    Lives here for the identical reason :class:`RefusedDisposition` does: this
    is the leaf module with no ``cw`` dependency, and ``cw.review_findings``
    must not depend on one executor's ledger implementation.
    """

    key: str
    reviewed_sha: str = ""
    current_sha: str = ""
