#!/usr/bin/env python3
# cw-script-version: 1
"""Gate script: validate premise-evidence stamps and dispatch freshness (#2343).

Usage (from ``/auto-dev-plan``, Step 1c item 1 and Step 4a):
    python .claude/scripts/check_premise_evidence.py scan-block \\
        --file "$SESSION_WT/.cw/pm-premises-block.txt" \\
        --context .claude/cw-context.json
    python .claude/scripts/check_premise_evidence.py draft \\
        --plan "$SESSION_WT/.cw/pm-working-plan.md" \\
        --context .claude/cw-context.json

Context: a stale premise carried forward in ``## Self-Verified Premises`` was
re-stamped "verified" without a fresh check, and a failed re-verification did
not downgrade it. The Product Manager Reviewer now stamps every ``Verified:
YES`` premise with ``Observed-at`` (``YYYY-MM-DDTHH:MM:SSZ``, UTC), ``Kind``
(``STATIC`` or ``LIVE``) and ``Tool-call`` (``<ToolName>: <input>``). This script
checks the *shape* of those stamps and the dispatch time window.

A "round" is one dispatch. A check is "this round" iff ``Observed-at`` is at or
after ``stage_started_at`` (floored to the second) in the context file and not
later than now plus ``_CLOCK_SKEW_SECONDS``. No counter is involved.

Subcommands:
    scan-block  A reviewer ``PREMISES TO VERIFY`` block. Every item whose
                ``Verified:`` leading token is ``YES`` gets BOUND or UNBOUND.
    draft       The ``## Self-Verified Premises`` section of a working plan.
                Every entry gets FRESH, CARRIED or STALE. An entry matching none
                of the entry forms is CARRIED with ``unparseable-legacy``: it is
                reported, never guessed at.

Entry forms parsed by ``draft`` (first match wins, in this order):
    (a) the multi-line ``- Claim:`` block (legacy; claim = the text after
        ``Claim:``, evidence = the ``Evidence:`` line, depends-on = the
        ``Plan depends on this for:`` or ``Plan depends on it for:`` line).
        A first line beginning ``- Claim:`` that fails (a) is never re-read as
        (b) or (c), so a label can never end up inside a claim.
    (b) the single-line ``<claim> -- Evidence: ... -- Depends on: ... -- Route:
        ... -- Observed-at: ... -- Kind: ... -- Tool-call: ...`` form, parsed on
        the closed key set so a literal em-dash inside a claim or citation is
        harmless.
    (c) the older one-line ``<claim> -- ...`` prose (claim = text before the
        first em-dash; the remainder is both evidence and depends-on text).
Legacy Route inference (entries with none of Route, Observed-at, Kind or
Tool-call): IMPACT if the evidence begins ``Impact-Reason`` plus an em-dash or
colon, RESOLUTION if it matches ``R<n>``/``row:<n>`` plus an em-dash and a quote,
else VERIFIED (treated as LIVE-stale). IMPACT and RESOLUTION entries are always
CARRIED.

Limit: this proves the shape and freshness of the stamp, not that the quoted
call actually ran or answers the claim. ``Tool-call`` is required and checked
for shape but is never matched against anything, and no session log is read.

Exit codes:
    0  every item is BOUND, FRESH or CARRIED
    1  any item is UNBOUND or STALE
    2  usage or parse error (missing option, unreadable ``--file``/``--plan``).
       Does NOT block: same fail-open convention as
       ``check_plan_scope_conformance.py``.

A missing or malformed ``--context`` file, or an absent ``stage_started_at``, is
a *verdict*, not exit 2: every evaluated item gets ``context-unavailable``.

Structural entry errors use the named reason codes ``route-missing-or-invalid``
and ``duplicate-key`` in addition to the freshness reason codes above.

The JSON verdict is written to stdout on exits 0 and 1:
    {"items":[{"claim":str,"verdict":"BOUND|UNBOUND|FRESH|CARRIED|STALE",
               "reasons":[<code>...]}]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

_CLOCK_SKEW_SECONDS = 120
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_EM = "—"
_SEP = f" {_EM} "
_SEP_EVIDENCE = f"{_SEP}Evidence: "
_KEY_SPLIT_RE = re.compile(
    f" {_EM} (Evidence|Depends on|Route|Observed-at|Kind|Tool-call|Observed-attempt): "
)
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_TOOL_CALL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*:\s*\S.*")
_ATTEMPT_RE = re.compile(r"[0-9]+")
_KINDS = ("STATIC", "LIVE")

_BLOCK_ITEM_RE = re.compile(r"^[0-9]+\.\s+(\S.*?)\s*$")
_BLOCK_SUB_RE = re.compile(r"^\s+-\s+([A-Za-z][A-Za-z -]*):\s*(.*?)\s*$")
_TRACKED_KEYS = frozenset(
    {"verified", "citation", "observed-at", "kind", "tool-call", "observed-attempt"}
)

_SECTION_HEADING = "## Self-Verified Premises"
_CLAIM_LABEL_RE = re.compile(r"^- Claim:\s*(.*?)\s*$")
_EVIDENCE_LINE_RE = re.compile(r"^\s+Evidence:\s*(.*?)\s*$")
_DEPENDS_LINE_RE = re.compile(r"^\s+Plan depends on (?:this|it) for:\s*(.*?)\s*$")
_IMPACT_RE = re.compile(rf"^Impact-Reason(?: {_EM}|:)")
_RESOLUTION_RE = re.compile(rf"^(?:R[0-9]+|row:[0-9]+) {_EM} .*[\"“”]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_RELABEL_ACTION_RE = re.compile(
    r"\b(?:re-?ran|re-?verified|independently)\b", re.IGNORECASE
)
_RELABEL_SCOPE_RE = re.compile(r"\b(?:this round|dispatch|session)\b", re.IGNORECASE)

_CARRIED_ROUTES = ("IMPACT", "RESOLUTION")
_ROUTES = ("VERIFIED", "IMPACT", "RESOLUTION")
_REASON_ROUTE_INVALID = "route-missing-or-invalid"
_REASON_DUPLICATE_KEY = "duplicate-key"
_REASON_OBSERVED_AT_MISSING = "observed-at-missing"
_REASON_OBSERVED_AT_MALFORMED = "observed-at-malformed"
_REASON_OBSERVED_BEFORE_WINDOW = "observed-before-window"
_REASON_OBSERVED_IN_FUTURE = "observed-in-future"
_REASON_KIND_INVALID = "kind-missing-or-invalid"
_REASON_TOOL_CALL_MISSING = "tool-call-missing"
_REASON_TOOL_CALL_MALFORMED = "tool-call-malformed"
_REASON_OBSERVED_ATTEMPT_INVALID = "observed-attempt-invalid"
_REASON_CONTEXT_UNAVAILABLE = "context-unavailable"
_REASON_LEGACY_NO_METADATA = "legacy-no-metadata"
_REASON_RELABELED_WITHOUT_FRESH_CHECK = "relabeled-without-fresh-check"
_REASON_UNPARSEABLE_LEGACY = "unparseable-legacy"
_REASON_CODES = frozenset(
    {
        _REASON_ROUTE_INVALID,
        _REASON_DUPLICATE_KEY,
        _REASON_OBSERVED_AT_MISSING,
        _REASON_OBSERVED_AT_MALFORMED,
        _REASON_OBSERVED_BEFORE_WINDOW,
        _REASON_OBSERVED_IN_FUTURE,
        _REASON_KIND_INVALID,
        _REASON_TOOL_CALL_MISSING,
        _REASON_TOOL_CALL_MALFORMED,
        _REASON_OBSERVED_ATTEMPT_INVALID,
        _REASON_CONTEXT_UNAVAILABLE,
        _REASON_LEGACY_NO_METADATA,
        _REASON_RELABELED_WITHOUT_FRESH_CHECK,
        _REASON_UNPARSEABLE_LEGACY,
    }
)

VERDICT_BOUND = "BOUND"
VERDICT_UNBOUND = "UNBOUND"
VERDICT_FRESH = "FRESH"
VERDICT_CARRIED = "CARRIED"
VERDICT_STALE = "STALE"
_VERDICTS = frozenset(
    {VERDICT_BOUND, VERDICT_UNBOUND, VERDICT_FRESH, VERDICT_CARRIED, VERDICT_STALE}
)
_FAILED_VERDICTS = _VERDICTS.intersection({VERDICT_UNBOUND, VERDICT_STALE})


@dataclass
class _BlockItem:
    claim: str
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class _Entry:
    form: str  # "a" | "b" | "c" | "unparseable"
    claim: str
    evidence: str = ""
    depends_on: str = ""
    route: str | None = None
    observed_at: str | None = None
    kind: str | None = None
    tool_call: str | None = None
    observed_attempt: str | None = None
    text: str = ""
    structural_reasons: tuple[str, ...] = ()

    def has_new_form_keys(self) -> bool:
        return any(
            value is not None
            for value in (self.route, self.observed_at, self.kind, self.tool_call)
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Context and timestamps
# ---------------------------------------------------------------------------


def _parse_context_timestamp(raw: str) -> datetime | None:
    """Lenient ``stage_started_at`` parse: naive, microseconds, ``+00:00``, ``Z``."""
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _load_window_start(path: Path) -> datetime | None:
    """The dispatch window start, or None when the context is unusable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("stage_started_at")
    if not isinstance(raw, str):
        return None
    parsed = _parse_context_timestamp(raw)
    if parsed is None:
        return None
    return parsed.replace(microsecond=0)


