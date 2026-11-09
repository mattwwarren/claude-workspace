"""The ``## Review sign-off`` PR-body section (#3043, RFC 0017 II2).

Pure: no ``gh``, no click, no git. The CLI (``cw.cli.review.signoff``) gathers
the attestation verdict, the deferred-findings entries and the redaction
anchors, then passes them in. The contract is ``docs/headless-contract.md``
section 13.7.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from cw.review_adjudication._models import DEFERRED_OUTCOME
from cw.tracker_post import plan_parts

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.models.review_attestation import ReviewAttestation
    from cw.review_adjudication._models import Adjudication

START_MARKER = "<!-- cw-review-signoff:start -->"
END_MARKER = "<!-- cw-review-signoff:end -->"
HEADING = "## Review sign-off"
MAX_LIST_LINES = 50
MAX_VALUE_CHARS = 200
HEAD_MISMATCH = "head_mismatch"
_MARKER_PREFIX = "<!-- cw-review-signoff:"
_FRICTION_PREFIX = "friction: review_signoff: "
_TOKEN_RE = re.compile(
    rf"(?:(?:skipped|stripped)_(?:{HEAD_MISMATCH}|truncated|invalid:[a-z_]+)"
    r"|repaired_markers|truncated|debt_count_mismatch|artifact_unreadable"
    r"|skipped_cli_failed|skipped_pr_head_unreadable)"
)
_NO_ENGINE = "none (generic cw review)"
_TICKET_PENDING = "ticket pending"


class SkipReason(NamedTuple):
    """Why the section is not written: a verifier token or ``HEAD_MISMATCH``."""

    token: str


class DebtLine(NamedTuple):
    severity: str
    file: str
    ticket: str


class DebtView(NamedTuple):
    """The accepted-debt list. ``shown`` False renders ``not reported``."""

    lines: tuple[DebtLine, ...]
    shown: bool
    mismatch: bool
    unreadable: bool


class SignoffResult(NamedTuple):
    body: str
    frictions: tuple[str, ...]


def friction_line(token: str) -> str:
    """The one printed form, ``friction: review_signoff: <token>``.

    Raises ``ValueError`` on a token outside the closed grammar, so a new
    token cannot reach stderr or ``friction_highlights`` unregistered.
    """
    if _TOKEN_RE.fullmatch(token) is None:
        msg = f"unknown review_signoff friction token: {token!r}"
        raise ValueError(msg)
    return f"{_FRICTION_PREFIX}{token}"


def sanitize(value: str) -> str:
    """One line, no marker syntax, no heading, at most ``MAX_VALUE_CHARS``."""
    text = value
    while "<!--" in text or "-->" in text:
        text = text.replace("<!--", "").replace("-->", "")
    text = " ".join(text.split())
    text = text.removeprefix("## ")
    return text[:MAX_VALUE_CHARS]


def build_debt_view(
    entries: Sequence[Adjudication] | None, deferral_count: int | None
) -> DebtView:
    """Accepted-debt rows from the artifact; ``entries`` None means unreadable.

    The attestation count is a cross-check only: an artifact that disagrees
    with it sets ``mismatch``, and an empty artifact never claims ``none``
    over a non-zero count.
    """
    if entries is None:
        return DebtView((), shown=False, mismatch=False, unreadable=True)
    defers = [entry for entry in entries if entry.outcome == DEFERRED_OUTCOME]
    mismatch = deferral_count is not None and deferral_count != len(defers)
    if defers:
        lines = tuple(
            DebtLine(
                sanitize(str(entry.severity)),
                sanitize(entry.file),
                sanitize(entry.ticket) or _TICKET_PENDING,
            )
            for entry in defers
        )
        return DebtView(lines, shown=True, mismatch=mismatch, unreadable=False)
    return DebtView((), shown=deferral_count == 0, mismatch=mismatch, unreadable=False)


def _list_rows(label: str, values: list[str] | None) -> list[str]:
    if values is None:
        return [f"- {label}: not reported"]
    if not values:
        return [f"- {label}: none"]
    rows = [f"  - {value}" for value in values[:MAX_LIST_LINES]]
    if len(values) > MAX_LIST_LINES:
        rows.append(f"  - … +{len(values) - MAX_LIST_LINES} more")
    return [f"- {label}:", *rows]


def _debt_rows(debt: DebtView) -> list[str]:
    if not debt.shown:
        return _list_rows("accepted debt", None)
    return _list_rows(
        "accepted debt",
        [f"{line.severity} {line.file} - — {line.ticket}" for line in debt.lines],
    )


def render_section(
    att: ReviewAttestation, *, pr_head: str, debt: DebtView, with_lists: bool = True
) -> str:
    """The R10 section, marker to marker. ``with_lists=False`` is the truncated form."""
    engine = sanitize(att.engine) if att.engine else _NO_ENGINE
    lines = [
        START_MARKER,
        HEADING,
        "",
        f"- engine: {engine}",
        f"- reviewer backend: {att.reviewer_backend}",
        f"- attested head: {att.head_sha}",
    ]
    if pr_head.lower() != att.head_sha.lower():
        lines.append(f"- verified at PR head: {pr_head.lower()}")
    lines += [
        f"- verdict: {att.verdict}",
        "- outstanding at head: "
        f"must_fix {att.counts.must_fix}, should_fix {att.counts.should_fix}",
        f"- risk: {att.risk or 'not reported'}",
    ]
    if with_lists:
        acceptance = (
            None
            if att.acceptance is None
            else [f"{sanitize(row.id)}: {row.status}" for row in att.acceptance]
        )
        overrides = [
            f"{sanitize(o.kind)} by {sanitize(o.actor)} at "
            f"{sanitize(o.recorded_at)}: {sanitize(o.reason)}"
            for o in att.overrides
        ]
        lines += _list_rows("acceptance", acceptance)
        lines += _debt_rows(debt)
        lines += _list_rows("overrides", overrides)
    lines.append(END_MARKER)
    return "\n".join(lines)


def _redacted(text: str, *, home: str, worktrees: Sequence[str]) -> str | None:
    """The redacted section, or None when it does not fit one comment body."""
    parts = plan_parts(text, home=home, worktrees=worktrees, single=True)
    if not parts.fits or not parts.bodies:
        return None
    return parts.bodies[0]


def _section_for(
    att: ReviewAttestation,
    *,
    pr_head: str,
    debt: DebtView,
    home: str,
    worktrees: Sequence[str],
) -> tuple[str | None, bool]:
    """``(section, truncated)``; section None when even the list-less form won't fit."""
    full = _redacted(
        render_section(att, pr_head=pr_head, debt=debt), home=home, worktrees=worktrees
    )
    if full is not None:
        return full, False
    lite = _redacted(
        render_section(att, pr_head=pr_head, debt=debt, with_lists=False),
        home=home,
        worktrees=worktrees,
    )
    return lite, True


