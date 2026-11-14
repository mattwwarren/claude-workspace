#!/usr/bin/env python3
# cw-script-version: 1
"""Gate script: verify quotes a plan attributes to the ticket (#1502).

Usage (from ``/auto-dev-plan``, Step 1f.2a, before each Plan Reviewer spawn):
    python .claude/scripts/check_ticket_quotes.py \\
        --plan "$SESSION_WT/.cw/ticket-quote-plan.md" \\
        --corpus "$SESSION_WT/.cw/ticket-quote-corpus.json" \\
        [--resolutions "$SESSION_WT/.cw/ticket-quote-resolutions.txt"]

Context: a plan quoted the ticket with words the ticket never contained and
nothing caught it, because the reviewer receives only the ticket description,
not the comment stream. This script is the deterministic fact source: it proves
each quoted span the plan *attributes to the ticket* occurs in the live ticket
corpus. It never fact-checks prose and never reads ``.cw/context.json``.

``--corpus`` is JSON ``{"title": str, "body": str, "comments": [{"body": str}]}``;
``description`` is accepted in place of ``body`` (the Linear shape). Extra keys
are ignored. Quote sources: the title, the body, every comment that passes the
provenance filter below, and the ``--resolutions`` text (binding resolutions,
including ``queue_metadata.operator_resolutions``).

Provenance filter (mirrored from ``cw``, which this script must not import; a
parity test pins each constant against its real home). A comment is dropped
when ANY of these holds:
    1. the agent marker occurs anywhere in the body (substring);
    2. the self-disclosure regex searches true anywhere in the body;
    3. ``body.lstrip().startswith(...)`` any of the four pipeline fixed headers
       (a comment that merely quotes a header mid-body is kept);
    4. a ``plan-spec-reviewed`` or ``plan-soundness-reviewed`` signoff marker
       prefix occurs anywhere in the body (substring).

Quote grammar (closed). Quote delimiters are double quotes or curly double
quotes only; single quotes are never delimiters (apostrophes). Fenced code,
HTML-comment lines and inline code spans are excluded (inline code is masked,
so a quote that merely *contains* a code span is still read in full). A span is
checked only when one of these triggers fires:
    field        a line starting (after a list marker or bold markers) with a
                 label such as ``Ticket quote:``, ``Operator quote:``,
                 ``AC3:``; every quoted span in the value is extracted.
    attribution  ``<attributor> <verb> ... "span"`` (up to 40 non-quote
                 characters after the verb) or ``per|according to|as <attributor>
                 "span"``. Attributors: the ticket/issue (+ body, description,
                 text, title, comment), the acceptance criteria/criterion,
                 ``AC<n>``, the operator/owner/reporter/requester (+ ``'s
                 comment|resolution|decision``), the (pre-flight) resolution,
                 ``R<n>``, ``comment <id>``. Verbs: says, states, reads,
                 specifies, requires, requests, asks, writes, notes, quotes,
                 mandates, demands (and their past/base forms).
    resolution-id ``[Decided:] R<n>|row:<n>|comment:<id> -|:|dash "span"``.
    blockquote   a run of ``>`` lines whose nearest preceding non-blank line
                 names an attributor; the whole block is one quote.
A span that misses its closing quote continues onto at most 3 following lines
of the same paragraph (a blank line, list marker or heading stops it).

Matching: each quote is split on ``...``, the ellipsis character and any
``[bracketed]`` text; each fragment of at least 3 words (``_MIN_QUOTE_WORDS``,
a documented under-detection chosen for conservatism) must be a substring of
one normalized corpus source. Normalization, both sides: NFKC, casefold, curly
quotes to ASCII, en/em dash to ``-``, drop ``*``, ``_``, backtick and
backslash, strip leading list/blockquote markers per line, collapse whitespace,
trim trailing ``.,;:`` from each fragment. Identical quote strings are checked
and reported once.

Limit: this proves a quote exists in the supplied corpus, not that the corpus
is complete or current; the caller writes it from the live fetch.

Exit codes:
    0  no unmatched quote (including a plan with no quotes at all)
    1  at least one unmatched quote
    2  usage or input error: missing option, unreadable or non-UTF-8 file,
       malformed JSON, wrong types, or an empty corpus. Does NOT block: same
       fail-open convention as ``check_premise_evidence.py``.

The JSON verdict is written to stdout on exits 0 and 1:
    {"quotes_checked":N,
     "unmatched":[{"quote":str,"fragment":str,"line":int,
                   "trigger":"field|attribution|resolution-id|blockquote"}]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_MIN_QUOTE_WORDS = 3
_MAX_CONTINUATION_LINES = 3
_MAX_ATTRIBUTION_GAP = 40
_TRAILING_PUNCTUATION = ".,;:"
_FENCE_PREFIXES = ("```", "~~~")
_FENCE_LENGTH = 3

TRIGGER_FIELD = "field"
TRIGGER_ATTRIBUTION = "attribution"
TRIGGER_RESOLUTION_ID = "resolution-id"
TRIGGER_BLOCKQUOTE = "blockquote"

# ---------------------------------------------------------------------------
# Provenance constants. MIRRORED, NOT SHARED: ``.claude/scripts`` must not
# import ``cw``. tests/test_check_ticket_quotes.py asserts each equals its home:
#   _AGENT_COMMENT_MARKER, _SELF_DISCLOSURE_RE, _PLAN_SPEC_MARKER  cw.gh.issues
#   _FIXED_HEADERS                                       cw.codex_fix_loop.constraints
#   _PLAN_SOUNDNESS_MARKER                               cw.dev_queue.lifecycle
# ---------------------------------------------------------------------------

_AGENT_COMMENT_MARKER = "<!-- cw-agent-authored -->"
_AGENT_NOUN = r"(?:(?:an?|the)\s+)?(?:orchestrator|agent|assistant|claude|ai)\b"
_OPERATOR_S = "operator(?:['\u2019]s)?"
_SELF_DISCLOSURE_RE = re.compile(
    "|".join(
        (
            rf"\b(?:authored|written|drafted|posted|relayed)\s+by\s+{_AGENT_NOUN}",
            r"\brelayed\s+on\s+behalf\s+of\b",
            r"\bon\s+behalf\s+of\s+the\s+operator\b",
            rf"\bposted\s+under\s+the\s+{_OPERATOR_S}\s+(?:own\s+)?"
            r"(?:account|identity|login|credentials)\b",
            rf"\brelaying\s+the\s+{_OPERATOR_S}\s+"
            r"(?:words|text|instructions|dictation|directives?)\b",
        )
    ),
    re.IGNORECASE,
)
_FIXED_HEADERS = (
    "## Multi-Marker Gate Blocked",
    "## Pending Verification Scan",
    "## Blocking Review Findings",
    "## Operator-Actionable Review Findings",
)
_PLAN_SPEC_MARKER = "<!-- plan-spec-reviewed"
_PLAN_SOUNDNESS_MARKER = "<!-- plan-soundness-reviewed"

# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

_OPEN_RE = re.compile('["\u201c]')
_CLOSE_RE = re.compile('["\u201d]')
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_PARAGRAPH_BREAK_RE = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s|#)")
_BLOCKQUOTE_MARK_RE = re.compile(r"^\s*(?:>\s*)+")
_QUOTE_LINE_RE = re.compile(r"^\s*>")

_FIELD_LABEL = (
    r"ticket\s+(?:quote|evidence|text|excerpt|comment)"
    r"|quote\s+from\s+(?:the\s+)?ticket"
    r"|operator\s+quote|resolution\s+quote"
    r"|AC\s?\d+|acceptance\s+criterion\s+\d+"
)
_FIELD_RE = re.compile(
    rf"^\s*(?:(?:[-*+]|\d+[.)])\s+)?[*_]*(?:{_FIELD_LABEL})[*_]*\s*:",
    re.IGNORECASE,
)
_ID_RE = re.compile(
    r"^\W*(?:Decided:[\s*_]*)?(?:R\d+|row:\d+|comment:[^\s\"\u201c\u201d\u2014\u2013]+)"
    '\\s*[\u2014\u2013:-]\\s*(?=["\u201c])',
    re.IGNORECASE,
)


def _attributor(article: str) -> str:
    """The closed attributor noun set, with the article required or optional."""
    return (
        rf"\b(?:{article}(?:ticket|issue)(?:\s+(?:body|description|text|title|comment))?"
        rf"|{article}acceptance\s+criteri(?:a|on)"
        r"|AC\s?\d+"
        rf"|{article}(?:operator|owner|reporter|requester)"
        r"(?:['\u2019]s\s+(?:comment|resolution|decision))?"
        rf"|{article}(?:pre-flight\s+)?resolution"
        r"|R\d+"
        r"|comment\s+[\w:.-]+)"
    )


_ARTICLE_REQUIRED = r"(?:the|an?)\s+"
_ARTICLE_OPTIONAL = r"(?:(?:the|an?)\s+)?"
_VERB = (
    r"(?:says?|said|states?|stated|reads?|specif(?:y|ies|ied)|requires?|required"
    r"|requests?|requested|asks?|asked|writes?|wrote|notes?|noted|quotes?|quoted"
    r"|mandates?|demands?)"
)
_ATTRIBUTION_VERB_RE = re.compile(
    rf"{_attributor(_ARTICLE_REQUIRED)}\s+{_VERB}\b"
    f'[^"\u201c\u201d]{{0,{_MAX_ATTRIBUTION_GAP}}}["\u201c]',
    re.IGNORECASE,
)
_ATTRIBUTION_PREFIX_RE = re.compile(
    rf"\b(?:per|according\s+to|as)\s+{_attributor(_ARTICLE_REQUIRED)}"
    '\\s*[:,\u2014\u2013-]?\\s*["\u201c]',
    re.IGNORECASE,
)
_LEAD_IN_RE = re.compile(rf"{_attributor(_ARTICLE_OPTIONAL)}\b", re.IGNORECASE)

_ELISION_RE = re.compile(r"\[[^\]]*\]|\.\.\.|\u2026")
_LINE_MARKER_RE = re.compile(r"^\s*(?:>\s*)*(?:(?:[-*+]|\d+[.)])\s+)?")
_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "*": None,
        "_": None,
        "`": None,
        "\\": None,
    }
)


class CorpusError(Exception):
    """The corpus JSON has the wrong shape or no text at all."""


class _InputError(Exception):
    """An input file could not be read."""


@dataclass(frozen=True)
class Span:
    """One quoted span the plan attributes to the ticket."""

    quote: str
    line: int
    trigger: str
    col: int = 0


@dataclass(frozen=True)
class _Doc:
    raw: list[str]
    masked: list[str]


# ---------------------------------------------------------------------------
# Plan pre-processing
# ---------------------------------------------------------------------------


def _dead_flags(lines: list[str]) -> list[bool]:
    """Per line: inside a fenced block or an HTML comment (or a fence/comment line)."""
    flags: list[bool] = []
    fence: str | None = None
    in_comment = False
    for line in lines:
        stripped = line.lstrip()
        if fence is not None:
            flags.append(True)
            if stripped.startswith(fence):
                fence = None
        elif in_comment:
            flags.append(True)
            in_comment = "-->" not in line
        elif stripped.startswith(_FENCE_PREFIXES):
            flags.append(True)
            fence = stripped[:_FENCE_LENGTH]
        elif stripped.startswith("<!--"):
            flags.append(True)
            in_comment = "-->" not in stripped
        else:
            flags.append(False)
    return flags


def _mask_code(line: str) -> str:
    """Blank inline code spans in place, so offsets into the raw line still hold."""
    return _INLINE_CODE_RE.sub(lambda match: " " * len(match.group()), line)


def _prepare(plan_text: str) -> _Doc:
    lines = plan_text.splitlines()
    raw = [
        "" if dead else line
        for line, dead in zip(lines, _dead_flags(lines), strict=True)
    ]
    return _Doc(raw=raw, masked=[_mask_code(line) for line in raw])


# ---------------------------------------------------------------------------
# Span extraction
# ---------------------------------------------------------------------------


def _ends_paragraph(line: str) -> bool:
    return not line.strip() or _PARAGRAPH_BREAK_RE.match(line) is not None


def _join_parts(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return " ".join(part.strip() for part in parts)


def _find_span(doc: _Doc, idx: int, col: int) -> tuple[str, int, int] | None:
    """The span opened at ``doc.masked[idx][col]``: (text, end line, end column)."""
    parts: list[str] = []
    start = col + 1
    for offset in range(_MAX_CONTINUATION_LINES + 1):
        line_idx = idx + offset
        if offset and (
            line_idx >= len(doc.masked) or _ends_paragraph(doc.masked[line_idx])
        ):
            return None
        close = _CLOSE_RE.search(doc.masked[line_idx], start)
        if close is not None:
            parts.append(doc.raw[line_idx][start : close.start()])
            return _join_parts(parts), line_idx, close.start()
        parts.append(doc.raw[line_idx][start:])
        start = 0
    return None


def _spans_from(
    doc: _Doc,
    idx: int,
    col: int,
    trigger: str,
    *,
    first_only: bool = False,
) -> list[Span]:
    """Spans opened at or after ``col`` on line ``idx``."""
    spans: list[Span] = []
    pos = col
    while True:
        opener = _OPEN_RE.search(doc.masked[idx], pos)
        if opener is None:
            break
        found = _find_span(doc, idx, opener.start())
        if found is None:
            break
        text, end_idx, end_col = found
        spans.append(Span(text, idx + 1, trigger, opener.start()))
        if first_only or end_idx != idx:
            break
        pos = end_col + 1
    return spans


def _field_spans(doc: _Doc) -> list[Span]:
    spans: list[Span] = []
    for idx, line in enumerate(doc.masked):
        match = _FIELD_RE.match(line)
        if match is not None:
            spans.extend(_spans_from(doc, idx, match.end(), TRIGGER_FIELD))
    return spans


def _identifier_spans(doc: _Doc) -> list[Span]:
    spans: list[Span] = []
    for idx, line in enumerate(doc.masked):
        match = _ID_RE.match(line)
        if match is not None:
            spans.extend(
                _spans_from(
                    doc, idx, match.end(), TRIGGER_RESOLUTION_ID, first_only=True
                )
            )
    return spans


def _attribution_spans(doc: _Doc) -> list[Span]:
    spans: list[Span] = []
    for idx, line in enumerate(doc.masked):
        for pattern in (_ATTRIBUTION_VERB_RE, _ATTRIBUTION_PREFIX_RE):
            for match in pattern.finditer(line):
                spans.extend(
                    _spans_from(
                        doc,
                        idx,
                        match.end() - 1,
                        TRIGGER_ATTRIBUTION,
                        first_only=True,
                    )
                )
    return spans


def _has_lead_in(masked: list[str], idx: int) -> bool:
    """True when the nearest preceding non-blank line names an attributor."""
    prev = idx - 1
    while prev >= 0 and not masked[prev].strip():
        prev -= 1
    if prev < 0 or _QUOTE_LINE_RE.match(masked[prev]):
        return False
    return _LEAD_IN_RE.search(masked[prev]) is not None


def _blockquote_spans(doc: _Doc) -> list[Span]:
    spans: list[Span] = []
    idx = 0
    total = len(doc.masked)
    while idx < total:
        if not _QUOTE_LINE_RE.match(doc.masked[idx]):
            idx += 1
            continue
        end = idx
        while end < total and _QUOTE_LINE_RE.match(doc.masked[end]):
            end += 1
        if _has_lead_in(doc.masked, idx):
            pieces = (
                _BLOCKQUOTE_MARK_RE.sub("", doc.raw[i]).strip() for i in range(idx, end)
            )
            text = " ".join(piece for piece in pieces if piece)
            if text:
                spans.append(Span(text, idx + 1, TRIGGER_BLOCKQUOTE))
        idx = end
    return spans


def extract_spans(plan_text: str) -> list[Span]:
    """Every attributed quoted span in plan order, identical quotes once."""
    doc = _prepare(plan_text)
    spans = [
        *_field_spans(doc),
        *_identifier_spans(doc),
        *_attribution_spans(doc),
        *_blockquote_spans(doc),
    ]
    unique: dict[str, Span] = {}
    for span in sorted(spans, key=lambda item: (item.line, item.col)):
        unique.setdefault(span.quote, span)
    return list(unique.values())


# ---------------------------------------------------------------------------
# Normalization and matching
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    lines = [_LINE_MARKER_RE.sub("", line) for line in text.splitlines()]
    folded = unicodedata.normalize("NFKC", " ".join(lines)).casefold()
    return " ".join(folded.translate(_TRANSLATION).split())


def _fragments(quote: str) -> list[tuple[str, str]]:
    """(raw, normalized) for each elision fragment of at least 3 words."""
    fragments: list[tuple[str, str]] = []
    for raw in _ELISION_RE.split(quote):
        normalized = _normalize(raw).rstrip(_TRAILING_PUNCTUATION).strip()
        if len(normalized.split()) >= _MIN_QUOTE_WORDS:
            fragments.append((raw.strip(), normalized))
    return fragments


def evaluate(plan_text: str, pieces: list[str]) -> dict[str, object]:
    """The verdict for a plan against normalized corpus ``pieces``."""
    unmatched: list[dict[str, object]] = []
    checked = 0
    for span in extract_spans(plan_text):
        fragments = _fragments(span.quote)
        if not fragments:
            continue
        checked += 1
        missing = next(
            (
                raw
                for raw, normalized in fragments
                if not any(normalized in piece for piece in pieces)
            ),
            None,
        )
        if missing is not None:
            unmatched.append(
                {
                    "quote": span.quote,
                    "fragment": missing,
                    "line": span.line,
                    "trigger": span.trigger,
                }
            )
    return {"quotes_checked": checked, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def _provenance_ok(body: str) -> bool:
    """False for a comment the pipeline or an agent wrote (not a quote source)."""
    if _AGENT_COMMENT_MARKER in body:
        return False
    if _SELF_DISCLOSURE_RE.search(body) is not None:
        return False
    if body.lstrip().startswith(_FIXED_HEADERS):
        return False
    return not (_PLAN_SPEC_MARKER in body or _PLAN_SOUNDNESS_MARKER in body)


def _optional_text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    message = f"corpus field {key!s} must be a string"
    raise CorpusError(message)


def _comment_bodies(data: dict[str, object]) -> list[str]:
    comments = data.get("comments")
    if comments is None:
        return []
    if not isinstance(comments, list):
        message = "corpus field comments must be a list"
        raise CorpusError(message)
    bodies: list[str] = []
    for comment in comments:
        if not isinstance(comment, dict):
            message = "each corpus comment must be an object"
            raise CorpusError(message)
        body = comment.get("body")
        if body is None:
            continue
        if not isinstance(body, str):
            message = "each corpus comment body must be a string"
            raise CorpusError(message)
        bodies.append(body)
    return bodies


def build_corpus(data: object, resolutions: str = "") -> list[str]:
    """Normalized quote sources: title, body, provenance-clean comments, resolutions."""
    if not isinstance(data, dict):
        message = "corpus must be a JSON object"
        raise CorpusError(message)
    texts = [_optional_text(data, key) for key in ("title", "body", "description")]
    bodies = _comment_bodies(data)
    if not any(text.strip() for text in [*texts, *bodies]):
        message = "corpus has no text"
        raise CorpusError(message)
    kept = [*texts, *(body for body in bodies if _provenance_ok(body)), resolutions]
    return [piece for piece in map(_normalize, kept) if piece]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        message = f"cannot read {path}"
        raise _InputError(message) from None


def _load_pieces(args: argparse.Namespace) -> list[str]:
    try:
        data = json.loads(_read_text(args.corpus))
    except ValueError:
        message = f"corpus {args.corpus} is not valid JSON"
        raise _InputError(message) from None
    resolutions = _read_text(args.resolutions) if args.resolutions else ""
    return build_corpus(data, resolutions)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_ticket_quotes.py",
        description="Verify quotes a plan attributes to the ticket (#1502).",
    )
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--resolutions", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        plan_text = _read_text(args.plan)
        pieces = _load_pieces(args)
    except (_InputError, CorpusError) as exc:
        print(f"check_ticket_quotes: {exc}", file=sys.stderr)
        return 2
    verdict = evaluate(plan_text, pieces)
    print(json.dumps(verdict, separators=(",", ":")))
    return 1 if verdict["unmatched"] else 0


if __name__ == "__main__":
    sys.exit(main())