def _parse_observed_at(value: str) -> datetime | None:
    if not _TIMESTAMP_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Stamp validation (shared by scan-block and draft)
# ---------------------------------------------------------------------------


def _observed_at_reasons(
    value: str | None, window_start: datetime | None, now: datetime
) -> list[str]:
    if not value:
        return [_REASON_OBSERVED_AT_MISSING]
    observed = _parse_observed_at(value)
    if observed is None:
        return [_REASON_OBSERVED_AT_MALFORMED]
    if observed > now + timedelta(seconds=_CLOCK_SKEW_SECONDS):
        return [_REASON_OBSERVED_IN_FUTURE]
    if window_start is not None and observed < window_start:
        return [_REASON_OBSERVED_BEFORE_WINDOW]
    return []


def _tool_call_reasons(value: str | None) -> list[str]:
    if not value:
        return [_REASON_TOOL_CALL_MISSING]
    if not _TOOL_CALL_RE.fullmatch(value):
        return [_REASON_TOOL_CALL_MALFORMED]
    return []


def _freshness_reasons(
    stamp: tuple[str | None, str | None, str | None, str | None],
    window_start: datetime | None,
    now: datetime,
) -> list[str]:
    """Reason codes for one stamp: (observed_at, kind, tool_call, observed_attempt)."""
    observed_at, kind, tool_call, observed_attempt = stamp
    reasons = _observed_at_reasons(observed_at, window_start, now)
    if kind not in _KINDS:
        reasons.append(_REASON_KIND_INVALID)
    reasons.extend(_tool_call_reasons(tool_call))
    if observed_attempt is not None and not _ATTEMPT_RE.fullmatch(observed_attempt):
        reasons.append(_REASON_OBSERVED_ATTEMPT_INVALID)
    if window_start is None:
        reasons.append(_REASON_CONTEXT_UNAVAILABLE)
    return reasons