def _classify(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith(_MARKER_PREFIX):
        return None
    if stripped == START_MARKER:
        return "start"
    if stripped == END_MARKER:
        return "end"
    return "stray"


def _marker_kinds(lines: Sequence[str]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        kind = _classify(line)
        if kind is not None:
            found.append((index, kind))
    return found


def _well_formed(kinds: list[tuple[int, str]]) -> tuple[int, int] | None:
    """``(start, end)`` indices when the only markers are one start then one end."""
    if [kind for _, kind in kinds] == ["start", "end"]:
        return kinds[0][0], kinds[1][0]
    return None


def _remove_sections(lines: Sequence[str]) -> list[str]:
    """Drop each start..end pair (and the blanks before it); drop lone markers."""
    kept: list[str] = []
    index = 0
    while index < len(lines):
        kind = _classify(lines[index])
        if kind == "start":
            end = _end_after(lines, index)
            if end is not None:
                while kept and not kept[-1].strip():
                    kept.pop()
                index = end + 1
                continue
        if kind is None:
            kept.append(lines[index])
        index += 1
    return kept


def _end_after(lines: Sequence[str], start: int) -> int | None:
    """Index of the end marker when it is the next marker line after ``start``."""
    for index in range(start + 1, len(lines)):
        kind = _classify(lines[index])
        if kind is None:
            continue
        return index if kind == "end" else None
    return None


def _insert(lines: Sequence[str], section: str) -> str:
    body = "\n".join(lines).rstrip()
    if not body:
        return f"{section}\n"
    return f"{body}\n\n{section}\n"


def _restore_newlines(text: str, *, crlf: bool) -> str:
    return text.replace("\n", "\r\n") if crlf else text


def _skip(
    body: str,
    token: str,
    lines: Sequence[str],
    kinds: list[tuple[int, str]],
    *,
    crlf: bool,
) -> SignoffResult:
    """No section is written: strip any existing one, else leave the body alone."""
    if not kinds:
        return SignoffResult(body, (friction_line(f"skipped_{token}"),))
    frictions: list[str] = []
    if _well_formed(kinds) is None:
        frictions.append(friction_line("repaired_markers"))
    frictions.append(friction_line(f"stripped_{token}"))
    stripped = "\n".join(_remove_sections(lines))
    return SignoffResult(_restore_newlines(stripped, crlf=crlf), tuple(frictions))


def compose_body(
    body: str,
    *,
    gate: ReviewAttestation | SkipReason,
    pr_head: str,
    debt: DebtView,
    home: str,
    worktrees: Sequence[str],
) -> SignoffResult:
    """Apply the sign-off section (``gate`` is the attestation) or strip it (a skip).

    Never raises on hostile input: a bad marker shape is repaired and reported
    as ``repaired_markers``. CRLF bodies stay CRLF.
    """
    crlf = "\r\n" in body
    lines = body.replace("\r\n", "\n").split("\n")
    kinds = _marker_kinds(lines)
    if isinstance(gate, SkipReason):
        return _skip(body, gate.token, lines, kinds, crlf=crlf)
    section, truncated = _section_for(
        gate, pr_head=pr_head, debt=debt, home=home, worktrees=worktrees
    )
    if section is None:
        return _skip(body, "truncated", lines, kinds, crlf=crlf)
    frictions: list[str] = []
    if debt.unreadable:
        frictions.append(friction_line("artifact_unreadable"))
    if debt.mismatch:
        frictions.append(friction_line("debt_count_mismatch"))
    if truncated:
        frictions.append(friction_line("truncated"))
    well = _well_formed(kinds)
    if not kinds:
        out = _insert(lines, section)
    elif well is not None:
        start, end = well
        out = "\n".join([*lines[:start], *section.split("\n"), *lines[end + 1 :]])
    else:
        out = _insert(_remove_sections(lines), section)
        frictions.append(friction_line("repaired_markers"))
    return SignoffResult(_restore_newlines(out, crlf=crlf), tuple(frictions))
