"""Duplicate-reader stop signal for the codex fix loop (#2667).

A fix cycle can answer an open "this duplicates an existing function" finding
by adding a second reader, parser or snapshot helper next to the one that
already exists (the #2590 shape: the helper was committed in cycle 1, the
duplication finding arrived after cycle 1's re-review, and cycle 2 was asked
to fix it). This guard parks such a cycle uncommitted, with the existing
function named, when both hold:

- the diff under measurement adds a top-level reader-like ``def`` to non-test
  source (the loop-wide diff when a loop budget is set, else the cycle's), and
- an open finding that relates to that helper says the code duplicates
  something and names a function that already exists in the tracked source.

Advisory and park-only, like the rest of the growth guard (#2633, #2648): it
never reverts, deletes or commits anything. It reuses the growth budget's park
reason and switch, and is hooked from ``commit._growth_breach`` inside the
same manifest gate. Related: #2590 (the incident shape), #2648 (the loop-wide
diff it measures over).
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING, NamedTuple

from cw._git import run_git
from cw.codex_fix_loop.baseline import cycle_diff, diff_since
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, SETTLE_FINDING_HINT, FenceBreach
from cw.codex_fix_loop.growth import is_source_path, removed_lines
from cw.codex_fix_loop.posted_text import (
    POSTED_TEXT_MAX_CHARS,
    describe_added_line,
    redact_and_cap,
    withhold_secret_lines,
)
from cw.codex_review import (
    CODEX_FIX_GROWTH_BUDGET,
    CODEX_FIX_LOOP_GROWTH_GUARD_KEY,
    _parse_unified_diff,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.codex_fix_loop.baseline import CycleBaseline
    from cw.codex_fix_loop.growth import LoopGrowthBudget
    from cw.review_findings import Finding

# A reader-like helper: a name token that starts with one of these stems.
_READER_STEMS = ("read", "pars", "load", "snapshot", "decod", "extract")
_DUPLICATION = re.compile(
    r"duplicat\w*|re-?implement\w*|re-?invent\w*|copy of|already exists?"
    r"|already implemented|redundant with"
    r"|(?:parallel|second|separate) (?:parser|reader|implementation|copy)",
    re.IGNORECASE,
)
_DEF = re.compile(r"^(?:async\s+)?def\s+(\w+)")
_DEF_NAME = re.compile(r"def[ \t]+(\w+)")
# A backticked span, or a bare ``name(`` call; the dotted form is kept whole. No
# space is allowed before ``(``: "the existing parser (see ...)" is prose, not a call.
_CANDIDATE = re.compile(r"`(?P<span>[^`\n]+)`|(?P<call>[A-Za-z_][\w.]*)\(")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_GREP_HIT = re.compile(r"^(.+?):(\d+):")
_MIN_NAME_CHARS = 4
_MAX_CANDIDATES = 5
_GREP_TIMEOUT_SECONDS = 30.0


class AddedDef(NamedTuple):
    """A reader-like top-level ``def`` the diff adds: name, file, line, text."""

    name: str
    path: str
    line: int
    text: str


class ExistingFunction(NamedTuple):
    """A tracked ``def`` that already exists: name, file and line."""

    name: str
    path: str
    line: int


class _Pair(NamedTuple):
    """An open finding, its candidate names and the helper it concerns."""

    finding: Finding
    names: list[str]
    helper: AddedDef


def is_reader_name(name: str) -> bool:
    """Whether any ``_``-separated token of *name* starts with a reader stem."""
    tokens = name.strip("_").split("_")
    return any(token.startswith(stem) for token in tokens for stem in _READER_STEMS)


def new_top_level_defs(
    file_diffs: dict[str, str], added: dict[str, dict[int, str]]
) -> list[AddedDef]:
    """Every top-level def the diff adds to non-test source, reader-named or not.

    A name that also appears as a removed ``def`` anywhere in the diff is
    dropped: an in-place edit or a cross-file move is not a new function.
    Counting by name, not by position, so an edit earlier in a file never shifts
    which added def survives.
    """
    removed_names = {
        m.group(1)
        for path, diff in file_diffs.items()
        if is_source_path(path)
        for line in removed_lines(diff)
        if (m := _DEF.match(line)) is not None
    }
    found: list[AddedDef] = []
    for path, lines in added.items():
        if not path.endswith(".py") or not is_source_path(path):
            continue
        for number, text in sorted(lines.items()):
            m = _DEF.match(text)
            if m is not None and m.group(1) not in removed_names:
                found.append(AddedDef(m.group(1), path, number, text))
    return found


def added_defs(
    file_diffs: dict[str, str], added: dict[str, dict[int, str]]
) -> list[AddedDef]:
    """The reader-like top-level defs the diff adds to non-test source."""
    return [d for d in new_top_level_defs(file_diffs, added) if is_reader_name(d.name)]


def _identifier(token: str) -> str | None:
    """The bare identifier a candidate token names, or ``None`` if it is not one."""
    text = token.strip()
    text = text.removesuffix("()")
    name = text.rsplit(".", 1)[-1]
    if not _IDENT.fullmatch(name) or len(name) < _MIN_NAME_CHARS:
        return None
    if name.startswith("__") and name.endswith("__"):
        return None
    return name


def _finding_text(finding: Finding) -> str:
    """The prose a duplication claim lives in (``evidence`` quotes diff code)."""
    return " ".join((finding.summary, finding.suggested_fix, finding.consequence))


def duplicated_names(finding: Finding) -> list[str]:
    """Identifiers a finding says duplicate something, deduplicated and capped.

    Empty unless the prose carries duplication language. Only identifiers that
    fully match the Python name shape are returned, so the names are safe to
    place in a ``git grep`` pattern.
    """
    text = _finding_text(finding)
    if not _DUPLICATION.search(text):
        return []
    names: list[str] = []
    for match in _CANDIDATE.finditer(text):
        name = _identifier(match.group("span") or match.group("call") or "")
        if name is None or name in names:
            continue
        names.append(name)
        if len(names) == _MAX_CANDIDATES:
            break
    return names


def _relevant_helper(finding: Finding, helpers: list[AddedDef]) -> AddedDef | None:
    """The first helper the finding concerns: same file, or named in its text."""
    text = _finding_text(finding)
    for helper in helpers:
        if finding.file == helper.path:
            return helper
        if re.search(rf"\b{re.escape(helper.name)}\b", text):
            return helper
    return None


def _pairs(findings: Iterable[Finding], helpers: list[AddedDef]) -> list[_Pair]:
    """Each finding with duplicated-function candidates that concerns a helper."""
    pairs: list[_Pair] = []
    for finding in findings:
        names = duplicated_names(finding)
        helper = _relevant_helper(finding, helpers) if names else None
        if names and helper is not None:
            pairs.append(_Pair(finding, names, helper))
    return pairs


def _grep_hits(
    worktree: Path, names: list[str], exclude: frozenset[tuple[str, int]]
) -> list[ExistingFunction]:
    """Every staged source ``def`` of *names*, sorted, minus *exclude*.

    A git failure or a timeout returns no hits: the signal is advisory and must
    never wedge the loop, so an unreadable repo simply does not park.
    """
    alternation = "|".join(names)
    pattern = (
        rf"^[[:space:]]*(async[[:space:]]+)?def[[:space:]]+"
        rf"({alternation})[[:space:]]*\("
    )
    try:
        proc = run_git(
            ["grep", "--cached", "-n", "-I", "-E", "-e", pattern, "--", "*.py"],
            cwd=worktree,
            capture_output=True,
            timeout=_GREP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    hits: list[ExistingFunction] = []
    for line in proc.stdout.splitlines():
        m = _GREP_HIT.match(line)
        if m is None:
            continue
        path, number = m.group(1), int(m.group(2))
        if not is_source_path(path) or (path, number) in exclude:
            continue
        name_m = _DEF_NAME.search(line[m.end() :])
        if name_m is not None:
            hits.append(ExistingFunction(name_m.group(1), path, number))
    return sorted(hits, key=lambda h: (h.path, h.line))


def resolve_existing(
    worktree: Path, names: list[str], exclude: frozenset[tuple[str, int]]
) -> dict[str, ExistingFunction]:
    """Map each name that resolves to a real existing ``def``, first hit per name.

    One ``git grep --cached`` for all names. Hits at *exclude* (the cycle's own
    new defs) never count, so a finding naming the new helper resolves nothing.
    """
    if not names:
        return {}
    hits = _grep_hits(worktree, names, exclude)
    resolved: dict[str, ExistingFunction] = {}
    for name in names:
        for hit in hits:
            if hit.name == name:
                resolved[name] = hit
                break
    return resolved


def _base_diff(
    worktree: Path, baseline: CycleBaseline, loop_budget: LoopGrowthBudget | None
) -> str:
    """The diff to look for helpers in: loop-wide when a loop budget is set (#2648)."""
    if loop_budget is None:
        return cycle_diff(worktree, baseline)
    return diff_since(worktree, loop_budget.start_sha)


def _summary_text(finding: Finding) -> str:
    """The finding summary as one posted line, secret-assignment lines withheld."""
    return redact_and_cap(" ".join(withhold_secret_lines(finding.summary).split()))


def _location(finding: Finding) -> str:
    if finding.line_start is None:
        return finding.file
    return f"{finding.file}:{finding.line_start}"


def _hint(helper: AddedDef, existing: ExistingFunction) -> str:
    return (
        f"Reuse `{existing.name}` ({existing.path}) and delete the new helper "
        f"`{helper.name}`; do not keep a second reader or parser. "
        "If the new helper is genuinely needed, settle the finding with "
        f"{SETTLE_FINDING_HINT} and requeue REVIEW. "
        "The helper may already be committed by an earlier cycle (the check is "
        "loop-wide); only this cycle's changes are staged. "
        "To switch this signal off along with the growth guard, set "
        f"`{CODEX_FIX_LOOP_GROWTH_GUARD_KEY}: false` for the lane. "
        f"{LEFT_STAGED_HINT}"
    )


def _breach(cycle: int, pair: _Pair, existing: ExistingFunction) -> FenceBreach:
    """Build the park for one resolved (finding, helper, existing function) triple."""
    helper = pair.helper
    finding = pair.finding
    lines = [
        f"codex fix cycle {cycle} added a reader/parser/snapshot helper while an "
        "open finding reports it duplicates an existing function; the cycle was "
        "not committed:",
        f"- new helper {helper.path}:{helper.line}: {describe_added_line(helper.text)}",
        f"- open finding {_location(finding)}: {_summary_text(finding)}",
        f"- existing function `{existing.name}` at {existing.path}:{existing.line}; "
        "reuse it (or extend it) instead of a second reader or parser",
    ]
    details = redact_and_cap("\n".join(lines), max_line_chars=POSTED_TEXT_MAX_CHARS)
    paths = tuple(sorted({helper.path, existing.path}))
    return FenceBreach(CODEX_FIX_GROWTH_BUDGET, paths, details, _hint(helper, existing))


def check_duplicate_reader(
    worktree: Path,
    baseline: CycleBaseline,
    open_findings: list[Finding],
    *,
    cycle: int,
    loop_budget: LoopGrowthBudget | None = None,
) -> FenceBreach | None:
    """Return the duplicate-reader park, or ``None``.

    Cheap text checks run first: with no finding carrying duplication language
    and a named identifier, this returns before any git call.
    """
    if not any(duplicated_names(f) for f in open_findings):
        return None
    file_diffs, added, _window, _changed = _parse_unified_diff(
        _base_diff(worktree, baseline, loop_budget)
    )
    helpers = added_defs(file_diffs, added)
    pairs = _pairs(open_findings, helpers)
    if not pairs:
        return None
    # Every def this diff adds is excluded, not only the reader-named ones: a
    # finding naming a freshly added `build_roster` names no *existing* function.
    exclude = frozenset((d.path, d.line) for d in new_top_level_defs(file_diffs, added))
    names = list(dict.fromkeys(n for pair in pairs for n in pair.names))
    existing = resolve_existing(worktree, names, exclude)
    for pair in pairs:
        for name in pair.names:
            if name in existing:
                return _breach(cycle, pair, existing[name])
    return None