# ---------------------------------------------------------------------------
# scan-block
# ---------------------------------------------------------------------------


def _parse_block_items(text: str) -> list[_BlockItem]:
    """Numbered items with ``   - Key: value`` sub-bullets (keys lowercased)."""
    items: list[_BlockItem] = []
    last_key: str | None = None
    for line in text.splitlines():
        item_match = _BLOCK_ITEM_RE.match(line)
        if item_match:
            items.append(_BlockItem(claim=item_match.group(1)))
            last_key = None
            continue
        if not items:
            continue
        sub_match = _BLOCK_SUB_RE.match(line)
        if sub_match:
            key = sub_match.group(1).strip().lower()
            last_key = key if key in _TRACKED_KEYS else None
            if last_key is not None:
                items[-1].fields.setdefault(last_key, sub_match.group(2))
            continue
        if line.strip() and last_key is not None:
            current = items[-1].fields[last_key]
            items[-1].fields[last_key] = f"{current} {line.strip()}".strip()
    return items


def _is_yes(value: str | None) -> bool:
    if not value:
        return False
    return value.split(maxsplit=1)[0] == "YES"


def evaluate_scan_block(
    text: str, window_start: datetime | None
) -> list[dict[str, object]]:
    """BOUND/UNBOUND verdicts for every ``Verified: YES`` item in a PM block."""
    now = _utcnow()
    results: list[dict[str, object]] = []
    for item in _parse_block_items(text):
        if not _is_yes(item.fields.get("verified")):
            continue
        stamp = (
            item.fields.get("observed-at"),
            item.fields.get("kind"),
            item.fields.get("tool-call"),
            item.fields.get("observed-attempt"),
        )
        reasons = _freshness_reasons(stamp, window_start, now)
        results.append(
            {
                "claim": item.claim,
                "verdict": VERDICT_UNBOUND if reasons else VERDICT_BOUND,
                "reasons": reasons,
            }
        )
    return results


