#!/usr/bin/env python3
"""Render the child citations for an epic's landing PR body (RFC 0020 B2).

Usage:
    python3 .claude/scripts/landing_citations.py --children '12,13 ABC-1'
    printf '12\\n13\\n' | python3 .claude/scripts/landing_citations.py

Context: GitHub's closing keyword only fires when a PR merges into the
default branch (RFC 0020 D-10). A child PR merged into the epic base closes
nothing, so the landing PR (epic branch -> default branch) must cite every
child ticket. This script turns the child ticket ids into that block: one
``Closes #<n>`` line per GitHub issue number, because the keyword applies to
the first reference only (``Closes #1, #2`` closes one issue).

An id that is not a GitHub issue number (a Linear id such as ``ABC-12``,
``0``, ``12x``, an empty string) cannot be cited. It is reported on stderr as
``uncitable: <id>`` so the caller records it in ``friction_highlights``; an
uncitable child is not a failure. Duplicates are cited once, first-seen order.

Exit codes: 0 when at least one id was supplied; 1 when none was (nothing is
printed to stdout, so an empty block is never pasted silently); 2 for an
argparse usage error.

Stdlib only, and it launches nothing: like every script under
``.claude/scripts/`` it imports nothing from ``cw``.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import TextIO

_CLOSING_FORM = "Closes"
_ISSUE_NUMBER = re.compile(r"#?([1-9][0-9]*)")
_ID_SEPARATORS = re.compile(r"[,\s]+")


class Citations(NamedTuple):
    """The cited ``Closes`` lines and the ids that could not be cited."""

    lines: tuple[str, ...]
    uncitable: tuple[str, ...]


def render_citations(child_ids: Iterable[str]) -> Citations:
    """Cite each GitHub issue number in *child_ids*; return the rest separately."""
    lines: list[str] = []
    seen: set[str] = set()
    uncitable: list[str] = []
    for raw in child_ids:
        child = raw.strip()
        match = _ISSUE_NUMBER.fullmatch(child)
        if match is None:
            uncitable.append(child)
            continue
        number = match.group(1)
        if number in seen:
            continue
        seen.add(number)
        lines.append(f"{_CLOSING_FORM} #{number}")
    return Citations(tuple(lines), tuple(uncitable))


def _split_ids(text: str) -> list[str]:
    return [part for part in _ID_SEPARATORS.split(text) if part]


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render `Closes #<n>` lines for an epic landing PR body."
    )
    parser.add_argument(
        "--children",
        help="Child ticket ids, split on commas or whitespace (default: stdin).",
    )
    args = parser.parse_args(argv)
    source = args.children
    if source is None:
        source = (stdin if stdin is not None else sys.stdin).read()
    ids = _split_ids(source)
    if not ids:
        print("landing_citations: no child ids supplied", file=sys.stderr)
        return 1
    citations = render_citations(ids)
    for line in citations.lines:
        print(line)
    for child in citations.uncitable:
        print(f"uncitable: {child}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
