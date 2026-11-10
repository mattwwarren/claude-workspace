"""Attribute a new finding to code the fix loop itself added (#2665).

The divergence guard (:mod:`cw.codex_fix_loop.divergence`) counts progress as
an originally-found MUST_FIX key leaving the open set. A reviewer that rewords
an original finding makes the original key vanish (counted "resolved") while
the reworded finding returns under a new key that cites a helper the loop
added in an earlier cycle. That cycle resolved nothing; it only renamed the
complaint.

This module is the pure text half of the fix: which symbols did the loop add,
and does a finding cite one of them by name. It runs no git; the diff parsing
and the file-and-line half of the attribution live in ``divergence``. A symbol
is a Python ``def``, ``class`` or column-0 module-level assignment the diff
adds to any ``.py`` file (test files included, since a loop-added test helper
is still loop-added), unless the same name is also removed in the diff, which
makes it an edit rather than a new symbol.

Advisory and park-only, like the rest of the guard: nothing is reverted or
deleted, and the signal only changes how ``stall_streak`` accrues.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cw.codex_fix_loop.duplicate_reader import cited_identifiers, identifier, new_defs

if TYPE_CHECKING:
    from cw.review_findings import Finding

# A def or class at any indent (methods count); group 1 is the name.
_SYMBOL = re.compile(r"^\s*(?:(?:async\s+)?def|class)\s+(\w+)")
# A column-0 assignment, annotated or not; ``==`` is a comparison, not a binding.
_MODULE_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)")
# A bare token that contains an underscore: `_helper`, `fold_roster`. A plain
# prose word ("process") never matches, so ordinary sentences cannot cite.
_UNDERSCORED = re.compile(r"\b[A-Za-z_]*_[A-Za-z0-9_]*\b")
# Cap on backtick and call-form candidates read from one finding.
_MAX_CITED = 64


def loop_added_symbols(
    file_diffs: dict[str, str], added: dict[str, dict[int, str]]
) -> frozenset[str]:
    """Names of the symbols the diff adds, minus any it also removes.

    Names must pass :func:`~cw.codex_fix_loop.duplicate_reader.identifier`
    (at least four characters, not a dunder), the same rule that filters what a
    finding cites, so both sides of the match are comparable.
    """
    found = (
        *new_defs(file_diffs, added, _SYMBOL, source_only=False),
        *new_defs(file_diffs, added, _MODULE_ASSIGN, source_only=False),
    )
    names = (identifier(d.name) for d in found)
    return frozenset(name for name in names if name is not None)


def cites_symbol(finding: Finding, symbols: frozenset[str]) -> bool:
    """Whether *finding* names one of *symbols* in its prose or evidence.

    Candidates are backticked spans, ``name(`` calls (the dotted tail is kept)
    and bare underscored tokens, matched by whole-identifier equality.
    """
    if not symbols:
        return False
    text = " ".join(
        (finding.summary, finding.consequence, finding.suggested_fix, finding.evidence)
    )
    cited = set(cited_identifiers(text, _MAX_CITED))
    cited.update(
        name
        for token in _UNDERSCORED.findall(text)
        if (name := identifier(token)) is not None
    )
    return not cited.isdisjoint(symbols)