# ---------------------------------------------------------------------------
# draft: entry splitting and parsing
# ---------------------------------------------------------------------------


def _split_entries(text: str) -> list[list[str]]:
    """Entries of the Self-Verified section: column-0 ``- `` starts, rest continues."""
    entries: list[list[str]] = []
    in_section = False
    for line in text.splitlines():
        if line.strip() == _SECTION_HEADING:
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("## "):
            break
        if line.startswith("- "):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
    for entry in entries:
        while entry and not entry[-1].strip():
            entry.pop()
    return entries


def _parse_form_a(lines: list[str]) -> _Entry | None:
    label = _CLAIM_LABEL_RE.match(lines[0])
    if label is None or not label.group(1):
        return None
    evidence: str | None = None
    depends = ""
    for line in lines[1:]:
        evidence_match = _EVIDENCE_LINE_RE.match(line)
        if evidence_match and evidence is None:
            evidence = evidence_match.group(1)
            continue
        depends_match = _DEPENDS_LINE_RE.match(line)
        if depends_match and not depends:
            depends = depends_match.group(1)
    if evidence is None:
        return None
    return _Entry(
        form="a",
        claim=label.group(1),
        evidence=evidence,
        depends_on=depends,
        text=" ".join(line.strip() for line in lines),
    )


def _parse_form_b(text: str) -> _Entry | None:
    idx = text.find(_SEP_EVIDENCE)
    if idx < 0:
        return None
    claim = text[:idx].strip()
    if not claim:
        return None
    parts = _KEY_SPLIT_RE.split(text[idx:])
    pairs: dict[str, str] = {}
    duplicate_keys: set[str] = set()
    for key, value in zip(parts[1::2], parts[2::2], strict=True):
        if key in pairs:
            duplicate_keys.add(key)
        else:
            pairs[key] = value.strip()
    return _Entry(
        form="b",
        claim=claim,
        evidence=pairs.get("Evidence", ""),
        depends_on=pairs.get("Depends on", ""),
        route=pairs.get("Route"),
        observed_at=pairs.get("Observed-at"),
        kind=pairs.get("Kind"),
        tool_call=pairs.get("Tool-call"),
        observed_attempt=pairs.get("Observed-attempt"),
        text=text,
        structural_reasons=(_REASON_DUPLICATE_KEY,) if duplicate_keys else (),
    )


def _parse_form_c(text: str) -> _Entry | None:
    claim, sep, rest = text.partition(_SEP)
    if not sep or not claim.strip():
        return None
    remainder = rest.strip()
    return _Entry(
        form="c",
        claim=claim.strip(),
        evidence=remainder,
        depends_on=remainder,
        text=text,
    )


def _parse_entry(lines: list[str]) -> _Entry:
    """Parse one entry by the first matching form (a, b, c), else unparseable."""
    first = lines[0][2:].strip()
    text = " ".join([first, *(line.strip() for line in lines[1:])]).strip()
    if first.startswith(_EM):
        return _Entry(form="unparseable", claim=first, text=text)
    if _CLAIM_LABEL_RE.match(lines[0]):
        parsed = _parse_form_a(lines)
    else:
        parsed = _parse_form_b(text)
        if parsed is None and _SEP_EVIDENCE not in text:
            parsed = _parse_form_c(text)
    if parsed is not None:
        return parsed
    return _Entry(form="unparseable", claim=first, text=text)


