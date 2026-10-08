"""In-file growth budget for the codex fix loop (#2633).

The scope fence (#2485) is path-level: it cannot see a cycle that builds an
unplanned mechanism *inside* a file the plan allows (the #2591 durable outbox:
a new on-disk file, a new lock file and a lifecycle, +268 lines in one
allowed module). This guard measures what the cycle itself added, from its
clean-start baseline, and parks the cycle uncommitted when it adds a new lock,
on-disk state file or path constant, more than
:data:`_MAX_NEW_TOP_LEVEL_DEFS` top-level definitions, or more net non-test
source lines than the budget allows — unless an open finding explicitly asks
for that kind of addition. Active only when the plan has a ``## Files
Modified`` manifest, like the fence. Regex detectors run on ``.py`` files;
test files and docs are excluded everywhere. Removed-line hits cancel added
ones per file and kind, so editing a line that already held a construct is
never an addition.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from cw.codex_fix_loop.baseline import cycle_diff
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach
from cw.codex_fix_loop.posted_text import (
    POSTED_TEXT_MAX_CHARS,
    describe_added_line,
    redact_and_cap,
)
from cw.codex_review import (
    CODEX_FIX_GROWTH_BUDGET,
    CODEX_FIX_LOOP_GROWTH_GUARD_KEY,
    _parse_unified_diff,
    is_test_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.codex_fix_loop.baseline import CycleBaseline
    from cw.review_findings import Finding

DEFAULT_GROWTH_BUDGET_LINES = 40
_MAX_NEW_TOP_LEVEL_DEFS = 3
_DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt", ".adoc")


class AdditionKind(StrEnum):
    """What a cycle added; the value is the label the park text uses."""

    LOCK = "lock"
    STATE_FILE = "on-disk state file"
    PATH_CONSTANT = "path constant"
    TOP_LEVEL_DEF = f"top-level definition (budget {_MAX_NEW_TOP_LEVEL_DEFS} per cycle)"


class Addition(NamedTuple):
    """One detected addition: its kind, file, new-file line and source text."""

    kind: AdditionKind
    path: str
    line: int
    text: str


_LOCK = re.compile(
    r"\bthreading\.(?:R?Lock|Semaphore|Condition)\b|\basyncio\.Lock\b"
    r"|\bfilelock\b|\bFileLock\b|\bflock\(|\bportalocker\b"
)
_STATE_LITERAL = re.compile(
    r"[\"'][^\"']*\.(?:json|jsonl|lock|db|sqlite|pid|state)[\"']"
)
_FS_CALL = re.compile(
    r"Path\(|\bopen\(|write_text|write_bytes|os\.replace|atomic_write"
)
_PATH_CONSTANT = re.compile(r"^[A-Z_][A-Z0-9_]*_(?:PATH|FILE|DIR)\s*(?::[^=]*)?=")
_TOP_LEVEL_DEF = re.compile(r"^(?:async\s+def|def|class)\s")
_LOCK_REQUEST = re.compile(r"(?i)\block\b")
_STATE_REQUESTS = ("state file", "on-disk file", "new file", "sidecar")


def _matches(kind: AdditionKind, text: str) -> bool:
    """Whether one source line holds *kind*."""
    if kind is AdditionKind.LOCK:
        return bool(_LOCK.search(text))
    if kind is AdditionKind.STATE_FILE:
        return bool(_STATE_LITERAL.search(text) and _FS_CALL.search(text))
    if kind is AdditionKind.PATH_CONSTANT:
        return bool(_PATH_CONSTANT.match(text))
    return bool(_TOP_LEVEL_DEF.match(text))


def is_source_path(path: str) -> bool:
    """Whether *path* is non-test, non-prose source: what a cycle is held to.

    Prose (markdown and friends, anything under a ``docs/`` directory) is
    excluded so a cycle that merely documents or mentions a construct is never
    parked for it (#2633). Shared with :mod:`cw.codex_fix_loop.constraints`.
    """
    return (
        not is_test_path(path)
        and not path.endswith(_DOC_SUFFIXES)
        and not path.startswith("docs/")
        and "/docs/" not in path
    )


def _removed_lines(file_diff: str) -> list[str]:
    """The ``-`` lines of one file's parsed diff (the ``---`` header is never kept)."""
    return [line[1:] for line in file_diff.splitlines() if line.startswith("-")]


def detect_additions(
    file_line_text: dict[str, dict[int, str]], file_diffs: dict[str, str]
) -> list[Addition]:
    """Return the net constructs a diff adds to non-test ``.py`` files.

    For each file and kind, as many added hits as there are removed hits are
    dropped, earliest first, so a construct merely edited in place nets to
    zero.
    """
    additions: list[Addition] = []
    for path, lines in file_line_text.items():
        if not path.endswith(".py") or not is_source_path(path):
            continue
        removed = _removed_lines(file_diffs.get(path, ""))
        for kind in AdditionKind:
            hits = [(n, t) for n, t in sorted(lines.items()) if _matches(kind, t)]
            cancelled = sum(1 for t in removed if _matches(kind, t))
            additions.extend(Addition(kind, path, n, t) for n, t in hits[cancelled:])
    return additions


def net_source_lines(
    file_diffs: dict[str, str], file_line_text: dict[str, dict[int, str]]
) -> int:
    """Added minus removed lines over non-test, non-doc files."""
    return sum(
        len(lines) - len(_removed_lines(file_diffs.get(path, "")))
        for path, lines in file_line_text.items()
        if is_source_path(path)
    )


def justified_kinds(findings: Iterable[Finding]) -> frozenset[AdditionKind]:
    """The kinds an open finding's ``suggested_fix`` explicitly asks for."""
    kinds: set[AdditionKind] = set()
    for finding in findings:
        fix = finding.suggested_fix.lower()
        if _LOCK_REQUEST.search(fix):
            kinds.add(AdditionKind.LOCK)
        if any(phrase in fix for phrase in _STATE_REQUESTS):
            kinds.add(AdditionKind.STATE_FILE)
    return frozenset(kinds)


def effective_budget(budget: int, n_open: int) -> int:
    """The cycle's net-line budget: *budget* per open finding, at least one."""
    return budget * max(1, n_open)


def check_growth_budget(
    worktree: Path,
    baseline: CycleBaseline,
    *,
    open_findings: list[Finding],
    budget_lines: int,
    cycle: int,
) -> FenceBreach | None:
    """Return the ``codex_fix_growth_budget_exceeded`` park, or ``None``.

    Measures the cycle's staged diff against its baseline tree, so additions
    committed before the cycle began never count.
    """
    file_diffs, added, _window, _changed = _parse_unified_diff(
        cycle_diff(worktree, baseline)
    )
    justified = justified_kinds(open_findings)
    found = [a for a in detect_additions(added, file_diffs) if a.kind not in justified]
    defs = [a for a in found if a.kind is AdditionKind.TOP_LEVEL_DEF]
    flagged = [a for a in found if a.kind is not AdditionKind.TOP_LEVEL_DEF]
    flagged += defs[_MAX_NEW_TOP_LEVEL_DEFS:]
    net = net_source_lines(file_diffs, added)
    allowed = effective_budget(budget_lines, len(open_findings))
    if not flagged and net <= allowed:
        return None
    lines = [
        f"codex fix cycle {cycle} added code inside allowed file(s) beyond what "
        "the open finding(s) justify; the cycle was not committed:",
        *(
            f"- {a.path}:{a.line} new {a.kind.value}: {describe_added_line(a.text)}"
            for a in flagged
        ),
    ]
    if net > allowed:
        lines.append(
            f"- net non-test source lines +{net} exceeds the budget of {allowed} "
            f"({budget_lines} per open finding)"
        )
    hint = (
        "If the mechanism is wanted, add it to the plan or to a finding that asks "
        "for it, then requeue REVIEW; otherwise settle the finding that led to it "
        "(`cw review settle`). To raise the net-line budget set "
        "`codex_fix_loop_growth_budget_lines` in orchestrator.yaml. To disable "
        f"this guard for a lane, set `{CODEX_FIX_LOOP_GROWTH_GUARD_KEY}: false` "
        "on that lane in clients.yaml (or globally in orchestrator.yaml). "
        f"{LEFT_STAGED_HINT}"
    )
    details = redact_and_cap("\n".join(lines), max_line_chars=POSTED_TEXT_MAX_CHARS)
    paths = tuple(sorted({a.path for a in flagged} or set(added)))
    return FenceBreach(CODEX_FIX_GROWTH_BUDGET, paths, details, hint)