# ---------------------------------------------------------------------------
# draft: verdicts
# ---------------------------------------------------------------------------


def _infer_legacy_route(evidence: str) -> str:
    if _IMPACT_RE.match(evidence):
        return "IMPACT"
    if _RESOLUTION_RE.match(evidence):
        return "RESOLUTION"
    return "VERIFIED"


def _entry_route(entry: _Entry) -> str:
    if entry.form == "b" and entry.has_new_form_keys():
        return entry.route if entry.route in _ROUTES else "INVALID"
    return _infer_legacy_route(entry.evidence)


def _is_relabeled(text: str) -> bool:
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if _RELABEL_ACTION_RE.search(sentence) and _RELABEL_SCOPE_RE.search(sentence):
            return True
    return False


def _verified_entry_verdict(
    entry: _Entry, window_start: datetime | None, now: datetime
) -> tuple[str, list[str]]:
    if entry.form != "b" or not entry.has_new_form_keys():
        reasons = [_REASON_LEGACY_NO_METADATA]
        if window_start is None:
            reasons.append(_REASON_CONTEXT_UNAVAILABLE)
        return VERDICT_STALE, reasons
    stamp = (entry.observed_at, entry.kind, entry.tool_call, entry.observed_attempt)
    reasons = [] if entry.route in _ROUTES else [_REASON_ROUTE_INVALID]
    reasons.extend(_freshness_reasons(stamp, window_start, now))
    if not reasons:
        return VERDICT_FRESH, []
    if reasons == [_REASON_OBSERVED_BEFORE_WINDOW] and entry.kind == "STATIC":
        return VERDICT_CARRIED, []
    return VERDICT_STALE, reasons


def _entry_verdict(
    entry: _Entry, window_start: datetime | None, now: datetime
) -> tuple[str, list[str]]:
    if entry.form == "unparseable":
        return VERDICT_CARRIED, [_REASON_UNPARSEABLE_LEGACY]
    if entry.structural_reasons:
        return VERDICT_STALE, list(entry.structural_reasons)
    if _entry_route(entry) in _CARRIED_ROUTES:
        return VERDICT_CARRIED, []
    verdict, reasons = _verified_entry_verdict(entry, window_start, now)
    if verdict != VERDICT_FRESH and _is_relabeled(entry.text):
        return VERDICT_STALE, [*reasons, _REASON_RELABELED_WITHOUT_FRESH_CHECK]
    return verdict, reasons


def evaluate_draft(text: str, window_start: datetime | None) -> list[dict[str, object]]:
    """FRESH/CARRIED/STALE verdicts for each Self-Verified entry in a plan."""
    now = _utcnow()
    results: list[dict[str, object]] = []
    for lines in _split_entries(text):
        entry = _parse_entry(lines)
        verdict, reasons = _entry_verdict(entry, window_start, now)
        results.append({"claim": entry.claim, "verdict": verdict, "reasons": reasons})
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_premise_evidence.py",
        description="Validate premise-evidence stamps and dispatch freshness (#2343).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan-block", help="validate a PM premises block")
    scan.add_argument("--file", required=True, type=Path)
    scan.add_argument("--context", required=True, type=Path)
    draft = sub.add_parser("draft", help="validate a plan's Self-Verified section")
    draft.add_argument("--plan", required=True, type=Path)
    draft.add_argument("--context", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    source: Path = args.file if args.command == "scan-block" else args.plan
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, ValueError):
        print(f"check_premise_evidence: cannot read {source}", file=sys.stderr)
        return 2
    window_start = _load_window_start(args.context)
    evaluate = evaluate_scan_block if args.command == "scan-block" else evaluate_draft
    items = evaluate(text, window_start)
    print(json.dumps({"items": items}, separators=(",", ":")))
    failed = any(item["verdict"] in _FAILED_VERDICTS for item in items)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
